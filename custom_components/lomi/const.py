"""Constants for the Lomi integration."""

from __future__ import annotations

from datetime import timedelta

from .api import (
    CMD_PAUSE,
    CMD_PROG,
    CMD_RESUME,
    CMD_START,
    CMD_STOP,
    COMMANDS,
    MODE_CLEANING,
    MODE_EXPRESS,
    MODE_GROW,
    MODE_LOMI_APPROVED,
    MODE_NAMES,
    MODES,
)

DOMAIN = "lomi"
MANUFACTURER = "Pela / Lomi"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
CONF_POLL_INTERVAL = "poll_interval"
CONF_POLL_STATUS = "poll_device_status"
CONF_POLL_PENDING = "poll_pending_commands"

DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 15
MAX_POLL_INTERVAL = 3600
DEFAULT_POLL_STATUS = True
DEFAULT_POLL_PENDING = True
DEFAULT_MODE = MODE_EXPRESS

SCAN_INTERVAL = timedelta(seconds=DEFAULT_POLL_INTERVAL)

# Attributes used in services and events.
ATTR_COMMAND = "command"
ATTR_MODE = "mode"
ATTR_START_AT = "start_at"
ATTR_COMMAND_ID = "command_id"
ATTR_SERIAL = "serial"
ATTR_NICKNAME = "nickname"
ATTR_USER_DEVICE_ID = "user_device_id"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

SERVICE_SEND_COMMAND = "send_command"
SERVICE_SCHEDULE_START = "schedule_start"
SERVICE_CANCEL_SCHEDULED = "cancel_scheduled"
SERVICE_REGISTER_DEVICE = "register_device"

# Bus event fired by the coordinator when a device record changes.
EVENT_LOMI = "lomi_event"
EVENT_TYPE_RECORD_UPDATED = "record_updated"
EVENT_TYPE_FILTER_COUNTER = "filter_counter_changed"
EVENT_TYPE_FILTER_RESET = "filter_reset"
EVENT_TYPE_STATUS_CHANGED = "status_changed"
EVENT_TYPE_COMMAND_ISSUED = "command_issued"
EVENT_TYPE_COMMAND_ACKED = "command_acknowledged"
EVENT_TYPES = (
    EVENT_TYPE_RECORD_UPDATED,
    EVENT_TYPE_FILTER_COUNTER,
    EVENT_TYPE_FILTER_RESET,
    EVENT_TYPE_STATUS_CHANGED,
    EVENT_TYPE_COMMAND_ISSUED,
    EVENT_TYPE_COMMAND_ACKED,
)

__all__ = [
    "CMD_PAUSE",
    "CMD_PROG",
    "CMD_RESUME",
    "CMD_START",
    "CMD_STOP",
    "COMMANDS",
    "MODES",
    "MODE_NAMES",
    "MODE_CLEANING",
    "MODE_EXPRESS",
    "MODE_GROW",
    "MODE_LOMI_APPROVED",
]
