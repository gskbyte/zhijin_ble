"""Tests for ZhiJin BLE ConfigFlow and OptionsFlow."""

from __future__ import annotations

import pytest

from tests.ha_stubs import install_ha_stubs

ha_state = install_ha_stubs()

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import AbortFlow, ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.zhijin_ble.ble.const import ConnectionMode, PowerSaveMode
from custom_components.zhijin_ble.config_flow import (
    MANUAL_ADDRESS_OPTION,
    ZhiJinConfigFlow,
)
from custom_components.zhijin_ble.const import (
    CONF_ADDRESS,
    CONF_COMMAND_RETRIES,
    CONF_COMMAND_TIMEOUT,
    CONF_CONNECTION_MODE,
    CONF_NAME,
    CONF_OFFLINE_ISSUE_SECONDS,
    CONF_POLL_INTERVAL,
    CONF_POWER_SAVE_INTERVAL,
    CONF_POWER_SAVE_MODE,
    CONF_SETTINGS_INTERVAL,
    CONF_TEMPERATURE_OFFSET,
    CONF_UNAVAILABLE_AFTER_FAILURES,
)


@pytest.fixture(autouse=True)
def _reset_ha_stubs():
    return install_ha_stubs()


def _flow(configured_ids: set[str] | None = None) -> ZhiJinConfigFlow:
    flow = ZhiJinConfigFlow()
    flow.hass = HomeAssistant()
    flow._configured_ids = configured_ids or set()
    return flow


class TestZhiJinConfigFlow:
    async def test_bluetooth_auto_discovery_and_confirm(self) -> None:
        flow = _flow()
        info = BluetoothServiceInfoBleak(name="ZJBE-250800000ABA", address="25:08:00:00:0a:ba", rssi=-60)

        step = await flow.async_step_bluetooth(info)
        assert step["type"] == "form"
        assert step["step_id"] == "bluetooth_confirm"
        assert flow.context["title_placeholders"] == {"name": "ZJBE-250800000ABA"}

        validated = step["data_schema"](
            {
                CONF_CONNECTION_MODE: ConnectionMode.ON_DEMAND.value,
                CONF_POLL_INTERVAL: 120,
                CONF_POWER_SAVE_MODE: PowerSaveMode.NIGHT_IDLE.value,
                CONF_POWER_SAVE_INTERVAL: 600,
            }
        )
        result = await flow.async_step_bluetooth_confirm(validated)
        assert result["type"] == "create_entry"
        assert result["title"] == "ZJBE-250800000ABA"
        assert result["data"] == {
            CONF_ADDRESS: "25:08:00:00:0A:BA",
            CONF_NAME: "ZJBE-250800000ABA",
        }
        assert result["options"][CONF_POLL_INTERVAL] == 120
        assert result["options"][CONF_POWER_SAVE_INTERVAL] == 600

    async def test_bluetooth_discovery_aborts_if_already_configured(self) -> None:
        flow = _flow(configured_ids={"25:08:00:00:0A:BA"})
        info = BluetoothServiceInfoBleak(name="ZJBE-250800000ABA", address="25:08:00:00:0a:ba")
        with pytest.raises(AbortFlow, match="already_configured"):
            await flow.async_step_bluetooth(info)

    async def test_user_flow_lists_connectable_zj_devices_sorted_by_rssi(self) -> None:
        ha_state["discovered_infos"].extend(
            [
                BluetoothServiceInfoBleak(name="ZJBE-WEAK", address="25:08:00:00:00:01", rssi=-85),
                BluetoothServiceInfoBleak(name="ZJBE-STRONG", address="25:08:00:00:00:02", rssi=-52),
                BluetoothServiceInfoBleak(name="OtherBLE", address="AA:BB:CC:DD:EE:01", rssi=-40),
                BluetoothServiceInfoBleak(
                    name="ZJBE-PASSIVE", address="25:08:00:00:00:03", rssi=-45, connectable=False
                ),
            ]
        )

        flow = _flow()
        form = await flow.async_step_user()
        assert form["type"] == "form"
        assert form["step_id"] == "user"

        validated = form["data_schema"](
            {
                CONF_ADDRESS: "25:08:00:00:00:02",
                CONF_CONNECTION_MODE: ConnectionMode.PERSISTENT.value,
                CONF_POLL_INTERVAL: 30,
                CONF_POWER_SAVE_MODE: PowerSaveMode.OFF.value,
                CONF_POWER_SAVE_INTERVAL: 300,
            }
        )
        result = await flow.async_step_user(validated)
        assert result["type"] == "create_entry"
        assert result["title"] == "ZJBE-STRONG"
        assert result["options"][CONF_CONNECTION_MODE] == "persistent"

    async def test_user_flow_falls_back_to_manual_when_none_found_or_selected(self) -> None:
        # No devices discovered -> directly opens manual form
        flow = _flow()
        form = await flow.async_step_user()
        assert form["step_id"] == "manual"

        # Invalid short address returns error
        err_form = await flow.async_step_manual(
            {
                CONF_ADDRESS: "123",
                CONF_NAME: "",
                CONF_CONNECTION_MODE: ConnectionMode.ON_DEMAND.value,
                CONF_POLL_INTERVAL: 60,
                CONF_POWER_SAVE_MODE: PowerSaveMode.IDLE.value,
                CONF_POWER_SAVE_INTERVAL: 300,
            }
        )
        assert err_form["errors"] == {CONF_ADDRESS: "invalid_address"}

        # Valid manual address creates entry
        created = await flow.async_step_manual(
            {
                CONF_ADDRESS: "25:08:00:00:0a:ba",
                CONF_NAME: "Cabin Solar",
                CONF_CONNECTION_MODE: ConnectionMode.ON_DEMAND.value,
                CONF_POLL_INTERVAL: 90,
                CONF_POWER_SAVE_MODE: PowerSaveMode.IDLE.value,
                CONF_POWER_SAVE_INTERVAL: 600,
            }
        )
        assert created["type"] == "create_entry"
        assert created["title"] == "Cabin Solar"
        assert created["data"][CONF_ADDRESS] == "25:08:00:00:0A:BA"

        # Selecting manual choice from populated user form also routes to manual step
        ha_state["discovered_infos"].append(
            BluetoothServiceInfoBleak(name="ZJBE-1", address="25:08:00:00:00:01", rssi=-60)
        )
        flow2 = _flow()
        await flow2.async_step_user()
        manual_step = await flow2.async_step_user({CONF_ADDRESS: MANUAL_ADDRESS_OPTION})
        assert manual_step["step_id"] == "manual"


