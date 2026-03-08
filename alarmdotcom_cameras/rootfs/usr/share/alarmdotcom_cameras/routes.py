"""HTTP route definitions for the Alarm.com Cameras add-on."""

import asyncio
import logging
import pathlib

from aiohttp import web

from alarmdotcom_cameras.browser import BrowserEngine
from alarmdotcom_cameras.credentials import CredentialStore

logger = logging.getLogger(__name__)

STATIC_DIR = pathlib.Path(__file__).parent / "static"


# ---- Health ----


async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint used by HA watchdog."""
    browser: BrowserEngine = request.app["browser"]
    health = browser.get_health()
    health["status"] = "ok"
    health["version"] = "0.1.0"
    # Include config for the settings display
    config = request.app["config"]
    health["snapshot_interval"] = config["snapshot_interval"]
    health["stream_fps"] = config["stream_fps"]
    health["stream_timeout"] = config["stream_timeout"]
    health["jpeg_quality"] = config["jpeg_quality"]
    health["trusted_device_name"] = config["trusted_device_name"]
    return web.json_response(health)


# ---- Credentials ----


async def get_credentials_status(request: web.Request) -> web.Response:
    """Check if credentials have been configured."""
    cred_store: CredentialStore = request.app["credentials"]
    return web.json_response(
        {
            "configured": cred_store.is_configured(),
            "username": cred_store.get_username(),
        }
    )


async def save_credentials(request: web.Request) -> web.Response:
    """Save alarm.com credentials."""
    cred_store: CredentialStore = request.app["credentials"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return web.json_response(
            {"error": "Username and password are required"}, status=400
        )

    cred_store.save(username, password)
    return web.json_response({"status": "saved", "username": username})


# ---- Auth ----


async def get_auth_status(request: web.Request) -> web.Response:
    """Get current authentication status."""
    browser: BrowserEngine = request.app["browser"]
    return web.json_response(browser.get_auth_status())


async def trigger_login(request: web.Request) -> web.Response:
    """Trigger a login attempt using stored credentials."""
    cred_store: CredentialStore = request.app["credentials"]
    browser: BrowserEngine = request.app["browser"]

    creds = cred_store.load()
    if not creds:
        return web.json_response({"error": "No credentials configured"}, status=400)

    status = await browser.login(creds["username"], creds["password"])
    result = browser.get_auth_status()

    # If login succeeded, discover cameras
    if status.value == "authenticated":
        await browser.discover_cameras()

    return web.json_response(result)


async def get_auth_challenge(request: web.Request) -> web.Response:
    """Get screenshot of current CAPTCHA/2FA challenge."""
    browser: BrowserEngine = request.app["browser"]
    screenshot = browser.state.challenge_screenshot

    if not screenshot:
        return web.json_response({"status": "no_challenge"}, status=404)

    return web.Response(
        body=screenshot,
        content_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


async def resend_2fa_code(request: web.Request) -> web.Response:
    """Request alarm.com to resend the 2FA code."""
    browser: BrowserEngine = request.app["browser"]
    result = await browser.resend_2fa_code()

    status_code = 200 if result.get("success") else 400
    return web.json_response(result, status=status_code)


async def solve_auth_challenge(request: web.Request) -> web.Response:
    """Submit CAPTCHA/2FA solution."""
    browser: BrowserEngine = request.app["browser"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    solution = data.get("solution", "").strip()
    if not solution:
        return web.json_response({"error": "Solution is required"}, status=400)

    status = await browser.solve_challenge(solution)
    result = browser.get_auth_status()

    if status.value == "authenticated":
        await browser.discover_cameras()

    return web.json_response(result)


# ---- Cameras ----


async def list_cameras(request: web.Request) -> web.Response:
    """List discovered cameras with snapshot metadata."""
    browser: BrowserEngine = request.app["browser"]
    cameras = []
    for cam in browser.state.cameras:
        cam_data = {
            "id": cam.id,
            "name": cam.name,
            "model": cam.model,
            "status": cam.status,
        }
        meta = browser.get_snapshot_metadata(cam.id)
        if meta:
            cam_data["last_snapshot"] = meta["timestamp"]
            cam_data["snapshot_width"] = meta["width"]
            cam_data["snapshot_height"] = meta["height"]
        cameras.append(cam_data)
    return web.json_response({"cameras": cameras})


async def refresh_cameras(request: web.Request) -> web.Response:
    """Force camera re-discovery (bypasses TTL cache)."""
    browser: BrowserEngine = request.app["browser"]
    cameras = await browser.discover_cameras(force=True)
    return web.json_response(
        {
            "status": "ok",
            "cameras": len(cameras),
        }
    )


async def get_snapshot_metadata(request: web.Request) -> web.Response:
    """Get metadata for the latest snapshot of a camera."""
    camera_id = request.match_info["camera_id"]
    browser: BrowserEngine = request.app["browser"]

    meta = browser.get_snapshot_metadata(camera_id)
    if not meta:
        return web.json_response(
            {"error": f"No snapshot metadata for camera {camera_id}"},
            status=404,
        )

    return web.json_response(meta)


# ---- Snapshots ----


async def get_snapshot(request: web.Request) -> web.Response:
    """Get latest cached snapshot for a camera."""
    camera_id = request.match_info["camera_id"]
    browser: BrowserEngine = request.app["browser"]

    jpeg = browser.get_latest_snapshot(camera_id)
    if not jpeg:
        return web.json_response(
            {"error": f"No snapshot available for camera {camera_id}"},
            status=404,
        )

    return web.Response(
        body=jpeg,
        content_type="image/jpeg",
        headers={"Cache-Control": "no-cache"},
    )


async def capture_snapshot(request: web.Request) -> web.Response:
    """Trigger a fresh snapshot capture and return the image."""
    camera_id = request.match_info["camera_id"]
    browser: BrowserEngine = request.app["browser"]

    async with browser._lock:
        jpeg = await browser.capture_snapshot(camera_id)
    if not jpeg:
        return web.json_response(
            {"error": f"Failed to capture snapshot for camera {camera_id}"},
            status=500,
        )

    return web.Response(
        body=jpeg,
        content_type="image/jpeg",
        headers={"Cache-Control": "no-cache"},
    )


# ---- Streams ----


async def get_stream(request: web.Request) -> web.Response:
    """MJPEG stream for a camera (screenshot loop)."""
    camera_id = request.match_info["camera_id"]
    browser: BrowserEngine = request.app["browser"]
    config = request.app["config"]
    fps = config["stream_fps"]
    timeout_minutes = config["stream_timeout"]

    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=frame",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    # Auto-stop after timeout
    timeout_task = asyncio.get_event_loop().call_later(
        timeout_minutes * 60, lambda: asyncio.ensure_future(browser.stop_stream())
    )

    try:
        async for jpeg_frame in browser.start_stream(camera_id, fps):
            frame_data = (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg_frame)).encode() + b"\r\n"
                b"\r\n" + jpeg_frame + b"\r\n"
            )
            await response.write(frame_data)
    except ConnectionResetError:
        logger.info("Stream client disconnected for camera %s", camera_id)
    finally:
        timeout_task.cancel()
        await browser.stop_stream()

    return response


async def start_stream(request: web.Request) -> web.Response:
    """Start streaming a camera (the actual stream is served via GET /api/stream/<id>)."""
    camera_id = request.match_info["camera_id"]
    browser: BrowserEngine = request.app["browser"]

    # Verify camera exists
    camera = next((c for c in browser.state.cameras if c.id == camera_id), None)
    if not camera:
        return web.json_response({"error": f"Camera {camera_id} not found"}, status=404)

    return web.json_response(
        {
            "status": "ready",
            "stream_url": f"/api/stream/{camera_id}",
        }
    )


async def stop_stream(request: web.Request) -> web.Response:
    """Stop the active stream."""
    browser: BrowserEngine = request.app["browser"]
    await browser.stop_stream()
    return web.json_response({"status": "stopped"})


async def get_stream_status(request: web.Request) -> web.Response:
    """Get current stream status."""
    browser: BrowserEngine = request.app["browser"]
    return web.json_response(browser.get_stream_status())


# ---- Debug ----


async def get_debug_screenshot(request: web.Request) -> web.Response:
    """Get the latest debug screenshot (login page, etc.)."""
    name = request.match_info.get("name", "login_page")
    data_dir = pathlib.Path(request.app["data_dir"])
    screenshot_path = data_dir / "debug" / f"{name}.png"

    if not screenshot_path.exists():
        return web.json_response({"error": "No debug screenshot available"}, status=404)

    return web.FileResponse(
        screenshot_path,
        headers={
            "Content-Type": "image/png",
            "Cache-Control": "no-cache",
        },
    )


# ---- Browser Profile ----


async def clear_browser_profile(request: web.Request) -> web.Response:
    """Clear the browser profile (cookies, cache, etc.) and restart."""
    browser: BrowserEngine = request.app["browser"]
    result = await browser.clear_browser_profile()
    status_code = 200 if result.get("success") else 500
    return web.json_response(result, status=status_code)


# ---- Web UI ----


async def index(request: web.Request) -> web.Response:
    """Serve the web UI."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return web.Response(text="Web UI not found", status=500)
    return web.FileResponse(index_path)


