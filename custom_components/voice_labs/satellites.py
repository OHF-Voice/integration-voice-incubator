"""The satellites the voice_labs app serves, as Home Assistant devices.

The app registers each satellite it connects to. A satellite becomes a device
of this integration, and uses its area's voice_timer_list for its timers --
every satellite there shares it -- or, in no area, one of its own
(voice_timer_list.py). Home Assistant no longer merges devices across
integrations, so a satellite that is also an ESPHome device is two devices;
the new one starts in the ESPHome device's area and has its own from then on.
Satellites are remembered across restarts, so their timer lists exist before
the app reconnects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.storage import Store

from .const import DOMAIN, MANUFACTURER, MODEL, STORAGE_KEY, STORAGE_VERSION
from .music import music_player_for

if TYPE_CHECKING:
    from .voice_timer_list import TimerLists

_MAC = re.compile(r"^[0-9a-f]{12}$")

type VoiceLabsConfigEntry = ConfigEntry[Satellites]


@dataclass
class Satellite:
    id: str
    """The MAC address, lowercase hex without separators; or whatever else the
    app keys a satellite on when it has none."""

    name: str

    @property
    def mac(self) -> str | None:
        return dr.format_mac(self.id) if _MAC.match(self.id) else None

    @property
    def timer_list_unique_id(self) -> str:
        return f"{self.id}_timers"

    def device_info(self) -> DeviceInfo:
        """The satellite's own device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.id)},
            connections={(CONNECTION_NETWORK_MAC, self.mac)} if self.mac else set(),
            name=self.name,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )


class Satellites:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._satellites: dict[str, Satellite] = {}
        self.timer_lists: TimerLists | None = None
        """The voice_timer_list platform's lists, kept to where the satellites are."""
        self.do_not_disturb: dict[str, Any] = {}
        """Each satellite's area's do not disturb switch (switch.py), by area id."""
        self.night_mode: dict[str, Any] = {}
        """And its night mode switch."""

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        for satellite_id, info in (data.get("satellites") or {}).items():
            self._satellites[satellite_id] = Satellite(satellite_id, info.get("name") or satellite_id)

    def all(self) -> list[Satellite]:
        return list(self._satellites.values())

    def _data(self) -> dict[str, Any]:
        return {"satellites": {s.id: {"name": s.name} for s in self._satellites.values()}}

    async def async_sync(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Register these satellites (new ones get a device, and a timer list
        where their area has none) and return what the app needs to know
        about each."""
        added = []
        changed = False
        for item in items:
            satellite_id = str(item["id"])
            name = str(item.get("name") or satellite_id)
            known = self._satellites.get(satellite_id)
            if known is None:
                known = self._satellites[satellite_id] = Satellite(satellite_id, name)
                added.append(known)
                changed = True
            elif known.name != name:
                known.name = name
                changed = True
        for satellite in added:
            self._register_device(satellite, first=True)
        if self.timer_lists is not None:
            # As the satellites are now: a new one's, one moved since.
            await self.timer_lists.async_update()
        if changed:
            # Right away rather than delayed: a reload in between would lose them.
            await self._store.async_save(self._data())
        return [self.describe(self._satellites[str(item["id"])]) for item in items]

    @callback
    def _register_device(self, satellite: Satellite, first: bool = False) -> dr.DeviceEntry:
        devices = dr.async_get(self.hass)
        info = satellite.device_info()
        device = devices.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers=info["identifiers"],
            connections=info["connections"],
            name=info["name"],
            manufacturer=info["manufacturer"],
            model=info["model"],
        )
        if first and device.area_id is None and satellite.mac:
            # Start where the same hardware already is; from here on the
            # areas are separate.
            other = self._other_device(satellite.mac)
            if other is not None and other.area_id is not None:
                device = devices.async_update_device(device.id, area_id=other.area_id) or device
        return device

    def _other_device(self, mac: str) -> dr.DeviceEntry | None:
        """Another integration's device for the same hardware (ESPHome's):
        each other config entry's devices, by the registry's own helper
        (``devices`` as a mapping is deprecated)."""
        devices = dr.async_get(self.hass)
        for entry in self.hass.config_entries.async_entries():
            if entry.entry_id == self.entry.entry_id:
                continue
            for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
                if self.entry.entry_id not in device.config_entries and (CONNECTION_NETWORK_MAC, mac) in device.connections:
                    return device
        return None

    def describe(self, satellite: Satellite) -> dict[str, Any]:
        device = self._register_device(satellite)
        timer_list = self.timer_lists.entity_id(satellite) if self.timer_lists is not None else None
        return {
            "id": satellite.id,
            "name": satellite.name,
            "device_id": device.id,
            "device_name": device.name_by_user or device.name,
            "area_id": device.area_id,
            "timer_list": timer_list,
            "media_player": music_player_for(self.hass, satellite.mac) if satellite.mac else None,
        }

    @callback
    def async_remove(self, satellite_id: str) -> None:
        """Forget a satellite whose device was removed from this integration."""
        if self._satellites.pop(satellite_id, None) is not None:
            self._store.async_delay_save(self._data, 0)
