"""Tests for all Home Assistant entity platforms, diagnostics, and translations."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.conftest import REAL_SETTINGS, REAL_TELEMETRY_12_0V, make_telemetry_frame
from tests.ha_stubs import install_ha_stubs

install_ha_stubs()

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.zhijin_ble.binary_sensor import (
    BINARY_SENSOR_DESCRIPTIONS,
)
from custom_components.zhijin_ble.binary_sensor import (
    async_setup_entry as setup_binary_sensors,
)
from custom_components.zhijin_ble.ble.const import (
    BatteryType,
    ConnectionMode,
    PowerSaveMode,
    SettingsRegister,
)
from custom_components.zhijin_ble.ble.models import ControllerState
from custom_components.zhijin_ble.ble.protocol import decode_settings, decode_telemetry
from custom_components.zhijin_ble.const import CONF_ADDRESS, CONF_NAME
from custom_components.zhijin_ble.coordinator import ZhiJinCoordinator
from custom_components.zhijin_ble.diagnostics import async_get_config_entry_diagnostics
from custom_components.zhijin_ble.number import (
    NUMBER_DESCRIPTIONS,
)
from custom_components.zhijin_ble.number import (
    async_setup_entry as setup_numbers,
)
from custom_components.zhijin_ble.select import (
    SELECT_DESCRIPTIONS,
)
from custom_components.zhijin_ble.select import (
    async_setup_entry as setup_selects,
)
from custom_components.zhijin_ble.sensor import (
    SENSOR_DESCRIPTIONS,
)
from custom_components.zhijin_ble.sensor import (
    async_setup_entry as setup_sensors,
)
from custom_components.zhijin_ble.switch import (
    async_setup_entry as setup_switches,
)

INTEGRATION_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "zhijin_ble"


def _make_populated_entry() -> tuple[HomeAssistant, ConfigEntry, ZhiJinCoordinator]:
    hass = HomeAssistant()
    entry = ConfigEntry(
        title="ZJBE-250800000ABA",
        data={CONF_ADDRESS: "25:08:00:00:0A:BA", CONF_NAME: "ZJBE-250800000ABA"},
    )
    coordinator = ZhiJinCoordinator(hass, entry)
    telem = decode_telemetry(
        make_telemetry_frame(
            voltage=13.2,
            charge_current=3.5,
            discharge_current=1.0,
            temperature=24.5,
            solar_status=1,
            load_status=1,
            wind_status=0,
            total_power_raw=250,
            total_power_resets=2,
        )[3:-2],
        battery_type=BatteryType.LITHIUM,
    )
    settings = decode_settings(REAL_SETTINGS[3:-2])
    state = ControllerState(
        telemetry=telem,
        settings=settings,
        rssi=-59,
        last_poll_ms=82.5,
        power_save_active=False,
        is_online=True,
        consecutive_failures=0,
        stats=coordinator.client.stats,
    )
    state.stats.commands_succeeded = 10
    state.stats.duplicate_frames = 12
    coordinator.data = state
    coordinator._state = state
    entry.runtime_data = coordinator
    return hass, entry, coordinator


class TestEntities:
    async def test_sensor_entities_values_icons_and_availability(self) -> None:
        hass, entry, coordinator = _make_populated_entry()
        entities = []
        await setup_sensors(hass, entry, entities.extend)

        by_key = {e.entity_description.key: e for e in entities}
        assert len(by_key) == len(SENSOR_DESCRIPTIONS)

        assert by_key["battery_voltage"].native_value == 13.2
        assert by_key["charge_current"].native_value == 3.5
        assert by_key["solar_power"].native_value == 46.2
        assert by_key["discharge_current"].native_value == 1.0
        assert by_key["load_power"].native_value == 13.2
        assert by_key["temperature"].native_value == 24.5
        assert by_key["temperature"].extra_state_attributes == {}

        # Non-zero temperature calibration offset adjusts reported value and exposes raw attrs
        coordinator.data.temperature_offset = -5.3
        assert by_key["temperature"].native_value == 19.2
        assert by_key["temperature"].extra_state_attributes == {
            "raw_temperature": 24.5,
            "temperature_offset": -5.3,
        }
        coordinator.data.temperature_offset = 0.0
        assert by_key["total_generation"].native_value == 2025.0
        assert by_key["total_generation"].extra_state_attributes == {
            "raw_counter": 250,
            "rollover_count": 2,
        }
        assert by_key["rssi"].native_value == -59
        assert by_key["poll_latency"].native_value == 82.5
        assert by_key["connection_success_rate"].native_value == 100.0

        # Healthy usable battery at 13.2V Lithium = 70% while charging -> mdi:battery-charging-70
        healthy = by_key["healthy_usable_battery"]
        assert healthy.native_value == 70.0
        assert healthy.icon == "mdi:battery-charging-70"
        assert healthy.extra_state_attributes == {"charging": True, "battery_type": "Lithium"}

        # Switch to discharging/idle at 12.0V Lithium = 0% -> mdi:battery-outline
        coordinator.data.telemetry = decode_telemetry(
            REAL_TELEMETRY_12_0V[3:-2], battery_type=BatteryType.LITHIUM
        )
        assert healthy.icon == "mdi:battery-outline"
        assert healthy.extra_state_attributes["charging"] is False

        # Test remaining dynamic healthy battery icon edge cases (0% charging, 100% charging, 100% idle, 50% idle)
        coordinator.data.telemetry = decode_telemetry(
            make_telemetry_frame(voltage=12.0, charge_current=2.0)[3:-2],
            battery_type=BatteryType.LITHIUM,
        )
        assert healthy.icon == "mdi:battery-charging-outline"

        coordinator.data.telemetry = decode_telemetry(
            make_telemetry_frame(voltage=13.5, charge_current=2.0)[3:-2],
            battery_type=BatteryType.LITHIUM,
        )
        assert healthy.icon == "mdi:battery-charging-100"

        coordinator.data.telemetry = decode_telemetry(
            make_telemetry_frame(voltage=13.5, charge_current=0.0)[3:-2],
            battery_type=BatteryType.LITHIUM,
        )
        assert healthy.icon == "mdi:battery"

        coordinator.data.telemetry = decode_telemetry(
            make_telemetry_frame(voltage=13.1, charge_current=0.0)[3:-2],
            battery_type=BatteryType.LITHIUM,
        )
        assert healthy.icon == "mdi:battery-50"

        # When coordinator fails, telemetry sensors become unavailable but RSSI stays available
        coordinator.last_update_success = False
        assert by_key["battery_voltage"].available is False
        assert by_key["rssi"].available is True

    async def test_binary_sensor_entities(self) -> None:
        hass, entry, coordinator = _make_populated_entry()
        entities = []
        await setup_binary_sensors(hass, entry, entities.extend)

        by_key = {e.entity_description.key: e for e in entities}
        assert len(by_key) == len(BINARY_SENSOR_DESCRIPTIONS)

        assert by_key["solar_status"].is_on is True
        assert by_key["load_status"].is_on is True
        assert by_key["wind_status"].is_on is False
        assert by_key["connectivity"].is_on is True
        assert by_key["connectivity"].extra_state_attributes["duplicate_frames_absorbed"] == 12
        assert by_key["power_save_active"].is_on is False

        # Connectivity stays available when offline so UI explicitly displays Disconnected
        coordinator.last_update_success = False
        coordinator.data.is_online = False
        assert by_key["connectivity"].available is True
        assert by_key["connectivity"].is_on is False
        assert by_key["solar_status"].available is False

    async def test_switch_number_and_select_writes(self) -> None:
        hass, entry, coordinator = _make_populated_entry()
        coordinator.async_write_register = AsyncMock()
        coordinator.async_write_voltage = AsyncMock()

        # Switch
        switches = []
        await setup_switches(hass, entry, switches.extend)
        assert len(switches) == 1
        assert switches[0].is_on is True
        await switches[0].async_turn_off()
        coordinator.async_write_register.assert_awaited_with(SettingsRegister.LOAD_SWITCH, 0)
        await switches[0].async_turn_on()
        coordinator.async_write_register.assert_awaited_with(SettingsRegister.LOAD_SWITCH, 1)

        # Numbers
        numbers = []
        await setup_numbers(hass, entry, numbers.extend)
        num_by_key = {e.entity_description.key: e for e in numbers}
        assert len(num_by_key) == len(NUMBER_DESCRIPTIONS)
        assert num_by_key["full_voltage"].native_value == 12.6
        assert num_by_key["cutoff_voltage"].native_value == 10.0
        assert num_by_key["recovery_voltage"].native_value == 11.0
        await num_by_key["cutoff_voltage"].async_set_native_value(11.3)
        coordinator.async_write_voltage.assert_awaited_once_with(
            SettingsRegister.CUTOFF_VOLTAGE, 11.3
        )

        # Selects
        selects = []
        await setup_selects(hass, entry, selects.extend)
        sel_by_key = {e.entity_description.key: e for e in selects}
        assert len(sel_by_key) == len(SELECT_DESCRIPTIONS)
        assert sel_by_key["battery_type"].current_option == "Lithium"
        assert sel_by_key["output_mode"].current_option == "Manual"
        assert sel_by_key["voltage_detect_mode"].current_option == "Auto"

        await sel_by_key["battery_type"].async_select_option("Gel")
        coordinator.async_write_register.assert_awaited_with(SettingsRegister.BATTERY_TYPE, 2)
        await sel_by_key["output_mode"].async_select_option("Auto")
        coordinator.async_write_register.assert_awaited_with(SettingsRegister.OUTPUT_MODE, 1)

        with pytest.raises(ValueError):
            await sel_by_key["battery_type"].async_select_option("InvalidChemistry")

    async def test_diagnostics_redacts_bluetooth_address(self) -> None:
        hass, entry, _coordinator = _make_populated_entry()
        diag = await async_get_config_entry_diagnostics(hass, entry)

        assert diag["entry"]["data"][CONF_ADDRESS] == "**REDACTED**"
        assert diag["state"]["telemetry"]["battery_voltage"] == 13.2
        assert diag["state"]["stats"]["duplicate_frames"] == 12
        assert diag["scheduler"]["poll_interval"] == 60


class TestManifestAndTranslationsIntegrity:
    """Verify manifest.json, hacs.json, strings.json, and English translations stay in sync."""

    def test_manifest_and_hacs_are_valid_json(self) -> None:
        manifest = json.loads((INTEGRATION_DIR / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["domain"] == "zhijin_ble"
        assert manifest["name"] == "ZhiJin BLE"
        assert manifest["config_flow"] is True
        assert any(rule.get("local_name") == "ZJ*" for rule in manifest["bluetooth"])

        hacs = json.loads((INTEGRATION_DIR.parent.parent / "hacs.json").read_text(encoding="utf-8"))
        assert hacs["name"] == "ZhiJin BLE"

    def test_only_english_translation_exists_and_matches_all_entity_keys(self) -> None:
        trans_dir = INTEGRATION_DIR / "translations"
        translation_files = sorted(p.name for p in trans_dir.glob("*.json"))
        assert translation_files == ["en.json"]

        strings = json.loads((INTEGRATION_DIR / "strings.json").read_text(encoding="utf-8"))
        en = json.loads((trans_dir / "en.json").read_text(encoding="utf-8"))
        assert strings == en

        entities = en["entity"]
        for desc in SENSOR_DESCRIPTIONS:
            assert desc.key in entities["sensor"]
        for desc in BINARY_SENSOR_DESCRIPTIONS:
            assert desc.key in entities["binary_sensor"]
        for desc in NUMBER_DESCRIPTIONS:
            assert desc.key in entities["number"]
        for desc in SELECT_DESCRIPTIONS:
            assert desc.key in entities["select"]
        assert "load_switch" in entities["switch"]
        assert "device_offline" in en["issues"]

        # Selector option descriptions cover every ConnectionMode and PowerSaveMode value
        selectors = en["selector"]
        for mode in ConnectionMode:
            assert mode.value in selectors["connection_mode"]["options"]
        for ps_mode in PowerSaveMode:
            assert ps_mode.value in selectors["power_save_mode"]["options"]
