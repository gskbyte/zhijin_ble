"""Binary sensor entities for ZhiJin BLE."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZhiJinConfigEntry
from .ble.models import ControllerState
from .coordinator import ZhiJinCoordinator
from .entity import ZhiJinEntity


def _connectivity_attrs(state: ControllerState) -> dict[str, Any]:
    return {
        "consecutive_failures": state.consecutive_failures,
        "command_success_rate": state.stats.command_success_rate,
        "duplicate_frames_absorbed": state.stats.duplicate_frames,
        "unexpected_disconnects": state.stats.unexpected_disconnects,
    }


@dataclass(frozen=True, kw_only=True)
class ZhiJinBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Declarative description for a ZhiJin BLE binary sensor."""

    value_fn: Callable[[ControllerState], bool | None]
    attrs_fn: Callable[[ControllerState], dict[str, Any]] | None = None
    always_available: bool = False


BINARY_SENSOR_DESCRIPTIONS: tuple[ZhiJinBinarySensorEntityDescription, ...] = (
    ZhiJinBinarySensorEntityDescription(
        key="solar_status",
        device_class=BinarySensorDeviceClass.LIGHT,
        icon="mdi:solar-panel",
        value_fn=lambda s: s.telemetry.solar_status if s.telemetry else None,
    ),
    ZhiJinBinarySensorEntityDescription(
        key="load_status",
        device_class=BinarySensorDeviceClass.POWER,
        icon="mdi:power-socket-us",
        value_fn=lambda s: s.telemetry.load_status if s.telemetry else None,
    ),
    ZhiJinBinarySensorEntityDescription(
        key="wind_status",
        device_class=BinarySensorDeviceClass.POWER,
        icon="mdi:wind-turbine",
        value_fn=lambda s: s.telemetry.wind_status if s.telemetry else None,
    ),
    ZhiJinBinarySensorEntityDescription(
        key="connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.is_online,
        attrs_fn=_connectivity_attrs,
        always_available=True,
    ),
    ZhiJinBinarySensorEntityDescription(
        key="power_save_active",
        icon="mdi:leaf",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.power_save_active,
        always_available=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZhiJinConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ZhiJin BLE binary sensor entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        ZhiJinBinarySensorEntity(coordinator, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class ZhiJinBinarySensorEntity(ZhiJinEntity, BinarySensorEntity):
    """Generic data-driven binary sensor entity for ZhiJin BLE."""

    entity_description: ZhiJinBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: ZhiJinCoordinator,
        description: ZhiJinBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Diagnostic connectivity/power-save sensors stay available to report offline state."""
        if self.entity_description.always_available:
            return True
        return super().available and self.coordinator.data is not None

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return False if self.entity_description.always_available else None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is not None and self.coordinator.data is not None:
            return self.entity_description.attrs_fn(self.coordinator.data)
        return None
