"""Headless browser engine for Alarm.com camera access.

Uses Playwright with Chromium to log into alarm.com, discover cameras,
and capture screenshots of live video feeds.
"""

import asyncio
import collections
import enum
import io
import json
import logging
import pathlib
import time
from dataclasses import dataclass, field

from PIL import Image
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

ALARM_BASE_URL = "https://www.alarm.com"
LOGIN_URL = f"{ALARM_BASE_URL}/login"
CAMERAS_URL = f"{ALARM_BASE_URL}/web/video"
DASHBOARD_URL = f"{ALARM_BASE_URL}/web/dashboard"


def _is_login_page(url: str) -> bool:
    """Check if URL is the alarm.com login page (not other pages with 'login' in path)."""
    # The login page URL is exactly /login or /login?... (with query params)
    # The 2FA page is /web/system-install/login-setup/... which should NOT match
    from urllib.parse import urlparse

    path = urlparse(url).path.rstrip("/").lower()
    return path == "/login"


# Selectors for alarm.com pages (may need adjustment based on actual DOM)
SELECTORS = {
    # Login page
    "username_input": 'input[name="ctl00$ContentPlaceHolder1$loginform$txtUserName"], input[id="txtUserName"], input[type="email"]',
    "password_input": 'input[name="ctl00$ContentPlaceHolder1$loginform$txtPassword"], input[id="txtPassword"], input[type="password"]',
    "login_button": '#ctl00_ContentPlaceHolder1_loginform_signInButton, input[id*="signIn"], input[id*="SignIn"], button[id*="signIn"], input[value="Log In"], input[value="Login"], button:has-text("Log In"), button:has-text("Sign In")',
    # Post-login indicators
    "logged_in_indicator": '#ctl00_phBody_CameraList, .video-page, .dashboard, [class*="dashboard"], .live-video-wrapper, section.cameras',
    # CAPTCHA / 2FA
    "captcha_element": 'iframe[src*="captcha"], [class*="captcha"], #captcha, .g-recaptcha, [data-sitekey]',
    "twofa_element": '#two-factor-authentication-input-field, input[name*="code"], input[id*="twoFactor"], input[id*="code" i], input[placeholder*="code" i], input.two-factor-input, input[class*="two-factor"], input[class*="verification"]',
    "twofa_submit": 'button:has-text("Verify"), button:has-text("Submit"), button:has-text("Confirm"), button.btn-color-primary',
    "twofa_resend": '.request-new-code-button, button:has-text("Request a new code"), button:has-text("Resend"), a:has-text("Resend"), a:has-text("Request a new code"), [class*="resend" i]',
    # Trust device page (shown after successful 2FA)
    "trust_device_name_input": 'input[placeholder*="Device Name"], input[placeholder*="device name"], input[class*="device-name"], input[id*="device-name" i]',
    "trust_device_submit": 'button:has-text("Trust Device")',
    "trust_device_skip": 'button:has-text("Skip")',
    # Camera list / live view page
    "camera_item": '.video-camera-card, .camera-item, [class*="camera-card"], [data-camera-id]',
    "camera_name": ".camera-name, .camera-description, .device-name, .bottom-bar-camera-name, h3, h4",
    "camera_link": 'a[href*="video"], a[href*="camera"]',
    # Video player (for snapshots)
    "video_element": 'video, canvas, .video-player, .webrtc-player, [class*="video-container"], [class*="video-stream"], [class*="live-view"]',
    # WebRTC player containers (for camera discovery from live view)
    "webrtc_player": '[id*="webrtc-player"], .video-player.webrtc-player, .live-video-player',
}


class AuthStatus(enum.Enum):
    NOT_CONFIGURED = "not_configured"
    LOGGED_OUT = "logged_out"
    LOGGING_IN = "logging_in"
    AUTHENTICATED = "authenticated"
    CAPTCHA_REQUIRED = "captcha_required"
    TWO_FA_REQUIRED = "2fa_required"
    ERROR = "error"


@dataclass
class CameraInfo:
    id: str
    name: str
    model: str = ""
    status: str = "unknown"
    url: str = ""


@dataclass
class SnapshotMetadata:
    camera_id: str
    timestamp: float = 0.0
    width: int = 0
    height: int = 0


@dataclass
class BrowserState:
    auth_status: AuthStatus = AuthStatus.NOT_CONFIGURED
    auth_message: str = ""
    challenge_screenshot: bytes | None = None
    cameras: list[CameraInfo] = field(default_factory=list)
    cameras_discovered_at: float = 0.0  # timestamp of last discovery
    snapshot_metadata: dict[str, SnapshotMetadata] = field(default_factory=dict)
    active_stream_camera: str | None = None
    stream_started_at: float = 0.0
    stream_fps: float = 0.0
    stream_frame_count: int = 0
    last_auth_time: float = 0.0
    last_snapshot_time: float = 0.0
    startup_time: float = field(default_factory=time.time)
    browser_alive: bool = False
    parked: bool = False
    last_manual_request_time: float = 0.0
    manual_burst_camera_id: str | None = None
    console_logs: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=200),
        repr=False,
    )
    _stream_stop_event: asyncio.Event | None = field(default=None, repr=False)


