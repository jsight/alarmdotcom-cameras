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
    health["version"] = "0.1.22"
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
    """Get current authentication status.

    If the state says 2FA/CAPTCHA but the browser has drifted back to the
    login page (alarm.com expired the session), update the state immediately
    so the UI reflects reality.
    """
    browser: BrowserEngine = request.app["browser"]

    auth_val = browser.state.auth_status.value
    if auth_val in ("2fa_required", "captcha_required"):
        # Use page.url (sync property) — don't call _get_page() or any async
        # page methods that could interfere with an active auth flow.
        try:
            page = browser._page
            if page and not page.is_closed():
                from alarmdotcom_cameras.browser import _is_login_page

                if _is_login_page(page.url):
                    from alarmdotcom_cameras.browser import AuthStatus

                    browser.state.auth_status = AuthStatus.LOGGED_OUT
                    browser.state.auth_message = "Session expired. Please try again."
                    browser.state.challenge_screenshot = None
        except Exception:
            pass

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
    """Trigger a fresh snapshot capture and return the image.

    Also signals the parking manager to start a 60-second burst of
    continuous captures for this camera.
    """
    import time

    camera_id = request.match_info["camera_id"]
    browser: BrowserEngine = request.app["browser"]

    # Signal the parking manager to start/extend a manual burst
    browser.state.last_manual_request_time = time.time()
    browser.state.manual_burst_camera_id = camera_id

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


