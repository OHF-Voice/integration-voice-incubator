"""Each area a satellite is in has two switches.

**Do not disturb**: on, the app keeps out what other areas send there:
announcements, intercom calls, and music that would start or get louder (the
app's voice_incubator/actions.py). What is said in the area itself still works
there.

**Night mode**: on, the satellites there keep light and sound to a minimum
(the app's voice_incubator/night.py): a command that acts answers with a dim
blink, a question or a confirmation is said quietly, and nothing comes in
from other areas -- night mode is do not disturb too. Timers and alarms
still ring.

The areas are those of the satellites' devices: the switches come with the
first satellite in an area and go with the last, and are renamed with their
area. Each is in its area, and keeps its state across restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .satellites import VoiceIncubatorConfigEntry


@dataclass(frozen=True)
class Kind:
    """One of an area's switches."""

    key: str
    """Its unique id's prefix, and the runtime data's dict of them (by area)."""
    name: str
    icon: str


DO_NOT_DISTURB = Kind("do_not_disturb", "do not disturb", "mdi:minus-circle-outline")
NIGHT_MODE = Kind("night_mode", "night mode", "mdi:weather-night")
KINDS = (DO_NOT_DISTURB, NIGHT_MODE)


async def async_setup_entry(
    hass: HomeAssistant, entry: VoiceIncubatorConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Each kind of switch for each satellite's area, now and as they change."""
    satellites = entry.runtime_data
    kinds = {DO_NOT_DISTURB: satellites.do_not_disturb, NIGHT_MODE: satellites.night_mode}

    @callback
    def update(_event: Event[Any] | None = None) -> None:
        devices = dr.async_get(hass)
        areas = {
            d.area_id for d in dr.async_entries_for_config_entry(devices, entry.entry_id) if d.area_id
        }
        registry = ar.async_get(hass)
        entities = er.async_get(hass)
        added = []
        for kind, switches in kinds.items():
            for area_id in sorted(areas - set(switches)):
                area = registry.async_get_area(area_id)
                if area is not None:
                    switches[area_id] = AreaSwitch(kind, area_id, area.name)
                    added.append(switches[area_id])
            for area_id in set(switches) - areas:
                switch = switches.pop(area_id)
                if switch.entity_id and entities.async_get(switch.entity_id) is not None:
                    entities.async_remove(switch.entity_id)
            for area_id, switch in switches.items():
                area = registry.async_get_area(area_id)
                if area is not None and switch.hass is not None and switch.name != f"{area.name} {kind.name}":
                    switch.rename(area.name)
        if added:
            async_add_entities(added)

    update()
    entry.async_on_unload(hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, update))
    entry.async_on_unload(hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, update))


class AreaSwitch(SwitchEntity, RestoreEntity):
    """An area's do not disturb, or its night mode."""

    _attr_should_poll = False

    def __init__(self, kind: Kind, area_id: str, area_name: str) -> None:
        self.kind = kind
        self.area = area_id
        self._attr_unique_id = f"{kind.key}_{area_id}"
        self._attr_name = f"{area_name} {kind.name}"
        self._attr_icon = kind.icon
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self._attr_is_on = last is not None and last.state == STATE_ON
        # In its area: no device carries it there.
        if self.registry_entry is not None and self.registry_entry.area_id is None:
            er.async_get(self.hass).async_update_entity(self.entity_id, area_id=self.area)

    @callback
    def rename(self, area_name: str) -> None:
        self._attr_name = f"{area_name} {self.kind.name}"
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


def describe(switches: dict[str, AreaSwitch]) -> dict[str, dict[str, Any]]:
    """Each area's switch and whether it is on, for the app."""
    return {
        area_id: {"entity_id": s.entity_id, "on": bool(s.is_on)}
        for area_id, s in sorted(switches.items())
        if s.entity_id is not None
    }
