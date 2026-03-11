"""Main HTTP server for the Alarm.com Cameras add-on."""

import argparse
import asyncio
import collections
import logging
import pathlib

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
        self.records.append({
            "time": record.created,
            "level": record.levelname,
            "name": record.name,
            "message": self.format(record),
        })


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alarm.com Cameras add-on")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--snapshot-interval",
        type=int,
        default=10,
        help="Minutes between periodic snapshots",
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
                            "session expired, re-logging in", auth_val
                        )
                        from alarmdotcom_cameras.browser import AuthStatus
                        browser.state.auth_status = AuthStatus.LOGGED_OUT
                        browser.state.challenge_screenshot = None
                        creds = cred_store.load()
                        if creds:
                            await browser.login(creds["username"], creds["password"])
                    else:
                        logger.debug(
                            "Skipping health check: auth flow in progress (%s)", auth_val
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


async def periodic_snapshot_task(app: web.Application) -> None:
    """Background task that captures snapshots on a timer.

    Round-robins through cameras one at a time. Skips cameras that
    had recent errors. Yields to active streams.
    """
    interval = app["config"]["snapshot_interval"] * 60  # convert to seconds
    browser: BrowserEngine = app["browser"]
    error_counts: dict[str, int] = {}
    MAX_CONSECUTIVE_ERRORS = 3

    logger.info("Periodic snapshot task started (interval: %ds)", interval)

    while True:
        await asyncio.sleep(interval)
        try:
            if browser.state.auth_status.value != "authenticated":
                logger.debug("Skipping periodic snapshots: not authenticated")
                continue

            cameras = browser.state.cameras
            if not cameras:
                logger.debug("Skipping periodic snapshots: no cameras discovered")
                continue

            for camera in cameras:
                # Don't interrupt an active stream
                if browser.state.active_stream_camera:
                    logger.debug(
                        "Skipping snapshot for %s: stream active on %s",
                        camera.id,
                        browser.state.active_stream_camera,
                    )
                    continue

                # Skip cameras with too many consecutive errors
                if error_counts.get(camera.id, 0) >= MAX_CONSECUTIVE_ERRORS:
                    logger.debug(
                        "Skipping snapshot for %s: %d consecutive errors",
                        camera.id,
                        error_counts[camera.id],
                    )
                    continue

                logger.info(
                    "Periodic snapshot for camera %s (%s)", camera.id, camera.name
                )
                async with browser._lock:
                    result = await browser.capture_snapshot(camera.id)

                if result:
                    error_counts[camera.id] = 0
                    logger.info(
                        "Snapshot captured for %s (%d bytes)",
                        camera.id,
                        len(result),
                    )
                else:
                    error_counts[camera.id] = error_counts.get(camera.id, 0) + 1
                    logger.warning(
                        "Snapshot failed for %s (attempt %d/%d)",
                        camera.id,
                        error_counts[camera.id],
                        MAX_CONSECUTIVE_ERRORS,
                    )

                # Brief pause between cameras to avoid hammering
                await asyncio.sleep(5)

            # Reset error counts periodically (every full cycle) so cameras
            # that had transient errors get retried
            for cam_id in list(error_counts.keys()):
                if error_counts[cam_id] < MAX_CONSECUTIVE_ERRORS:
                    error_counts[cam_id] = 0

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error in periodic snapshot task")


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
    app["snapshot_task"] = asyncio.create_task(periodic_snapshot_task(app))
    app["health_task"] = asyncio.create_task(session_health_task(app))


async def on_shutdown(app: web.Application) -> None:
    """Clean up on server shutdown."""
    logger.info("Alarm.com Cameras add-on shutting down")

    # Cancel background tasks
    for task_name in ("snapshot_task", "health_task"):
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
