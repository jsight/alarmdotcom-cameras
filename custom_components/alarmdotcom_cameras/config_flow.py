"""Config flow for Alarm.com Cameras integration."""

import logging
import os

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_ADDON_URL, DEFAULT_ADDON_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

ADDON_SLUG = "alarmdotcom_cameras"


async def _test_addon_url(url: str) -> bool:
    """Test if the add-on is reachable at the given URL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{url}/api/health",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("status") == "ok"
    except Exception:
        pass
    return False


async def _discover_addon_url() -> str | None:
    """Use the Supervisor API to discover the add-on's internal URL.

    The Supervisor provides addon info including the internal IP address.
    This works regardless of the repo hash in the addon's hostname.
    """
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        return None

    headers = {"Authorization": f"Bearer {supervisor_token}"}

    try:
        async with aiohttp.ClientSession() as session:
            # List all addons to find ours by slug suffix
            async with session.get(
                "http://supervisor/addons",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                addons = data.get("data", {}).get("addons", [])

            # Find our addon — the full slug includes the repo prefix
            # (e.g., "a1b2c3d4_alarmdotcom_cameras" or "local_alarmdotcom_cameras")
            our_addon = None
            for addon in addons:
                slug = addon.get("slug", "")
                if slug.endswith(f"_{ADDON_SLUG}") or slug == ADDON_SLUG:
                    our_addon = slug
                    break

            if not our_addon:
                _LOGGER.debug("Add-on not found in Supervisor addon list")
                return None

            # Get detailed info including the internal IP
            async with session.get(
                f"http://supervisor/addons/{our_addon}/info",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                info = await resp.json()
                addon_data = info.get("data", {})
                ip = addon_data.get("ip_address")
                # The ingress port from config.yaml
                port = 8099

                if ip:
                    url = f"http://{ip}:{port}"
                    _LOGGER.info(
                        "Discovered add-on via Supervisor API: %s (slug: %s)",
                        url, our_addon,
                    )
                    return url

                # Fallback: try the hostname (slug with underscores → hyphens)
                hostname = our_addon.replace("_", "-")
                url = f"http://{hostname}:{port}"
                _LOGGER.info(
                    "Using add-on hostname: %s (slug: %s)", url, our_addon,
                )
                return url

    except Exception as exc:
        _LOGGER.debug("Supervisor API discovery failed: %s", exc)

    return None


class AlarmDotComCamerasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Alarm.com Cameras."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            addon_url = user_input[CONF_ADDON_URL].rstrip("/")

            if await _test_addon_url(addon_url):
                await self.async_set_unique_id(addon_url)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Alarm.com Cameras",
                    data={"addon_url": addon_url},
                )
            errors["base"] = "cannot_connect"

        # Auto-detect the add-on URL via Supervisor API, then fall back
        # to probing well-known addresses.
        suggested_url = DEFAULT_ADDON_URL

        supervisor_url = await _discover_addon_url()
        if supervisor_url and await _test_addon_url(supervisor_url):
            suggested_url = supervisor_url
        elif await _test_addon_url(DEFAULT_ADDON_URL):
            suggested_url = DEFAULT_ADDON_URL

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDON_URL, default=suggested_url): str,
                }
            ),
            errors=errors,
        )
