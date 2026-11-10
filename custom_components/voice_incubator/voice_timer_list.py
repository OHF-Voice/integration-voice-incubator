"""The timers: a voice_timer_list entity for each area a satellite is in --
every satellite there shares it -- and one on each satellite in no area.

An area's list is in the area, on no device ("Kitchen timers",
``voice_timer_list.kitchen_timers``); a satellite's own is on its device
("Desk Timers"). They follow the satellites: one moved into an area uses
the area's (its own goes, with any timer on it), one moved out of every area
has its own again, and an area's list goes with the last satellite there,
and is renamed with its area. Timers are held in memory: they do not
survive a restart.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    EntityPlatform,
    async_get_current_platform,
)

from .const import DOMAIN
from .satellites import Satellite, Satellites, VoiceIncubatorConfigEntry
from .timer_list_const import DOMAIN as TIMER_LIST_DOMAIN
from .timer_list_local import InMemoryTimerListEntity


def area_unique_id(area_id: str) -> str:
    return f"area_{area_id}_timers"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VoiceIncubatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """A list for each satellite's area, and for each satellite in none:
    now, and as satellites come and move."""
    satellites = entry.runtime_data
    lists = satellites.timer_lists = TimerLists(hass, satellites, async_get_current_platform())
    await lists.async_update()

    @callback
    def changed(_event: Event[Any]) -> None:
        hass.async_create_task(lists.async_update())

    entry.async_on_unload(hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, changed))
    entry.async_on_unload(hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, changed))


class TimerLists:
    """The lists there are, kept to where the satellites are."""

    def __init__(self, hass: HomeAssistant, satellites: Satellites, platform: EntityPlatform) -> None:
        self.hass = hass
        self.satellites = satellites
        self.platform = platform
        self.areas: dict[str, AreaTimerList] = {}
        self.own: dict[str, VoiceTimerList] = {}
        """By satellite id."""
        self._lock = asyncio.Lock()

    def area_of(self, satellite: Satellite) -> str | None:
        device = dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, satellite.id)})
        return device.area_id if device is not None else None

    def entity_id(self, satellite: Satellite) -> str | None:
        """The list the satellite uses: its area's, else its own."""
        area_id = self.area_of(satellite)
        unique_id = area_unique_id(area_id) if area_id else satellite.timer_list_unique_id
        return er.async_get(self.hass).async_get_entity_id(TIMER_LIST_DOMAIN, DOMAIN, unique_id)

    async def async_update(self) -> None:
        async with self._lock:
            satellites = self.satellites.all()
            where = {s.id: self.area_of(s) for s in satellites}
            areas = {a for a in where.values() if a}
            alone = {s.id: s for s in satellites if where[s.id] is None}
            names = ar.async_get(self.hass)
            entities = er.async_get(self.hass)

            def name_of(area_id: str) -> str:
                area = names.async_get_area(area_id)
                return area.name if area is not None else area_id

            added: list[InMemoryTimerListEntity] = []
            for area_id in sorted(areas - set(self.areas)):
                self.areas[area_id] = AreaTimerList(area_id, name_of(area_id))
                added.append(self.areas[area_id])
            for satellite_id in sorted(set(alone) - set(self.own)):
                self.own[satellite_id] = VoiceTimerList(alone[satellite_id])
                added.append(self.own[satellite_id])
            for area_id in set(self.areas) - areas:
                self._remove(self.areas.pop(area_id).entity_id)
            # In an area now: its own list goes, even one from before areas had them.
            for satellite in satellites:
                if where[satellite.id] is not None:
                    gone = self.own.pop(satellite.id, None)
                    self._remove(
                        gone.entity_id if gone is not None
                        else entities.async_get_entity_id(TIMER_LIST_DOMAIN, DOMAIN, satellite.timer_list_unique_id)
                    )
            for area_id, timer_list in self.areas.items():
                if timer_list.hass is not None and timer_list.name != f"{name_of(area_id)} timers":
                    timer_list.rename(name_of(area_id))
            if added:
                await self.platform.async_add_entities(added)

    def _remove(self, entity_id: str | None) -> None:
        entities = er.async_get(self.hass)
        if entity_id and entities.async_get(entity_id) is not None:
            entities.async_remove(entity_id)


class VoiceTimerList(InMemoryTimerListEntity):
    """A satellite's own timers, in no area."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-outline"

    def __init__(self, satellite: Satellite) -> None:
        super().__init__(
            name="Timers",
            unique_id=satellite.timer_list_unique_id,
            device_info=satellite.device_info(),
        )


class AreaTimerList(InMemoryTimerListEntity):
    """An area's timers, which every satellite there shares."""

    _attr_icon = "mdi:timer-outline"

    def __init__(self, area_id: str, area_name: str) -> None:
        super().__init__(name=f"{area_name} timers", unique_id=area_unique_id(area_id))
        self.area = area_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # In its area: no device carries it there.
        if self.registry_entry is not None and self.registry_entry.area_id is None:
            er.async_get(self.hass).async_update_entity(self.entity_id, area_id=self.area)

    @callback
    def rename(self, area_name: str) -> None:
        self._attr_name = f"{area_name} timers"
        self.async_write_ha_state()
