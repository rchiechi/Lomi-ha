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
    shadow = coordinator.shadow
    devices = {}
    for uid, data in (coordinator.data or {}).items():
        dev = asdict(data.device)
        devices[uid] = {
            "device": dev,
            "pending_commands": data.pending_commands,
            "last_command": data.last_command,
            "selected_mode": data.selected_mode,
            "last_seen": data.last_seen.isoformat() if data.last_seen else None,
            "shadow": data.shadow.as_dict() if data.shadow else None,
            "shadow_raw": data.shadow.raw if data.shadow else None,
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
                "poll_pending": coordinator.poll_pending,
            },
            "shadow": {
                "enabled": shadow is not None,
                "connected": coordinator.shadow_connected,
                "last_error": coordinator.shadow_error,
                "token_kind": shadow.config.token_kind if shadow else None,
                "transport": shadow.config.transport if shadow else None,
                "client_id_template": shadow.config.client_id_template if shadow else None,
                "mqtt_username_template": shadow.config.mqtt_username_template if shadow else None,
                "messages": shadow.message_count if shadow else 0,
                "last_connected_at": shadow.last_connected_at.isoformat()
                if shadow and shadow.last_connected_at
                else None,
                "last_message_at": shadow.last_message_at.isoformat()
                if shadow and shadow.last_message_at
                else None,
            },
            "devices": devices,
        },
        TO_REDACT,
    )
