"""Number entities for ZhiJin BLE voltage thresholds and local calibration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfElectricPotential, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZhiJinConfigEntry
from .ble.const import (
    CUTOFF_VOLTAGE_RANGE,
    FULL_VOLTAGE_RANGE,
    RECOVERY_VOLTAGE_RANGE,
    TEMPERATURE_OFFSET_RANGE,
    SettingsRegister,
)
from .ble.models import SettingsData
from .coordinator import ZhiJinCoordinator
from .entity import ZhiJinEntity


@dataclass(frozen=True, kw_only=True)
class ZhiJinNumberEntityDescription(NumberEntityDescription):
    """Declarative description for a ZhiJin BLE voltage threshold number."""

    register: SettingsRegister
    value_fn: Callable[[SettingsData], float]


NUMBER_DESCRIPTIONS: tuple[ZhiJinNumberEntityDescription, ...] = (
    ZhiJinNumberEntityDescription(
        key="full_voltage",
        register=SettingsRegister.FULL_VOLTAGE,
        device_class=NumberDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        native_min_value=FULL_VOLTAGE_RANGE[0],
        native_max_value=FULL_VOLTAGE_RANGE[1],
        native_step=0.1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.full_voltage,
    ),
    ZhiJinNumberEntityDescription(
        key="cutoff_voltage",
        register=SettingsRegister.CUTOFF_VOLTAGE,
        device_class=NumberDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        native_min_value=CUTOFF_VOLTAGE_RANGE[0],
        native_max_value=CUTOFF_VOLTAGE_RANGE[1],
        native_step=0.1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.cutoff_voltage,
    ),
    ZhiJinNumberEntityDescription(
        key="recovery_voltage",
        register=SettingsRegister.RECOVERY_VOLTAGE,
        device_class=NumberDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        native_min_value=RECOVERY_VOLTAGE_RANGE[0],
        native_max_value=RECOVERY_VOLTAGE_RANGE[1],
        native_step=0.1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.recovery_voltage,
    ),
)

TEMPERATURE_OFFSET_DESCRIPTION = NumberEntityDescription(
    key="temperature_offset",
    device_class=NumberDeviceClass.TEMPERATURE_DELTA,
    icon="mdi:thermometer-plus",
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    native_min_value=TEMPERATURE_OFFSET_RANGE[0],
    native_max_value=TEMPERATURE_OFFSET_RANGE[1],
    native_step=0.1,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZhiJinConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ZhiJin BLE number entities."""
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [
        ZhiJinNumberEntity(coordinator, description) for description in NUMBER_DESCRIPTIONS
    ]
    entities.append(ZhiJinTemperatureOffsetNumberEntity(coordinator))
    async_add_entities(entities)


class ZhiJinNumberEntity(ZhiJinEntity, NumberEntity):
    """Data-driven voltage threshold configuration entity."""

    entity_description: ZhiJinNumberEntityDescription

    def __init__(
        self,
        coordinator: ZhiJinCoordinator,
        description: ZhiJinNumberEntityDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None or data.settings is None:
            return None
        return self.entity_description.value_fn(data.settings)

    async def async_set_native_value(self, value: float) -> None:
        """Write updated voltage threshold to the controller."""
        await self.coordinator.async_write_voltage(self.entity_description.register, value)


class ZhiJinTemperatureOffsetNumberEntity(ZhiJinEntity, NumberEntity):
    """Device configuration entity for local temperature calibration offset (°C)."""

    entity_description = TEMPERATURE_OFFSET_DESCRIPTION

    def __init__(self, coordinator: ZhiJinCoordinator) -> None:
        super().__init__(coordinator, TEMPERATURE_OFFSET_DESCRIPTION.key)

    @property
    def available(self) -> bool:
        """Always available since temperature calibration is stored locally."""
        return True

    @property
    def native_value(self) -> float:
        return self.coordinator.temperature_offset

    async def async_set_native_value(self, value: float) -> None:
        """Persist new temperature calibration offset and immediately update Temperature."""
        await self.coordinator.async_set_temperature_offset(value)
