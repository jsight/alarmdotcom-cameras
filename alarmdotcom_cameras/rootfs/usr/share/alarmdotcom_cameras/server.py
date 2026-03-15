"""Main HTTP server for the Alarm.com Cameras add-on."""

import argparse
import asyncio
import collections
import logging
import pathlib
import time

from aiohttp import web

from alarmdotcom_cameras.browser import BrowserEngine
from alarmdotcom_cameras.credentials import CredentialStore
from alarmdotcom_cameras.routes import setup_routes

logger = logging.getLogger(__name__)


class RingBufferLogHandler(logging.Handler):
    """Logging handler that stores records in a fixed-size ring buffer.

    Records are accessible via the ``records`` deque for display in the
    debug UI.
    """

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.records: collections.deque[dict] = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            {
                "time": record.created,
                "level": record.levelname,
                "name": record.name,
                "message": self.format(record),
            }
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alarm.com Cameras add-on")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--snapshot-interval",
        type=int,
        default=30,
        help="Minutes between periodic snapshot bursts (default: 30)",
    )
    parser.add_argument("--stream-fps", type=float, default=1.0)
    parser.add_argument(
        "--stream-timeout",
        type=int,
        default=5,
        help="Minutes before auto-stopping a stream",
    )
    parser.add_argument("--jpeg-quality", type=int, default=80)
    parser.add_argument(
        "--trusted-device-name",
        default="HA Alarm.com Cameras",
        help="Device name when trusting this browser after 2FA",
    )
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--data-dir", default="/data")
    return parser.parse_args()


HEALTH_CHECK_INTERVAL = 300  # 5 minutes


async def session_health_task(app: web.Application) -> None:
    """Background task that monitors session health and auto-recovers."""
    browser: BrowserEngine = app["browser"]
    cred_store: CredentialStore = app["credentials"]

    logger.info("Session health monitor started")

    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        try:
            # Don't interfere while login is actively running
            auth_val = browser.state.auth_status.value
            if auth_val == "logging_in":
                logger.debug("Skipping health check: login in progress")
                continue

            # If state says 2FA/CAPTCHA but the browser has drifted back
            # to the login page (session expired), reset state and re-login.
            if auth_val in ("2fa_required", "captcha_required"):
                try:
                    page = await browser._get_page()
                    from alarmdotcom_cameras.browser import _is_login_page

                    if _is_login_page(page.url):
                        logger.warning(
                            "Auth state is %s but browser is on login page — "
                            "session expired, re-logging in",
                            auth_val,
                        )
                        from alarmdotcom_cameras.browser import AuthStatus

                        browser.state.auth_status = AuthStatus.LOGGED_OUT
                        browser.state.challenge_screenshot = None
                        creds = cred_store.load()
                        if creds:
                            await browser.login(creds["username"], creds["password"])
                    else:
                        logger.debug(
                            "Skipping health check: auth flow in progress (%s)",
                            auth_val,
                        )
                except Exception:
                    pass
                continue

            # Don't interfere while the lock is held (active browser operation)
            if browser._lock.locked():
                logger.debug("Skipping health check: browser lock held")
                continue

            # Check if browser is responsive
            responsive = await browser.is_browser_responsive()
            if not responsive:
                # Only restart if not in an interactive auth state
                logger.warning("Browser unresponsive, restarting...")
                await browser.restart()
                # Re-login if we have credentials
                creds = cred_store.load()
                if creds:
                    await browser.login(creds["username"], creds["password"])
                continue

            # Check if session is still valid
            if auth_val == "authenticated":
                valid = await browser.check_session()
                if not valid:
                    logger.warning("Session expired, attempting re-login...")
                    creds = cred_store.load()
                    if creds:
                        status = await browser.login(
                            creds["username"], creds["password"]
                        )
                        if status.value == "authenticated":
                            logger.info("Re-login successful")
                            await browser.discover_cameras()
                            async with browser._lock:
                                await browser.park()
                        elif status.value in ("captcha_required", "2fa_required"):
                            logger.warning(
                                "Re-login requires user action: %s", status.value
                            )
                        else:
                            logger.error("Re-login failed: %s", status.value)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error in session health monitor")


PARKING_BURST_MANUAL_DURATION = 60  # seconds of burst after manual trigger
PARKING_BURST_PERIODIC_DURATION = 20  # seconds of burst for periodic check
PARKING_PERIODIC_INTERVAL = 1800  # 30 minutes between periodic bursts


