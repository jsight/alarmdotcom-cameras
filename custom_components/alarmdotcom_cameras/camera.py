"""Camera platform for Alarm.com Cameras integration."""

import logging
from datetime import timedelta

import aiohttp
import voluptuous as vol

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

SERVICE_CAPTURE_SNAPSHOT = "capture_snapshot"

# How often to poll the addon for new cameras (seconds).
# Kept short so cameras appear quickly after addon finishes discovery.
_DISCOVERY_INTERVAL = 30


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera entities from the add-on."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    resolver = entry_data["resolver"]
    session = async_get_clientsession(hass)

    # Track all entities we've created so periodic discovery can add new ones
    entities: list[AlarmDotComCamera] = []
    entry_data["entities"] = entities

    # Try to discover cameras now (single, non-blocking attempt)
    cameras = await _fetch_cameras(session, resolver.url)
    if cameras:
        new_entities = _make_entities(entry, resolver, cameras, session, set())
        async_add_entities(new_entities, update_before_add=True)
        entities.extend(new_entities)
        _LOGGER.info("Added %d Alarm.com camera entities", len(new_entities))
    else:
        _LOGGER.warning(
            "No cameras found yet at %s — will keep polling every %ds",
            resolver.url,
            _DISCOVERY_INTERVAL,
        )

    # Register the capture_snapshot service (once per domain)
    if not hass.services.has_service(DOMAIN, SERVICE_CAPTURE_SNAPSHOT):

        async def handle_capture_snapshot(call: ServiceCall) -> None:
            """Handle the capture_snapshot service call."""
            entity_ids = call.data.get("entity_id", [])
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]

            for eid in hass.data.get(DOMAIN, {}):
                for entity in hass.data[DOMAIN][eid].get("entities", []):
                    if not entity_ids or entity.entity_id in entity_ids:
                        await entity.async_capture_snapshot()

        hass.services.async_register(
            DOMAIN,
            SERVICE_CAPTURE_SNAPSHOT,
            handle_capture_snapshot,
            schema=vol.Schema(
                {
                    vol.Optional("entity_id"): vol.Any(
                        cv.entity_id, vol.All(cv.ensure_list, [cv.entity_id])
                    ),
                }
            ),
        )

    # Periodic discovery — picks up cameras that weren't ready at startup
    # and detects newly added cameras.
    async def _periodic_discovery(_now=None):
        try:
            await resolver.resolve()
            new_cameras = await _fetch_cameras(session, resolver.url)
            existing_ids = {e.camera_id for e in entities}
            new_entities = _make_entities(
                entry, resolver, new_cameras, session, existing_ids
            )
            if new_entities:
                async_add_entities(new_entities, update_before_add=True)
                entities.extend(new_entities)
                _LOGGER.info("Discovered %d new camera(s)", len(new_entities))
        except Exception:
            _LOGGER.exception("Error during camera discovery")

    entry.async_on_unload(
        async_track_time_interval(
            hass, _periodic_discovery, timedelta(seconds=_DISCOVERY_INTERVAL)
        )
    )


def _make_entities(
    entry: ConfigEntry,
    resolver,
    cameras: list[dict],
    session: aiohttp.ClientSession,
    existing_ids: set[str],
) -> list["AlarmDotComCamera"]:
    """Create camera entity objects for cameras not already tracked."""
    return [
        AlarmDotComCamera(
            entry=entry,
            resolver=resolver,
            camera_id=cam["id"],
            camera_name=cam["name"],
            camera_model=cam.get("model", ""),
            session=session,
        )
        for cam in cameras
        if cam["id"] not in existing_ids
    ]


async def _fetch_cameras(session: aiohttp.ClientSession, addon_url: str) -> list[dict]:
    """Fetch camera list from the add-on API."""
    url = f"{addon_url}/api/cameras"
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                cameras = data.get("cameras", [])
                _LOGGER.debug("Fetched %d camera(s) from %s", len(cameras), url)
                return cameras
            _LOGGER.warning("Unexpected status %d from %s", resp.status, url)
    except Exception:
        _LOGGER.exception("Failed to fetch cameras from %s", url)
    return []


