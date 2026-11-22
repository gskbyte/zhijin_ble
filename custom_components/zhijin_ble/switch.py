"""Switch entities for ZhiJin BLE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZhiJinConfigEntry
from .ble.const import SettingsRegister
from .coordinator import ZhiJinCoordinator
from .entity import ZhiJinEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZhiJinConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ZhiJin BLE switch entities."""
    async_add_entities([ZhiJinLoadSwitchEntity(entry.runtime_data)])


class ZhiJinLoadSwitchEntity(ZhiJinEntity, SwitchEntity):
    """Manual load output switch (register 0x1007)."""

    _attr_icon = "mdi:power"

    def __init__(self, coordinator: ZhiJinCoordinator) -> None:
        super().__init__(coordinator, "load_switch")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None or data.settings is None:
            return None
        return data.settings.load_switch

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable manual load output."""
        await self.coordinator.async_write_register(SettingsRegister.LOAD_SWITCH, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable manual load output."""
        await self.coordinator.async_write_register(SettingsRegister.LOAD_SWITCH, 0)
