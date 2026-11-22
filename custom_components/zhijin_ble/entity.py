"""Shared base entity class for all ZhiJin BLE Home Assistant entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import ZhiJinCoordinator


class ZhiJinEntity(CoordinatorEntity[ZhiJinCoordinator]):
    """Base entity providing shared DeviceInfo, naming, and unique_id conventions."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ZhiJinCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            identifiers={(DOMAIN, coordinator.address)},
            name=coordinator.device_name,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )
