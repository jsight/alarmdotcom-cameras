# Changelog

## 0.1.24

### Added - Browser parking to reduce alarm.com load

- **Browser parking** — the headless browser now navigates to the
  alarm.com dashboard when not actively capturing, instead of sitting
  on the video page 24/7.  This significantly reduces load on
  alarm.com's video infrastructure.
- **Manual burst capture (60s)** — when a snapshot is triggered via the
  HA service call or refresh button, the browser captures at ~1fps for
  60 seconds then parks back on the dashboard.  Additional requests
  during a burst extend the timer.
- **Periodic burst capture (20s)** — every 30 minutes (configurable),
  the browser wakes up, captures snapshots for 20 seconds across all
  cameras, then parks again.
- **Default snapshot interval** changed from 10 to 30 minutes to be
  more considerate of alarm.com's servers.

## 0.1.21

### Added - Snapshot refresh button on camera card

- **ON_OFF feature flag** — the camera entity now advertises turn-on/off
  support, which adds a refresh button to the HA camera card that
  triggers an on-demand snapshot capture

## 0.1.20

### Fixed - Camera entities not appearing

- **Fix camera entity creation** — the camera platform setup was blocking
  for up to 60s with a retry loop, causing HA to cancel the setup
  coroutine before periodic discovery was registered
- **Fix deprecated HA timer API** — use properly imported
  `async_track_time_interval` instead of deprecated `hass.helpers.event`
  proxy which silently failed in modern HA
- **30-second discovery polling** — cameras are now discovered via a
  non-blocking 30s polling interval, so entities appear quickly even if
  the addon isn't ready at startup
- **Ruff formatting** — all Python files now pass `ruff format --check`

## 0.1.19

### Added - Diagnostic entities and improved URL discovery

- **Diagnostic sensor entities** — the companion integration now creates
  diagnostic entities: Auth Status, Cameras Discovered, Addon Version,
  Addon Uptime, and Last Snapshot time
- **Improved addon URL discovery** — both config flow and runtime URL
  resolver now use HA's built-in `get_addons_info`/`async_get_addon_info`
  instead of raw HTTP calls to the Supervisor API

## 0.1.18

### Added - Camera snapshots and capture service

- **Auto-capture on first view** — if no cached snapshot exists, the camera
  entity automatically triggers an on-demand capture so it always shows an
  image when possible
- **`alarmdotcom_cameras.capture_snapshot` service** — trigger a fresh
  snapshot capture from HA automations, scripts, or the Developer Tools
  service call UI
- **`async_turn_on`** now triggers a snapshot capture (useful from the
  camera card's "turn on" button)

## 0.1.17

### Fixed - HA companion integration

- **Fix "Invalid handler specified"** — update deprecated HA imports
  (`FlowResult` → `ConfigFlowResult`, remove `is_hassio`)
- **Auto-discover addon URL** via Supervisor API — works regardless of
  repo hash in the addon hostname
- **Auto-resolve URL after reboots** — if the addon's IP changes, the
  integration re-discovers it via Supervisor API automatically
- **Bump companion integration manifest** version so the install script
  actually copies updated files

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
