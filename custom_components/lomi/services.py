"""Domain services for Lomi (schedule / cancel / raw command / register)."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.util import dt as dt_util

from .api import CMD_START, COMMANDS, MODES, LomiApiError, LomiError
from .const import (
    ATTR_COMMAND,
    ATTR_COMMAND_ID,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_MODE,
    ATTR_START_AT,
    DOMAIN,
    SERVICE_CANCEL_SCHEDULED,
    SERVICE_REGISTER_DEVICE,
    SERVICE_SCHEDULE_START,
    SERVICE_SEND_COMMAND,
)
from .coordinator import LomiConfigEntry, LomiCoordinator

_LOGGER = logging.getLogger(__name__)

ATTR_DEVICE_ID = "device_id"
ATTR_SERIAL_NUMBER = "serial_number"
ATTR_NICKNAME = "nickname"

SEND_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_COMMAND): vol.In(COMMANDS),
        vol.Optional(ATTR_MODE): vol.In(MODES),
        vol.Optional(ATTR_START_AT): cv.datetime,
    }
)
SCHEDULE_START_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_START_AT): cv.datetime,
        vol.Optional(ATTR_MODE): vol.In(MODES),
    }
)
CANCEL_SCHEDULED_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_COMMAND_ID): cv.string,
    }
)
REGISTER_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_SERIAL_NUMBER): cv.string,
        vol.Optional(ATTR_NICKNAME, default="Lomi"): cv.string,
    }
)


def _loaded_entries(hass: HomeAssistant) -> list[LomiConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


def _resolve_device(hass: HomeAssistant, device_id: str) -> tuple[LomiCoordinator, str]:
    """Map an HA device id to (coordinator, userDeviceId)."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"device_id": device_id},
        )
    serial = next((ident for dom, ident in device.identifiers if dom == DOMAIN), None)
    if serial is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_lomi_device",
            translation_placeholders={"device_id": device_id},
        )
    for entry in _loaded_entries(hass):
        if entry.entry_id not in device.config_entries:
            continue
        coordinator = entry.runtime_data
        if data := coordinator.find_by_serial(serial):
            return coordinator, data.device.user_device_id
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="device_not_loaded",
        translation_placeholders={"device_id": device_id},
    )


def _future_utc(when: datetime) -> datetime:
    """Normalise a service datetime to aware UTC and require it to be ahead."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt_util.get_default_time_zone())
    when = dt_util.as_utc(when)
    if when <= dt_util.utcnow():
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="start_time_in_past",
            translation_placeholders={"start_at": when.isoformat()},
        )
    return when


def _command_response(result: dict[str, Any]) -> ServiceResponse:
    return {
        "command_id": result.get("_id"),
        "command": result.get("commandType"),
        "mode": (result.get("arguments") or {}).get("mode"),
        "status": result.get("status"),
        "scheduled": result.get("scheduledStart"),
        "issue_at": result.get("issueAt"),
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the lomi.* services once."""

    async def _send_command(call: ServiceCall) -> ServiceResponse:
        coordinator, uid = _resolve_device(hass, call.data[ATTR_DEVICE_ID])
        issue_at = call.data.get(ATTR_START_AT)
        try:
            result = await coordinator.async_send_command(
                uid,
                call.data[ATTR_COMMAND],
                mode=call.data.get(ATTR_MODE),
                issue_at=_future_utc(issue_at) if issue_at else None,
            )
        except LomiError as err:
            raise HomeAssistantError(f"Lomi command failed: {err}") from err
        return _command_response(result)

    async def _schedule_start(call: ServiceCall) -> ServiceResponse:
        coordinator, uid = _resolve_device(hass, call.data[ATTR_DEVICE_ID])
        try:
            result = await coordinator.async_send_command(
                uid,
                CMD_START,
                mode=call.data.get(ATTR_MODE),
                issue_at=_future_utc(call.data[ATTR_START_AT]),
            )
        except LomiError as err:
            raise HomeAssistantError(f"Lomi scheduling failed: {err}") from err
        return _command_response(result)

    async def _cancel_scheduled(call: ServiceCall) -> None:
        coordinator, uid = _resolve_device(hass, call.data[ATTR_DEVICE_ID])
        data = coordinator.get_device(uid)
        if data is None:
            return
        if cmd_id := call.data.get(ATTR_COMMAND_ID):
            targets = [cmd_id]
        else:
            targets = [c["_id"] for c in data.scheduled_starts if c.get("_id")]
        try:
            for target in targets:
                await coordinator.async_cancel_command(uid, target)
        except LomiError as err:
            raise HomeAssistantError(f"Lomi cancel failed: {err}") from err

    async def _register_device(call: ServiceCall) -> ServiceResponse:
        entries = _loaded_entries(hass)
        if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
            entries = [e for e in entries if e.entry_id == entry_id]
        if len(entries) != 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="ambiguous_entry"
            )
        coordinator = entries[0].runtime_data
        serial = call.data[ATTR_SERIAL_NUMBER].strip().upper()
        try:
            result = await coordinator.client.async_register_device(
                serial, call.data[ATTR_NICKNAME].strip() or "Lomi"
            )
        except LomiApiError as err:
            raise HomeAssistantError(f"Registration rejected: {err}") from err
        except LomiError as err:
            raise HomeAssistantError(f"Registration failed: {err}") from err
        await coordinator.async_request_refresh()
        return {"result": result if isinstance(result, dict) else {"value": result}}

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        _send_command,
        schema=SEND_COMMAND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCHEDULE_START,
        _schedule_start,
        schema=SCHEDULE_START_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_SCHEDULED, _cancel_scheduled, schema=CANCEL_SCHEDULED_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REGISTER_DEVICE,
        _register_device,
        schema=REGISTER_DEVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
