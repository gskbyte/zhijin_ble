"""Tests for ZhiJinCoordinator and integration setup/unload lifecycle."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from bleak.backends.device import BLEDevice

from tests.conftest import (
    REAL_SETTINGS,
    REAL_TELEMETRY_12_0V,
    FakeClock,
    make_settings_frame,
    make_telemetry_frame,
)
from tests.ha_stubs import install_ha_stubs

ha_state = install_ha_stubs()

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.zhijin_ble import (
    _async_reload_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.zhijin_ble.ble.const import (
    ConnectionMode,
    PowerSaveMode,
    SettingsRegister,
)
from custom_components.zhijin_ble.ble.exceptions import ZhiJinTimeoutError
from custom_components.zhijin_ble.ble.protocol import decode_settings, decode_telemetry
from custom_components.zhijin_ble.const import (
    CONF_ADDRESS,
    CONF_CONNECTION_MODE,
    CONF_NAME,
    CONF_OFFLINE_ISSUE_SECONDS,
    CONF_POLL_INTERVAL,
    CONF_POWER_SAVE_INTERVAL,
    CONF_POWER_SAVE_MODE,
    CONF_TEMPERATURE_OFFSET,
    CONF_UNAVAILABLE_AFTER_FAILURES,
    DOMAIN,
    offline_issue_id,
)
from custom_components.zhijin_ble.coordinator import ZhiJinCoordinator


@pytest.fixture(autouse=True)
def _reset_ha_stubs():
    return install_ha_stubs()


def _make_entry(**options) -> ConfigEntry:
    return ConfigEntry(
        entry_id="entry_1",
        title="ZJBE-SOLAR",
        data={CONF_ADDRESS: "25:08:00:00:0A:BA", CONF_NAME: "ZJBE-SOLAR"},
        options=options,
    )


class TestZhiJinCoordinator:
    async def test_poll_updates_state_rssi_and_switches_to_night_power_save(self) -> None:
        hass = HomeAssistant()
        entry = _make_entry(
            **{
                CONF_CONNECTION_MODE: ConnectionMode.ON_DEMAND.value,
                CONF_POLL_INTERVAL: 60,
                CONF_POWER_SAVE_MODE: PowerSaveMode.NIGHT_IDLE.value,
                CONF_POWER_SAVE_INTERVAL: 300,
                CONF_TEMPERATURE_OFFSET: -5.3,
            }
        )
        ha_state["service_infos"]["25:08:00:00:0A:BA"] = BluetoothServiceInfoBleak(
            name="ZJBE-SOLAR", address="25:08:00:00:0A:BA", rssi=-61
        )

        night_idle_telem = decode_telemetry(
            make_telemetry_frame(solar_status=0, charge_current=0.0, discharge_current=0.0)[3:-2],
            battery_type=1,
        )
        day_telem = decode_telemetry(REAL_TELEMETRY_12_0V[3:-2], battery_type=1)
        settings = decode_settings(REAL_SETTINGS[3:-2])

        coordinator = ZhiJinCoordinator(hass, entry)
        coordinator.client.poll = AsyncMock(
            side_effect=[(day_telem, settings), (night_idle_telem, None)]
        )

        # First poll: daytime -> stays at 60s
        await coordinator.async_refresh()
        assert coordinator.data.telemetry == day_telem
        assert coordinator.data.temperature_offset == -5.3
        assert coordinator.data.calibrated_temperature == 7.9
        assert coordinator.data.settings == settings
        assert coordinator.data.rssi == -61
        assert coordinator.data.is_online is True
        assert coordinator.data.power_save_active is False
        assert coordinator.update_interval == timedelta(seconds=60)

        # Second poll: night & idle -> slows down to 300s power-save interval
        await coordinator.async_refresh()
        assert coordinator.data.power_save_active is True
        assert coordinator.update_interval == timedelta(seconds=300)

    async def test_transient_failure_tolerance_and_offline_repairs_lifecycle(self) -> None:
        hass = HomeAssistant()
        clock = FakeClock(500.0)
        entry = _make_entry(
            **{
                CONF_POLL_INTERVAL: 60,
                CONF_UNAVAILABLE_AFTER_FAILURES: 2,
                CONF_OFFLINE_ISSUE_SECONDS: 120,
            }
        )
        coordinator = ZhiJinCoordinator(hass, entry)
        coordinator.scheduler._clock = clock

        telem = decode_telemetry(REAL_TELEMETRY_12_0V[3:-2], battery_type=1)
        settings = decode_settings(REAL_SETTINGS[3:-2])
        coordinator.client.poll = AsyncMock(return_value=(telem, settings))
        await coordinator.async_refresh()
        assert coordinator.data.is_online is True

        # Failure #1 (transient): preserves cached telemetry, marks connectivity offline
        coordinator.client.poll = AsyncMock(side_effect=ZhiJinTimeoutError("RF hiccup"))
        await coordinator.async_refresh()
        assert coordinator.last_update_success is True
        assert coordinator.data.is_online is False
        assert coordinator.data.consecutive_failures == 1
        assert coordinator.data.telemetry == telem
        assert (DOMAIN, offline_issue_id(coordinator.address)) not in ha_state["issues"]

        # Failure #2 after 120s: raises UpdateFailed and creates Repairs issue
        clock.advance(120.0)
        with pytest.raises(UpdateFailed):
            await coordinator.async_refresh()
        assert coordinator.last_update_success is False
        assert (DOMAIN, offline_issue_id(coordinator.address)) in ha_state["issues"]

        # Recovery clears the Repairs issue automatically
        coordinator.client.poll = AsyncMock(return_value=(telem, None))
        await coordinator.async_refresh()
        assert coordinator.data.is_online is True
        assert coordinator.data.consecutive_failures == 0
        assert (DOMAIN, offline_issue_id(coordinator.address)) not in ha_state["issues"]

    async def test_async_write_register_and_voltage_update_state_and_wrap_errors(self) -> None:
        hass = HomeAssistant()
        entry = _make_entry()
        coordinator = ZhiJinCoordinator(hass, entry)

        telem = decode_telemetry(REAL_TELEMETRY_12_0V[3:-2], battery_type=1)
        coordinator._state.telemetry = telem
        updated_settings = decode_settings(make_settings_frame(battery_type=3, cutoff_voltage=11.3)[3:-2])

        coordinator.client.write_register = AsyncMock(return_value=updated_settings)
        await coordinator.async_write_voltage(SettingsRegister.CUTOFF_VOLTAGE, 11.3)

        coordinator.client.write_register.assert_awaited_once_with(
            int(SettingsRegister.CUTOFF_VOLTAGE),
            113,
            refresh_settings=True,
        )
        assert coordinator.data.settings == updated_settings
        # Healthy battery recalculated for newly fetched battery_type=3 (Lead-Acid @ 12.0V -> 0.0%)
        assert coordinator.data.telemetry is not None
        assert coordinator.data.is_online is True

        # BLE failure raises HomeAssistantError for UI toast notification
        coordinator.client.write_register = AsyncMock(side_effect=ZhiJinTimeoutError("offline"))
        with pytest.raises(HomeAssistantError):
            await coordinator.async_write_register(SettingsRegister.LOAD_SWITCH, 1)

    async def test_setup_stop_listener_and_unload_entry(self) -> None:
        hass = HomeAssistant()
        entry = _make_entry()
        ha_state["ble_devices"]["25:08:00:00:0A:BA"] = BLEDevice(
            "25:08:00:00:0A:BA", "ZJBE-SOLAR", details={}
        )

        telem = decode_telemetry(REAL_TELEMETRY_12_0V[3:-2], battery_type=1)
        settings = decode_settings(REAL_SETTINGS[3:-2])

        with (
            patch(
                "custom_components.zhijin_ble.ble.client.ZhiJinBLEClient.poll",
                new=AsyncMock(return_value=(telem, settings)),
            ),
            patch(
                "custom_components.zhijin_ble.ble.client.ZhiJinBLEClient.disconnect",
                new=AsyncMock(),
            ) as mock_disc,
        ):
            assert await async_setup_entry(hass, entry) is True
            assert isinstance(entry.runtime_data, ZhiJinCoordinator)
            assert len(hass.config_entries.forwarded) == 1
            assert EVENT_HOMEASSISTANT_STOP in hass.bus.listeners

            # Triggering HA stop listener disconnects BLE cleanly
            await hass.bus.listeners[EVENT_HOMEASSISTANT_STOP][0](None)
            assert mock_disc.await_count == 1

            # Unload entry disconnects BLE and deletes any offline issue
            assert await async_unload_entry(hass, entry) is True
            assert len(hass.config_entries.unloaded) == 1
            assert mock_disc.await_count == 2
            assert (DOMAIN, offline_issue_id("25:08:00:00:0A:BA")) in ha_state["deleted_issues"]

    async def test_unload_entry_returns_false_when_platform_unload_fails(self) -> None:
        hass = HomeAssistant()
        entry = _make_entry()
        coordinator = ZhiJinCoordinator(hass, entry)
        entry.runtime_data = coordinator

        with (
            patch.object(
                hass.config_entries,
                "async_unload_platforms",
                new=AsyncMock(return_value=False),
            ),
            patch.object(coordinator, "async_shutdown_ble", new=AsyncMock()) as mock_shutdown,
        ):
            assert await async_unload_entry(hass, entry) is False
            mock_shutdown.assert_not_awaited()
            assert ha_state["deleted_issues"] == []

    async def test_temperature_offset_updates_live_without_reloading_ble(self) -> None:
        hass = HomeAssistant()
        entry = _make_entry()
        coordinator = ZhiJinCoordinator(hass, entry)
        entry.runtime_data = coordinator
        coordinator._state.telemetry = decode_telemetry(REAL_TELEMETRY_12_0V[3:-2], battery_type=1)

        # Updating offset persists to entry.options and updates calibrated temperature immediately
        await coordinator.async_set_temperature_offset(-5.3)
        assert coordinator.temperature_offset == -5.3
        assert coordinator.data.calibrated_temperature == 7.9
        assert entry.options[CONF_TEMPERATURE_OFFSET] == -5.3

        # Options update listener applies temperature_offset live without triggering full reload
        entry.options = {**entry.options, CONF_TEMPERATURE_OFFSET: -4.2}
        await _async_reload_entry(hass, entry)
        assert coordinator.temperature_offset == -4.2
        assert hass.config_entries.reloaded == []

        # Changing a BLE/polling option triggers a full reload
        entry.options = {**entry.options, CONF_POLL_INTERVAL: 120}
        await _async_reload_entry(hass, entry)
        assert hass.config_entries.reloaded == [entry.entry_id]