class AlarmDotComCamera(Camera):
    """Representation of an Alarm.com camera."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        resolver,
        camera_id: str,
        camera_name: str,
        camera_model: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the camera."""
        super().__init__()
        self._entry = entry
        self._resolver = resolver
        self._camera_id = camera_id
        self._camera_model = camera_model
        self._session = session

        self._attr_name = camera_name
        self._attr_unique_id = f"{DOMAIN}_{camera_id}"
        self._attr_is_streaming = False
        self._attr_frame_interval = 1.0  # seconds between frames

        self._last_image: bytes | None = None
        self._last_snapshot_time: float | None = None

    @property
    def _addon_url(self) -> str:
        """Get the current addon URL (may change after re-discovery)."""
        return self._resolver.url

    @property
    def camera_id(self) -> str:
        """Return the camera ID."""
        return self._camera_id

    @property
    def device_info(self):
        """Return device info for the camera."""
        return {
            "identifiers": {(DOMAIN, self._camera_id)},
            "name": self._attr_name,
            "manufacturer": "Alarm.com",
            "model": self._camera_model or "Camera",
            "via_device": (DOMAIN, "addon"),
        }

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {
            "camera_id": self._camera_id,
            "addon_url": self._addon_url,
        }
        if self._camera_model:
            attrs["camera_model"] = self._camera_model
        if self._last_snapshot_time:
            attrs["last_snapshot_time"] = self._last_snapshot_time
        return attrs

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest snapshot image.

        First tries the cached snapshot from the addon. If no cached
        snapshot exists (404), triggers an on-demand capture so the
        camera entity always shows an image when possible.
        """
        try:
            # Try cached snapshot first (fast)
            async with self._session.get(
                f"{self._addon_url}/api/snapshot/{self._camera_id}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    self._last_image = await resp.read()
                    await self._update_metadata()
                    return self._last_image

                if resp.status == 404:
                    # No cached snapshot — trigger a capture
                    _LOGGER.info(
                        "No cached snapshot for %s, triggering capture",
                        self._camera_id,
                    )
                    return await self._do_capture()

        except Exception:
            _LOGGER.debug(
                "Failed to fetch snapshot for %s, re-resolving URL",
                self._camera_id,
            )
            await self._resolver.resolve()

        return self._last_image

    async def _do_capture(self) -> bytes | None:
        """Trigger an on-demand snapshot capture from the addon."""
        try:
            async with self._session.post(
                f"{self._addon_url}/api/snapshot/{self._camera_id}/capture",
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    self._last_image = await resp.read()
                    await self._update_metadata()
                    _LOGGER.info(
                        "On-demand snapshot captured for %s (%d bytes)",
                        self._camera_id,
                        len(self._last_image),
                    )
                    return self._last_image
                _LOGGER.warning(
                    "Capture returned status %d for %s", resp.status, self._camera_id
                )
        except Exception:
            _LOGGER.exception("Failed to capture snapshot for %s", self._camera_id)
        return self._last_image

    async def async_capture_snapshot(self) -> None:
        """Service call: trigger a fresh snapshot capture."""
        image = await self._do_capture()
        if image:
            self.async_write_ha_state()

    async def _update_metadata(self) -> None:
        """Update snapshot metadata from the add-on."""
        try:
            async with self._session.get(
                f"{self._addon_url}/api/snapshot/{self._camera_id}/metadata",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    meta = await resp.json()
                    self._last_snapshot_time = meta.get("timestamp")
        except Exception:
            pass

    @property
    def is_streaming(self) -> bool:
        """Return whether the camera is streaming."""
        return self._attr_is_streaming

    async def async_turn_on(self) -> None:
        """Trigger an on-demand snapshot capture."""
        await self.async_capture_snapshot()

    async def async_turn_off(self) -> None:
        """Stop streaming."""
        try:
            async with self._session.post(
                f"{self._addon_url}/api/stream/{self._camera_id}/stop",
                timeout=aiohttp.ClientTimeout(total=5),
            ):
                pass
        except Exception:
            pass
        self._attr_is_streaming = False