# ---- Route setup ----


def setup_routes(app: web.Application) -> None:
    """Register all HTTP routes."""
    # Web UI — register extra slash patterns as safety net for HA Ingress
    app.router.add_get("/", index)
    app.router.add_get("//", index)
    app.router.add_get("///", index)
    app.router.add_get("////", index)
    app.router.add_static("/static", STATIC_DIR, name="static")

    # Health
    app.router.add_get("/api/health", health_check)

    # Credentials
    app.router.add_get("/api/credentials/status", get_credentials_status)
    app.router.add_post("/api/credentials", save_credentials)

    # Auth
    app.router.add_get("/api/auth/status", get_auth_status)
    app.router.add_post("/api/auth/login", trigger_login)
    app.router.add_get("/api/auth/challenge", get_auth_challenge)
    app.router.add_post("/api/auth/solve", solve_auth_challenge)
    app.router.add_post("/api/auth/resend", resend_2fa_code)

    # Cameras
    app.router.add_get("/api/cameras", list_cameras)
    app.router.add_post("/api/cameras/refresh", refresh_cameras)

    # Snapshots
    app.router.add_get("/api/snapshot/{camera_id}", get_snapshot)
    app.router.add_post("/api/snapshot/{camera_id}/capture", capture_snapshot)
    app.router.add_get("/api/snapshot/{camera_id}/metadata", get_snapshot_metadata)

    # Streams
    app.router.add_get("/api/stream/{camera_id}", get_stream)
    app.router.add_post("/api/stream/{camera_id}/start", start_stream)
    app.router.add_post("/api/stream/{camera_id}/stop", stop_stream)
    app.router.add_get("/api/stream/status", get_stream_status)

    # Browser profile
    app.router.add_post("/api/browser/clear-profile", clear_browser_profile)

    # Debug
    app.router.add_get("/api/debug/screenshot/{name}", get_debug_screenshot)
