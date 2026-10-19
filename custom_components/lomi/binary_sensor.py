"""Binary sensors for Lomi."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import LomiConfigEntry, LomiCoordinator, LomiDeviceData
from .entity import LomiEntity, async_setup_device_entities


@dataclass(frozen=True, kw_only=True)
class LomiBinarySensorDescription(BinarySensorEntityDescription):
    """Describe a Lomi binary sensor."""

    is_on_fn: Callable[[LomiDeviceData], bool | None]


BINARY_SENSORS: tuple[LomiBinarySensorDescription, ...] = (
    LomiBinarySensorDescription(
        key="scheduled_start",
        translation_key="scheduled_start",
        icon="mdi:calendar-clock",
        is_on_fn=lambda d: bool(d.scheduled_starts),
    ),
    LomiBinarySensorDescription(
        key="command_pending",
        translation_key="command_pending",
        icon="mdi:progress-clock",
        is_on_fn=lambda d: any(not c.get("scheduledStart") for c in d.pending_commands),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LomiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lomi binary sensors."""
    async_setup_device_entities(
        entry,
        async_add_entities,
        lambda coordinator, uid: [
            LomiBinarySensor(coordinator, uid, description)
            for description in BINARY_SENSORS
        ],
    )


class LomiBinarySensor(LomiEntity, BinarySensorEntity):
    """A Lomi binary sensor."""

    entity_description: LomiBinarySensorDescription

    def __init__(
        self,
        coordinator: LomiCoordinator,
        user_device_id: str,
        description: LomiBinarySensorDescription,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator, user_device_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        if (data := self.data) is None:
            return None
        return self.entity_description.is_on_fn(data)
