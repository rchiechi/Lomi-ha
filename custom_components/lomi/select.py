"""Program (mode) selector for Lomi."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api import CMD_PROG, MODES, LomiError
from .const import DEFAULT_MODE
from .coordinator import LomiConfigEntry, LomiCoordinator
from .entity import LomiEntity, async_setup_device_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LomiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mode select."""
    async_setup_device_entities(
        entry, async_add_entities, lambda coordinator, uid: [LomiModeSelect(coordinator, uid)]
    )


class LomiModeSelect(LomiEntity, SelectEntity, RestoreEntity):
    """Choose the program. Sends ``prog`` so the device's display follows.

    The cloud record does not report the current program, so the state is the
    last value chosen through Home Assistant (restored across restarts).
    """

    _attr_translation_key = "mode"
    _attr_options = list(MODES)
    _attr_icon = "mdi:leaf"

    def __init__(self, coordinator: LomiCoordinator, user_device_id: str) -> None:
        """Initialise."""
        super().__init__(coordinator, user_device_id, "mode")

    async def async_added_to_hass(self) -> None:
        """Restore the last selected program."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in MODES:
            self.coordinator.set_selected_mode(self.user_device_id, last.state)

    @property
    def current_option(self) -> str | None:
        """Return the selected program."""
        if (data := self.data) is None:
            return None
        return data.selected_mode or DEFAULT_MODE

    async def async_select_option(self, option: str) -> None:
        """Switch program on the device without starting a cycle."""
        try:
            await self.coordinator.async_send_command(
                self.user_device_id, CMD_PROG, mode=option
            )
        except LomiError as err:
            raise HomeAssistantError(f"Failed to set Lomi program: {err}") from err
        self.coordinator.set_selected_mode(self.user_device_id, option)