async def parking_manager_task(app: web.Application) -> None:
    """Background task that manages browser parking and burst captures.

    The browser stays parked on the alarm.com dashboard most of the time
    to avoid constant load on alarm.com's video infrastructure.

    Two burst modes:
    - Manual: triggered by a capture_snapshot API call, captures at ~1fps
      for 60 seconds then parks.
    - Periodic: every 30 minutes, captures at ~1fps for 20 seconds across
      all cameras then parks.
    """
    browser: BrowserEngine = app["browser"]
    periodic_interval = app["config"]["snapshot_interval"] * 60
    last_periodic_burst = time.time()  # don't burst immediately at startup

    logger.info(
        "Parking manager started (periodic every %ds, manual burst %ds, "
        "periodic burst %ds)",
        periodic_interval,
        PARKING_BURST_MANUAL_DURATION,
        PARKING_BURST_PERIODIC_DURATION,
    )

    # Park the browser initially (after login/discovery in on_startup)
    await asyncio.sleep(10)
    if browser.state.auth_status.value == "authenticated":
        try:
            async with browser._lock:
                await browser.park()
        except Exception:
            logger.debug("Initial park failed (non-critical)")

    while True:
        try:
            await asyncio.sleep(2)

            if browser.state.auth_status.value != "authenticated":
                continue
            if not browser.state.cameras:
                continue
            if browser.state.active_stream_camera:
                continue

            now = time.time()
            manual_age = now - browser.state.last_manual_request_time

            if manual_age < PARKING_BURST_MANUAL_DURATION:
                # Manual burst mode — capture the requested camera
                camera_id = browser.state.manual_burst_camera_id
                if camera_id:
                    logger.info(
                        "Manual burst starting for camera %s", camera_id
                    )
                    async with browser._lock:
                        await browser.burst_capture(
                            camera_id, PARKING_BURST_MANUAL_DURATION
                        )
                        await browser.park()

            elif (now - last_periodic_burst) >= periodic_interval:
                # Periodic burst mode — round-robin all cameras
                cameras = browser.state.cameras
                per_camera = max(
                    PARKING_BURST_PERIODIC_DURATION // len(cameras), 5
                )
                logger.info(
                    "Periodic burst starting for %d camera(s) "
                    "(%ds each)",
                    len(cameras),
                    per_camera,
                )
                async with browser._lock:
                    for camera in cameras:
                        if browser.state.active_stream_camera:
                            break
                        # If a manual request came in, abort periodic burst
                        if (
                            time.time() - browser.state.last_manual_request_time
                            < PARKING_BURST_MANUAL_DURATION
                        ):
                            logger.info(
                                "Periodic burst interrupted by manual request"
                            )
                            break
                        await browser.burst_capture(camera.id, per_camera)
                    await browser.park()
                last_periodic_burst = time.time()

            elif not browser.state.parked:
                # Not in burst and not parked — park now
                try:
                    async with browser._lock:
                        await browser.park()
                except Exception:
                    logger.debug("Park attempt failed (non-critical)")

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error in parking manager")


async def on_startup(app: web.Application) -> None:
    """Initialize services on server startup."""
    logger.info("Alarm.com Cameras add-on starting up")
    data_dir = pathlib.Path(app["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "snapshots").mkdir(exist_ok=True)
    (data_dir / "browser_state").mkdir(exist_ok=True)
    (data_dir / "credentials").mkdir(exist_ok=True)

    # Initialize credential store
    cred_store = CredentialStore(str(data_dir))
    app["credentials"] = cred_store

    # Initialize and start browser engine
    browser = BrowserEngine(
        data_dir=str(data_dir),
        jpeg_quality=app["config"]["jpeg_quality"],
        trusted_device_name=app["config"]["trusted_device_name"],
    )
    app["browser"] = browser
    await browser.start()

    # If credentials exist, try to log in automatically
    creds = cred_store.load()
    if creds:
        logger.info("Credentials found, attempting auto-login...")
        status = await browser.login(creds["username"], creds["password"])
        if status.value == "authenticated":
            await browser.discover_cameras()

    # Start background tasks
    app["parking_task"] = asyncio.create_task(parking_manager_task(app))
    app["health_task"] = asyncio.create_task(session_health_task(app))


async def on_shutdown(app: web.Application) -> None:
    """Clean up on server shutdown."""
    logger.info("Alarm.com Cameras add-on shutting down")

    # Cancel background tasks
    for task_name in ("parking_task", "health_task"):
        task = app.get(task_name)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # Stop browser engine
    browser = app.get("browser")
    if browser:
        await browser.stop()


def create_app(args: argparse.Namespace) -> web.Application:
    app = web.Application()

    app["config"] = {
        "snapshot_interval": args.snapshot_interval,
        "stream_fps": args.stream_fps,
        "stream_timeout": args.stream_timeout,
        "jpeg_quality": args.jpeg_quality,
        "trusted_device_name": args.trusted_device_name,
    }
    app["data_dir"] = args.data_dir

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    setup_routes(app)

    return app


def main() -> None:
    args = parse_args()

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    logging.basicConfig(level=log_level, format=log_format)

    # Install ring buffer handler so debug UI can show server logs
    ring_handler = RingBufferLogHandler(capacity=500)
    ring_handler.setFormatter(logging.Formatter(log_format))
    ring_handler.setLevel(log_level)
    logging.getLogger().addHandler(ring_handler)

    app = create_app(args)
    app["log_ring_buffer"] = ring_handler
    web.run_app(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
