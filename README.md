# Alarm.com Cameras for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/jsight/alarmdotcom-cameras?style=flat-square)](https://github.com/jsight/alarmdotcom-cameras/releases)
[![License](https://img.shields.io/github/license/jsight/alarmdotcom-cameras?style=flat-square)](LICENSE)
[![GitHub Actions](https://img.shields.io/github/actions/workflow/status/jsight/alarmdotcom-cameras/build.yaml?style=flat-square&label=build)](https://github.com/jsight/alarmdotcom-cameras/actions)

A Home Assistant add-on that brings **Alarm.com camera support** to your smart home. View snapshots and live video from your Alarm.com cameras directly in Home Assistant dashboards.

## How It Works

Alarm.com has no public API for camera access. This add-on runs a **headless Chromium browser** (via Playwright) that logs into your Alarm.com account, navigates to the camera pages, and captures screenshots of the live video feeds. It's the same approach you'd use viewing cameras in your own browser — just automated.

```
Alarm.com Website  <──  Headless Chromium (Playwright)
                              │
                        Screenshot capture
                              │
                        REST API (aiohttp)
                              │
              HA Camera Entities  /  Web UI
```

### Features

- **Periodic snapshots** — automatically captures camera images (default: every 10 minutes)
- **On-demand snapshots** — trigger a fresh capture anytime
- **Live view** — ~1fps MJPEG stream via screenshot loop
- **Built-in web UI** — manage credentials, solve CAPTCHAs, handle 2FA challenges
- **Persistent sessions** — browser profile survives restarts (no re-login needed)
- **Trusted device support** — automatically trusts the browser after 2FA
- **Auto-install companion integration** — camera entities appear in HA automatically
- **Session health monitoring** — auto-recovers from expired sessions

## Installation

### Step 1: Add the Repository

In Home Assistant, go to **Settings > Add-ons > Add-on Store**, click the three-dot menu (**⋮**), select **Repositories**, and add:

```
https://github.com/jsight/alarmdotcom-cameras
```

### Step 2: Install & Start

Find **"Alarm.com Cameras"** in the add-on store and click **Install**. Once installed, click **Start**.

### Step 3: Log In

1. Open **Alarm.com Cameras** from the HA sidebar
2. Enter your Alarm.com email and password in the **Setup** tab
3. Click **Save & Login**
4. If a CAPTCHA appears, solve it in the web UI
5. If 2FA is required, enter the code sent to your phone/email
6. The add-on will automatically trust the device for future logins

### Step 4: Add the Integration

The add-on automatically installs a companion integration on first startup. After the first install:

1. **Restart Home Assistant** (needed once to load the new integration)
2. Go to **Settings > Devices & Services > Add Integration**
3. Search for **"Alarm.com Cameras"**
4. The add-on URL is auto-detected — just click **Submit**

Your cameras will now appear as standard HA camera entities that you can add to any dashboard.

### Step 5: Add to Dashboard

- Edit any dashboard and click **Add Card**
- Choose **Picture Entity** or **Picture Glance**
- Select your `camera.alarm_com_*` entity
- The card shows the latest snapshot and auto-refreshes

## Configuration

Settings are configured in the add-on's **Configuration** tab:

| Setting | Default | Description |
|---|---|---|
| `snapshot_interval_minutes` | `10` | Minutes between automatic snapshots |
| `stream_fps` | `1` | Live view frames per second (0.5, 1, or 2) |
| `stream_timeout_minutes` | `5` | Auto-stop live view after this many minutes |
| `jpeg_quality` | `80` | JPEG compression quality (10–100) |
| `trusted_device_name` | `HA Alarm.com Cameras` | Name shown in Alarm.com's trusted devices list |
| `log_level` | `info` | Logging verbosity (debug, info, warning, error) |

## Web UI

The add-on includes a built-in web interface accessible from the HA sidebar with four tabs:

| Tab | Purpose |
|---|---|
| **Setup** | Enter credentials, solve CAPTCHAs, handle 2FA challenges |
| **Cameras** | View discovered cameras, capture snapshots, start live view |
| **Settings** | View current configuration, clear browser profile |
| **Status** | Monitor add-on health, uptime, session status |

## Troubleshooting

### "Not Connected" after restart
The add-on preserves browser cookies across restarts. If your session expired, open the web UI and log in again. Use **Clear Browser Profile** in the Settings tab if you're stuck in a bad state.

### No cameras found
Make sure the auth badge shows **"Connected"** (green). Then click **Refresh** on the Cameras tab. Camera discovery requires an active authenticated session.

### CAPTCHA keeps appearing
Alarm.com may show CAPTCHAs during login. Solve them through the web UI. The add-on preserves the session afterward, so CAPTCHAs should be rare after initial setup.

### 2FA code rejected
If your code is rejected, click **Resend Code** to get a fresh one. Make sure you enter it promptly — codes expire quickly.

### Live view is slow
Live view captures screenshots at the configured FPS (default 1fps). This is a deliberate trade-off — real-time streaming would require reverse-engineering Alarm.com's WebRTC protocol. You can increase to 2fps at the cost of higher CPU usage.

### High CPU or memory usage
The headless Chromium browser is resource-intensive. The add-on minimizes idle usage by reusing a single browser page, but active streaming will use more resources. Consider:
- Reducing `stream_fps` to `0.5`
- Increasing `snapshot_interval_minutes`
- Stopping live view when not needed (it auto-stops after `stream_timeout_minutes`)

## Architecture

### Add-on (`alarmdotcom_cameras/`)

The Docker container runs:
- **Playwright + Chromium** — headless browser for Alarm.com interaction
- **aiohttp server** — REST API on port 8099 (via Ingress)
- **S6 Overlay** — process supervision

Key modules:
- `browser.py` — Playwright engine: login, CAPTCHA/2FA handling, camera discovery, snapshot capture, MJPEG streaming
- `server.py` — HTTP server with background tasks (periodic snapshots, session health monitoring)
- `routes.py` — 17 REST API endpoints
- `credentials.py` — Fernet-encrypted credential storage
- `static/` — Single-page web UI (HTML/JS/CSS)

### Companion Integration (`custom_components/alarmdotcom_cameras/`)

A standard HA integration that:
- Connects to the add-on's REST API
- Creates `camera` entities for each discovered camera
- Polls for snapshots via `async_camera_image()`
- Periodically re-discovers new cameras (every 5 minutes)

The integration is **automatically installed** by the add-on on first startup.

## Requirements

- Home Assistant OS or Supervised installation
- An Alarm.com account with camera access
- ~512MB RAM for the headless browser
- amd64 or aarch64 architecture

## License

[MIT](LICENSE)
