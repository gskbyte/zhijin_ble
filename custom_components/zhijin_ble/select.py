"""Select entities for ZhiJin BLE controller modes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import IntEnum

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZhiJinConfigEntry
from .ble.const import (
    BatteryType,
    OutputMode,
    SettingsRegister,
    VoltageDetectMode,
)
from .ble.models import SettingsData
from .coordinator import ZhiJinCoordinator
from .entity import ZhiJinEntity

# Lowercase translation-key option values required by Home Assistant hassfest.
BATTERY_TYPE_OPTIONS: dict[BatteryType, str] = {
    BatteryType.LITHIUM: "lithium",
    BatteryType.GEL: "gel",
    BatteryType.LEAD_ACID: "lead_acid",
}

OUTPUT_MODE_OPTIONS: dict[OutputMode, str] = {
    OutputMode.MANUAL: "manual",
    OutputMode.AUTO: "auto",
    OutputMode.TIMER: "timer",
    OutputMode.DIRECT: "direct",
}

VOLTAGE_DETECT_MODE_OPTIONS: dict[VoltageDetectMode, str] = {
    VoltageDetectMode.AUTO: "auto",
    VoltageDetectMode.V12: "12v",
    VoltageDetectMode.V24: "24v",
    VoltageDetectMode.V36: "36v",
    VoltageDetectMode.V48: "48v",
    VoltageDetectMode.V60: "60v",
    VoltageDetectMode.V72: "72v",
    VoltageDetectMode.V84: "84v",
    VoltageDetectMode.V96: "96v",
}


@dataclass(frozen=True, kw_only=True)
class ZhiJinSelectEntityDescription(SelectEntityDescription):
    """Declarative description for a ZhiJin BLE select entity."""

    register: SettingsRegister
    options_map: Mapping[IntEnum, str]
    value_fn: Callable[[SettingsData], int]


SELECT_DESCRIPTIONS: tuple[ZhiJinSelectEntityDescription, ...] = (
    ZhiJinSelectEntityDescription(
        key="battery_type",
        register=SettingsRegister.BATTERY_TYPE,
        options_map=BATTERY_TYPE_OPTIONS,
        options=list(BATTERY_TYPE_OPTIONS.values()),
        icon="mdi:battery",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.battery_type,
    ),
    ZhiJinSelectEntityDescription(
        key="output_mode",
        register=SettingsRegister.OUTPUT_MODE,
        options_map=OUTPUT_MODE_OPTIONS,
        options=list(OUTPUT_MODE_OPTIONS.values()),
        icon="mdi:brightness-auto",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.output_mode,
    ),
    ZhiJinSelectEntityDescription(
        key="voltage_detect_mode",
        register=SettingsRegister.VOLTAGE_DETECT_MODE,
        options_map=VOLTAGE_DETECT_MODE_OPTIONS,
        options=list(VOLTAGE_DETECT_MODE_OPTIONS.values()),
        icon="mdi:current-dc",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.voltage_detect_mode,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZhiJinConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ZhiJin BLE select entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        ZhiJinSelectEntity(coordinator, description) for description in SELECT_DESCRIPTIONS
    )


class ZhiJinSelectEntity(ZhiJinEntity, SelectEntity):
    """Data-driven configuration select entity."""

    entity_description: ZhiJinSelectEntityDescription

    def __init__(
        self,
        coordinator: ZhiJinCoordinator,
        description: ZhiJinSelectEntityDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._int_to_option = {int(k): v for k, v in description.options_map.items()}
        self._option_to_int = {v: int(k) for k, v in description.options_map.items()}

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data
        if data is None or data.settings is None:
            return None
        raw = self.entity_description.value_fn(data.settings)
        return self._int_to_option.get(raw)

    async def async_select_option(self, option: str) -> None:
        """Write selected option to the controller."""
        if option not in self._option_to_int:
            raise ValueError(f"Unsupported option '{option}' for {self.entity_description.key}")
        await self.coordinator.async_write_register(
            self.entity_description.register,
            self._option_to_int[option],
        )
