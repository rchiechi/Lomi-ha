"""Binary sensors for Lomi."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import LomiConfigEntry, LomiCoordinator, LomiDeviceData
from .entity import LomiEntity, async_setup_device_entities


@dataclass(frozen=True, kw_only=True)
class LomiBinarySensorDescription(BinarySensorEntityDescription):
    """Describe a Lomi binary sensor."""

    is_on_fn: Callable[[LomiDeviceData], bool | None]
    # Entity depends on the realtime shadow (unknown until the first document).
    shadow: bool = False


def _shadow(data: LomiDeviceData, attr: str) -> bool | None:
    if data.shadow is None:
        return None
    return getattr(data.shadow, attr)


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
    # ---- realtime (device shadow) ----------------------------------------
    LomiBinarySensorDescription(
        key="lid_open",
        translation_key="lid_open",
        device_class=BinarySensorDeviceClass.OPENING,
        shadow=True,
        is_on_fn=lambda d: _shadow(d, "cover_open"),
    ),
    LomiBinarySensorDescription(
        key="running",
        translation_key="running",
        device_class=BinarySensorDeviceClass.RUNNING,
        shadow=True,
        is_on_fn=lambda d: _shadow(d, "is_running"),
    ),
    LomiBinarySensorDescription(
        key="paused",
        translation_key="paused",
        icon="mdi:pause-circle",
        shadow=True,
        is_on_fn=lambda d: _shadow(d, "paused"),
    ),
    LomiBinarySensorDescription(
        key="cycle_finished",
        translation_key="cycle_finished",
        icon="mdi:check-circle",
        shadow=True,
        is_on_fn=lambda d: _shadow(d, "finished"),
    ),
    LomiBinarySensorDescription(
        key="standby",
        translation_key="standby",
        icon="mdi:power-standby",
        entity_category=EntityCategory.DIAGNOSTIC,
        shadow=True,
        is_on_fn=lambda d: _shadow(d, "standby"),
    ),
    LomiBinarySensorDescription(
        key="recovery",
        translation_key="recovery",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        shadow=True,
        is_on_fn=lambda d: _shadow(d, "recovery"),
    ),
    LomiBinarySensorDescription(
        key="shadow_connected",
        translation_key="shadow_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: None,  # overridden below
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
        if self.entity_description.key == "shadow_connected":
            return self.coordinator.shadow_connected
        if (data := self.data) is None:
            return None
        return self.entity_description.is_on_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the shadow error on the connectivity sensor."""
        if self.entity_description.key != "shadow_connected":
            return None
        shadow = self.coordinator.shadow
        return {
            "enabled": shadow is not None,
            "last_error": self.coordinator.shadow_error,
            "client_id": shadow._client_id() if shadow is not None else None,  # noqa: SLF001
            "messages": shadow.message_count if shadow is not None else 0,
            "last_message_at": shadow.last_message_at.isoformat()
            if shadow is not None and shadow.last_message_at
            else None,
        }
