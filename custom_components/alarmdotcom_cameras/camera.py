"""Camera platform for Alarm.com Cameras integration."""

import logging
from datetime import timedelta

import aiohttp

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera entities from the add-on."""
    addon_url = hass.data[DOMAIN][entry.entry_id]["addon_url"]
    session = async_get_clientsession(hass)

    # Discover cameras from the add-on
    cameras = await _fetch_cameras(session, addon_url)

    entities = [
        AlarmDotComCamera(
            entry=entry,
            addon_url=addon_url,
            camera_id=cam["id"],
            camera_name=cam["name"],
            camera_model=cam.get("model", ""),
            session=session,
        )
        for cam in cameras
    ]

    if entities:
        async_add_entities(entities, update_before_add=True)
        _LOGGER.info("Added %d Alarm.com camera entities", len(entities))
    else:
        _LOGGER.warning("No cameras found from the add-on at %s", addon_url)

    # Schedule periodic re-discovery to pick up new cameras
    async def _periodic_discovery(_now=None):
        new_cameras = await _fetch_cameras(session, addon_url)
        existing_ids = {e.camera_id for e in entities}
        new_entities = [
            AlarmDotComCamera(
                entry=entry,
                addon_url=addon_url,
                camera_id=cam["id"],
                camera_name=cam["name"],
                camera_model=cam.get("model", ""),
                session=session,
            )
            for cam in new_cameras
            if cam["id"] not in existing_ids
        ]
        if new_entities:
            async_add_entities(new_entities, update_before_add=True)
            entities.extend(new_entities)
            _LOGGER.info("Discovered %d new camera(s)", len(new_entities))

    entry.async_on_unload(
        hass.helpers.event.async_track_time_interval(
            _periodic_discovery, timedelta(seconds=SCAN_INTERVAL)
        )
    )


async def _fetch_cameras(session: aiohttp.ClientSession, addon_url: str) -> list[dict]:
    """Fetch camera list from the add-on API."""
    try:
        async with session.get(
            f"{addon_url}/api/cameras",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("cameras", [])
    except Exception:
        _LOGGER.exception("Failed to fetch cameras from add-on")
    return []


class AlarmDotComCamera(Camera):
    """Representation of an Alarm.com camera."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        addon_url: str,
        camera_id: str,
        camera_name: str,
        camera_model: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the camera."""
        super().__init__()
        self._entry = entry
        self._addon_url = addon_url
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
        """Return the latest snapshot image."""
        try:
            async with self._session.get(
                f"{self._addon_url}/api/snapshot/{self._camera_id}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    self._last_image = await resp.read()
                    # Fetch metadata too
                    await self._update_metadata()
                    return self._last_image
        except Exception:
            _LOGGER.debug("Failed to fetch snapshot for %s", self._camera_id)

        return self._last_image

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
        """Start live stream (trigger snapshot capture)."""
        try:
            async with self._session.post(
                f"{self._addon_url}/api/snapshot/{self._camera_id}/capture",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    self._last_image = await resp.read()
                    _LOGGER.info("On-demand snapshot captured for %s", self._camera_id)
        except Exception:
            _LOGGER.exception(
                "Failed to capture on-demand snapshot for %s", self._camera_id
            )

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
