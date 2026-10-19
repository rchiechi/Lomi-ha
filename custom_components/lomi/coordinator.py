"""Polling coordinator for the Lomi cloud."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    MODE_NAMES,
    LomiApiError,
    LomiAuthError,
    LomiClient,
    LomiConnectionError,
    LomiDevice,
    LomiError,
    parse_iso,
)
from .const import (
    ATTR_COMMAND,
    ATTR_COMMAND_ID,
    ATTR_MODE,
    ATTR_NICKNAME,
    ATTR_SERIAL,
    ATTR_USER_DEVICE_ID,
    CONF_POLL_INTERVAL,
    CONF_POLL_PENDING,
    CONF_POLL_STATUS,
    DEFAULT_MODE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POLL_PENDING,
    DEFAULT_POLL_STATUS,
    DOMAIN,
    EVENT_LOMI,
    EVENT_TYPE_COMMAND_ACKED,
    EVENT_TYPE_COMMAND_ISSUED,
    EVENT_TYPE_FILTER_COUNTER,
    EVENT_TYPE_FILTER_RESET,
    EVENT_TYPE_RECORD_UPDATED,
    EVENT_TYPE_STATUS_CHANGED,
)

_LOGGER = logging.getLogger(__name__)

type LomiConfigEntry = ConfigEntry[LomiCoordinator]

EventCallback = Callable[[str, str, dict[str, Any]], None]


@dataclass(slots=True)
class LomiDeviceData:
    """Everything the coordinator knows about one device."""

    device: LomiDevice
    status: dict[str, Any] = field(default_factory=dict)
    pending_commands: list[dict[str, Any]] = field(default_factory=list)
    last_command: dict[str, Any] | None = None
    selected_mode: str = DEFAULT_MODE
    last_seen: datetime | None = None

    @property
    def scheduled_starts(self) -> list[dict[str, Any]]:
        """Pending commands that are future scheduled starts."""
        return [c for c in self.pending_commands if c.get("scheduledStart")]

    @property
    def next_scheduled_start(self) -> datetime | None:
        """Earliest scheduled start time, if any."""
        times = [parse_iso(c.get("issueAt")) for c in self.scheduled_starts]
        times = [t for t in times if t is not None]
        return min(times) if times else None


class LomiCoordinator(DataUpdateCoordinator[dict[str, LomiDeviceData]]):
    """Poll ``/userDevices`` (plus optional status and pending commands)."""

    config_entry: LomiConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: LomiConfigEntry, client: LomiClient
    ) -> None:
        """Initialise the coordinator."""
        options = entry.options
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(
                seconds=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
            ),
        )
        self.client = client
        self.poll_status: bool = options.get(CONF_POLL_STATUS, DEFAULT_POLL_STATUS)
        self.poll_pending: bool = options.get(CONF_POLL_PENDING, DEFAULT_POLL_PENDING)
        # Snapshot so the update listener can tell an options change from a
        # token rotation written to entry.data.
        self.options_snapshot: dict[str, Any] = dict(options)
        self._event_listeners: list[EventCallback] = []

    # ---- event plumbing ------------------------------------------------------

    @callback
    def async_add_event_listener(self, listener: EventCallback) -> Callable[[], None]:
        """Register a callback for device events; returns an unsubscribe."""
        self._event_listeners.append(listener)

        def _remove() -> None:
            self._event_listeners.remove(listener)

        return _remove

    @callback
    def _fire(self, data: LomiDeviceData, event_type: str, extra: dict[str, Any]) -> None:
        payload = {
            "type": event_type,
            ATTR_SERIAL: data.device.serial,
            ATTR_NICKNAME: data.device.nickname,
            ATTR_USER_DEVICE_ID: data.device.user_device_id,
            **extra,
        }
        self.hass.bus.async_fire(EVENT_LOMI, payload)
        for listener in list(self._event_listeners):
            listener(data.device.user_device_id, event_type, payload)

    # ---- polling -------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, LomiDeviceData]:
        try:
            devices = await self.client.async_get_devices()
        except LomiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (LomiConnectionError, LomiApiError, LomiError) as err:
            raise UpdateFailed(f"Error fetching Lomi devices: {err}") from err

        previous = self.data or {}
        result: dict[str, LomiDeviceData] = {}
        for device in devices:
            old = previous.get(device.user_device_id)
            data = LomiDeviceData(
                device=device,
                status=old.status if old else {},
                pending_commands=old.pending_commands if old else [],
                last_command=old.last_command if old else None,
                selected_mode=old.selected_mode if old else DEFAULT_MODE,
                last_seen=old.last_seen if old else None,
            )
            result[device.user_device_id] = data

        try:
            await self._async_poll_extras(result)
        except LomiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        for uid, data in result.items():
            self._detect_changes(previous.get(uid), data)
        return result

    async def _async_poll_extras(self, result: dict[str, LomiDeviceData]) -> None:
        """Fetch /devices/status and pending commands; failures are non-fatal."""
        tasks = []
        for data in result.values():
            if self.poll_status:
                tasks.append(self._async_fetch_status(data))
            if self.poll_pending:
                tasks.append(self._async_fetch_pending(data))
        if tasks:
            await asyncio.gather(*tasks)

    async def _async_fetch_status(self, data: LomiDeviceData) -> None:
        try:
            data.status = await self.client.async_get_device_status(data.device.serial)
        except LomiAuthError:
            raise
        except LomiError as err:
            _LOGGER.debug("Status fetch failed for %s: %s", data.device.serial, err)

    async def _async_fetch_pending(self, data: LomiDeviceData) -> None:
        try:
            data.pending_commands = await self.client.async_get_pending_commands(
                data.device.user_device_id
            )
        except LomiAuthError:
            raise
        except LomiError as err:
            _LOGGER.debug("Pending fetch failed for %s: %s", data.device.serial, err)

    @callback
    def _detect_changes(self, old: LomiDeviceData | None, new: LomiDeviceData) -> None:
        """Compare with the previous poll and emit events."""
        dev = new.device
        if dev.last_modified or dev.filter_counter_modified_at:
            candidates = [t for t in (dev.last_modified, dev.filter_counter_modified_at) if t]
            new.last_seen = max(candidates)
        if old is None:
            return
        od = old.device

        if dev.version is not None and od.version is not None and dev.version != od.version:
            self._fire(
                new,
                EVENT_TYPE_RECORD_UPDATED,
                {"old_version": od.version, "new_version": dev.version},
            )
        if dev.last_filter_counter != od.last_filter_counter:
            self._fire(
                new,
                EVENT_TYPE_FILTER_COUNTER,
                {
                    "old_counter": od.last_filter_counter,
                    "new_counter": dev.last_filter_counter,
                    "modified_at": dev.filter_counter_modified_at.isoformat()
                    if dev.filter_counter_modified_at
                    else None,
                },
            )
        if dev.last_filter_reset_date != od.last_filter_reset_date:
            self._fire(
                new,
                EVENT_TYPE_FILTER_RESET,
                {
                    "reset_date": dev.last_filter_reset_date.isoformat()
                    if dev.last_filter_reset_date
                    else None
                },
            )
        if new.status != old.status:
            self._fire(new, EVENT_TYPE_STATUS_CHANGED, {"status": new.status})

        new_ids = {c.get("_id") for c in new.pending_commands}
        for cmd in old.pending_commands:
            if cmd.get("_id") not in new_ids:
                self._fire(
                    new,
                    EVENT_TYPE_COMMAND_ACKED,
                    {
                        ATTR_COMMAND_ID: cmd.get("_id"),
                        ATTR_COMMAND: cmd.get("commandType"),
                        ATTR_MODE: MODE_NAMES.get(
                            (cmd.get("arguments") or {}).get("mode")
                        ),
                        "scheduled": bool(cmd.get("scheduledStart")),
                    },
                )

    # ---- helpers for entities / services ------------------------------------

    def get_device(self, user_device_id: str) -> LomiDeviceData | None:
        """Lookup by userDeviceId."""
        return (self.data or {}).get(user_device_id)

    def find_by_serial(self, serial: str) -> LomiDeviceData | None:
        """Lookup by serial number."""
        for data in (self.data or {}).values():
            if data.device.serial == serial:
                return data
        return None

    @callback
    def set_selected_mode(self, user_device_id: str, mode: str) -> None:
        """Remember the program the user picked (cloud does not report it)."""
        if data := self.get_device(user_device_id):
            data.selected_mode = mode
            self.async_update_listeners()

    async def async_send_command(
        self,
        user_device_id: str,
        command: str,
        *,
        mode: str | None = None,
        issue_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Send a command, record it, and refresh pending state."""
        data = self.get_device(user_device_id)
        if data is None:
            raise LomiApiError("UNKNOWN_DEVICE", f"No device {user_device_id}")
        mode = mode or data.selected_mode
        try:
            result = await self.client.async_send_command(
                user_device_id, command, mode=mode, issue_at=issue_at
            )
        except LomiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        data.last_command = result
        # The app always echoes the current program in ``value``; sending a
        # different one switches the device's program, so track it.
        data.selected_mode = mode
        self._fire(
            data,
            EVENT_TYPE_COMMAND_ISSUED,
            {
                ATTR_COMMAND: command,
                ATTR_MODE: mode,
                ATTR_COMMAND_ID: result.get("_id"),
                "status": result.get("status"),
                "scheduled": bool(result.get("scheduledStart")),
                "issue_at": result.get("issueAt"),
            },
        )
        if result.get("status") == "pending" and not any(
            c.get("_id") == result.get("_id") for c in data.pending_commands
        ):
            data.pending_commands = [*data.pending_commands, result]
        self.async_update_listeners()
        # Pull the pending list again shortly so acks show up quickly. Bound
        # to the entry so it is cancelled on unload.
        self.config_entry.async_create_background_task(
            self.hass, self._async_delayed_refresh(), name=f"{DOMAIN}_delayed_refresh"
        )
        return result

    async def async_cancel_command(self, user_device_id: str, command_id: str) -> None:
        """Cancel a scheduled command and drop it from the pending list."""
        try:
            await self.client.async_cancel_command(command_id)
        except LomiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        if data := self.get_device(user_device_id):
            data.pending_commands = [
                c for c in data.pending_commands if c.get("_id") != command_id
            ]
            self.async_update_listeners()

    async def _async_delayed_refresh(self, delay: float = 5.0) -> None:
        await asyncio.sleep(delay)
        await self.async_request_refresh()
