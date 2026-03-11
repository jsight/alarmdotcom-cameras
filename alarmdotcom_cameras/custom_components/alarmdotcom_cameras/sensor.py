"""Diagnostic sensor platform for Alarm.com Cameras integration."""

import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up diagnostic sensor entities from the add-on."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    resolver = entry_data["resolver"]
    session = async_get_clientsession(hass)

    async_add_entities(
        [
            AddonAuthStatusSensor(entry, resolver, session),
            AddonCamerasCountSensor(entry, resolver, session),
            AddonVersionSensor(entry, resolver, session),
            AddonUptimeSensor(entry, resolver, session),
            AddonLastSnapshotSensor(entry, resolver, session),
        ],
        update_before_add=True,
    )


async def _fetch_health(session: aiohttp.ClientSession, addon_url: str) -> dict | None:
    """Fetch health data from the add-on."""
    try:
        async with session.get(
            f"{addon_url}/api/health",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        _LOGGER.debug("Failed to fetch health from add-on")
    return None


class AddonDiagnosticSensor(SensorEntity):
    """Base class for addon diagnostic sensors."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: ConfigEntry,
        resolver,
        session: aiohttp.ClientSession,
        description: SensorEntityDescription,
    ) -> None:
        self._entry = entry
        self._resolver = resolver
        self._session = session
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_addon_{description.key}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "addon")},
            "name": "Alarm.com Cameras Add-on",
            "manufacturer": "Alarm.com Cameras",
            "model": "Add-on",
        }

    async def _get_health(self) -> dict | None:
        return await _fetch_health(self._session, self._resolver.url)


class AddonAuthStatusSensor(AddonDiagnosticSensor):
    """Sensor showing the addon's authentication status."""

    def __init__(self, entry, resolver, session) -> None:
        super().__init__(
            entry,
            resolver,
            session,
            SensorEntityDescription(
                key="auth_status",
                name="Auth Status",
                icon="mdi:shield-account",
            ),
        )

    async def async_update(self) -> None:
        health = await self._get_health()
        if health:
            self._attr_native_value = health.get("auth_status", "unknown")
            self._attr_extra_state_attributes = {
                "session_valid": health.get("session_valid"),
                "browser_alive": health.get("browser_alive"),
            }


class AddonCamerasCountSensor(AddonDiagnosticSensor):
    """Sensor showing the number of discovered cameras."""

    def __init__(self, entry, resolver, session) -> None:
        super().__init__(
            entry,
            resolver,
            session,
            SensorEntityDescription(
                key="cameras_count",
                name="Cameras Discovered",
                icon="mdi:cctv",
            ),
        )

    async def async_update(self) -> None:
        health = await self._get_health()
        if health:
            self._attr_native_value = health.get("cameras_count", 0)


class AddonVersionSensor(AddonDiagnosticSensor):
    """Sensor showing the addon version."""

    def __init__(self, entry, resolver, session) -> None:
        super().__init__(
            entry,
            resolver,
            session,
            SensorEntityDescription(
                key="version",
                name="Addon Version",
                icon="mdi:package-variant",
            ),
        )

    async def async_update(self) -> None:
        health = await self._get_health()
        if health:
            self._attr_native_value = health.get("version", "unknown")


class AddonUptimeSensor(AddonDiagnosticSensor):
    """Sensor showing the addon uptime."""

    def __init__(self, entry, resolver, session) -> None:
        super().__init__(
            entry,
            resolver,
            session,
            SensorEntityDescription(
                key="uptime",
                name="Addon Uptime",
                icon="mdi:clock-outline",
                device_class=SensorDeviceClass.DURATION,
                native_unit_of_measurement="s",
            ),
        )

    async def async_update(self) -> None:
        health = await self._get_health()
        if health:
            self._attr_native_value = health.get("uptime_seconds")


class AddonLastSnapshotSensor(AddonDiagnosticSensor):
    """Sensor showing when the last snapshot was taken."""

    def __init__(self, entry, resolver, session) -> None:
        super().__init__(
            entry,
            resolver,
            session,
            SensorEntityDescription(
                key="last_snapshot",
                name="Last Snapshot",
                icon="mdi:camera-timer",
                device_class=SensorDeviceClass.TIMESTAMP,
            ),
        )

    async def async_update(self) -> None:
        health = await self._get_health()
        if health:
            ts = health.get("last_snapshot_time")
            if ts:
                self._attr_native_value = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                self._attr_native_value = None
