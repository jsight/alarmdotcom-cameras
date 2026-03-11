"""Config flow for Alarm.com Cameras integration."""

import logging

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_ADDON_URL, DEFAULT_ADDON_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

ADDON_SLUG_SUFFIX = "alarmdotcom_cameras"


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


def _discover_addon_slug(hass) -> str | None:
    """Find the full addon slug using HA's cached addon info."""
    try:
        from homeassistant.components.hassio import get_addons_info

        addons = get_addons_info(hass)
        if not addons:
            return None

        for slug in addons:
            if slug.endswith(ADDON_SLUG_SUFFIX) or slug == ADDON_SLUG_SUFFIX:
                return slug
    except Exception as exc:
        _LOGGER.debug("Could not query hassio for addon list: %s", exc)
    return None


async def _discover_addon_url(hass) -> str | None:
    """Discover the addon's internal URL via the Supervisor API."""
    slug = _discover_addon_slug(hass)
    if not slug:
        _LOGGER.debug("Addon slug not found in hassio addon list")
        return None

    try:
        from homeassistant.components.hassio import async_get_addon_info

        info = await async_get_addon_info(hass, slug)
        ip = info.get("ip_address")
        if ip:
            url = f"http://{ip}:8099"
            _LOGGER.info("Discovered addon URL: %s (slug: %s)", url, slug)
            return url

        # Fallback: hostname from slug
        hostname = slug.replace("_", "-")
        url = f"http://{hostname}:8099"
        _LOGGER.info("Using addon hostname: %s (slug: %s)", url, slug)
        return url
    except Exception as exc:
        _LOGGER.debug("Failed to get addon info for %s: %s", slug, exc)
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

        # Auto-detect the add-on URL via HA's hassio component,
        # then fall back to probing well-known addresses.
        suggested_url = DEFAULT_ADDON_URL

        supervisor_url = await _discover_addon_url(self.hass)
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