async def get_debug_live(request: web.Request) -> web.Response:
    """Return current browser URL and DOM summary as JSON.

    Always returns 200 with useful state info, even when the browser
    or page is unavailable.  Screenshot is served separately via
    /api/debug/live/screenshot.
    """
    browser: BrowserEngine = request.app["browser"]
    no_cache = {"Cache-Control": "no-cache, no-store"}

    # Base state — always included so the frontend always has something
    base = {
        "browser_alive": browser.state.browser_alive,
        "auth_status": browser.state.auth_status.value,
        "auth_message": browser.state.auth_message or "",
        "lock_held": browser._lock.locked(),
    }

    if not browser.state.browser_alive:
        return web.json_response(
            {
                **base,
                "url": "",
                "title": "",
                "dom_summary": "",
                "raw_html": "",
                "error": "Browser not running",
            },
            headers=no_cache,
        )

    # Check for an existing page without creating a blank one
    page = browser._page
    if page is None or page.is_closed():
        return web.json_response(
            {
                **base,
                "url": "",
                "title": "",
                "dom_summary": "",
                "raw_html": "",
                "error": "No page open (browser idle)",
            },
            headers=no_cache,
        )

    # If the browser lock is held (auth flow, snapshot, etc.), don't execute
    # JS on the page — concurrent page.evaluate()/title()/screenshot() calls
    # destroy the execution context and cause "no_session" redirects.
    if browser._lock.locked():
        return web.json_response(
            {
                **base,
                "url": page.url,
                "title": "(locked — auth flow in progress)",
                "dom_summary": "",
                "raw_html": "",
                "error": "Browser lock held — skipping page access to avoid interference",
            },
            headers=no_cache,
        )

    try:
        url = page.url
        title = await asyncio.wait_for(page.title(), timeout=5)

        # Dump DOM summary
        dom_summary = await asyncio.wait_for(
            page.evaluate("""
                () => {
                    const parts = [];
                    parts.push('URL: ' + window.location.href);
                    parts.push('Title: ' + document.title);
                    parts.push('');

                    // Forms
                    const forms = document.querySelectorAll('form');
                    parts.push('=== FORMS (' + forms.length + ') ===');
                    forms.forEach((f, i) => {
                        parts.push('  form[' + i + '] id="' + f.id + '" action="' + f.action + '" method="' + f.method + '"');
                    });
                    parts.push('');

                    // Inputs
                    const inputs = document.querySelectorAll('input, textarea, select');
                    parts.push('=== INPUTS (' + inputs.length + ') ===');
                    Array.from(inputs).slice(0, 30).forEach(e => {
                        const val = e.type === 'password' ? '***' : (e.value || '').substring(0, 50);
                        parts.push('  <' + e.tagName + ' id="' + e.id + '" type="' + e.type +
                            '" name="' + e.name + '" placeholder="' + (e.placeholder || '') +
                            '" value="' + val + '"' +
                            (e.disabled ? ' disabled' : '') +
                            (e.hidden ? ' hidden' : '') + '>');
                    });
                    parts.push('');

                    // Buttons
                    const buttons = document.querySelectorAll('button, input[type="submit"]');
                    parts.push('=== BUTTONS (' + buttons.length + ') ===');
                    Array.from(buttons).slice(0, 20).forEach(e => {
                        parts.push('  <' + e.tagName + ' id="' + e.id + '" class="' +
                            (e.className || '').toString().substring(0, 80) + '"' +
                            (e.disabled ? ' disabled' : '') + '>' +
                            (e.textContent || e.value || '').trim().substring(0, 60));
                    });
                    parts.push('');

                    // Links (first 30)
                    const links = document.querySelectorAll('a[href]');
                    parts.push('=== LINKS (' + links.length + ', showing first 30) ===');
                    Array.from(links).slice(0, 30).forEach(a => {
                        parts.push('  <A href="' + a.getAttribute('href') + '">' +
                            (a.textContent || '').trim().substring(0, 60));
                    });
                    parts.push('');

                    // Key elements (errors, alerts, camera/video)
                    const special = document.querySelectorAll(
                        '[class*="error"], [class*="alert"], [class*="success"], ' +
                        '[class*="dashboard"], [class*="camera"], [class*="video"], ' +
                        '[class*="two-factor"], [class*="verification"], [class*="trust"]'
                    );
                    if (special.length) {
                        parts.push('=== KEY ELEMENTS (' + special.length + ') ===');
                        Array.from(special).slice(0, 15).forEach(e => {
                            parts.push('  <' + e.tagName + ' class="' +
                                (e.className || '').toString().substring(0, 100) + '">');
                            const text = (e.textContent || '').trim().substring(0, 120);
                            if (text) parts.push('    text: ' + text);
                        });
                        parts.push('');
                    }

                    // Full visible text (first 5000 chars)
                    parts.push('=== PAGE TEXT ===');
                    const bodyText = document.body ? document.body.innerText : '<no body>';
                    parts.push(bodyText.substring(0, 5000));

                    return parts.join('\\n');
                }
            """),
            timeout=10,
        )

        # Full raw HTML (separate evaluate to keep independent of summary)
        raw_html = ""
        try:
            raw_html = await asyncio.wait_for(
                page.evaluate(
                    "() => document.documentElement.outerHTML.substring(0, 512000)"
                ),
                timeout=10,
            )
        except Exception as html_exc:
            raw_html = "[Failed to capture raw HTML: " + str(html_exc) + "]"

        return web.json_response(
            {
                **base,
                "url": url,
                "title": title,
                "dom_summary": dom_summary,
                "raw_html": raw_html,
            },
            headers=no_cache,
        )
    except asyncio.TimeoutError:
        logger.warning("Debug live state timed out (page may be navigating)")
        return web.json_response(
            {
                **base,
                "url": getattr(page, "url", ""),
                "title": "",
                "dom_summary": "",
                "raw_html": "",
                "error": "Timed out reading page (browser may be navigating)",
            },
            headers=no_cache,
        )
    except Exception as exc:
        logger.exception("Debug live state failed")
        return web.json_response(
            {
                **base,
                "url": getattr(page, "url", ""),
                "title": "",
                "dom_summary": "",
                "raw_html": "",
                "error": str(exc),
            },
            headers=no_cache,
        )


