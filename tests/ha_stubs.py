"""Minimal Home Assistant runtime stubs for testing the HA integration layer.

Allows exercising the real coordinator, config/options flow, entity platforms,
setup/unload lifecycle, and diagnostics without installing full Home Assistant.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


def install_ha_stubs() -> dict[str, Any]:
    """Install lightweight ``homeassistant.*`` stubs into ``sys.modules`` if absent.

    Returns a dictionary of mutable hook registries so tests can inspect
    created/deleted issues, Bluetooth advertisements, and bus events.
    """
    state: dict[str, Any] = {
        "ble_devices": {},
        "service_infos": {},
        "discovered_infos": [],
        "issues": {},
        "deleted_issues": [],
    }

    if "homeassistant" in sys.modules and hasattr(sys.modules["homeassistant"], "_zhijin_stub_state"):
        stub_state = sys.modules["homeassistant"]._zhijin_stub_state
        stub_state["ble_devices"].clear()
        stub_state["service_infos"].clear()
        stub_state["discovered_infos"].clear()
        stub_state["issues"].clear()
        stub_state["deleted_issues"].clear()
        return stub_state

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    ha = _mod("homeassistant")
    ha._zhijin_stub_state = state  # type: ignore[attr-defined]

    # homeassistant.const
    const_mod = _mod("homeassistant.const")

    class Platform(StrEnum):
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"
        NUMBER = "number"
        SELECT = "select"
        SWITCH = "switch"

    class EntityCategory(StrEnum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    class UnitOfElectricPotential(StrEnum):
        VOLT = "V"

    class UnitOfElectricCurrent(StrEnum):
        AMPERE = "A"

    class UnitOfPower(StrEnum):
        WATT = "W"

    class UnitOfTemperature(StrEnum):
        CELSIUS = "°C"

    class UnitOfTime(StrEnum):
        MILLISECONDS = "ms"

    const_mod.Platform = Platform
    const_mod.EntityCategory = EntityCategory
    const_mod.PERCENTAGE = "%"
    const_mod.SIGNAL_STRENGTH_DECIBELS_MILLIWATT = "dBm"
    const_mod.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
    const_mod.UnitOfElectricPotential = UnitOfElectricPotential
    const_mod.UnitOfElectricCurrent = UnitOfElectricCurrent
    const_mod.UnitOfPower = UnitOfPower
    const_mod.UnitOfTemperature = UnitOfTemperature
    const_mod.UnitOfTime = UnitOfTime

    # homeassistant.exceptions
    exc_mod = _mod("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    exc_mod.HomeAssistantError = HomeAssistantError

    # homeassistant.core
    core_mod = _mod("homeassistant.core")

    class FakeBus:
        def __init__(self) -> None:
            self.listeners: dict[str, list[Callable[..., Any]]] = {}

        def async_listen_once(self, event_type: str, listener: Callable[..., Any]) -> Callable[[], None]:
            self.listeners.setdefault(event_type, []).append(listener)

            def _unsub() -> None:
                if listener in self.listeners.get(event_type, []):
                    self.listeners[event_type].remove(listener)

            return _unsub

    class FakeConfigEntriesManager:
        def __init__(self) -> None:
            self.forwarded: list[tuple[Any, list[Any]]] = []
            self.unloaded: list[tuple[Any, list[Any]]] = []
            self.reloaded: list[str] = []

        async def async_forward_entry_setups(self, entry: Any, platforms: list[Any]) -> None:
            self.forwarded.append((entry, list(platforms)))

        async def async_unload_platforms(self, entry: Any, platforms: list[Any]) -> bool:
            self.unloaded.append((entry, list(platforms)))
            return True

        async def async_unload_platforms_or_raise(self, entry: Any, platforms: list[Any]) -> None:
            self.unloaded.append((entry, list(platforms)))

        async def async_reload(self, entry_id: str) -> None:
            self.reloaded.append(entry_id)

        def async_update_entry(self, entry: Any, *, options: dict[str, Any] | None = None) -> None:
            if options is not None:
                entry.options = dict(options)

    class HomeAssistant:
        def __init__(self) -> None:
            self.bus = FakeBus()
            self.config_entries = FakeConfigEntriesManager()
            self.data: dict[str, Any] = {}

    def callback(func: Callable[..., Any]) -> Callable[..., Any]:
        return func

    core_mod.HomeAssistant = HomeAssistant
    core_mod.callback = callback

    # homeassistant.config_entries
    ce_mod = _mod("homeassistant.config_entries")
    ha.config_entries = ce_mod  # type: ignore[attr-defined]

    class AbortFlow(Exception):
        def __init__(self, reason: str) -> None:
            super().__init__(reason)
            self.reason = reason

    class ConfigEntry(Generic[T]):
        def __init__(
            self,
            *,
            entry_id: str = "test_entry_1",
            title: str = "ZJBE-TEST",
            data: dict[str, Any] | None = None,
            options: dict[str, Any] | None = None,
        ) -> None:
            self.entry_id = entry_id
            self.title = title
            self.data = data or {}
            self.options = options or {}
            self.runtime_data: Any = None
            self.unload_callbacks: list[Callable[[], None]] = []
            self.update_listeners: list[Callable[..., Any]] = []

        def async_on_unload(self, func: Callable[[], None]) -> None:
            self.unload_callbacks.append(func)

        def add_update_listener(self, listener: Callable[..., Any]) -> Callable[[], None]:
            self.update_listeners.append(listener)

            def _remove() -> None:
                if listener in self.update_listeners:
                    self.update_listeners.remove(listener)

            return _remove

    class ConfigFlow:
        def __init_subclass__(cls, *, domain: str = "", **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)
            cls.domain = domain

        def __init__(self) -> None:
            self.hass: Any = None
            self.context: dict[str, Any] = {}
            self._unique_id: str | None = None
            self._configured_ids: set[str] = set()

        async def async_set_unique_id(self, unique_id: str, **_kwargs: Any) -> None:
            self._unique_id = unique_id

        def _abort_if_unique_id_configured(self) -> None:
            if self._unique_id in self._configured_ids:
                raise AbortFlow("already_configured")

        def _async_current_ids(self) -> set[str]:
            return set(self._configured_ids)

        def _set_confirm_only(self) -> None:
            self.context["confirm_only"] = True

        def async_show_form(
            self,
            *,
            step_id: str,
            data_schema: Any = None,
            errors: dict[str, str] | None = None,
            description_placeholders: dict[str, str] | None = None,
        ) -> dict[str, Any]:
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
                "description_placeholders": description_placeholders or {},
            }

        def async_create_entry(
            self,
            *,
            title: str,
            data: dict[str, Any],
            options: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            return {
                "type": "create_entry",
                "title": title,
                "data": data,
                "options": options or {},
            }

        def async_abort(self, *, reason: str) -> dict[str, Any]:
            return {"type": "abort", "reason": reason}

    class OptionsFlow:
        def async_show_form(self, *, step_id: str, data_schema: Any = None) -> dict[str, Any]:
            return {"type": "form", "step_id": step_id, "data_schema": data_schema}

        def async_create_entry(self, *, title: str, data: dict[str, Any]) -> dict[str, Any]:
            return {"type": "create_entry", "title": title, "data": data}

    ce_mod.AbortFlow = AbortFlow
    ce_mod.ConfigEntry = ConfigEntry
    ce_mod.ConfigFlow = ConfigFlow
    ce_mod.OptionsFlow = OptionsFlow
    ce_mod.ConfigFlowResult = dict[str, Any]

    # homeassistant.components.*
    _mod("homeassistant.components")

    bt_mod = _mod("homeassistant.components.bluetooth")

    @dataclass
    class BluetoothServiceInfoBleak:
        name: str
        address: str
        rssi: int | None = -65
        connectable: bool = True

    def async_ble_device_from_address(_hass: Any, address: str, connectable: bool = True) -> Any:
        return state["ble_devices"].get(address.upper())

    def async_last_service_info(_hass: Any, address: str, connectable: bool = True) -> Any:
        return state["service_infos"].get(address.upper())

    def async_discovered_service_info(_hass: Any, connectable: bool = True) -> list[Any]:
        return [info for info in state["discovered_infos"] if (not connectable or info.connectable)]

    bt_mod.BluetoothServiceInfoBleak = BluetoothServiceInfoBleak
    bt_mod.async_ble_device_from_address = async_ble_device_from_address
    bt_mod.async_last_service_info = async_last_service_info
    bt_mod.async_discovered_service_info = async_discovered_service_info

    # homeassistant.components.diagnostics
    diag_mod = _mod("homeassistant.components.diagnostics")

    def async_redact_data(data: dict[str, Any], to_redact: set[str]) -> dict[str, Any]:
        return {k: ("**REDACTED**" if k in to_redact else v) for k, v in data.items()}

    diag_mod.async_redact_data = async_redact_data

    # homeassistant.helpers.*
    _mod("homeassistant.helpers")

    ir_mod = _mod("homeassistant.helpers.issue_registry")

    class IssueSeverity(StrEnum):
        WARNING = "warning"
        ERROR = "error"

    def async_create_issue(_hass: Any, domain: str, issue_id: str, **kwargs: Any) -> None:
        state["issues"][(domain, issue_id)] = kwargs

    def async_delete_issue(_hass: Any, domain: str, issue_id: str) -> None:
        state["issues"].pop((domain, issue_id), None)
        state["deleted_issues"].append((domain, issue_id))

    ir_mod.IssueSeverity = IssueSeverity
    ir_mod.async_create_issue = async_create_issue
    ir_mod.async_delete_issue = async_delete_issue

    dr_mod = _mod("homeassistant.helpers.device_registry")
    dr_mod.CONNECTION_BLUETOOTH = "bluetooth"

    @dataclass
    class DeviceInfo:
        connections: set[tuple[str, str]] = field(default_factory=set)
        identifiers: set[tuple[str, str]] = field(default_factory=set)
        name: str | None = None
        manufacturer: str | None = None
        model: str | None = None

    dr_mod.DeviceInfo = DeviceInfo

    ep_mod = _mod("homeassistant.helpers.entity_platform")
    ep_mod.AddEntitiesCallback = Callable[[Any], None]

    sel_helper_mod = _mod("homeassistant.helpers.selector")

    class SelectSelectorMode(StrEnum):
        LIST = "list"
        DROPDOWN = "dropdown"

    @dataclass
    class SelectSelectorConfig:
        options: list[str]
        mode: SelectSelectorMode = SelectSelectorMode.DROPDOWN
        translation_key: str | None = None

    class SelectSelector:
        def __init__(self, config: SelectSelectorConfig) -> None:
            self.config = config

        def __call__(self, value: Any) -> str:
            if value not in self.config.options:
                raise ValueError(f"Invalid option {value!r}; expected one of {self.config.options}")
            return str(value)

    sel_helper_mod.SelectSelectorMode = SelectSelectorMode
    sel_helper_mod.SelectSelectorConfig = SelectSelectorConfig
    sel_helper_mod.SelectSelector = SelectSelector

    uc_mod = _mod("homeassistant.helpers.update_coordinator")

    class UpdateFailed(Exception):
        pass

    class DataUpdateCoordinator(Generic[T]):
        def __init__(
            self,
            hass: Any,
            logger: Any,
            *,
            name: str,
            update_interval: timedelta | None = None,
        ) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data: Any = None
            self.last_update_success = True
            self.updated_callbacks: list[Callable[[], None]] = []

        async def async_config_entry_first_refresh(self) -> None:
            await self.async_refresh()

        async def async_refresh(self) -> None:
            try:
                self.data = await self._async_update_data()
                self.last_update_success = True
            except UpdateFailed:
                self.last_update_success = False
                raise

        def async_set_updated_data(self, data: T) -> None:
            self.data = data
            self.last_update_success = True
            for cb in self.updated_callbacks:
                cb()

        async def _async_update_data(self) -> T:
            raise NotImplementedError

    class CoordinatorEntity(Generic[T]):
        def __init__(self, coordinator: T) -> None:
            self.coordinator = coordinator

        @property
        def available(self) -> bool:
            return bool(getattr(self.coordinator, "last_update_success", True))

    uc_mod.UpdateFailed = UpdateFailed
    uc_mod.DataUpdateCoordinator = DataUpdateCoordinator
    uc_mod.CoordinatorEntity = CoordinatorEntity

    # Platform entity base classes and descriptions
    sensor_mod = _mod("homeassistant.components.sensor")

    class SensorDeviceClass(StrEnum):
        VOLTAGE = "voltage"
        BATTERY = "battery"
        CURRENT = "current"
        POWER = "power"
        TEMPERATURE = "temperature"
        SIGNAL_STRENGTH = "signal_strength"
        DURATION = "duration"

    class SensorStateClass(StrEnum):
        MEASUREMENT = "measurement"
        TOTAL_INCREASING = "total_increasing"

    @dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:
        key: str
        device_class: Any = None
        native_unit_of_measurement: str | None = None
        state_class: Any = None
        suggested_display_precision: int | None = None
        icon: str | None = None
        entity_category: Any = None
        entity_registry_enabled_default: bool = True

    class SensorEntity:
        pass

    sensor_mod.SensorDeviceClass = SensorDeviceClass
    sensor_mod.SensorStateClass = SensorStateClass
    sensor_mod.SensorEntityDescription = SensorEntityDescription
    sensor_mod.SensorEntity = SensorEntity

    bs_mod = _mod("homeassistant.components.binary_sensor")

    class BinarySensorDeviceClass(StrEnum):
        LIGHT = "light"
        POWER = "power"
        CONNECTIVITY = "connectivity"

    @dataclass(frozen=True, kw_only=True)
    class BinarySensorEntityDescription:
        key: str
        device_class: Any = None
        icon: str | None = None
        entity_category: Any = None

    class BinarySensorEntity:
        pass

    bs_mod.BinarySensorDeviceClass = BinarySensorDeviceClass
    bs_mod.BinarySensorEntityDescription = BinarySensorEntityDescription
    bs_mod.BinarySensorEntity = BinarySensorEntity

    sw_mod = _mod("homeassistant.components.switch")

    class SwitchEntity:
        pass

    sw_mod.SwitchEntity = SwitchEntity

    num_mod = _mod("homeassistant.components.number")

    class NumberDeviceClass(StrEnum):
        VOLTAGE = "voltage"
        TEMPERATURE_DELTA = "temperature_delta"

    class NumberMode(StrEnum):
        BOX = "box"

    @dataclass(frozen=True, kw_only=True)
    class NumberEntityDescription:
        key: str
        device_class: Any = None
        icon: str | None = None
        native_unit_of_measurement: str | None = None
        native_min_value: float | None = None
        native_max_value: float | None = None
        native_step: float | None = None
        mode: Any = None
        entity_category: Any = None

    class NumberEntity:
        pass

    num_mod.NumberDeviceClass = NumberDeviceClass
    num_mod.NumberMode = NumberMode
    num_mod.NumberEntityDescription = NumberEntityDescription
    num_mod.NumberEntity = NumberEntity

    sel_mod = _mod("homeassistant.components.select")

    @dataclass(frozen=True, kw_only=True)
    class SelectEntityDescription:
        key: str
        options: list[str] | None = None
        icon: str | None = None
        entity_category: Any = None

    class SelectEntity:
        pass

    sel_mod.SelectEntityDescription = SelectEntityDescription
    sel_mod.SelectEntity = SelectEntity

    return state
