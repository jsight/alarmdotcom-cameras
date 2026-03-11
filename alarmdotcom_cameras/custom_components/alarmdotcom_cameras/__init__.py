"""Alarm.com Cameras integration for Home Assistant.

Connects to the Alarm.com Cameras add-on and creates camera entities
for each discovered camera.
"""

import logging
import time

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CAMERA, Platform.SENSOR]

ADDON_SLUG_SUFFIX = "alarmdotcom_cameras"

# Re-resolve the addon URL at most once per minute
_RESOLVE_COOLDOWN = 60


def _discover_addon_slug(hass: HomeAssistant) -> str | None:
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


async def _discover_addon_url(hass: HomeAssistant) -> str | None:
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


class AddonUrlResolver:
    """Resolves and caches the addon's internal URL.

    Uses the configured URL by default but falls back to the Supervisor
    API to re-discover the addon's IP if the configured URL stops working
    (e.g., after a reboot when the addon gets a new IP).
    """

    def __init__(
        self, hass: HomeAssistant, configured_url: str, session: aiohttp.ClientSession
    ) -> None:
        self._hass = hass
        self._configured_url = configured_url
        self._current_url = configured_url
        self._session = session
        self._last_resolve_attempt: float = 0

    @property
    def url(self) -> str:
        return self._current_url

    async def resolve(self) -> str:
        """Re-discover the addon URL via Supervisor API if needed."""
        now = time.monotonic()
        if now - self._last_resolve_attempt < _RESOLVE_COOLDOWN:
            return self._current_url

        self._last_resolve_attempt = now

        # First check if the current URL still works
        if await self._test_url(self._current_url):
            return self._current_url

        # Try the configured URL if different from current
        if self._current_url != self._configured_url:
            if await self._test_url(self._configured_url):
                self._current_url = self._configured_url
                _LOGGER.info("Reverted to configured addon URL: %s", self._current_url)
                return self._current_url

        # Try Supervisor API discovery
        new_url = await _discover_addon_url(self._hass)
        if new_url and await self._test_url(new_url):
            self._current_url = new_url
            _LOGGER.info("Re-discovered addon URL via Supervisor: %s", self._current_url)
            return self._current_url

        # Nothing worked, keep current
        _LOGGER.warning("Addon unreachable at %s", self._current_url)
        return self._current_url

    async def _test_url(self, url: str) -> bool:
        try:
            async with self._session.get(
                f"{url}/api/health",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("status") == "ok"
        except Exception:
            pass
        return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Alarm.com Cameras from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    resolver = AddonUrlResolver(hass, entry.data["addon_url"], session)

    # Resolve the URL now to ensure we have a working one at startup
    await resolver.resolve()

    hass.data[DOMAIN][entry.entry_id] = {
        "addon_url": resolver.url,
        "resolver": resolver,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
