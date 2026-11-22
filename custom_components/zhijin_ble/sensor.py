"""Sensor entities for ZhiJin BLE."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZhiJinConfigEntry
from .ble.models import ControllerState
from .coordinator import ZhiJinCoordinator
from .entity import ZhiJinEntity


def _healthy_battery_icon(state: ControllerState) -> str:
    """Return dynamic charging/level battery icon for healthy usable battery."""
    if state.telemetry is None:
        return "mdi:battery-unknown"
    val = int(round(state.telemetry.healthy_usable_battery / 10.0) * 10)
    val = max(0, min(100, val))
    if state.telemetry.is_charging:
        if val == 0:
            return "mdi:battery-charging-outline"
        if val == 100:
            return "mdi:battery-charging-100"
        return f"mdi:battery-charging-{val}"
    if val == 0:
        return "mdi:battery-outline"
    if val == 100:
        return "mdi:battery"
    return f"mdi:battery-{val}"


def _healthy_battery_attrs(state: ControllerState) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    if state.telemetry is not None:
        attrs["charging"] = state.telemetry.is_charging
    if state.settings is not None:
        attrs["battery_type"] = state.settings.battery_type_label or state.settings.battery_type
    return attrs


def _total_generation_attrs(state: ControllerState) -> dict[str, Any]:
    if state.telemetry is None:
        return {}
    return {
        "raw_counter": state.telemetry.total_power_raw,
        "rollover_count": state.telemetry.total_power_resets,
    }


def _temperature_attrs(state: ControllerState) -> dict[str, Any]:
    if state.telemetry is None or state.temperature_offset == 0.0:
        return {}
    return {
        "raw_temperature": state.telemetry.temperature,
        "temperature_offset": state.temperature_offset,
    }


@dataclass(frozen=True, kw_only=True)
class ZhiJinSensorEntityDescription(SensorEntityDescription):
    """Declarative description for a ZhiJin BLE sensor entity."""

    value_fn: Callable[[ControllerState], float | int | None]
    icon_fn: Callable[[ControllerState], str] | None = None
    attrs_fn: Callable[[ControllerState], dict[str, Any]] | None = None
    always_available: bool = False


SENSOR_DESCRIPTIONS: tuple[ZhiJinSensorEntityDescription, ...] = (
    ZhiJinSensorEntityDescription(
        key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.telemetry.battery_voltage if s.telemetry else None,
    ),
    ZhiJinSensorEntityDescription(
        key="battery_percent",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.telemetry.battery_percent if s.telemetry else None,
    ),
    ZhiJinSensorEntityDescription(
        key="healthy_usable_battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.telemetry.healthy_usable_battery if s.telemetry else None,
        icon_fn=_healthy_battery_icon,
        attrs_fn=_healthy_battery_attrs,
    ),
    ZhiJinSensorEntityDescription(
        key="charge_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.telemetry.charge_current if s.telemetry else None,
    ),
    ZhiJinSensorEntityDescription(
        key="solar_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.telemetry.solar_power if s.telemetry else None,
    ),
    ZhiJinSensorEntityDescription(
        key="discharge_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.telemetry.discharge_current if s.telemetry else None,
    ),
    ZhiJinSensorEntityDescription(
        key="load_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.telemetry.load_power if s.telemetry else None,
    ),
    ZhiJinSensorEntityDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.calibrated_temperature,
        attrs_fn=_temperature_attrs,
    ),
    ZhiJinSensorEntityDescription(
        key="total_generation",
        icon="mdi:solar-power",
        native_unit_of_measurement="Ah",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        value_fn=lambda s: s.telemetry.total_generation if s.telemetry else None,
        attrs_fn=_total_generation_attrs,
    ),
    # Diagnostic sensors for monitoring BLE link health
    ZhiJinSensorEntityDescription(
        key="rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.rssi,
        always_available=True,
    ),
    ZhiJinSensorEntityDescription(
        key="poll_latency",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda s: s.last_poll_ms,
        always_available=True,
    ),
    ZhiJinSensorEntityDescription(
        key="connection_success_rate",
        icon="mdi:bluetooth-connect",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.stats.command_success_rate,
        always_available=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZhiJinConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ZhiJin BLE sensor entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        ZhiJinSensorEntity(coordinator, description) for description in SENSOR_DESCRIPTIONS
    )


class ZhiJinSensorEntity(ZhiJinEntity, SensorEntity):
    """Generic data-driven sensor entity for ZhiJin BLE."""

    entity_description: ZhiJinSensorEntityDescription

    def __init__(
        self,
        coordinator: ZhiJinCoordinator,
        description: ZhiJinSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Diagnostic link sensors remain available even while controller is offline."""
        if self.entity_description.always_available:
            return self.coordinator.data is not None
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self) -> float | int | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def icon(self) -> str | None:
        if self.entity_description.icon_fn is not None and self.coordinator.data is not None:
            return self.entity_description.icon_fn(self.coordinator.data)
        return self.entity_description.icon

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is not None and self.coordinator.data is not None:
            return self.entity_description.attrs_fn(self.coordinator.data)
        return None
