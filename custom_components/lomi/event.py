"""Event entity for Lomi: fires when the cloud record or command state changes."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import EVENT_TYPES
from .coordinator import LomiConfigEntry, LomiCoordinator
from .entity import LomiEntity, async_setup_device_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LomiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the event entity."""
    async_setup_device_entities(
        entry, async_add_entities, lambda coordinator, uid: [LomiEvent(coordinator, uid)]
    )


class LomiEvent(LomiEntity, EventEntity):
    """One event entity per device.

    ``filter_counter_changed`` is the verified "a cycle just finished" signal:
    the counter ticks within ~12 s of the device reporting the end of a run.
    """

    _attr_translation_key = "activity"
    _attr_event_types = list(EVENT_TYPES)
    _attr_icon = "mdi:bell-ring"

    def __init__(self, coordinator: LomiCoordinator, user_device_id: str) -> None:
        """Initialise."""
        super().__init__(coordinator, user_device_id, "activity")

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator events."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_event_listener(self._handle))

    @callback
    def _handle(self, user_device_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if user_device_id != self.user_device_id or event_type not in EVENT_TYPES:
            return
        attrs = {k: v for k, v in payload.items() if k != "type"}
        self._trigger_event(event_type, attrs)
        self.async_write_ha_state()
