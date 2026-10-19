"""Buttons for Lomi: start / pause / resume / stop / cancel scheduled."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import CMD_PAUSE, CMD_RESUME, CMD_START, CMD_STOP, LomiError
from .coordinator import LomiConfigEntry, LomiCoordinator
from .entity import LomiEntity, async_setup_device_entities


@dataclass(frozen=True, kw_only=True)
class LomiButtonDescription(ButtonEntityDescription):
    """Describe a Lomi button."""

    press_fn: Callable[[LomiCoordinator, str], Awaitable[None]]


async def _send(coordinator: LomiCoordinator, uid: str, command: str) -> None:
    await coordinator.async_send_command(uid, command)


async def _cancel_scheduled(coordinator: LomiCoordinator, uid: str) -> None:
    data = coordinator.get_device(uid)
    if data is None:
        return
    for cmd in list(data.scheduled_starts):
        if cmd_id := cmd.get("_id"):
            await coordinator.async_cancel_command(uid, cmd_id)


BUTTONS: tuple[LomiButtonDescription, ...] = (
    LomiButtonDescription(
        key="start",
        translation_key="start",
        icon="mdi:play",
        press_fn=lambda c, uid: _send(c, uid, CMD_START),
    ),
    LomiButtonDescription(
        key="pause",
        translation_key="pause",
        icon="mdi:pause",
        press_fn=lambda c, uid: _send(c, uid, CMD_PAUSE),
    ),
    LomiButtonDescription(
        key="resume",
        translation_key="resume",
        icon="mdi:play-pause",
        press_fn=lambda c, uid: _send(c, uid, CMD_RESUME),
    ),
    LomiButtonDescription(
        key="stop",
        translation_key="stop",
        icon="mdi:stop",
        press_fn=lambda c, uid: _send(c, uid, CMD_STOP),
    ),
    LomiButtonDescription(
        key="cancel_scheduled",
        translation_key="cancel_scheduled",
        icon="mdi:calendar-remove",
        press_fn=_cancel_scheduled,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LomiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lomi buttons."""
    async_setup_device_entities(
        entry,
        async_add_entities,
        lambda coordinator, uid: [
            LomiButton(coordinator, uid, description) for description in BUTTONS
        ],
    )


class LomiButton(LomiEntity, ButtonEntity):
    """A Lomi command button."""

    entity_description: LomiButtonDescription

    def __init__(
        self,
        coordinator: LomiCoordinator,
        user_device_id: str,
        description: LomiButtonDescription,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator, user_device_id, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        """Send the command."""
        try:
            await self.entity_description.press_fn(self.coordinator, self.user_device_id)
        except LomiError as err:
            raise HomeAssistantError(
                f"Lomi command '{self.entity_description.key}' failed: {err}"
            ) from err
