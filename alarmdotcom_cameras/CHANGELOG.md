# Changelog

## 0.1.13

### Fixed - Docker/container auth flow now works reliably

The auth flow (login, 2FA, trust device) was failing inside Docker containers
due to bot detection by alarm.com. This release fixes the root causes:

- **Upgrade Playwright/Chromium** from v131 to v145 — the year-old browser
  version was a major bot-detection signal
- **Dynamic user agent detection** — automatically matches the actual Chromium
  version instead of hardcoding a stale version string
- **Comprehensive anti-fingerprinting** — spoof WebGL renderer (hide
  SwiftShader), fix `window.outerWidth/outerHeight`, screen properties,
  and fully delete `navigator.webdriver` property
- **Set timezone** to America/New_York in browser context (container defaults
  to UTC which is suspicious)
- **Browser lock serialization** — `login()`, `solve_challenge()`, and
  `resend_2fa_code()` now acquire the browser lock, preventing concurrent
  page access from debug endpoints or health checks that destroyed execution
  contexts during navigation
- **Debug endpoints skip page access** when browser lock is held, avoiding
  interference with auth flow
- **Health monitor** skips checks during active auth flows and when browser
  lock is held
- **Stale auth state detection** — if alarm.com expires the 2FA session,
  the UI now detects this and shows the correct state
- **Fingerprint diagnostic logging** on startup for easier debugging of
  bot-detection issues
- **Video player retry handler** — automatically clicks Retry when the
  video player shows an error (e.g. CSP-blocked blob: workers)

## 0.1.12

- Debug tab: split screenshot from JSON, add no-cache to all static files
- Debug tab: robust error handling, raw HTML DOM, console/server logs

## 0.1.11

- Add Debug tab with live browser screenshot, URL, and copyable DOM

## 0.1.10

- Fix 2FA submit, auth flow detection, and add step-by-step debug logging

## 0.1.9

- Fix post-2FA auth check, screenshots, and debug logging

## 0.1.8

- Fix 2FA detection: /login substring matched /login-setup/ URL

## 0.1.0

- Initial release
- Add-on scaffolding with S6 overlay, Ingress web UI
- REST API with placeholder endpoints for all camera operations
- Web UI with Setup, Cameras, Settings, and Status tabs
