"""Coordinator for the Lomi cloud: REST polling plus device-shadow push."""

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
from homeassistant.helpers import device_registry as dr
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
    EVENT_TYPE_CYCLE_FINISHED,
    EVENT_TYPE_CYCLE_PAUSED,
    EVENT_TYPE_CYCLE_RESUMED,
    EVENT_TYPE_CYCLE_STARTED,
    EVENT_TYPE_CYCLE_STATE_CHANGED,
    EVENT_TYPE_FILTER_COUNTER,
    EVENT_TYPE_FILTER_RESET,
    EVENT_TYPE_LID_CLOSED,
    EVENT_TYPE_LID_OPENED,
    EVENT_TYPE_RECORD_UPDATED,
    EVENT_TYPE_SHADOW_UPDATED,
    EVENT_TYPE_STATUS_CHANGED,
)
from .shadow import LomiShadowClient, LomiShadowState

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
    shadow: LomiShadowState | None = None

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
        # Realtime channel (AWS IoT device shadow); attached by __init__.
        self.shadow: LomiShadowClient | None = None
        self.shadow_connected = False
        self.shadow_error: str | None = None

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
                shadow=old.shadow if old else None,
            )
            result[device.user_device_id] = data

        try:
            await self._async_poll_extras(result)
        except LomiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        for uid, data in result.items():
            self._detect_changes(previous.get(uid), data)
        if self.shadow is not None:
            self.shadow.set_things(self._thing_map(result))
        return result

    @staticmethod
    def _thing_map(result: dict[str, LomiDeviceData]) -> dict[str, str]:
        return {
            d.device.thing_name: d.device.serial
            for d in result.values()
            if d.device.thing_name
        }

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

    def find_by_thing(self, thing_name: str) -> LomiDeviceData | None:
        """Lookup by AWS IoT thing name (falls back to the serial suffix)."""
        for data in (self.data or {}).values():
            if data.device.thing_name == thing_name:
                return data
        for data in (self.data or {}).values():
            if data.device.serial and thing_name.endswith(data.device.serial):
                return data
        return None

    # ---- realtime shadow -------------------------------------------------------

    @callback
    def attach_shadow(self, shadow: LomiShadowClient) -> None:
        """Bind the shadow client and hand it the current things."""
        self.shadow = shadow
        shadow.set_things(self._thing_map(self.data or {}))

    @callback
    def async_set_shadow_connection(self, connected: bool, error: str | None) -> None:
        """Connection state callback from the shadow client."""
        self.shadow_connected = connected
        self.shadow_error = error
        self.async_update_listeners()

    @callback
    def async_handle_shadow(self, thing_name: str, state: LomiShadowState, source: str) -> None:
        """A shadow document arrived: store it and emit change events."""
        data = self.find_by_thing(thing_name)
        if data is None:
            _LOGGER.debug("Shadow for unknown thing %s ignored", thing_name)
            return
        old = data.shadow
        data.shadow = state
        if state.firmware and (old is None or old.firmware != state.firmware):
            self._update_firmware(data, state.firmware)
        self.hass.bus.async_fire(
            EVENT_LOMI,
            {
                "type": EVENT_TYPE_SHADOW_UPDATED,
                ATTR_SERIAL: data.device.serial,
                ATTR_NICKNAME: data.device.nickname,
                ATTR_USER_DEVICE_ID: data.device.user_device_id,
                "source": source,
                **state.as_dict(),
            },
        )
        if old is not None:
            self._detect_shadow_changes(data, old, state)
        self.async_update_listeners()

    @callback
    def _update_firmware(self, data: LomiDeviceData, firmware: str) -> None:
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, data.device.serial)})
        if device is not None and device.sw_version != firmware:
            registry.async_update_device(device.id, sw_version=firmware)

    @callback
    def _detect_shadow_changes(
        self, data: LomiDeviceData, old: LomiShadowState, new: LomiShadowState
    ) -> None:
        base = {
            "state": new.current_state,
            "previous_state": old.current_state,
            "time_remaining": new.time_remaining,
            "run_time": new.run_time,
        }
        if new.current_state != old.current_state:
            self._fire(data, EVENT_TYPE_CYCLE_STATE_CHANGED, base)
        if old.cover_open is not None and new.cover_open is not None and new.cover_open != old.cover_open:
            self._fire(data, EVENT_TYPE_LID_OPENED if new.cover_open else EVENT_TYPE_LID_CLOSED, base)
        was_active, is_active = old.is_active, new.is_active
        if is_active and not was_active:
            self._fire(data, EVENT_TYPE_CYCLE_STARTED, base)
        elif was_active and not is_active:
            self._fire(data, EVENT_TYPE_CYCLE_FINISHED, {**base, "finished": new.finished})
        elif (
            old.finished is not None
            and new.finished
            and not old.finished
            and not is_active
        ):
            self._fire(data, EVENT_TYPE_CYCLE_FINISHED, {**base, "finished": True})
        if old.paused is not None and new.paused is not None and new.paused != old.paused:
            self._fire(data, EVENT_TYPE_CYCLE_PAUSED if new.paused else EVENT_TYPE_CYCLE_RESUMED, base)

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
