"""Config flow for Alarm.com Cameras integration."""

import logging

import aiohttp
import voluptuous as vol

from homeassistant.components.hassio import is_hassio
from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_ADDON_URL, DEFAULT_ADDON_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

# When running as a HA add-on, the Supervisor provides a hostname alias
ADDON_HOSTNAME_URL = "http://local-alarmdotcom-cameras:8099"


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


class AlarmDotComCamerasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Alarm.com Cameras."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> FlowResult:
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

        # Auto-detect the add-on URL
        suggested_url = DEFAULT_ADDON_URL
        if is_hassio(self.hass):
            # Try the Supervisor hostname alias first (works for HA OS / Supervised)
            if await _test_addon_url(ADDON_HOSTNAME_URL):
                suggested_url = ADDON_HOSTNAME_URL
            elif await _test_addon_url(DEFAULT_ADDON_URL):
                suggested_url = DEFAULT_ADDON_URL

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_ADDON_URL, default=suggested_url): str,
            }),
            errors=errors,
        )
