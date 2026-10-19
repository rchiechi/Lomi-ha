"""Shared entity base for Lomi."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import LomiConfigEntry, LomiCoordinator, LomiDeviceData


class LomiEntity(CoordinatorEntity[LomiCoordinator]):
    """Base class: one entity bound to one Lomi device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LomiCoordinator, user_device_id: str, key: str) -> None:
        """Initialise with the device id and a unique key suffix."""
        super().__init__(coordinator)
        self.user_device_id = user_device_id
        data = coordinator.data[user_device_id]
        dev = data.device
        self._attr_unique_id = f"{dev.serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev.serial)},
            manufacturer=MANUFACTURER,
            model=dev.model,
            model_id=dev.device_type or None,
            name=dev.nickname,
            serial_number=dev.serial,
            hw_version=dev.device_version,
            configuration_url="https://lomi.com/pages/app",
        )

    @property
    def data(self) -> LomiDeviceData | None:
        """Current coordinator data for this device."""
        return self.coordinator.get_device(self.user_device_id)

    @property
    def available(self) -> bool:
        """Unavailable if the device vanished from the account."""
        return super().available and self.data is not None


@callback
def async_setup_device_entities(
    entry: LomiConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[LomiCoordinator, str], Iterable[Entity]],
) -> None:
    """Create entities for every device, now and whenever a new one appears."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new_ids = [uid for uid in (coordinator.data or {}) if uid not in known]
        if not new_ids:
            return
        known.update(new_ids)
        entities: list[Entity] = []
        for uid in new_ids:
            entities.extend(factory(coordinator, uid))
        async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))
