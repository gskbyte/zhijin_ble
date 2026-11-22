"""Unit tests for standalone BLE discovery and macOS/Linux address matching."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bleak.backends.device import BLEDevice

from custom_components.zhijin_ble.ble.const import SERVICE_UUID
from custom_components.zhijin_ble.ble.scanner import (
    find_controller,
    is_zhijin_advertisement,
    scan_controllers,
)


def _adv(local_name: str | None = None, rssi: int | None = -65, service_uuids: list[str] | None = None):
    return SimpleNamespace(
        local_name=local_name,
        rssi=rssi,
        service_uuids=service_uuids or [],
    )


class TestScanner:
    def test_is_zhijin_advertisement_matching(self) -> None:
        dev_zj = BLEDevice("25:08:00:00:0A:BA", "ZJBE-250800000ABA", details={})
        dev_other = BLEDevice("AA:BB:CC:DD:EE:FF", "OtherSensor", details={})

        assert is_zhijin_advertisement(dev_zj, _adv("ZJBE-250800000ABA")) is True
        assert is_zhijin_advertisement(dev_other, _adv("OtherSensor")) is False
        assert (
            is_zhijin_advertisement(
                dev_other,
                _adv("OtherSensor", service_uuids=[SERVICE_UUID.upper()]),
                match_service_uuid=True,
            )
            is True
        )
        assert (
            is_zhijin_advertisement(
                dev_other,
                _adv("OtherSensor", service_uuids=[SERVICE_UUID]),
                match_service_uuid=False,
            )
            is False
        )

    async def test_scan_controllers_filters_and_sorts_by_rssi(self) -> None:
        d1 = BLEDevice("UUID-WEAK", "ZJBE-WEAK", details={})
        d2 = BLEDevice("UUID-STRONG", "ZJBE-STRONG", details={})
        d3 = BLEDevice("UUID-OTHER", "RandomDevice", details={})
        d4 = BLEDevice("UUID-NUS", "GenericUART", details={})

        discovered = {
            d1.address: (d1, _adv("ZJBE-WEAK", rssi=-82)),
            d2.address: (d2, _adv("ZJBE-STRONG", rssi=-54, service_uuids=[SERVICE_UUID])),
            d3.address: (d3, _adv("RandomDevice", rssi=-40)),
            d4.address: (d4, _adv("GenericUART", rssi=-50, service_uuids=[SERVICE_UUID])),
        }

        with patch("bleak.BleakScanner.discover", new=AsyncMock(return_value=discovered)):
            default_res = await scan_controllers(timeout=2.0)
            assert [c.address for c in default_res] == ["UUID-STRONG", "UUID-WEAK"]

            all_res = await scan_controllers(timeout=2.0, include_service_uuid_match=True)
            assert [c.address for c in all_res] == ["UUID-NUS", "UUID-STRONG", "UUID-WEAK"]

    async def test_find_controller_by_address_name_and_auto(self) -> None:
        d1 = BLEDevice("11111111-2222-3333-4444-555555555555", "ZJBE-MACOS", details={})
        d2 = BLEDevice("25:08:00:00:0A:BA", "ZJBE-250800000ABA", details={})
        discovered = {
            d1.address: (d1, _adv("ZJBE-MACOS", rssi=-60)),
            d2.address: (d2, _adv("ZJBE-250800000ABA", rssi=-75)),
        }

        with patch("bleak.BleakScanner.discover", new=AsyncMock(return_value=discovered)):
            # Auto picks strongest RSSI
            auto = await find_controller()
            assert auto is not None and auto.address == d1.address

            # Case-insensitive address lookup
            by_addr = await find_controller(address="25:08:00:00:0a:ba")
            assert by_addr is not None and by_addr.name == "ZJBE-250800000ABA"

            # Substring name lookup
            by_name = await find_controller(name="macos")
            assert by_name is not None and by_name.address == d1.address

            # Missing target returns None
            assert await find_controller(address="FF:FF:FF:FF:FF:FF") is None