class BrowserEngine:
    """Manages the headless Chromium browser for alarm.com interaction."""

    CAMERA_CACHE_TTL = 3600  # 1 hour default

    def __init__(
        self,
        data_dir: str,
        jpeg_quality: int = 80,
        trusted_device_name: str = "HA Alarm.com Cameras",
    ) -> None:
        self._data_dir = data_dir
        self._jpeg_quality = jpeg_quality
        self._trusted_device_name = trusted_device_name
        self._browser_state_dir = pathlib.Path(data_dir) / "browser_state"
        self._snapshot_dir = pathlib.Path(data_dir) / "snapshots"
        self._lock = asyncio.Lock()  # Serialize browser operations

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        self.state = BrowserState()

    async def start(self) -> None:
        """Launch the browser."""
        self._browser_state_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()

        # Detect the actual Chromium version so we can build a matching UA.
        # Playwright's default headless UA contains "HeadlessChrome" which is
        # a bot-detection red flag.  We replace it with "Chrome".
        _chrome_version = "131.0.6778.33"  # fallback
        try:
            _tmp_browser = await self._playwright.chromium.launch(
                headless=True, args=["--no-sandbox"]
            )
            _tmp_page = await _tmp_browser.new_page()
            _tmp_ua = await _tmp_page.evaluate("() => navigator.userAgent")
            await _tmp_browser.close()
            # Extract version from "HeadlessChrome/X.Y.Z.W" or "Chrome/X.Y.Z.W"
            import re

            m = re.search(r"(?:Headless)?Chrome/([\d.]+)", _tmp_ua)
            if m:
                _chrome_version = m.group(1)
            logger.info("Detected Chromium version: %s", _chrome_version)
        except Exception as exc:
            logger.warning("Could not detect Chromium version: %s", exc)

        _user_agent = (
            f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{_chrome_version} Safari/537.36"
        )
        logger.info("Using user agent: %s", _user_agent)

        # Use persistent context to preserve cookies/localStorage across restarts
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._browser_state_dir / "chromium_profile"),
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-extensions",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-translate",
                "--no-first-run",
                "--disable-blink-features=AutomationControlled",
                # Fix locale — without this, navigator.languages shows
                # 'en-US@posix' in containers, which is a bot-detection
                # red flag.  Force proper locale.
                "--lang=en-US",
                # Video/WebRTC support
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
            ],
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 720},
            user_agent=_user_agent,
            ignore_https_errors=True,
        )

        # Mask headless browser fingerprint signals.
        # alarm.com's bot detection checks these properties and kills
        # sessions that look automated.  These patches make the browser
        # look like a normal desktop Chrome installation.
        await self._context.add_init_script("""
            // Remove webdriver flag — delete it entirely so
            // 'webdriver' in navigator returns false
            delete Object.getPrototypeOf(navigator).webdriver;

            // Fix navigator.languages (container shows ['en-US@posix'])
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });

            // Add window.chrome object (missing in headless Chrome)
            if (!window.chrome) {
                window.chrome = {
                    runtime: {
                        onMessage: { addListener: function() {} },
                        sendMessage: function() {},
                    },
                    loadTimes: function() { return {}; },
                    csi: function() { return {}; },
                };
            }

            // Add fake plugins (headless Chrome has 0 plugins)
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const plugins = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
                          description: 'Portable Document Format',
                          length: 1, item: function(i) { return this[i]; } },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                          description: '', length: 1, item: function(i) { return this[i]; } },
                        { name: 'Native Client', filename: 'internal-nacl-plugin',
                          description: '', length: 2, item: function(i) { return this[i]; } },
                    ];
                    plugins.namedItem = function(name) {
                        return this.find(p => p.name === name) || null;
                    };
                    plugins.refresh = function() {};
                    return plugins;
                }
            });

            // Add deviceMemory (missing in headless)
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8
            });

            // Fix Permissions API (headless returns 'denied' for notifications)
            const origQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = (params) => {
                if (params.name === 'notifications') {
                    return Promise.resolve({ state: 'prompt', onchange: null });
                }
                return origQuery(params);
            };

            // Fix window.outerWidth/outerHeight (0 in headless = dead giveaway)
            if (window.outerWidth === 0) {
                Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
            }
            if (window.outerHeight === 0) {
                Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 85 });
            }

            // Fix screen properties (headless may have inconsistent values)
            if (screen.availWidth === 0 || screen.width === 0) {
                Object.defineProperty(screen, 'width', { get: () => 1920 });
                Object.defineProperty(screen, 'height', { get: () => 1080 });
                Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
                Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
                Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
                Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
            }

            // Override WebGL renderer to hide SwiftShader (headless GPU emulator)
            const origGetParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(param) {
                // UNMASKED_VENDOR_WEBGL = 0x9245, UNMASKED_RENDERER_WEBGL = 0x9246
                if (param === 0x9245) return 'Google Inc. (Intel)';
                if (param === 0x9246) return 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)';
                return origGetParameter.call(this, param);
            };
            // Also patch WebGL2
            if (typeof WebGL2RenderingContext !== 'undefined') {
                const origGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
                WebGL2RenderingContext.prototype.getParameter = function(param) {
                    if (param === 0x9245) return 'Google Inc. (Intel)';
                    if (param === 0x9246) return 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)';
                    return origGetParameter2.call(this, param);
                };
            }
        """)

        self.state.browser_alive = True
        logger.info("Browser engine started")

        # Run fingerprint diagnostic — log all the signals bot detectors check
        # so we can compare container vs non-container.
        await self._log_fingerprint_diagnostic()

    async def _log_fingerprint_diagnostic(self) -> None:
        """Log browser fingerprint signals for debugging bot detection."""
        try:
            page = await self._get_page()
            # Navigate to a blank page to run diagnostics
            await page.goto("about:blank")
            fp = await page.evaluate("""() => {
                const r = {};
                r.userAgent = navigator.userAgent;
                r.platform = navigator.platform;
                r.languages = JSON.stringify(navigator.languages);
                r.language = navigator.language;
                r.hardwareConcurrency = navigator.hardwareConcurrency;
                r.deviceMemory = navigator.deviceMemory;
                r.maxTouchPoints = navigator.maxTouchPoints;
                r.webdriver = navigator.webdriver;
                r.vendor = navigator.vendor;
                r.productSub = navigator.productSub;

                // Screen
                r.screenWidth = screen.width;
                r.screenHeight = screen.height;
                r.screenAvailWidth = screen.availWidth;
                r.screenAvailHeight = screen.availHeight;
                r.screenColorDepth = screen.colorDepth;
                r.screenPixelDepth = screen.pixelDepth;
                r.outerWidth = window.outerWidth;
                r.outerHeight = window.outerHeight;
                r.innerWidth = window.innerWidth;
                r.innerHeight = window.innerHeight;
                r.devicePixelRatio = window.devicePixelRatio;

                // Timezone
                r.timezoneOffset = new Date().getTimezoneOffset();
                r.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

                // Chrome object
                r.hasChrome = !!window.chrome;
                r.chromeKeys = window.chrome ? Object.keys(window.chrome).join(',') : 'N/A';

                // Plugins
                r.pluginCount = navigator.plugins ? navigator.plugins.length : 0;
                r.pluginNames = navigator.plugins ?
                    Array.from(navigator.plugins).map(p => p.name).join(', ') : 'N/A';

                // WebGL
                try {
                    const canvas = document.createElement('canvas');
                    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
                    if (gl) {
                        const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
                        r.webglVendor = debugInfo ? gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL) : 'N/A';
                        r.webglRenderer = debugInfo ? gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL) : 'N/A';
                    } else {
                        r.webglVendor = 'no WebGL';
                        r.webglRenderer = 'no WebGL';
                    }
                } catch(e) {
                    r.webglVendor = 'error: ' + e.message;
                    r.webglRenderer = 'error: ' + e.message;
                }

                // Connection
                r.connectionType = navigator.connection ?
                    navigator.connection.effectiveType : 'N/A';
                r.connectionDownlink = navigator.connection ?
                    navigator.connection.downlink : 'N/A';

                // Permissions
                r.permissions = 'checking...';

                // Automation signals
                r.webdriver_prop = 'webdriver' in navigator;
                r.webdriver_val = navigator.webdriver;
                r.automationControlled = !!(window.cdc_adoQpoasnfa76pfcZLmcfl_Array ||
                    window.cdc_adoQpoasnfa76pfcZLmcfl_Promise ||
                    document.$cdc_asdjflasutopfhvcZLmcfl_);

                return r;
            }""")
            logger.info("=== BROWSER FINGERPRINT DIAGNOSTIC ===")
            for key, val in fp.items():
                logger.info("  FP: %s = %s", key, val)
            logger.info("=== END FINGERPRINT DIAGNOSTIC ===")
        except Exception as exc:
            logger.warning("Fingerprint diagnostic failed: %s", exc)

    async def restart(self) -> None:
        """Restart the browser after a crash or error."""
        logger.info("Restarting browser engine...")
        await self.stop()
        await self.start()

    async def stop(self) -> None:
        """Shut down the browser cleanly."""
        if self.state.active_stream_camera and self.state._stream_stop_event:
            self.state._stream_stop_event.set()

        if self._context:
            await self._context.close()
            self._context = None

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        self.state.browser_alive = False
        logger.info("Browser engine stopped")

    def _attach_console_listeners(self, page: Page) -> None:
        """Attach console and error listeners to capture browser output."""

        def on_console(msg):
            entry = {
                "time": time.time(),
                "type": msg.type,
                "text": msg.text,
                "url": msg.location.get("url", "")
                if hasattr(msg, "location") and msg.location
                else "",
            }
            self.state.console_logs.append(entry)

        def on_pageerror(exc):
            entry = {
                "time": time.time(),
                "type": "pageerror",
                "text": str(exc),
                "url": "",
            }
            self.state.console_logs.append(entry)

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)

    async def _get_page(self) -> Page:
        """Get or create a browser page."""
        if not self._context:
            raise RuntimeError("Browser not started")

        if self._page and not self._page.is_closed():
            return self._page

        self._page = await self._context.new_page()
        self._attach_console_listeners(self._page)
        return self._page

    async def _close_page(self) -> None:
        """Close the current page to free resources."""
        if self._page and not self._page.is_closed():
            await self._page.close()
        self._page = None

    # ---- Debug Helpers ----

    _debug_counter = 0

    async def _debug_page_state(self, page: Page, label: str) -> None:
        """Log comprehensive page state and save a numbered screenshot.

        Call this at every major step to build a visual timeline of what
        the browser is doing.  Screenshots are saved as debug/step_NNN_<label>.png.
        """
        BrowserEngine._debug_counter += 1
        n = BrowserEngine._debug_counter
        tag = f"[STEP {n:03d} {label}]"

        try:
            url = page.url
            logger.info("%s URL: %s", tag, url)
        except Exception:
            logger.warning("%s could not read URL (page may be closed)", tag)
            return

        # Save screenshot
        try:
            debug_dir = pathlib.Path(self._data_dir) / "debug"
            debug_dir.mkdir(exist_ok=True)
            safe_label = label.replace(" ", "_").replace("/", "_")[:40]
            filename = f"step_{n:03d}_{safe_label}.png"
            await page.screenshot(path=str(debug_dir / filename), full_page=True)
            logger.info("%s screenshot saved: %s", tag, filename)
        except Exception as exc:
            logger.debug("%s screenshot failed: %s", tag, exc)

        # Dump visible text (first 1500 chars)
        try:
            page_text = await page.evaluate(
                "() => document.body ? document.body.innerText.substring(0, 1500) : '<no body>'"
            )
            logger.info("%s page text:\n%s", tag, page_text)
        except Exception as exc:
            logger.debug("%s page text failed: %s", tag, exc)

        # Dump key DOM elements: forms, inputs, buttons
        try:
            dom_info = await page.evaluate("""
                () => {
                    const parts = [];
                    // Forms
                    const forms = document.querySelectorAll('form');
                    parts.push('FORMS (' + forms.length + '):');
                    forms.forEach((f, i) => {
                        parts.push('  form[' + i + '] id=' + f.id + ' action=' + f.action + ' method=' + f.method);
                    });
                    // Inputs
                    const inputs = document.querySelectorAll('input, textarea, select');
                    parts.push('INPUTS (' + inputs.length + '):');
                    Array.from(inputs).slice(0, 20).forEach(e => {
                        parts.push('  <' + e.tagName + ' id="' + e.id + '" type="' + e.type +
                            '" name="' + e.name + '" placeholder="' + (e.placeholder||'') +
                            '" value="' + (e.type === 'password' ? '***' : (e.value||'').substring(0, 30)) + '">');
                    });
                    // Buttons
                    const buttons = document.querySelectorAll('button, input[type="submit"]');
                    parts.push('BUTTONS (' + buttons.length + '):');
                    Array.from(buttons).slice(0, 15).forEach(e => {
                        parts.push('  <' + e.tagName + ' id="' + e.id + '" class="' +
                            (e.className||'').toString().substring(0, 60) + '" disabled=' + e.disabled +
                            '>' + (e.textContent||e.value||'').trim().substring(0, 50));
                    });
                    // Key elements
                    const indicators = document.querySelectorAll(
                        '[class*="error"], [class*="alert"], [class*="success"], ' +
                        '[class*="dashboard"], [class*="camera"], [class*="video"]'
                    );
                    if (indicators.length) {
                        parts.push('KEY ELEMENTS (' + indicators.length + '):');
                        Array.from(indicators).slice(0, 10).forEach(e => {
                            parts.push('  <' + e.tagName + ' class="' +
                                (e.className||'').toString().substring(0, 80) + '">' +
                                (e.textContent||'').trim().substring(0, 80));
                        });
                    }
                    return parts.join('\\n');
                }
            """)
            logger.info("%s DOM dump:\n%s", tag, dom_info)
        except Exception as exc:
            logger.debug("%s DOM dump failed: %s", tag, exc)

    # ---- Authentication ----

    async def login(self, username: str, password: str) -> AuthStatus:
        """Attempt to log into alarm.com."""
        async with self._lock:
            return await self._login_impl(username, password)

    async def _login_impl(self, username: str, password: str) -> AuthStatus:
        """Login implementation (must be called with self._lock held)."""
        self.state.auth_status = AuthStatus.LOGGING_IN
        self.state.auth_message = "Logging in..."
        self.state.challenge_screenshot = None

        try:
            page = await self._get_page()

            # alarm.com can be very slow (60+ seconds).  Use generous timeouts
            # everywhere and never bail out just because a wait timed out.
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=120_000)
            await self._wait_for_page_content(page, "login page")
            await self._debug_page_state(page, "login_page_loaded")

            # Check if already logged in (session restored from persistent context)
            if await self._check_logged_in(page):
                self.state.auth_status = AuthStatus.AUTHENTICATED
                self.state.auth_message = "Already authenticated (session restored)"
                self.state.last_auth_time = time.time()
                logger.info("Already authenticated from saved session")
                return AuthStatus.AUTHENTICATED

            # Dismiss cookie consent banner if present
            await self._dismiss_cookie_banner(page)

            # Check for CAPTCHA before filling in credentials
            if await self._detect_captcha(page):
                return await self._handle_captcha(page)

            # Fill in credentials
            username_input = await page.wait_for_selector(
                SELECTORS["username_input"], timeout=60_000
            )
            await username_input.fill(username)

            password_input = await page.wait_for_selector(
                SELECTORS["password_input"], timeout=30_000
            )
            await password_input.fill(password)

            # Submit login form
            login_button = await page.wait_for_selector(
                SELECTORS["login_button"], timeout=30_000
            )

            try:
                await login_button.evaluate("el => el.click()")
                logger.debug("Clicked login button via JavaScript")
            except Exception as exc:
                logger.debug("Login click error (may be expected): %s", exc)

            # Wait for the login page to go away.  In Docker the navigation
            # can be very slow (60+ seconds).  _wait_for_page_content() would
            # return immediately because the login page itself has inputs, so
            # we explicitly wait for the URL to change or login elements to
            # disappear before inspecting the next state.
            pre_login_url = page.url
            for i in range(90):
                await asyncio.sleep(2)
                cur_url = page.url
                if cur_url != pre_login_url:
                    logger.info(
                        "Login page navigated after %ds: %s -> %s",
                        (i + 1) * 2,
                        pre_login_url,
                        cur_url,
                    )
                    break
                # Also check if the login form itself disappeared (SPA transition)
                try:
                    still_login = await page.evaluate("""
                        () => {
                            const loginBtn = document.querySelector('#ctl00_ContentPlaceHolder1_loginform_signInButton, input[value="Log In"]');
                            return !!loginBtn;
                        }
                    """)
                    if not still_login:
                        logger.info("Login form disappeared after %ds", (i + 1) * 2)
                        break
                except Exception:
                    break  # page context destroyed = navigation happened
                if i % 15 == 14:
                    logger.info(
                        "Still waiting for login navigation (%ds)...", (i + 1) * 2
                    )
            else:
                logger.warning("Login page did not navigate after 180s")

            # Now wait for the NEW page to render its content
            await self._wait_for_page_content(page, "post-login", timeout=120)

            # Handle alarm.com "We're having problems" error page
            await self._handle_error_page_retry(page)

            await self._debug_page_state(page, "post_login_settled")

            # Check result
            if await self._detect_captcha(page):
                return await self._handle_captcha(page)

            if await self._detect_trust_device(page):
                return await self._handle_trust_device(page)

            if await self._detect_2fa(page):
                return await self._handle_2fa(page)

            if await self._check_logged_in(page):
                self.state.auth_status = AuthStatus.AUTHENTICATED
                self.state.auth_message = "Successfully authenticated"
                self.state.last_auth_time = time.time()
                logger.info("Login successful")
                return AuthStatus.AUTHENTICATED

            # Unknown state - take a screenshot for debugging
            await self._debug_page_state(page, "login_failed")
            screenshot = await page.screenshot(full_page=True)
            self.state.challenge_screenshot = screenshot
            self.state.auth_status = AuthStatus.ERROR
            current_url = page.url
            if _is_login_page(current_url):
                self.state.auth_message = (
                    "Login failed - still on login page. Check credentials."
                )
                logger.warning("Login failed - still on login page: %s", current_url)
            else:
                self.state.auth_message = (
                    f"Login failed - unexpected page: {current_url}"
                )
                logger.warning(
                    "Login resulted in unexpected page state: %s", current_url
                )
            return AuthStatus.ERROR

        except Exception as e:
            self.state.auth_status = AuthStatus.ERROR
            self.state.auth_message = f"Login error: {str(e)}"
            logger.exception("Login failed")
            return AuthStatus.ERROR

    async def _check_logged_in(self, page: Page) -> bool:
        """Check if we're on a logged-in page (customer portal, not public site).

        Auth flow pages under /web/system-install/ are NOT considered logged in.
        Only actual customer portal pages (/web/video, /web/dashboard, etc.) count.
        """
        url = page.url
        logger.debug("_check_logged_in: URL=%s", url)

        if _is_login_page(url) or ALARM_BASE_URL not in url:
            return False

        from urllib.parse import urlparse

        path = urlparse(url).path.lower()

        # Auth flow pages (2FA, trust device, login-setup) are NOT logged in
        auth_flow_prefixes = (
            "/web/system-install/",
            "/web/two-factor",
            "/web/login",
        )
        if any(path.startswith(prefix) for prefix in auth_flow_prefixes):
            logger.debug("_check_logged_in: on auth flow path %s, NOT logged in", path)
            return False

        # If on a /web/ path (but not auth flow), check for dashboard indicators
        if path.startswith("/web/"):
            try:
                await page.wait_for_selector(
                    SELECTORS["logged_in_indicator"], timeout=3_000
                )
                logger.debug("_check_logged_in: found indicator on %s", path)
                return True
            except Exception:
                # On /web/ path with no indicator — likely still loading or
                # on a non-dashboard page. Check page text for confirmation.
                try:
                    page_text = await page.evaluate(
                        "() => document.body ? document.body.innerText.substring(0, 500) : ''"
                    )
                    # If page has logout/sign-out links, we're likely logged in
                    if any(
                        kw in page_text.lower()
                        for kw in ("log out", "sign out", "dashboard")
                    ):
                        logger.debug(
                            "_check_logged_in: found auth keywords on %s", path
                        )
                        return True
                except Exception:
                    pass
                logger.debug(
                    "_check_logged_in: on /web/ path %s but no indicators found", path
                )
                return False

        # Not on /web/ path and not on /login - likely public site
        try:
            await page.wait_for_selector(
                SELECTORS["logged_in_indicator"], timeout=3_000
            )
            return True
        except Exception:
            logger.debug(
                "_check_logged_in: not on /web/ path and no indicator, NOT logged in"
            )
            return False

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        """Dismiss cookie consent banner if present on alarm.com."""
        try:
            # alarm.com uses <input type="submit" id="acceptCookies"> not <button>
            accept_btn = await page.query_selector(
                "#acceptCookies, "
                "#onetrust-accept-btn-handler, "
                'input[value="Accept all cookies" i], '
                'button:has-text("Accept all Cookies"), '
                'button:has-text("Accept All Cookies"), '
                'button:has-text("Accept all cookies"), '
                'button:has-text("Accept Cookies"), '
                'a:has-text("Accept all Cookies")'
            )
            if accept_btn:
                await accept_btn.click(force=True)
                logger.info("Dismissed cookie consent banner")
                await asyncio.sleep(1)
            else:
                logger.debug("No cookie consent banner found")
        except Exception as exc:
            logger.debug("Cookie banner dismissal error: %s", exc)

    async def _wait_for_spa_settled(self, page: Page, timeout: int = 30) -> None:
        """Wait for the Ember SPA to finish initial load after auth.

        The SPA makes API calls on load (dashboard data, WebSocket setup, etc).
        These can fail (403, 409) causing it to transition to /web/system/app-error.
        We wait for the page to stabilize, then handle errors before proceeding.
        """
        from urllib.parse import urlparse

        logger.debug("Waiting for SPA to settle (up to %ds)...", timeout)

        # Give the SPA time to make its initial API calls
        prev_url = page.url
        for i in range(timeout // 3):
            await asyncio.sleep(3)
            cur_url = page.url

            # If we got redirected to login, session is dead
            if _is_login_page(cur_url):
                logger.warning("Session lost during SPA load (redirected to login)")
                return

            # Check if the URL stopped changing
            if cur_url == prev_url:
                # Check if the SPA has a loading spinner or is still loading
                try:
                    is_loading = await page.evaluate("""
                        () => {
                            const spinners = document.querySelectorAll('.loading, .spinner, [class*="loading"], [class*="spinner"]');
                            return spinners.length > 0;
                        }
                    """)
                    if not is_loading:
                        break
                except Exception:
                    break
            prev_url = cur_url
            if i % 3 == 2:
                logger.debug("SPA still settling (%ds), URL: %s", (i + 1) * 3, cur_url)

        # Check for app-error state
        cur_path = urlparse(page.url).path.lower()
        if "app-error" in cur_path or "error" in cur_path:
            logger.warning("SPA landed on error page: %s", page.url)
            await self._debug_page_state(page, "spa_error")
            await self._handle_error_page_retry(page)

        logger.debug("SPA settled, URL: %s", page.url)

    async def _wait_for_page_content(
        self, page: Page, label: str, timeout: int = 90
    ) -> bool:
        """Wait for the page to have meaningful DOM content.

        Polls every 2 seconds for up to `timeout` seconds. Returns True if
        content was found, False if we gave up. alarm.com can be very slow
        (60+ seconds) so this must be patient.
        """
        for attempt in range(timeout // 2):
            try:
                has_content = await page.evaluate("""
                    () => {
                        const inputs = document.querySelectorAll('input');
                        const buttons = document.querySelectorAll('button');
                        const hasText = (document.body?.innerText || '').trim().length > 50;
                        return (inputs.length > 0 || buttons.length > 1 || hasText);
                    }
                """)
                if has_content:
                    if attempt > 0:
                        logger.debug(
                            "Page content ready for %s after %ds",
                            label,
                            (attempt + 1) * 2,
                        )
                    return True
            except Exception:
                pass
            await asyncio.sleep(2)
            if attempt % 10 == 9:
                logger.info(
                    "Still waiting for %s to render (%ds)...", label, (attempt + 1) * 2
                )
        logger.warning("Timed out waiting for %s content after %ds", label, timeout)
        return False

    async def _handle_error_page_retry(self, page: Page) -> None:
        """Detect alarm.com error page and attempt recovery.

        alarm.com sometimes shows 'We're having problems' or transitions to
        /web/system/app-error when the Ember SPA's initial API calls fail
        (e.g. 403 due to cookie/session timing). Retrying usually works.
        """
        from urllib.parse import urlparse

        for retry in range(3):
            cur_path = urlparse(page.url).path.lower()
            try:
                error_text = await page.evaluate("""
                    () => {
                        const h1 = document.querySelector('h1.page-header, h1');
                        const body = (document.body?.innerText || '').substring(0, 500).toLowerCase();
                        return {
                            h1: h1 ? h1.textContent.trim() : '',
                            hasError: body.includes('having problems') || body.includes('error') || body.includes('try again'),
                        };
                    }
                """)
            except Exception:
                return

            is_error = (
                "having problems" in error_text.get("h1", "").lower()
                or "app-error" in cur_path
                or (error_text.get("hasError", False) and "error" in cur_path)
            )
            if not is_error:
                return

            logger.warning(
                "Alarm.com error page detected (attempt %d, URL: %s), trying recovery",
                retry + 1,
                page.url,
            )

            # Try clicking "Try Again" / "Reload Application" button
            try:
                retry_btn = await page.query_selector(
                    "button.btn-color-primary, "
                    'button:has-text("Try Again"), '
                    'button:has-text("Reload"), '
                    "button.refresh"
                )
                if retry_btn:
                    btn_text = await retry_btn.evaluate(
                        "el => el.textContent.trim().substring(0, 40)"
                    )
                    logger.info("Clicking recovery button: %s", btn_text)
                    await retry_btn.click()
                    await self._wait_for_page_content(page, "error retry", timeout=90)
                else:
                    # No button found — try reloading the SPA via the nav
                    logger.info("No retry button, attempting page reload via F5")
                    await page.reload(wait_until="domcontentloaded", timeout=60_000)
                    await self._wait_for_page_content(page, "error reload", timeout=90)
            except Exception as exc:
                logger.debug("Error recovery failed: %s", exc)
                return

    async def _detect_captcha(self, page: Page) -> bool:
        """Detect if a CAPTCHA challenge is present."""
        try:
            element = await page.query_selector(SELECTORS["captcha_element"])
            return element is not None
        except Exception:
            return False

    async def _detect_2fa(self, page: Page) -> bool:
        """Detect if a 2FA prompt is present."""
        try:
            # Don't detect 2FA if we're still on the exact login page
            if _is_login_page(page.url):
                logger.debug("Still on login page, skipping 2FA detection")
                return False
            element = await page.query_selector(SELECTORS["twofa_element"])
            return element is not None
        except Exception:
            return False

    async def _handle_captcha(self, page: Page) -> AuthStatus:
        """Handle a CAPTCHA challenge by screenshotting for the user."""
        logger.info("CAPTCHA detected - user intervention required")
        screenshot = await page.screenshot(full_page=True)
        self.state.challenge_screenshot = screenshot
        self.state.auth_status = AuthStatus.CAPTCHA_REQUIRED
        self.state.auth_message = "CAPTCHA detected. Please solve it in the web UI."
        return AuthStatus.CAPTCHA_REQUIRED

    async def _handle_2fa(self, page: Page) -> AuthStatus:
        """Handle a 2FA prompt by screenshotting for the user."""
        logger.info("2FA prompt detected - user intervention required")
        screenshot = await page.screenshot(full_page=True)
        self.state.challenge_screenshot = screenshot
        self.state.auth_status = AuthStatus.TWO_FA_REQUIRED

        # Check for error messages (invalid/expired code)
        error_msg = ""
        try:
            error_msg = await page.evaluate("""
                () => {
                    const err = document.querySelector('.error-message, .alert-danger, [class*="error"], [class*="invalid"]');
                    return err ? err.textContent.trim() : '';
                }
            """)
        except Exception:
            pass

        if error_msg:
            self.state.auth_message = (
                f"2FA code rejected: {error_msg}. Try again or resend code."
            )
            logger.info("2FA error message: %s", error_msg)
        else:
            self.state.auth_message = (
                "Two-factor authentication required. Enter your code."
            )

        return AuthStatus.TWO_FA_REQUIRED

    async def _detect_trust_device(self, page: Page) -> bool:
        """Detect if alarm.com is showing a 'trust this device' prompt."""
        try:
            # Check for the specific "Device Name" placeholder input + Trust Device button
            has_trust = await page.evaluate("""
                () => {
                    const hasDeviceInput = !!document.querySelector('input[placeholder*="Device Name"], input[placeholder*="device name"]');
                    const hasTrustBtn = !!document.querySelector('button');
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const hasTrustText = buttons.some(b => {
                        const t = b.textContent.trim().toLowerCase();
                        return t.includes('trust device') || t.includes('trust this');
                    });
                    const hasSkipBtn = buttons.some(b => b.textContent.trim().toLowerCase() === 'skip');
                    return (hasDeviceInput && hasTrustText) || (hasDeviceInput && hasSkipBtn);
                }
            """)
            return bool(has_trust)
        except Exception:
            return False

    async def _handle_trust_device(self, page: Page) -> AuthStatus:
        """Auto-fill device name and trust the device."""
        logger.info(
            "Trust device page detected - auto-trusting as '%s'",
            self._trusted_device_name,
        )

        try:
            # Debug: dump the page elements
            try:
                page_info = await page.evaluate("""
                    () => {
                        const inputs = document.querySelectorAll('input');
                        const buttons = document.querySelectorAll('button');
                        const inputInfo = Array.from(inputs).map(e =>
                            `<INPUT id="${e.id}" class="${e.className}" type="${e.type}" name="${e.name}" placeholder="${e.placeholder}" value="${e.value}">`
                        ).join('\\n');
                        const buttonInfo = Array.from(buttons).map(e =>
                            `<BUTTON id="${e.id}" class="${e.className}">${e.textContent.trim().substring(0, 50)}`
                        ).join('\\n');
                        return inputInfo + '\\n---\\n' + buttonInfo;
                    }
                """)
                logger.debug("Trust device page elements:\n%s", page_info[:3000])
            except Exception:
                pass

            # Find the device name input — same element ID is reused from 2FA
            # but now has placeholder="Device Name" and type="text"
            name_input = await page.query_selector(SELECTORS["trust_device_name_input"])
            if not name_input:
                # Fallback: the input with id two-factor-authentication-input-field
                # which alarm.com reuses for the device name field
                name_input = await page.query_selector(
                    "#two-factor-authentication-input-field"
                )

            if name_input:
                await name_input.click(force=True)
                await name_input.evaluate('el => el.value = ""')
                await name_input.fill("")
                await name_input.type(self._trusted_device_name, delay=30)
                await asyncio.sleep(0.3)
                logger.info("Filled device name: %s", self._trusted_device_name)
            else:
                logger.warning("No device name input found on trust page")

            await asyncio.sleep(0.5)

            # Click "Trust Device" button
            trust_btn = await page.query_selector(SELECTORS["trust_device_submit"])
            if trust_btn:
                btn_info = await trust_btn.evaluate("""
                    el => `<${el.tagName} id="${el.id}" class="${el.className}" text="${el.textContent?.trim().substring(0, 60)}">`
                """)
                logger.info("Trust Device button found: %s", btn_info)
                await trust_btn.click(force=True)
                logger.info("Clicked Trust Device button")
            else:
                logger.warning(
                    "Trust Device button not found with selector: %s",
                    SELECTORS["trust_device_submit"],
                )
                # Log all buttons on the page for debugging
                try:
                    all_btns = await page.evaluate("""
                        () => Array.from(document.querySelectorAll('button')).map(b =>
                            `<BUTTON id="${b.id}" class="${b.className}" text="${b.textContent.trim().substring(0, 60)}">`
                        ).join('\\n')
                    """)
                    logger.info("All buttons on trust page:\n%s", all_btns[:2000])
                except Exception:
                    pass

                skip_btn = await page.query_selector(SELECTORS["trust_device_skip"])
                if skip_btn:
                    await skip_btn.click(force=True)
                    logger.info("Clicked Skip button on trust page")
                else:
                    logger.error("Neither Trust Device nor Skip button found")

            # Wait patiently for the SPA to process and redirect.
            # alarm.com can be very slow (60+ seconds).
            pre_url = page.url
            for i in range(90):
                await asyncio.sleep(2)
                current_url = page.url
                if current_url != pre_url:
                    logger.info(
                        "Trust device redirected after %ds: %s -> %s",
                        (i + 1) * 2,
                        pre_url,
                        current_url,
                    )
                    await self._wait_for_page_content(
                        page, "post-trust-device", timeout=90
                    )
                    break
                # Check if the trust device form has disappeared
                try:
                    trust_still_visible = await page.evaluate("""
                        () => {
                            const input = document.querySelector('input[placeholder*="Device Name"]');
                            return !!input;
                        }
                    """)
                    if not trust_still_visible:
                        logger.info(
                            "Trust device form disappeared after %ds", (i + 1) * 2
                        )
                        break
                except Exception:
                    pass
                if i % 15 == 14:
                    logger.info(
                        "Still waiting for trust device transition (%ds)...",
                        (i + 1) * 2,
                    )

            # Handle error page if it appeared
            await self._handle_error_page_retry(page)

            await self._debug_page_state(page, "post_trust_device")

            # Check if we're now logged in
            if await self._check_logged_in(page):
                self.state.auth_status = AuthStatus.AUTHENTICATED
                self.state.auth_message = "Successfully authenticated (device trusted)"
                self.state.challenge_screenshot = None
                self.state.last_auth_time = time.time()
                logger.info("Device trusted, now authenticated")
                return AuthStatus.AUTHENTICATED

            # If on a /web/ path (not auth flow), treat as authenticated
            from urllib.parse import urlparse

            path = urlparse(page.url).path.lower()
            auth_flow_prefixes = (
                "/web/system-install/",
                "/web/two-factor",
                "/web/login",
            )
            if path.startswith("/web/") and not any(
                path.startswith(p) for p in auth_flow_prefixes
            ):
                logger.info(
                    "On /web/ path after trust device (%s), treating as authenticated",
                    page.url,
                )
                self.state.auth_status = AuthStatus.AUTHENTICATED
                self.state.auth_message = "Successfully authenticated (device trusted)"
                self.state.challenge_screenshot = None
                self.state.last_auth_time = time.time()
                return AuthStatus.AUTHENTICATED

            # Not yet authenticated - screenshot for debugging
            screenshot = await page.screenshot(full_page=True)
            self.state.challenge_screenshot = screenshot
            self.state.auth_status = AuthStatus.ERROR
            self.state.auth_message = (
                "Trust device step completed but not authenticated"
            )
            logger.warning(
                "Trust device completed but auth check failed (URL: %s)", page.url
            )
            return AuthStatus.ERROR

        except Exception as e:
            logger.exception("Failed to handle trust device page")
            self.state.auth_status = AuthStatus.ERROR
            self.state.auth_message = f"Error trusting device: {str(e)}"
            return AuthStatus.ERROR

    async def solve_challenge(self, solution: str) -> AuthStatus:
        """Submit a CAPTCHA solution or 2FA code."""
        async with self._lock:
            return await self._solve_challenge_impl(solution)

    async def _solve_challenge_impl(self, solution: str) -> AuthStatus:
        """Solve challenge implementation (must be called with self._lock held)."""
        page = await self._get_page()

        try:
            if self.state.auth_status == AuthStatus.CAPTCHA_REQUIRED:
                # CAPTCHA solving depends on the type. For reCAPTCHA, the user
                # typically needs to interact directly. For text CAPTCHAs, we can
                # type the solution. Take a pragmatic approach: look for any
                # visible text input near the CAPTCHA and type there.
                # This may need refinement based on actual alarm.com CAPTCHA type.
                inputs = await page.query_selector_all('input[type="text"]:visible')
                if inputs:
                    await inputs[0].fill(solution)
                    submit = await page.query_selector(SELECTORS["login_button"])
                    if submit:
                        await submit.click(force=True)

            elif self.state.auth_status == AuthStatus.TWO_FA_REQUIRED:
                await self._debug_page_state(page, "2fa_before_input")
                # Debug: dump all inputs on the 2FA page
                try:
                    inputs_html = await page.evaluate("""
                        () => {
                            const inputs = document.querySelectorAll('input, textarea');
                            return Array.from(inputs).map(e =>
                                `<${e.tagName} id="${e.id}" class="${e.className}" type="${e.type}" name="${e.name}" placeholder="${e.placeholder}">`
                            ).join('\\n');
                        }
                    """)
                    logger.debug("2FA page inputs:\n%s", inputs_html[:3000])
                except Exception:
                    pass

                twofa_input = await page.query_selector(SELECTORS["twofa_element"])
                if not twofa_input:
                    # Fallback: find any visible text/tel/number input
                    twofa_input = await page.query_selector(
                        'input[type="text"]:visible, input[type="tel"]:visible, input[type="number"]:visible'
                    )
                if not twofa_input:
                    logger.error("Could not find 2FA code input on the page")
                    self.state.auth_status = AuthStatus.ERROR
                    self.state.auth_message = "Could not find 2FA input field"
                    return AuthStatus.ERROR

                # Use click + type (real keystrokes) instead of fill() so that
                # Ember.js data-binding picks up the value properly.
                await twofa_input.click(force=True)
                await twofa_input.fill("")  # clear any existing value
                await twofa_input.type(solution, delay=50)

                # Brief pause to let Ember process the input
                await asyncio.sleep(0.5)
                await self._debug_page_state(page, "2fa_code_entered")

                # Re-query the submit button fresh (Ember may have re-rendered)
                submit = await page.query_selector(SELECTORS["twofa_submit"])
                submitted_2fa = False

                # Log exactly which element we found to catch mis-targeting
                if submit:
                    btn_info = await submit.evaluate("""
                        el => `<${el.tagName} id="${el.id}" class="${el.className}" type="${el.type || ''}" text="${el.textContent?.trim().substring(0, 60) || el.value || ''}">`
                    """)
                    logger.info("2FA submit button found: %s", btn_info)
                else:
                    logger.warning(
                        "No 2FA submit button found with selector: %s",
                        SELECTORS["twofa_submit"],
                    )

                if submit:
                    # Click the submit button ONCE. Ember processes the click
                    # asynchronously (API call → navigation), so we must NOT
                    # double-click — that confuses alarm.com's server and
                    # results in session loss (/login?m=no_session).
                    try:
                        await submit.click(force=True)
                        logger.info("Clicked 2FA submit button")
                        submitted_2fa = True
                    except Exception as exc:
                        if (
                            "context was destroyed" in str(exc)
                            or "navigation" in str(exc).lower()
                        ):
                            logger.debug("2FA click triggered navigation (good)")
                            submitted_2fa = True
                        else:
                            logger.warning("2FA submit click error: %s", exc)

                if not submitted_2fa and not submit:
                    # Last resort: try to find ANY button in the 2FA Ember component
                    # that looks like a submit action, avoiding ASP.NET shell buttons
                    logger.warning(
                        "Primary selector failed, searching for verify button in Ember component"
                    )
                    fallback = await page.query_selector(
                        '[class*="two-factor"] button, '
                        '[class*="verification"] button, '
                        '[class*="login-setup"] button, '
                        ".ember-view button.btn-color-primary"
                    )
                    if fallback:
                        fb_info = await fallback.evaluate("""
                            el => `<${el.tagName} id="${el.id}" class="${el.className}" text="${el.textContent?.trim().substring(0, 60)}">`
                        """)
                        logger.info("Fallback 2FA button found: %s", fb_info)
                        try:
                            await fallback.click(force=True)
                            logger.info("Clicked fallback 2FA button")
                        except Exception as exc:
                            if (
                                "context was destroyed" in str(exc)
                                or "navigation" in str(exc).lower()
                            ):
                                logger.debug(
                                    "Fallback click triggered navigation (good)"
                                )
                            else:
                                logger.warning("Fallback 2FA click error: %s", exc)
                    else:
                        logger.error("No 2FA submit button found anywhere on the page")

            # Wait patiently for the SPA to process and transition.
            # The URL may or may not change — Ember sometimes swaps content
            # within the same URL (e.g. 2FA -> trust device on the same path).
            # alarm.com can be very slow (60+ seconds).
            pre_url = page.url
            try:
                pre_page_text = await page.evaluate(
                    "() => (document.body?.innerText || '').substring(0, 200)"
                )
            except Exception:
                pre_page_text = ""

            for i in range(90):
                await asyncio.sleep(2)
                current = page.url
                if current != pre_url:
                    logger.info(
                        "URL changed after %ds: %s -> %s", (i + 1) * 2, pre_url, current
                    )
                    await self._wait_for_page_content(
                        page, "post-challenge", timeout=90
                    )
                    break
                # Check if page content changed (Ember swapped views in same URL)
                try:
                    cur_text = await page.evaluate(
                        "() => (document.body?.innerText || '').substring(0, 200)"
                    )
                    if cur_text != pre_page_text and len(cur_text.strip()) > 20:
                        logger.info("Page content changed after %ds", (i + 1) * 2)
                        break
                except Exception:
                    pass
                if i % 15 == 14:
                    logger.info(
                        "Still waiting for page transition (%ds), current: %s",
                        (i + 1) * 2,
                        current,
                    )

            # Handle error page if it appeared
            await self._handle_error_page_retry(page)

            await self._debug_page_state(page, "post_challenge_settled")

            # Check for trust device page FIRST (shown after successful 2FA)
            if await self._detect_trust_device(page):
                return await self._handle_trust_device(page)

            # Check if we're now logged in
            if await self._check_logged_in(page):
                self.state.auth_status = AuthStatus.AUTHENTICATED
                self.state.auth_message = "Successfully authenticated"
                self.state.challenge_screenshot = None
                self.state.last_auth_time = time.time()
                logger.info("Challenge solved, now authenticated")
                return AuthStatus.AUTHENTICATED

            # If we're on a /web/ path, we may be authenticated even if
            # _check_logged_in didn't find the specific indicator element.
            # The SPA may not have rendered the dashboard yet. Accept it.
            from urllib.parse import urlparse

            path = urlparse(page.url).path.lower()
            if path.startswith("/web/") and not _is_login_page(page.url):
                logger.info(
                    "On /web/ path after challenge (%s), treating as authenticated",
                    page.url,
                )
                self.state.auth_status = AuthStatus.AUTHENTICATED
                self.state.auth_message = "Successfully authenticated"
                self.state.challenge_screenshot = None
                self.state.last_auth_time = time.time()
                return AuthStatus.AUTHENTICATED

            # Still challenged?
            if await self._detect_captcha(page):
                return await self._handle_captcha(page)
            if await self._detect_2fa(page):
                return await self._handle_2fa(page)

            # Unknown state - save screenshot for debugging
            screenshot = await page.screenshot(full_page=True)
            self.state.challenge_screenshot = screenshot
            try:
                debug_dir = pathlib.Path(self._data_dir) / "debug"
                (debug_dir / "challenge_failed.png").write_bytes(screenshot)
            except Exception:
                pass
            self.state.auth_status = AuthStatus.ERROR
            self.state.auth_message = (
                f"Challenge solution failed. Post-challenge URL: {page.url}. "
                "See debug screenshots in Status tab."
            )
            logger.warning("Challenge failed, URL: %s", page.url)
            return AuthStatus.ERROR

        except Exception as e:
            self.state.auth_status = AuthStatus.ERROR
            self.state.auth_message = f"Error solving challenge: {str(e)}"
            logger.exception("Failed to solve challenge")
            return AuthStatus.ERROR

    async def resend_2fa_code(self) -> dict:
        """Click the 'resend code' link on the 2FA page."""
        if self.state.auth_status != AuthStatus.TWO_FA_REQUIRED:
            return {"success": False, "error": "Not in 2FA state"}

        async with self._lock:
            return await self._resend_2fa_code_impl()

    async def _resend_2fa_code_impl(self) -> dict:
        """Resend 2FA code implementation (must be called with self._lock held)."""
        page = await self._get_page()

        try:
            resend_elem = await page.query_selector(SELECTORS["twofa_resend"])
            if not resend_elem:
                # Debug: log what links/buttons are on the page
                try:
                    page_links = await page.evaluate("""
                        () => {
                            const elems = document.querySelectorAll('a, button');
                            return Array.from(elems).map(e =>
                                `<${e.tagName} id="${e.id}" class="${e.className}" href="${e.href || ''}">${e.textContent.trim().substring(0, 50)}`
                            ).join('\\n');
                        }
                    """)
                    logger.debug("2FA page links/buttons:\n%s", page_links[:3000])
                except Exception:
                    pass
                return {
                    "success": False,
                    "error": "Could not find resend link on the page",
                }

            await resend_elem.click(force=True)
            logger.info("Clicked 2FA resend code button")

            # Wait for the 2FA input to reappear (Ember re-renders the component)
            await asyncio.sleep(5)
            try:
                await page.wait_for_selector(SELECTORS["twofa_element"], timeout=30_000)
            except Exception:
                logger.debug("2FA input not found after resend, page may have changed")

            # Take a fresh screenshot
            screenshot = await page.screenshot(full_page=True)
            self.state.challenge_screenshot = screenshot

            return {"success": True, "message": "Resend code requested"}

        except Exception as e:
            logger.exception("Failed to resend 2FA code")
            return {"success": False, "error": str(e)}

    def get_auth_status(self) -> dict:
        """Get current auth status as a dict for API responses."""
        return {
            "status": self.state.auth_status.value,
            "message": self.state.auth_message,
        }

    # ---- Camera Discovery ----

    async def discover_cameras(self, force: bool = False) -> list[CameraInfo]:
        """Navigate to the cameras page and discover available cameras.

        Uses a TTL cache (default 1 hour). Pass force=True to bypass the cache.
        """
        if self.state.auth_status != AuthStatus.AUTHENTICATED:
            logger.warning("Cannot discover cameras: not authenticated")
            return []

        # Return cached list if still fresh
        if not force and self.state.cameras and self.state.cameras_discovered_at:
            age = time.time() - self.state.cameras_discovered_at
            if age < self.CAMERA_CACHE_TTL:
                logger.debug("Using cached camera list (age: %ds)", int(age))
                return self.state.cameras

        page = await self._get_page()

        try:
            current_url = page.url
            logger.debug("Starting camera discovery from URL: %s", current_url)

            # After auth, the Ember SPA needs time to make its initial API
            # calls and render the dashboard.  If we rush into discovery
            # the SPA may still be loading or in an error state.  Wait for
            # the page to settle first.
            await self._wait_for_spa_settled(page)

            # Strategy 1: Try the internal API from the browser context.
            # This works from ANY authenticated page — no navigation needed.
            cameras = await self._discover_cameras_via_api(page)

            # Strategy 2: Navigate to the video page via SPA link clicking
            # and scrape camera info from the rendered DOM.
            # We MUST NOT use page.goto() or window.location — those are full
            # page navigations that destroy the Ember SPA session.
            if not cameras:
                logger.debug("API discovery found no cameras, trying DOM discovery")

                # If we're not already on the video page, navigate there
                from urllib.parse import urlparse

                current_path = urlparse(page.url).path.lower()
                if "/video" not in current_path:
                    logger.debug(
                        "Not on video page (%s), looking for Video nav link",
                        current_path,
                    )

                    # Log all links with "video" for debugging
                    try:
                        video_links = await page.evaluate("""
                            () => Array.from(document.querySelectorAll('a')).filter(a => {
                                const href = (a.getAttribute('href') || '').toLowerCase();
                                const text = (a.textContent || '').trim().toLowerCase();
                                return href.includes('video') || text === 'video';
                            }).map(a => ({
                                href: a.getAttribute('href'),
                                text: a.textContent.trim().substring(0, 40),
                                visible: a.offsetParent !== null,
                                classes: a.className.toString().substring(0, 80),
                                inEmber: !!a.closest('.ember-application, .ember-view')
                            }))
                        """)
                        logger.info(
                            "Video-related links on page: %s",
                            json.dumps(video_links, indent=2)[:3000],
                        )
                    except Exception:
                        pass

                    # Click the SPA's Video nav link.
                    # Alarm.com's Ember SPA uses href="#" on nav links — routing
                    # is handled by Ember click handlers.  Use data-testid which
                    # is stable across renders.
                    video_link = await page.query_selector(
                        'a[data-testid="video-link"]'
                    )

                    if video_link:
                        link_info = await video_link.evaluate("""
                            el => `<A href="${el.getAttribute('href')}" class="${el.className}" text="${el.textContent.trim().substring(0, 40)}">`
                        """)
                        logger.info("Clicking Video nav link: %s", link_info)
                        await video_link.click()

                        # Wait for the video page to render.  Don't use
                        # networkidle (alarm.com analytics never settle).
                        await self._wait_for_page_content(
                            page, "video page", timeout=60
                        )
                        # Extra time for WebRTC players to initialize
                        await asyncio.sleep(8)
                    else:
                        logger.warning(
                            "No Video nav link found in DOM — cannot navigate to video page"
                        )
                else:
                    logger.debug("Already on video page, waiting for players to render")
                    await asyncio.sleep(3)

                if _is_login_page(page.url):
                    logger.warning(
                        "Session lost navigating to video (URL: %s)", page.url
                    )
                elif "/video" in page.url or "/video" in current_path:
                    # Dump the full page state for debugging camera discovery
                    await self._debug_page_state(page, "video_page_for_discovery")

                    cameras = await self._discover_cameras_from_page(page)

                    # Retry with longer wait if no cameras found (players may still be loading)
                    if not cameras:
                        logger.info(
                            "No cameras found yet, waiting 10s for WebRTC players to initialize..."
                        )
                        await asyncio.sleep(10)
                        cameras = await self._discover_cameras_from_page(page)

            # Debug: save a screenshot
            try:
                debug_dir = pathlib.Path(self._data_dir) / "debug"
                debug_dir.mkdir(exist_ok=True)
                await page.screenshot(
                    path=str(debug_dir / "cameras_page.png"), full_page=True
                )
                logger.debug("Saved cameras page screenshot (URL: %s)", page.url)
            except Exception:
                pass

            self.state.cameras = cameras
            self.state.cameras_discovered_at = time.time()
            logger.info("Discovered %d cameras", len(cameras))

            # Capture initial snapshots while we're on the video page
            if cameras and "/video" in page.url:
                for cam in cameras:
                    try:
                        video_elem = await page.query_selector(
                            SELECTORS["video_element"]
                        )
                        if video_elem:
                            await asyncio.sleep(2)
                            screenshot = await video_elem.screenshot()
                            jpeg = self._to_jpeg(screenshot)
                            self._save_snapshot(cam.id, jpeg)
                            logger.info(
                                "Initial snapshot for %s (%d bytes)",
                                cam.name,
                                len(jpeg),
                            )
                    except Exception:
                        logger.debug("Failed initial snapshot for %s", cam.id)

            return cameras

        except Exception:
            logger.exception("Camera discovery failed")
            return self.state.cameras  # Return cached list on failure

    async def _discover_cameras_via_api(self, page: Page) -> list[CameraInfo]:
        """Try to call the alarm.com video API directly from the browser context."""
        cameras = []
        try:
            # Try multiple API endpoints that alarm.com might use
            result = await page.evaluate("""
                async () => {
                    const endpoints = [
                        '/web/api/video/cameras',
                        '/web/api/devices/cameras',
                        '/web/api/video/devices',
                    ];
                    for (const ep of endpoints) {
                        try {
                            const resp = await fetch(ep, {
                                credentials: 'same-origin',
                                headers: { 'Accept': 'application/json' }
                            });
                            if (resp.ok) {
                                const data = await resp.json();
                                return { endpoint: ep, data: data };
                            }
                        } catch (e) {}
                    }

                    // Try to find camera data in the page's Ember data store
                    try {
                        const appInstance = document.querySelector('.ember-application')?.__emberApp__;
                        if (appInstance) {
                            return { endpoint: 'ember', data: 'ember app found' };
                        }
                    } catch (e) {}

                    return null;
                }
            """)

            logger.debug(
                "Camera API discovery result: %s",
                str(result)[:2000] if result else "None",
            )

            if result and isinstance(result, dict):
                api_data = result.get("data", result)
                # Handle nested response structure
                if isinstance(api_data, dict):
                    camera_data = (
                        api_data.get("data", [])
                        or api_data.get("cameras", [])
                        or api_data.get("devices", [])
                        or api_data.get("value", [])
                    )
                elif isinstance(api_data, list):
                    camera_data = api_data
                else:
                    camera_data = []

                if isinstance(camera_data, list):
                    for cam in camera_data:
                        if not isinstance(cam, dict):
                            continue
                        cam_id = str(
                            cam.get("id", cam.get("deviceId", cam.get("deviceID", "")))
                        )
                        name = cam.get(
                            "description",
                            cam.get("name", cam.get("deviceName", f"Camera {cam_id}")),
                        )
                        model = cam.get(
                            "model",
                            cam.get("deviceModel", cam.get("deviceModelName", "")),
                        )
                        cameras.append(
                            CameraInfo(
                                id=cam_id,
                                name=name,
                                model=model,
                                status="online",
                            )
                        )
                    logger.debug(
                        "API discovery found %d cameras from %s",
                        len(cameras),
                        result.get("endpoint", "unknown"),
                    )
        except Exception:
            logger.debug("API-based camera discovery failed, falling back to DOM")

        return cameras

    async def _discover_cameras_from_page(self, page: Page) -> list[CameraInfo]:
        """Scrape camera info from the live video page.

        Tries multiple strategies:
        A) WebRTC player container IDs (most reliable for camera IDs)
        B) Video player elements with numeric IDs
        C) Camera description/name elements (works even before video loads)
        D) Camera group ID from the URL
        """
        cameras = []
        try:
            result = await page.evaluate("""
                () => {
                    const cameras = [];
                    const seenIds = new Set();

                    // Strategy A: Extract from WebRTC player container IDs
                    // Format: webrtc-player_<cameraId>-container
                    const players = document.querySelectorAll('[id*="webrtc-player"]');
                    players.forEach((el, i) => {
                        const id = el.id || '';
                        const match = id.match(/webrtc-player_([\\d-]+?)(?:-container)?$/);
                        if (match && !seenIds.has(match[1])) {
                            seenIds.add(match[1]);
                            const playerParent = el.closest('.video-player, .live-video-player') || el.parentElement;
                            let name = '';
                            if (playerParent) {
                                const nameEl = playerParent.querySelector('.camera-description, .bottom-bar-camera-name, .camera-name');
                                if (nameEl) name = nameEl.textContent.trim();
                            }
                            if (!name) {
                                const desc = document.querySelector('.camera-description');
                                if (desc) name = desc.textContent.trim();
                            }
                            cameras.push({
                                id: match[1],
                                name: name || 'Camera ' + (i + 1),
                                url: window.location.href,
                                strategy: 'webrtc-player'
                            });
                        }
                    });

                    // Strategy B: Extract from video-player elements
                    if (cameras.length === 0) {
                        const videoPlayers = document.querySelectorAll('.video-player, .live-video-player');
                        videoPlayers.forEach((el, i) => {
                            const container = el.querySelector('[id*="player"]');
                            let camId = '';
                            if (container) {
                                const match = container.id.match(/(\\d{5,})/);
                                if (match) camId = match[1];
                            }
                            const nameEl = el.querySelector('.camera-description, .camera-name');
                            const name = nameEl ? nameEl.textContent.trim() : '';
                            if (camId && !seenIds.has(camId)) {
                                seenIds.add(camId);
                                cameras.push({
                                    id: camId,
                                    name: name || 'Camera ' + (i + 1),
                                    url: window.location.href,
                                    strategy: 'video-player'
                                });
                            }
                        });
                    }

                    // Strategy C: Camera description elements (works even before
                    // WebRTC players have initialized — the camera cards/names
                    // render first in the Ember template)
                    if (cameras.length === 0) {
                        const descElements = document.querySelectorAll(
                            '.camera-description, .bottom-bar-camera-name, .camera-name, ' +
                            '.video-camera-card, [class*="camera-card"], [data-camera-id]'
                        );
                        descElements.forEach((el, i) => {
                            let camId = el.getAttribute('data-camera-id') || '';
                            // Try to extract numeric ID from nearby elements
                            if (!camId) {
                                const parent = el.closest('[data-camera-id]');
                                if (parent) camId = parent.getAttribute('data-camera-id');
                            }
                            // Try to find ID in any child element's ID attribute
                            if (!camId) {
                                const idElem = el.querySelector('[id*="player"], [id*="camera"]');
                                if (idElem) {
                                    const match = idElem.id.match(/(\\d{5,})/);
                                    if (match) camId = match[1];
                                }
                            }
                            const name = el.textContent.trim();
                            if (!camId) camId = 'camera_' + i;
                            if (name && !seenIds.has(camId)) {
                                seenIds.add(camId);
                                cameras.push({
                                    id: camId,
                                    name: name,
                                    url: window.location.href,
                                    strategy: 'camera-description'
                                });
                            }
                        });
                    }

                    // Strategy D: Extract camera group/ID from the URL
                    if (cameras.length === 0) {
                        const url = window.location.href;
                        const groupMatch = url.match(/cameraGroupId=(\\d+)/);
                        if (groupMatch) {
                            const desc = document.querySelector('.camera-description, .bottom-bar-camera-name');
                            const name = desc ? desc.textContent.trim() : 'Camera';
                            cameras.push({
                                id: 'group_' + groupMatch[1],
                                name: name,
                                url: url,
                                strategy: 'url-group'
                            });
                        }
                    }

                    return cameras;
                }
            """)

            if result and isinstance(result, list):
                seen_ids = set()
                for cam in result:
                    cam_id = str(cam.get("id", ""))
                    if cam_id and cam_id not in seen_ids:
                        seen_ids.add(cam_id)
                        url = cam.get("url", "")
                        if url and not url.startswith("http"):
                            url = f"{ALARM_BASE_URL}{url}"
                        cameras.append(
                            CameraInfo(
                                id=cam_id,
                                name=cam.get("name", f"Camera {cam_id}"),
                                url=url,
                            )
                        )
                if cameras:
                    strategies = set(cam.get("strategy", "?") for cam in result)
                    logger.info(
                        "Page scrape found %d camera(s) via %s",
                        len(cameras),
                        strategies,
                    )

        except Exception:
            logger.exception("Page-based camera discovery failed")

        return cameras

    # ---- Snapshot Capture ----

    async def _navigate_to_live_view(self, page: Page, camera: CameraInfo) -> bool:
        """Navigate to a camera's live view via SPA link clicking.

        NEVER uses page.goto() — that destroys the Ember SPA session.
        Returns True if the live view is showing video content.
        """
        current_url = page.url
        logger.info(
            "_navigate_to_live_view: camera=%s, current URL=%s",
            camera.id,
            current_url[:120],
        )

        # If we're already on the live view page with video playing, stay here
        if "/video" in current_url:
            video = await page.query_selector(SELECTORS["video_element"])
            if video:
                logger.info("Already on video page with player visible")
                return True
            logger.info("On video page but no video element yet, checking for retry")
            # Check for video error with Retry button
            await self._retry_video_player(page)
            video = await page.query_selector(SELECTORS["video_element"])
            if video:
                logger.info("Video element found after retry")
                return True
            # Wait longer — the video player may still be loading
            logger.info("Waiting up to 15s for video element to appear...")
            try:
                video = await page.wait_for_selector(
                    SELECTORS["video_element"], timeout=15_000
                )
                if video:
                    logger.info("Video element appeared after wait")
                    return True
            except Exception:
                logger.info("Video element did not appear within 15s")

        # Navigate via SPA — Ember uses href="#" on nav links with
        # data-testid attributes for identification.
        video_link = await page.query_selector('a[data-testid="video-link"]')
        if video_link:
            logger.info("Clicking Video nav link for live view")
            await video_link.click()
            await self._wait_for_page_content(page, "video nav", timeout=30)
            await asyncio.sleep(5)
            logger.info("After video link click, URL=%s", page.url[:120])

            # Check for video player error and retry if needed
            await self._retry_video_player(page)

            video = await page.query_selector(SELECTORS["video_element"])
            if video:
                logger.info("Video element found after nav link click")
                return True
            # Wait for late-loading video
            logger.info("Waiting up to 15s for video element after nav...")
            try:
                video = await page.wait_for_selector(
                    SELECTORS["video_element"], timeout=15_000
                )
                if video:
                    logger.info("Video element appeared after nav wait")
                    return True
            except Exception:
                logger.info("Video element did not appear after nav")
        else:
            logger.info("No video-link nav element found on page")

        # Log what we can see on the page to help debug
        try:
            debug_info = await page.evaluate("""() => {
                const r = {};
                r.url = window.location.href;
                r.title = document.title;
                r.bodyText = document.body ?
                    document.body.innerText.substring(0, 500) : '<no body>';
                r.videoElements = document.querySelectorAll(
                    'video, canvas, .video-player, [class*="video"]'
                ).length;
                r.navLinks = Array.from(
                    document.querySelectorAll('a[data-testid]')
                ).map(a => a.getAttribute('data-testid')).join(', ');
                r.iframes = document.querySelectorAll('iframe').length;
                return r;
            }""")
            logger.warning(
                "Could not navigate to live view for camera %s. "
                "Page state: url=%s, title=%s, videoElements=%s, "
                "navLinks=[%s], iframes=%s, bodyText=%.200s",
                camera.id,
                debug_info.get("url", "?"),
                debug_info.get("title", "?"),
                debug_info.get("videoElements", "?"),
                debug_info.get("navLinks", "?"),
                debug_info.get("iframes", "?"),
                debug_info.get("bodyText", "?"),
            )
        except Exception as exc:
            logger.warning(
                "Could not navigate to live view for camera %s "
                "(page debug also failed: %s)",
                camera.id,
                exc,
            )
        return False

    async def _retry_video_player(self, page: Page) -> None:
        """Click the Retry button if the video player shows an error."""
        for attempt in range(3):
            try:
                retry_btn = await page.query_selector(
                    'button:has-text("Retry"), '
                    'button:has-text("RETRY"), '
                    ".video-player button.btn-color-primary"
                )
                if not retry_btn:
                    return
                logger.info(
                    "Video player error detected, clicking Retry (attempt %d)",
                    attempt + 1,
                )
                await retry_btn.click()
                await asyncio.sleep(8)
                # Check if video loaded after retry
                video = await page.query_selector(SELECTORS["video_element"])
                if video:
                    logger.info("Video loaded after retry")
                    return
            except Exception as exc:
                logger.debug("Video retry failed: %s", exc)
                return

    async def capture_snapshot(self, camera_id: str) -> bytes | None:
        """Navigate to a camera's live view and capture a screenshot.

        Returns JPEG image bytes or None on failure.  Handles the parked
        state by navigating to the cameras page first if needed.
        """
        if self.state.auth_status != AuthStatus.AUTHENTICATED:
            logger.warning("Cannot capture snapshot: not authenticated")
            return None

        camera = next((c for c in self.state.cameras if c.id == camera_id), None)
        if not camera:
            logger.warning("Camera %s not found", camera_id)
            return None

        page = await self._get_page()

        try:
            # Use unpark_to_camera which handles dashboard→video navigation
            if not await self.unpark_to_camera(camera_id):
                return None

            # Try to screenshot just the video player element
            video_elem = await page.query_selector(SELECTORS["video_element"])
            if video_elem:
                # Give the video a moment to render a frame
                await asyncio.sleep(2)
                screenshot_bytes = await video_elem.screenshot()
                jpeg_bytes = self._to_jpeg(screenshot_bytes)
                self._save_snapshot(camera_id, jpeg_bytes)
                logger.info(
                    "Snapshot captured for camera %s (%d bytes)",
                    camera_id,
                    len(jpeg_bytes),
                )
                return jpeg_bytes

            # Fallback: screenshot the main content area
            logger.debug("No video element found, taking page screenshot")
            screenshot = await page.screenshot()
            jpeg = self._to_jpeg(screenshot)
            self._save_snapshot(camera_id, jpeg)
            return jpeg

        except Exception:
            logger.exception("Failed to capture snapshot for camera %s", camera_id)

    def _to_jpeg(self, png_bytes: bytes) -> bytes:
        """Convert PNG screenshot to JPEG with configured quality."""
        img = Image.open(io.BytesIO(png_bytes))
        if img.mode == "RGBA":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._jpeg_quality)
        return buf.getvalue()

    def _save_snapshot(self, camera_id: str, jpeg_bytes: bytes) -> None:
        """Save a snapshot to disk with metadata."""
        cam_dir = self._snapshot_dir / camera_id
        cam_dir.mkdir(parents=True, exist_ok=True)
        (cam_dir / "latest.jpg").write_bytes(jpeg_bytes)

        # Extract dimensions and save metadata
        try:
            img = Image.open(io.BytesIO(jpeg_bytes))
            meta = SnapshotMetadata(
                camera_id=camera_id,
                timestamp=time.time(),
                width=img.width,
                height=img.height,
            )
            self.state.snapshot_metadata[camera_id] = meta
            self.state.last_snapshot_time = meta.timestamp
            # Persist metadata to disk
            meta_dict = {
                "camera_id": meta.camera_id,
                "timestamp": meta.timestamp,
                "width": meta.width,
                "height": meta.height,
            }
            (cam_dir / "metadata.json").write_text(json.dumps(meta_dict))
        except Exception:
            logger.debug("Failed to save snapshot metadata for %s", camera_id)

    def get_latest_snapshot(self, camera_id: str) -> bytes | None:
        """Get the latest cached snapshot from disk."""
        path = self._snapshot_dir / camera_id / "latest.jpg"
        if path.exists():
            return path.read_bytes()
        return None

    def get_snapshot_metadata(self, camera_id: str) -> dict | None:
        """Get metadata for the latest snapshot."""
        # Try in-memory first
        if camera_id in self.state.snapshot_metadata:
            meta = self.state.snapshot_metadata[camera_id]
            return {
                "camera_id": meta.camera_id,
                "timestamp": meta.timestamp,
                "width": meta.width,
                "height": meta.height,
            }
        # Try disk
        meta_path = self._snapshot_dir / camera_id / "metadata.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text())
            except Exception:
                pass
        return None

    # ---- Browser Parking ----

    async def park(self) -> None:
        """Navigate away from the video page to stop video streaming.

        Uses SPA nav link clicks — NEVER page.goto() which destroys the
        Ember SPA and causes 'Page Not Found'.  Must be called with
        self._lock held.
        """
        try:
            page = await self._get_page()
            current_url = page.url

            # If already not on video page, nothing to do
            if "/video" not in current_url:
                self.state.parked = True
                logger.info("Browser already off video page, marked as parked")
                return

            # Click the Home nav link to leave the video page.
            # alarm.com's Ember SPA uses data-testid on nav links.
            home_link = await page.query_selector(
                'a[data-testid="home-link"], '
                'a[data-testid="dashboard-link"], '
                'a[href="#/home"], '
                'a[href="#/dashboard"]'
            )
            if home_link:
                logger.info("Parking: clicking Home nav link")
                await home_link.click()
                await asyncio.sleep(3)
                self.state.parked = True
                logger.info("Browser parked (URL: %s)", page.url[:80])
                return

            # Try broader selectors — look for any nav link that isn't Video
            nav_links = await page.query_selector_all("a[data-testid]")
            for link in nav_links:
                testid = await link.get_attribute("data-testid")
                if testid and "video" not in testid.lower():
                    logger.info("Parking: clicking non-video nav link: %s", testid)
                    await link.click()
                    await asyncio.sleep(3)
                    self.state.parked = True
                    logger.info(
                        "Browser parked via %s (URL: %s)",
                        testid,
                        page.url[:80],
                    )
                    return

            # Last resort: just mark as parked — we'll be on the video page
            # but at least we won't destroy the SPA with page.goto()
            logger.warning("Could not find nav link to park, staying on video page")
            self.state.parked = True
        except Exception:
            logger.exception("Failed to park browser")

    async def unpark_to_camera(self, camera_id: str) -> bool:
        """Navigate from parked state to a camera's live view.

        Uses SPA nav link clicks via _navigate_to_live_view — NEVER
        page.goto() which destroys the Ember SPA.  Must be called with
        self._lock held.
        """
        camera = next((c for c in self.state.cameras if c.id == camera_id), None)
        if not camera:
            logger.warning("Cannot unpark: camera %s not found", camera_id)
            return False

        page = await self._get_page()
        current_url = page.url
        logger.info(
            "Unparking to camera %s from URL: %s",
            camera_id,
            current_url[:80],
        )

        # Check if the SPA is still alive (we should be on an alarm.com
        # /web/ page).  If not, we can't navigate via SPA links.
        if ALARM_BASE_URL not in current_url or _is_login_page(current_url):
            logger.warning(
                "SPA not available (URL: %s), session may be expired",
                current_url[:80],
            )
            self.state.auth_status = AuthStatus.LOGGED_OUT
            return False

        self.state.parked = False
        # _navigate_to_live_view handles clicking the Video nav link
        # and waiting for the video player to load
        return await self._navigate_to_live_view(page, camera)

    async def burst_capture(
        self, camera_id: str, duration_seconds: int
    ) -> bytes | None:
        """Capture frames at ~1fps for *duration_seconds*.

        Must be called with self._lock held.  Returns the first captured
        frame, or None on failure.  Subsequent frames are saved to the
        snapshot cache.  If ``last_manual_request_time`` is updated during
        the burst (new manual request), the timer resets so the burst
        continues for another full duration.
        """
        if not await self.unpark_to_camera(camera_id):
            return None

        page = await self._get_page()
        video_elem = await page.query_selector(SELECTORS["video_element"])
        if not video_elem:
            try:
                video_elem = await page.wait_for_selector(
                    SELECTORS["video_element"], timeout=15_000
                )
            except Exception:
                logger.warning("No video element found for burst capture")
                return None

        # Give the video a moment to start rendering
        await asyncio.sleep(2)

        first_frame: bytes | None = None
        burst_start = time.time()
        deadline = burst_start + duration_seconds
        frame_count = 0

        while time.time() < deadline:
            # Check if a stream started (yield to it)
            if self.state.active_stream_camera:
                logger.info("Burst yielding to active stream")
                break

            try:
                screenshot = await video_elem.screenshot()
                jpeg = self._to_jpeg(screenshot)
                self._save_snapshot(camera_id, jpeg)
                frame_count += 1
                if first_frame is None:
                    first_frame = jpeg

                # If a new manual request came in, extend the deadline
                manual_age = time.time() - self.state.last_manual_request_time
                if manual_age < 2.0:
                    new_deadline = (
                        self.state.last_manual_request_time + duration_seconds
                    )
                    if new_deadline > deadline:
                        deadline = new_deadline
                        logger.debug("Burst deadline extended by new manual request")

            except Exception:
                # Video element may have gone away - try to re-acquire
                try:
                    video_elem = await page.wait_for_selector(
                        SELECTORS["video_element"], timeout=5_000
                    )
                except Exception:
                    logger.warning("Lost video element during burst")
                    break

            await asyncio.sleep(1)

        elapsed = time.time() - burst_start
        logger.info(
            "Burst capture ended for %s: %d frames in %.1fs",
            camera_id,
            frame_count,
            elapsed,
        )
        return first_frame

    # ---- Live Stream (Screenshot Loop) ----

    async def start_stream(self, camera_id: str, fps: float = 1.0):
        """Start a screenshot-loop stream. Yields JPEG frames as an async iterator.

        Only one stream can be active at a time. Starting a new stream
        will stop any existing one. The stream auto-populates the latest
        snapshot cache for the camera.
        """
        if self.state.active_stream_camera:
            logger.info(
                "Stopping existing stream for %s", self.state.active_stream_camera
            )
            await self.stop_stream()
            # Brief wait for the previous generator to wind down
            await asyncio.sleep(0.5)

        self.state.active_stream_camera = camera_id
        self.state.stream_started_at = time.time()
        self.state.stream_fps = fps
        self.state.stream_frame_count = 0
        self.state._stream_stop_event = asyncio.Event()
        interval = 1.0 / fps

        camera = next((c for c in self.state.cameras if c.id == camera_id), None)
        if not camera:
            logger.warning("Camera %s not found for streaming", camera_id)
            self.state.active_stream_camera = None
            return

        page = await self._get_page()

        try:
            logger.info("Starting stream for %s at %.1f fps", camera_id, fps)

            if not await self._navigate_to_live_view(page, camera):
                logger.error("Cannot start stream: failed to navigate to live view")
                self.state.active_stream_camera = None
                return

            video_elem = await page.query_selector(SELECTORS["video_element"])
            if not video_elem:
                video_elem = await page.wait_for_selector(
                    SELECTORS["video_element"], timeout=15_000
                )
            # Give the video a moment to start rendering
            await asyncio.sleep(2)

            while not self.state._stream_stop_event.is_set():
                try:
                    screenshot = await video_elem.screenshot()
                    jpeg = self._to_jpeg(screenshot)
                    self._save_snapshot(camera_id, jpeg)
                    self.state.stream_frame_count += 1
                    yield jpeg
                except Exception:
                    # Video element may have disappeared - try to re-find it
                    try:
                        video_elem = await page.wait_for_selector(
                            SELECTORS["video_element"], timeout=5_000
                        )
                        screenshot = await video_elem.screenshot()
                        jpeg = self._to_jpeg(screenshot)
                        self.state.stream_frame_count += 1
                        yield jpeg
                    except Exception:
                        logger.warning("Lost video element during stream")
                        break

                await asyncio.sleep(interval)

        except Exception:
            logger.exception("Stream failed for camera %s", camera_id)
        finally:
            duration = time.time() - self.state.stream_started_at
            frames = self.state.stream_frame_count
            logger.info(
                "Stream ended for %s: %d frames in %.1fs (%.2f actual fps)",
                camera_id,
                frames,
                duration,
                frames / duration if duration > 0 else 0,
            )
            self.state.active_stream_camera = None
            self.state.stream_started_at = 0.0
            self.state.stream_fps = 0.0
            self.state.stream_frame_count = 0
            self.state._stream_stop_event = None

    async def stop_stream(self) -> None:
        """Stop the active stream."""
        if self.state._stream_stop_event:
            self.state._stream_stop_event.set()
            logger.info("Stream stop requested")

    def get_stream_status(self) -> dict:
        """Get current stream status."""
        result = {
            "active": self.state.active_stream_camera is not None,
            "camera_id": self.state.active_stream_camera,
            "fps": self.state.stream_fps,
            "frame_count": self.state.stream_frame_count,
        }
        if self.state.stream_started_at:
            result["started_at"] = self.state.stream_started_at
            result["duration_seconds"] = int(time.time() - self.state.stream_started_at)
        return result

    # ---- Session Check & Health ----

    async def check_session(self) -> bool:
        """Quick check if the session is still valid.

        Does NOT navigate — uses the current page state and cookies to
        determine if the session is alive.  Navigating (page.goto) would
        destroy the Ember SPA context.

        When parked on the dashboard, checks that the page is still on
        an alarm.com /web/ path (not redirected to login).
        """
        if not self._context:
            return False

        try:
            page = await self._get_page()
            url = page.url

            # If we're on the login page, session is clearly expired
            if _is_login_page(url):
                self.state.auth_status = AuthStatus.LOGGED_OUT
                self.state.auth_message = "Session expired"
                return False

            # If we're on a /web/ path (not auth flow), session is likely valid
            from urllib.parse import urlparse

            path = urlparse(url).path.lower()
            auth_flow_prefixes = (
                "/web/system-install/",
                "/web/two-factor",
                "/web/login",
            )
            if path.startswith("/web/") and not any(
                path.startswith(p) for p in auth_flow_prefixes
            ):
                logger.debug(
                    "Session check: on /web/ path %s, session appears valid", path
                )
                return True

            # Use a lightweight fetch to check auth without navigating
            try:
                is_authed = await page.evaluate("""
                    async () => {
                        try {
                            const resp = await fetch('/web/api/appload', {
                                credentials: 'same-origin',
                                headers: { 'Accept': 'application/json' }
                            });
                            // 401/403 = expired, 200 = valid, redirect to login = expired
                            if (resp.redirected && resp.url.includes('/login')) return false;
                            return resp.ok;
                        } catch (e) {
                            return false;
                        }
                    }
                """)
                if not is_authed:
                    self.state.auth_status = AuthStatus.LOGGED_OUT
                    self.state.auth_message = "Session expired"
                    return False
            except Exception:
                # If evaluate fails, page might be in a bad state
                logger.debug("Session check evaluate failed, assuming valid")
                pass
            self.state.auth_status = AuthStatus.AUTHENTICATED
            return True
        except Exception:
            return False

    async def is_browser_responsive(self) -> bool:
        """Check if the browser is still responsive."""
        if not self._context:
            return False
        try:
            page = await self._get_page()
            await page.evaluate("() => 1 + 1", timeout=30_000)
            return True
        except Exception:
            return False

    async def clear_browser_profile(self) -> dict:
        """Clear the browser profile (cookies, localStorage, etc.).

        Requires a browser restart to take effect.
        """
        import shutil

        profile_dir = self._browser_state_dir / "chromium_profile"
        logger.info("Clearing browser profile at %s", profile_dir)

        try:
            # Stop the browser first
            await self.stop()

            # Remove the profile directory
            if profile_dir.exists():
                shutil.rmtree(profile_dir)
                logger.info("Browser profile cleared")

            # Reset auth state
            self.state.auth_status = AuthStatus.LOGGED_OUT
            self.state.auth_message = "Browser profile cleared. Please log in again."
            self.state.challenge_screenshot = None
            self.state.cameras = []
            self.state.cameras_discovered_at = 0.0
            self.state.last_auth_time = 0.0

            # Restart with fresh profile
            await self.start()

            return {
                "success": True,
                "message": "Browser profile cleared and browser restarted",
            }
        except Exception as e:
            logger.exception("Failed to clear browser profile")
            return {"success": False, "error": str(e)}

    def get_health(self) -> dict:
        """Get comprehensive health status."""
        now = time.time()
        return {
            "browser_alive": self.state.browser_alive,
            "session_valid": self.state.auth_status == AuthStatus.AUTHENTICATED,
            "auth_status": self.state.auth_status.value,
            "last_auth_time": self.state.last_auth_time,
            "last_snapshot_time": self.state.last_snapshot_time,
            "uptime_seconds": int(now - self.state.startup_time),
            "cameras_count": len(self.state.cameras),
            "cameras_cache_age": (
                int(now - self.state.cameras_discovered_at)
                if self.state.cameras_discovered_at
                else None
            ),
            "active_stream": self.state.active_stream_camera,
            "parked": self.state.parked,
        }
