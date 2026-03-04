"""Headless browser engine for Alarm.com camera access.

Uses Playwright with Chromium to log into alarm.com, discover cameras,
and capture screenshots of live video feeds.
"""

import asyncio
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
    "twofa_element": '#two-factor-authentication-input-field, input[name*="code"], input[id*="twoFactor"], input[id*="code" i], input[placeholder*="code"], input[type="tel"], input[type="number"], input.two-factor-input, input[class*="two-factor"], input[class*="verification"]',
    "twofa_submit": 'button:has-text("Verify"), button.btn-color-primary, button[type="submit"], input[type="submit"]',
    "twofa_resend": '.request-new-code-button, button:has-text("Request a new code"), button:has-text("Resend"), a:has-text("Resend"), a:has-text("Request a new code"), [class*="resend" i]',
    # Trust device page (shown after successful 2FA)
    "trust_device_name_input": 'input[placeholder*="Device Name"], input[placeholder*="device name"], input[class*="device-name"], input[id*="device-name" i]',
    "trust_device_submit": 'button:has-text("Trust Device"), button:has-text("Trust"), button:has-text("Save"), button.btn-color-primary',
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

        # Use persistent context to preserve cookies/localStorage across restarts
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._browser_state_dir / "chromium_profile"),
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-translate",
                "--metrics-recording-only",
                "--no-first-run",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )

        # Remove the webdriver property to avoid detection
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        self.state.browser_alive = True
        logger.info("Browser engine started")

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

    async def _get_page(self) -> Page:
        """Get or create a browser page."""
        if not self._context:
            raise RuntimeError("Browser not started")

        if self._page and not self._page.is_closed():
            return self._page

        self._page = await self._context.new_page()
        return self._page

    async def _close_page(self) -> None:
        """Close the current page to free resources."""
        if self._page and not self._page.is_closed():
            await self._page.close()
        self._page = None

    # ---- Authentication ----

    async def login(self, username: str, password: str) -> AuthStatus:
        """Attempt to log into alarm.com."""
        self.state.auth_status = AuthStatus.LOGGING_IN
        self.state.auth_message = "Logging in..."
        self.state.challenge_screenshot = None

        try:
            page = await self._get_page()
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

            # Check if already logged in (session restored from persistent context)
            if await self._check_logged_in(page):
                self.state.auth_status = AuthStatus.AUTHENTICATED
                self.state.auth_message = "Already authenticated (session restored)"
                self.state.last_auth_time = time.time()
                logger.info("Already authenticated from saved session")
                return AuthStatus.AUTHENTICATED

            # Save a debug screenshot of the login page
            try:
                debug_dir = pathlib.Path(self._data_dir) / "debug"
                debug_dir.mkdir(exist_ok=True)
                await page.screenshot(
                    path=str(debug_dir / "login_page.png"), full_page=True
                )
                logger.debug(
                    "Saved login page screenshot to %s", debug_dir / "login_page.png"
                )
            except Exception:
                pass

            # Check for CAPTCHA before filling in credentials
            if await self._detect_captcha(page):
                return await self._handle_captcha(page)

            # Fill in credentials
            username_input = await page.wait_for_selector(
                SELECTORS["username_input"], timeout=10_000
            )
            await username_input.fill(username)

            password_input = await page.wait_for_selector(
                SELECTORS["password_input"], timeout=5_000
            )
            await password_input.fill(password)

            # Debug: dump the form HTML to help find the right submit button
            try:
                form_html = await page.evaluate("""
                    () => {
                        const form = document.querySelector('form');
                        if (form) return form.outerHTML;
                        // Fallback: find all submit-type inputs/buttons
                        const submits = document.querySelectorAll('input[type="submit"], button[type="submit"], button');
                        return Array.from(submits).map(e =>
                            `<${e.tagName} id="${e.id}" class="${e.className}" type="${e.type}" value="${e.value}" name="${e.name}">`
                        ).join('\\n');
                    }
                """)
                logger.debug("Login page form HTML:\n%s", form_html[:3000])
            except Exception:
                pass

            # Click login
            login_button = await page.wait_for_selector(
                SELECTORS["login_button"], timeout=5_000
            )
            await login_button.click()

            # Wait for navigation
            await page.wait_for_load_state("networkidle", timeout=30_000)

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
            screenshot = await page.screenshot()
            self.state.challenge_screenshot = screenshot
            self.state.auth_status = AuthStatus.ERROR
            self.state.auth_message = "Login failed - unexpected page state"
            logger.warning("Login resulted in unexpected page state")
            return AuthStatus.ERROR

        except Exception as e:
            self.state.auth_status = AuthStatus.ERROR
            self.state.auth_message = f"Login error: {str(e)}"
            logger.exception("Login failed")
            return AuthStatus.ERROR

    async def _check_logged_in(self, page: Page) -> bool:
        """Check if we're on a logged-in page."""
        # Check URL - if we're past /login, we're probably authenticated
        url = page.url
        if "/login" not in url.lower() and ALARM_BASE_URL in url:
            # Double-check by looking for a dashboard/video element
            try:
                await page.wait_for_selector(
                    SELECTORS["logged_in_indicator"], timeout=3_000
                )
                return True
            except Exception:
                # URL changed but no indicator found - still might be logged in
                # if we're on a page that's not the login page
                return "/login" not in url.lower()
        return False

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
                await name_input.click()
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
                await trust_btn.click()
                logger.info("Clicked Trust Device button")
            else:
                logger.warning("Trust Device button not found, trying Skip")
                skip_btn = await page.query_selector(SELECTORS["trust_device_skip"])
                if skip_btn:
                    await skip_btn.click()
                    logger.info("Clicked Skip button on trust page")

            # Wait for the SPA to process and redirect
            pre_url = page.url
            for _ in range(15):
                await asyncio.sleep(1)
                current_url = page.url
                if current_url != pre_url:
                    logger.info(
                        "Trust device redirected: %s -> %s", pre_url, current_url
                    )
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    break
                # Check if button is still processing
                try:
                    still_processing = await page.evaluate("""
                        () => {
                            const btns = document.querySelectorAll('button.btn-color-primary');
                            return Array.from(btns).some(b => b.disabled);
                        }
                    """)
                    if not still_processing:
                        # Button done but URL didn't change — maybe the SPA
                        # transitioned within the same URL; give it a moment
                        await asyncio.sleep(1)
                        break
                except Exception:
                    break

            # After trusting, alarm.com may redirect to dashboard or another page.
            # Navigate explicitly to the main page to verify auth.
            current_url = page.url
            logger.debug("Post-trust URL: %s", current_url)

            # If still on the 2FA flow page, try navigating to the dashboard
            if "two-factor" in current_url or "login-setup" in current_url:
                logger.info("Still on 2FA flow page, navigating to dashboard...")
                await page.goto(
                    f"{ALARM_BASE_URL}/web/system/home",
                    wait_until="networkidle",
                    timeout=15_000,
                )

            # Check if we're now logged in
            final_url = page.url
            if "/login" not in final_url.lower() and ALARM_BASE_URL in final_url:
                self.state.auth_status = AuthStatus.AUTHENTICATED
                self.state.auth_message = "Successfully authenticated (device trusted)"
                self.state.challenge_screenshot = None
                self.state.last_auth_time = time.time()
                logger.info("Device trusted, now authenticated (URL: %s)", final_url)
                return AuthStatus.AUTHENTICATED

            if await self._check_logged_in(page):
                self.state.auth_status = AuthStatus.AUTHENTICATED
                self.state.auth_message = "Successfully authenticated (device trusted)"
                self.state.challenge_screenshot = None
                self.state.last_auth_time = time.time()
                logger.info("Device trusted, now authenticated")
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
                        await submit.click()

            elif self.state.auth_status == AuthStatus.TWO_FA_REQUIRED:
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
                await twofa_input.click()
                await twofa_input.fill("")  # clear any existing value
                await twofa_input.type(solution, delay=50)

                # Brief pause to let Ember process the input
                await asyncio.sleep(0.5)

                # Re-query the submit button fresh (Ember may have re-rendered)
                submit = await page.query_selector(SELECTORS["twofa_submit"])
                if submit:
                    await submit.click()
                    logger.info("Clicked 2FA submit button")

            # Wait for the SPA to process the submission.
            # alarm.com is an Ember.js app so there's no full page navigation;
            # networkidle fires almost instantly.  Instead, wait for either a
            # URL change (success → redirect) or for the submit button to
            # become enabled again (failure → same page).
            await page.wait_for_load_state("networkidle", timeout=30_000)

            # Give the SPA time to process and transition
            pre_url = page.url
            for _ in range(10):
                await asyncio.sleep(1)
                if page.url != pre_url:
                    # URL changed — likely authenticated, wait for it to settle
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    break
                # Check if the submit button is re-enabled (SPA finished processing)
                try:
                    btn_disabled = await page.evaluate("""
                        () => {
                            const btn = document.querySelector('button:has-text("Verify"), button.btn-color-primary');
                            return btn ? btn.classList.contains('is-async') && btn.hasAttribute('disabled') : false;
                        }
                    """)
                    if not btn_disabled:
                        break
                except Exception:
                    break

            logger.debug("Post-2FA URL: %s (was: %s)", page.url, pre_url)

            # Check if we're now logged in
            if await self._check_logged_in(page):
                self.state.auth_status = AuthStatus.AUTHENTICATED
                self.state.auth_message = "Successfully authenticated"
                self.state.challenge_screenshot = None
                logger.info("Challenge solved, now authenticated")
                return AuthStatus.AUTHENTICATED

            # Check for trust device page (shown after successful 2FA)
            if await self._detect_trust_device(page):
                return await self._handle_trust_device(page)

            # Still challenged?
            if await self._detect_captcha(page):
                return await self._handle_captcha(page)
            if await self._detect_2fa(page):
                return await self._handle_2fa(page)

            # Unknown state
            screenshot = await page.screenshot()
            self.state.challenge_screenshot = screenshot
            self.state.auth_status = AuthStatus.ERROR
            self.state.auth_message = "Challenge solution failed"
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

            await resend_elem.click()
            logger.info("Clicked 2FA resend code button")

            # Wait for the SPA to process the resend and re-render
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle", timeout=10_000)

            # Wait for the 2FA input to reappear (Ember re-renders the component)
            try:
                await page.wait_for_selector(SELECTORS["twofa_element"], timeout=5_000)
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

            # Navigate to video page via Ember SPA routing.
            # The nav links use href="#" so we can't click them directly.
            # Instead, click the nav text to trigger Ember's routing.
            video_nav = await page.query_selector('a:has-text("Video")')
            if video_nav:
                await video_nav.click()
                logger.debug("Clicked Video nav link")
                # Wait for the SPA route to transition
                try:
                    await page.wait_for_url("**/video**", timeout=10_000)
                except Exception:
                    logger.debug("URL didn't change to video, waiting for content")
                await page.wait_for_load_state("networkidle", timeout=15_000)
                # Give the Ember app time to render camera cards
                await asyncio.sleep(5)
            else:
                # Fallback: navigate directly
                logger.debug("No Video nav link found, navigating to %s", CAMERAS_URL)
                await page.goto(CAMERAS_URL, wait_until="networkidle", timeout=30_000)
                await asyncio.sleep(5)

            # If we're still not on /video, try direct navigation
            if "/video" not in page.url:
                logger.debug(
                    "Not on video page (URL: %s), navigating directly", page.url
                )
                await page.goto(CAMERAS_URL, wait_until="networkidle", timeout=30_000)
                await asyncio.sleep(5)

            # Debug: save a screenshot and dump the page structure
            try:
                debug_dir = pathlib.Path(self._data_dir) / "debug"
                debug_dir.mkdir(exist_ok=True)
                await page.screenshot(
                    path=str(debug_dir / "cameras_page.png"), full_page=True
                )
                logger.debug("Saved cameras page screenshot")

                page_info = await page.evaluate("""
                    () => {
                        const url = window.location.href;
                        const title = document.title;
                        // Dump the cameras section if it exists
                        const cameraSec = document.querySelector('section.cameras, .cameras');
                        const cameraHtml = cameraSec ? cameraSec.innerHTML.substring(0, 2000) : 'No cameras section found';
                        // Dump all nav links
                        const navLinks = Array.from(document.querySelectorAll('a[href]')).slice(0, 30).map(a =>
                            `  <A href="${a.getAttribute('href')}">${a.textContent.trim().substring(0, 40)}`
                        ).join('\\n');
                        // Look for anything camera/video-related
                        const allElements = document.querySelectorAll(
                            '[class*="camera"], [class*="video"], [data-camera-id], ' +
                            '[class*="device"], [class*="Camera"], [class*="Video"]'
                        );
                        const elemInfo = Array.from(allElements).slice(0, 20).map(e =>
                            `  <${e.tagName} id="${e.id}" class="${e.className.toString().substring(0, 80)}"> children=${e.children.length}`
                        ).join('\\n');
                        return `URL: ${url}\\nTitle: ${title}\\n` +
                               `Nav links:\\n${navLinks}\\n` +
                               `Camera-related elements (${allElements.length}):\\n${elemInfo}\\n` +
                               `Cameras section HTML:\\n${cameraHtml}`;
                    }
                """)
                logger.debug("Cameras page info:\n%s", page_info[:5000])
            except Exception:
                pass

            # Try to extract camera data using multiple strategies.
            # Strategy 1 (best): Scrape from WebRTC players / live view page
            cameras = await self._discover_cameras_from_page(page)

            # Strategy 2: Try the internal API from the browser context
            if not cameras:
                cameras = await self._discover_cameras_via_api(page)

            # Strategy 3: DOM card-based discovery (camera list pages)
            if not cameras:
                camera_elements = await page.query_selector_all(
                    '.video-camera-card, .camera-item, [class*="camera-card"], [data-camera-id]'
                )
                for i, elem in enumerate(camera_elements):
                    cam_id = await elem.get_attribute("data-camera-id") or f"camera_{i}"
                    name_elem = await elem.query_selector(SELECTORS["camera_name"])
                    name = (
                        await name_elem.inner_text() if name_elem else f"Camera {i + 1}"
                    )
                    cameras.append(
                        CameraInfo(
                            id=cam_id.strip(),
                            name=name.strip(),
                            url=page.url,
                        )
                    )
                if cameras:
                    logger.debug("DOM card discovery found %d cameras", len(cameras))

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

        Extracts camera IDs from WebRTC player container IDs
        (e.g. "webrtc-player_106711738-2048-container") and camera names
        from .camera-description elements.
        """
        cameras = []
        try:
            result = await page.evaluate("""
                () => {
                    const cameras = [];

                    // Strategy A: Extract from WebRTC player container IDs
                    // Format: webrtc-player_<cameraId>-container
                    const players = document.querySelectorAll('[id*="webrtc-player"]');
                    players.forEach((el, i) => {
                        const id = el.id || '';
                        // e.g. "webrtc-player_106711738-2048-container"
                        const match = id.match(/webrtc-player_([\\d-]+?)(?:-container)?$/);
                        if (match) {
                            // Find associated camera name
                            const playerParent = el.closest('.video-player, .live-video-player') || el.parentElement;
                            let name = '';
                            if (playerParent) {
                                const nameEl = playerParent.querySelector('.camera-description, .bottom-bar-camera-name, .camera-name');
                                if (nameEl) name = nameEl.textContent.trim();
                            }
                            // Also check siblings/nearby elements
                            if (!name) {
                                const desc = document.querySelector('.camera-description');
                                if (desc) name = desc.textContent.trim();
                            }
                            cameras.push({
                                id: match[1],
                                name: name || 'Camera ' + (i + 1),
                                url: window.location.href
                            });
                        }
                    });

                    // Strategy B: Extract from video-player elements
                    if (cameras.length === 0) {
                        const videoPlayers = document.querySelectorAll('.video-player, .live-video-player');
                        videoPlayers.forEach((el, i) => {
                            // Try to find an ID in data attributes or child elements
                            const container = el.querySelector('[id*="player"]');
                            let camId = '';
                            if (container) {
                                const match = container.id.match(/(\\d{5,})/);
                                if (match) camId = match[1];
                            }
                            const nameEl = el.querySelector('.camera-description, .camera-name');
                            const name = nameEl ? nameEl.textContent.trim() : '';
                            if (camId) {
                                cameras.push({
                                    id: camId,
                                    name: name || 'Camera ' + (i + 1),
                                    url: window.location.href
                                });
                            }
                        });
                    }

                    // Strategy C: Extract camera group/ID from the URL
                    if (cameras.length === 0) {
                        const url = window.location.href;
                        const groupMatch = url.match(/cameraGroupId=(\\d+)/);
                        if (groupMatch) {
                            // There's at least one camera in this group
                            const desc = document.querySelector('.camera-description, .bottom-bar-camera-name');
                            const name = desc ? desc.textContent.trim() : 'Camera';
                            cameras.push({
                                id: 'group_' + groupMatch[1],
                                name: name,
                                url: url
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
                    logger.info("Page scrape found %d camera(s)", len(cameras))

        except Exception:
            logger.exception("Page-based camera discovery failed")

        return cameras

    # ---- Snapshot Capture ----

    async def _navigate_to_live_view(self, page: Page, camera: CameraInfo) -> bool:
        """Navigate to a camera's live view, handling SPA routing.

        Returns True if the live view is showing video content.
        """
        current_url = page.url

        # If we're already on the live view page with video playing, stay here
        if "/video" in current_url:
            video = await page.query_selector(SELECTORS["video_element"])
            if video:
                logger.debug("Already on video page with player visible")
                return True

        # If camera has a stored URL (the live view page), navigate there
        if camera.url and "/video" in camera.url:
            logger.debug("Navigating to camera URL: %s", camera.url)
            await page.goto(camera.url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(5)
            video = await page.query_selector(SELECTORS["video_element"])
            if video:
                return True

        # Otherwise, navigate via SPA - click Video in the nav
        video_nav = await page.query_selector('a:has-text("Video")')
        if video_nav:
            await video_nav.click()
            try:
                await page.wait_for_url("**/video**", timeout=10_000)
            except Exception:
                pass
            await page.wait_for_load_state("networkidle", timeout=15_000)
            await asyncio.sleep(5)
            video = await page.query_selector(SELECTORS["video_element"])
            if video:
                return True

        logger.warning("Could not navigate to live view for camera %s", camera.id)
        return False

    async def capture_snapshot(self, camera_id: str) -> bytes | None:
        """Navigate to a camera's live view and capture a screenshot.

        Returns JPEG image bytes or None on failure.
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
            if not await self._navigate_to_live_view(page, camera):
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
        """Quick check if the session is still valid."""
        if not self._context:
            return False

        try:
            page = await self._get_page()
            await page.goto(f"{ALARM_BASE_URL}/web/system/home", timeout=15_000)
            url = page.url
            if "/login" in url.lower():
                self.state.auth_status = AuthStatus.LOGGED_OUT
                self.state.auth_message = "Session expired"
                return False
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
            await page.evaluate("() => 1 + 1", timeout=5_000)
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
        }
