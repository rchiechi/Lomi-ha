"""Diagnostics for Lomi."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_REFRESH_TOKEN, CONF_USER_ID
from .coordinator import LomiConfigEntry

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    "userId",
    "email",
    "firstName",
    "lastName",
    "deliveryAddress",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LomiConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics."""
    coordinator = entry.runtime_data
    devices = {}
    for uid, data in (coordinator.data or {}).items():
        dev = asdict(data.device)
        devices[uid] = {
            "device": dev,
            "status": data.status,
            "pending_commands": data.pending_commands,
            "last_command": data.last_command,
            "selected_mode": data.selected_mode,
            "last_seen": data.last_seen.isoformat() if data.last_seen else None,
        }
    return async_redact_data(
        {
            "entry": {
                "data": dict(entry.data),
                "options": dict(entry.options),
                "unique_id": entry.unique_id,
            },
            "coordinator": {
                "update_interval": str(coordinator.update_interval),
                "last_update_success": coordinator.last_update_success,
                "poll_status": coordinator.poll_status,
                "poll_pending": coordinator.poll_pending,
            },
            "devices": devices,
        },
        TO_REDACT,
    )