class TestZhiJinOptionsFlow:
    async def test_options_flow_validates_and_saves_tuning_parameters(self) -> None:
        entry = ConfigEntry(
            data={CONF_ADDRESS: "25:08:00:00:0A:BA"},
            options={CONF_POLL_INTERVAL: 60},
        )
        opt_flow = ZhiJinConfigFlow.async_get_options_flow(entry)
        form = await opt_flow.async_step_init()
        assert form["type"] == "form"
        assert form["step_id"] == "init"

        payload = form["data_schema"](
            {
                CONF_CONNECTION_MODE: ConnectionMode.ON_DEMAND.value,
                CONF_POLL_INTERVAL: 120,
                CONF_POWER_SAVE_MODE: PowerSaveMode.NIGHT.value,
                CONF_POWER_SAVE_INTERVAL: 600,
                CONF_SETTINGS_INTERVAL: 1200,
                CONF_COMMAND_TIMEOUT: 6.0,
                CONF_COMMAND_RETRIES: 3,
                CONF_UNAVAILABLE_AFTER_FAILURES: 3,
                CONF_OFFLINE_ISSUE_SECONDS: 300,
                CONF_TEMPERATURE_OFFSET: -5.3,
            }
        )
        saved = await opt_flow.async_step_init(payload)
        assert saved["type"] == "create_entry"
        assert saved["data"][CONF_POLL_INTERVAL] == 120
        assert saved["data"][CONF_COMMAND_RETRIES] == 3
        assert saved["data"][CONF_SETTINGS_INTERVAL] == 1200
        assert saved["data"][CONF_TEMPERATURE_OFFSET] == -5.3
