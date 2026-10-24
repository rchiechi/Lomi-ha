"""Sensors for Lomi."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .api import COMMANDS, MODE_NAMES
from .coordinator import LomiConfigEntry, LomiCoordinator, LomiDeviceData
from .entity import LomiEntity, async_setup_device_entities
from .shadow import STATE_OPTIONS

# Unit of ``tRem`` / ``runTime`` in the shadow. The app shows remaining time in
# minutes; flip this if a live capture proves otherwise.
SHADOW_DURATION_UNIT = UnitOfTime.MINUTES


@dataclass(frozen=True, kw_only=True)
class LomiSensorDescription(SensorEntityDescription):
    """Describe a Lomi sensor."""

    value_fn: Callable[[LomiDeviceData], StateType | datetime]
    attrs_fn: Callable[[LomiDeviceData], dict[str, Any]] | None = None


def _status_state(data: LomiDeviceData) -> StateType:
    """Best-effort state from /devices/status.

    The route returns ``{}`` while idle; the populated shape has not been
    captured yet, so look for the keys the app binary references.
    """
    status = data.status or {}
    for key in ("state", "status", "cycleState", "mode"):
        value = status.get(key)
        if isinstance(value, (str, int, float)):
            return str(value)
    return None


def _status_attrs(data: LomiDeviceData) -> dict[str, Any]:
    return {"raw": data.status or {}}


def _last_command_state(data: LomiDeviceData) -> StateType:
    if not data.last_command:
        return None
    return data.last_command.get("commandType")


def _last_command_attrs(data: LomiDeviceData) -> dict[str, Any]:
    cmd = data.last_command or {}
    mode = (cmd.get("arguments") or {}).get("mode")
    return {
        "command_id": cmd.get("_id"),
        "mode": MODE_NAMES.get(mode, mode),
        "status": cmd.get("status"),
        "scheduled": cmd.get("scheduledStart"),
        "issue_at": cmd.get("issueAt"),
        "created_at": cmd.get("createdAt"),
    }


def _shadow_value(attr: str) -> Callable[[LomiDeviceData], StateType | datetime]:
    def _get(data: LomiDeviceData) -> StateType | datetime:
        if data.shadow is None:
            return None
        return getattr(data.shadow, attr)

    return _get


def _cycle_state(data: LomiDeviceData) -> StateType:
    return data.shadow.state_option if data.shadow else None


def _previous_state(data: LomiDeviceData) -> StateType:
    return data.shadow.previous_state_option if data.shadow else None


def _cycle_attrs(data: LomiDeviceData) -> dict[str, Any]:
    if data.shadow is None:
        return {}
    sh = data.shadow
    return {
        "raw_state": sh.current_state,
        "raw_previous_state": sh.previous_state,
        "stage1": sh.stage1,
        "stage2": sh.stage2,
        "stage3": sh.stage3,
        "program_location": sh.program_location,
        "paused": sh.paused,
        "finished": sh.finished,
        "standby": sh.standby,
        "recovery": sh.recovery,
        "shadow_version": sh.version,
        "received_at": sh.received_at.isoformat(),
    }


def _pending_attrs(data: LomiDeviceData) -> dict[str, Any]:
    return {
        "commands": [
            {
                "command_id": c.get("_id"),
                "command": c.get("commandType"),
                "mode": MODE_NAMES.get((c.get("arguments") or {}).get("mode")),
                "status": c.get("status"),
                "scheduled": c.get("scheduledStart"),
                "issue_at": c.get("issueAt"),
            }
            for c in data.pending_commands
        ]
    }


SENSORS: tuple[LomiSensorDescription, ...] = (
    LomiSensorDescription(
        key="filter_counter",
        translation_key="filter_counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:air-filter",
        value_fn=lambda d: d.device.last_filter_counter,
    ),
    LomiSensorDescription(
        key="last_filter_reset",
        translation_key="last_filter_reset",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-refresh",
        value_fn=lambda d: d.device.last_filter_reset_date,
    ),
    LomiSensorDescription(
        key="filter_counter_modified",
        translation_key="filter_counter_modified",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.device.filter_counter_modified_at,
    ),
    LomiSensorDescription(
        key="last_checkin",
        translation_key="last_checkin",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.last_seen,
    ),
    LomiSensorDescription(
        key="record_version",
        translation_key="record_version",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:counter",
        value_fn=lambda d: d.device.version,
    ),
    LomiSensorDescription(
        key="status",
        translation_key="status",
        icon="mdi:compost",
        value_fn=_status_state,
        attrs_fn=_status_attrs,
    ),
    LomiSensorDescription(
        key="last_command",
        translation_key="last_command",
        device_class=SensorDeviceClass.ENUM,
        options=list(COMMANDS),
        icon="mdi:send-check",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_command_state,
        attrs_fn=_last_command_attrs,
    ),
    LomiSensorDescription(
        key="pending_commands",
        translation_key="pending_commands",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:tray-full",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: len(d.pending_commands),
        attrs_fn=_pending_attrs,
    ),
    LomiSensorDescription(
        key="next_scheduled_start",
        translation_key="next_scheduled_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-start",
        value_fn=lambda d: d.next_scheduled_start,
    ),
    # ---- realtime (device shadow) ----------------------------------------
    LomiSensorDescription(
        key="cycle_state",
        translation_key="cycle_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(STATE_OPTIONS),
        icon="mdi:state-machine",
        value_fn=_cycle_state,
        attrs_fn=_cycle_attrs,
    ),
    LomiSensorDescription(
        key="previous_cycle_state",
        translation_key="previous_cycle_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(STATE_OPTIONS),
        icon="mdi:history",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_previous_state,
    ),
    LomiSensorDescription(
        key="time_remaining",
        translation_key="time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=SHADOW_DURATION_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-sand",
        value_fn=_shadow_value("time_remaining"),
    ),
    LomiSensorDescription(
        key="run_time",
        translation_key="run_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=SHADOW_DURATION_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_shadow_value("run_time"),
    ),
    LomiSensorDescription(
        key="start_delay",
        translation_key="start_delay",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:timer-play-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_shadow_value("start_delay"),
    ),
    LomiSensorDescription(
        key="filter_state",
        translation_key="filter_state",
        icon="mdi:air-filter",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_shadow_value("filter_state"),
    ),
    LomiSensorDescription(
        key="shadow_filter_counter",
        translation_key="shadow_filter_counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:air-filter",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_shadow_value("filter_counter"),
    ),
    LomiSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_shadow_value("firmware"),
    ),
    LomiSensorDescription(
        key="program_location",
        translation_key="program_location",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_shadow_value("program_location"),
    ),
    LomiSensorDescription(
        key="shadow_updated",
        translation_key="shadow_updated",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_shadow_value("received_at"),
    ),
    LomiSensorDescription(
        key="device_reported_at",
        translation_key="device_reported_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_shadow_value("last_updated"),
    ),
    LomiSensorDescription(
        key="device_type",
        translation_key="device_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.device.device_type or None,
    ),
    LomiSensorDescription(
        key="thing_name",
        translation_key="thing_name",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.device.thing_name,
    ),
    LomiSensorDescription(
        key="manufacture_date",
        translation_key="manufacture_date",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.device.manufacture_date,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LomiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lomi sensors."""
    async_setup_device_entities(
        entry,
        async_add_entities,
        lambda coordinator, uid: [
            LomiSensor(coordinator, uid, description) for description in SENSORS
        ],
    )


class LomiSensor(LomiEntity, SensorEntity):
    """A Lomi sensor."""

    entity_description: LomiSensorDescription

    def __init__(
        self,
        coordinator: LomiCoordinator,
        user_device_id: str,
        description: LomiSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, user_device_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        """Return the state."""
        if (data := self.data) is None:
            return None
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes."""
        if (data := self.data) is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(data)