async def get_debug_live_screenshot(request: web.Request) -> web.Response:
    """Take a live screenshot and return it as a PNG image.

    Served as a separate endpoint so the browser can load it as a plain
    <img src="..."> without base64 encoding or large JSON payloads.

    Returns a JSON error (not plain text) on failure so the frontend
    can distinguish image-load errors from network errors.
    """
    browser: BrowserEngine = request.app["browser"]
    no_cache = {"Cache-Control": "no-cache, no-store"}

    if not browser.state.browser_alive:
        return web.json_response(
            {"error": "Browser not running"},
            status=503,
            headers=no_cache,
        )

    # Use existing page — don't create a blank one as a side-effect
    page = browser._page
    if page is None or page.is_closed():
        return web.json_response(
            {"error": "No page open (browser idle)"},
            status=503,
            headers=no_cache,
        )

    # Don't take a screenshot while the browser lock is held — it interferes
    # with auth flow navigation and causes session loss.
    if browser._lock.locked():
        return web.json_response(
            {"error": "Browser lock held — skipping screenshot to avoid interference"},
            status=503,
            headers=no_cache,
        )

    try:
        screenshot_bytes = await asyncio.wait_for(
            page.screenshot(full_page=True),
            timeout=10,
        )
        return web.Response(
            body=screenshot_bytes,
            content_type="image/png",
            headers=no_cache,
        )
    except asyncio.TimeoutError:
        logger.warning("Debug screenshot timed out (page may be navigating)")
        return web.json_response(
            {"error": "Screenshot timed out (page may be navigating)"},
            status=503,
            headers=no_cache,
        )
    except Exception as exc:
        logger.exception("Debug live screenshot failed")
        return web.json_response(
            {"error": str(exc)},
            status=500,
            headers=no_cache,
        )


async def get_debug_logs(request: web.Request) -> web.Response:
    """Return browser console logs and server logs for the debug UI."""
    browser: BrowserEngine = request.app["browser"]
    no_cache = {"Cache-Control": "no-cache, no-store"}

    # Browser console logs (from Playwright page events)
    console_logs = list(browser.state.console_logs)

    # Server logs from ring buffer (if available)
    ring = request.app.get("log_ring_buffer")
    server_logs = list(ring.records) if ring else []

    return web.json_response(
        {"console_logs": console_logs, "server_logs": server_logs},
        headers=no_cache,
    )


# ---- Browser Profile ----


async def clear_browser_profile(request: web.Request) -> web.Response:
    """Clear the browser profile (cookies, cache, etc.) and restart."""
    browser: BrowserEngine = request.app["browser"]
    result = await browser.clear_browser_profile()
    status_code = 200 if result.get("success") else 500
    return web.json_response(result, status=status_code)


# ---- Web UI ----


NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


async def index(request: web.Request) -> web.Response:
    """Serve the web UI."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return web.Response(text="Web UI not found", status=500)
    return web.FileResponse(index_path, headers=NO_CACHE_HEADERS)


async def serve_static_no_cache(request: web.Request) -> web.Response:
    """Serve a static file with no-cache headers.

    HA Ingress can aggressively cache static files, so we serve JS/CSS
    via explicit routes with no-cache headers instead of add_static().
    """
    filename = request.match_info["filename"]
    # Security: only allow files directly in the static dir (no path traversal)
    if "/" in filename or "\\" in filename or ".." in filename:
        return web.Response(text="Forbidden", status=403)
    filepath = STATIC_DIR / filename
    if not filepath.exists():
        return web.Response(text="Not found", status=404)
    return web.FileResponse(filepath, headers=NO_CACHE_HEADERS)


# ---- Route setup ----


def setup_routes(app: web.Application) -> None:
    """Register all HTTP routes."""
    # Web UI — register extra slash patterns as safety net for HA Ingress
    app.router.add_get("/", index)
    app.router.add_get("//", index)
    app.router.add_get("///", index)
    app.router.add_get("////", index)
    app.router.add_get("/static/{filename}", serve_static_no_cache)

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
    app.router.add_get("/api/debug/live", get_debug_live)
    app.router.add_get("/api/debug/live/screenshot", get_debug_live_screenshot)
    app.router.add_get("/api/debug/logs", get_debug_logs)
