# Alarm.com Cameras

## Overview

This add-on provides Alarm.com camera support for Home Assistant using a headless
browser approach. It logs into the Alarm.com web portal, discovers your cameras,
and captures screenshots of the video feeds.

**Features:**
- Periodic snapshots (default: every 10 minutes)
- On-demand snapshot capture
- Live view at ~1fps via MJPEG stream
- Built-in web UI for credential management and CAPTCHA/2FA solving
- Persistent browser sessions (survives restarts)
- Trusted device support (auto-trusts after 2FA)
- Auto-install companion integration for Home Assistant camera entities

## Getting Started

### 1. Log in to Alarm.com

1. Open **Alarm.com Cameras** from the HA sidebar
2. Enter your Alarm.com email and password in the **Setup** tab
3. Click **Save & Login**
4. If a CAPTCHA appears, solve it in the web UI
5. If 2FA is required, enter the code sent to your phone/email
6. The add-on will automatically trust the device for future logins

### 2. Add the integration

The add-on automatically installs a companion HA integration on first startup.

1. **Restart Home Assistant** once (needed to load the new integration)
2. Go to **Settings > Devices & Services > Add Integration**
3. Search for **"Alarm.com Cameras"**
4. The add-on URL is auto-detected — just click **Submit**

Your cameras will appear as standard HA camera entities that you can add to any dashboard.

### 3. Add to a dashboard

- Edit a dashboard and click **Add Card**
- Choose **Picture Entity** or **Picture Glance**
- Select your `camera.alarm_com_*` entity

## Configuration

| Setting | Default | Description |
|---|---|---|
| `snapshot_interval_minutes` | 10 | Minutes between automatic snapshots |
| `stream_fps` | 1 | Live view frames per second (0.5, 1, or 2) |
| `stream_timeout_minutes` | 5 | Auto-stop live view after this many minutes |
| `jpeg_quality` | 80 | JPEG compression quality (10–100) |
| `trusted_device_name` | HA Alarm.com Cameras | Name shown in Alarm.com's trusted devices list |
| `log_level` | info | Logging verbosity (debug, info, warning, error) |

## Web UI Tabs

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
Alarm.com may show CAPTCHAs during login. Solve them through the web UI. The add-on preserves the session, so CAPTCHAs should be rare after initial setup.

### 2FA code rejected
Click **Resend Code** to get a fresh code. Enter it promptly — codes expire quickly.

### High CPU or memory usage
The headless browser is resource-intensive. To reduce usage:
- Lower `stream_fps` to `0.5`
- Increase `snapshot_interval_minutes`
- Stop live view when not needed (auto-stops after `stream_timeout_minutes`)

## How It Works

This add-on uses Playwright with headless Chromium to render Alarm.com's
camera pages and capture screenshots. This avoids reverse-engineering
Alarm.com's undocumented WebRTC/Janus streaming protocol.

```
Alarm.com Website  <──  Headless Chromium (Playwright)
                              │
                        Screenshot capture
                              │
                        REST API (aiohttp)
                              │
              HA Camera Entities  /  Web UI
```
