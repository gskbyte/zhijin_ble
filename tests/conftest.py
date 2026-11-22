"""Shared test fixtures, real hardware frames, and fake BLE transport."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from bleak.backends.device import BLEDevice

from custom_components.zhijin_ble.ble.const import (
    DEFAULT_UNIT_ID,
    EXCEPTION_FLAG,
    FUNC_READ_HOLDING,
    FUNC_WRITE_MULTIPLE,
    NOTIFY_UUID,
    WRITE_UUID,
)
from custom_components.zhijin_ble.ble.protocol import crc16

# Real hardware frames captured from controller 25:08:00:00:0A:BA
REAL_TELEMETRY_12_0V_HEX = "01031400010078000000000528000100010000005200000daf"
REAL_TELEMETRY_12_1V_HEX = "01031400010079000000000528000100010000005200005c3f"
REAL_SETTINGS_HEX = "010312000100000000007e0000006400010000006ea4d1"

REAL_TELEMETRY_12_0V = bytes.fromhex(REAL_TELEMETRY_12_0V_HEX)
REAL_TELEMETRY_12_1V = bytes.fromhex(REAL_TELEMETRY_12_1V_HEX)
REAL_SETTINGS = bytes.fromhex(REAL_SETTINGS_HEX)


def make_telemetry_frame(
    *,
    unit_id: int = DEFAULT_UNIT_ID,
    model_type: int = 1,
    voltage: float = 12.8,
    charge_current: float = 4.5,
    discharge_current: float = 1.2,
    temperature: float = 25.5,
    solar_status: int = 1,
    load_status: int = 1,
    wind_status: int = 0,
    total_power_raw: int = 123,
    total_power_resets: int = 0,
) -> bytes:
    """Build a valid CRC-protected telemetry response frame (25 bytes)."""
    payload = struct.pack(
        ">HHHHhHHHHH",
        model_type,
        round(voltage * 10),
        round(charge_current * 10),
        round(discharge_current * 10),
        round(temperature * 100),
        solar_status,
        load_status,
        wind_status,
        total_power_raw,
        total_power_resets,
    )
    body = struct.pack(">BBB", unit_id, FUNC_READ_HOLDING, len(payload)) + payload
    return body + crc16(body)


def make_settings_frame(
    *,
    unit_id: int = DEFAULT_UNIT_ID,
    battery_type: int = 1,
    time_hour: int = 6,
    time_min: int = 30,
    full_voltage: float = 14.4,
    output_mode: int = 0,
    cutoff_voltage: float = 10.5,
    load_switch: int = 1,
    voltage_detect_mode: int = 0,
    recovery_voltage: float = 12.2,
) -> bytes:
    """Build a valid CRC-protected settings response frame (23 bytes)."""
    payload = struct.pack(
        ">HHHHHHHHH",
        battery_type,
        time_hour,
        time_min,
        round(full_voltage * 10),
        output_mode,
        round(cutoff_voltage * 10),
        load_switch,
        voltage_detect_mode,
        round(recovery_voltage * 10),
    )
    body = struct.pack(">BBB", unit_id, FUNC_READ_HOLDING, len(payload)) + payload
    return body + crc16(body)


def make_write_ack_frame(
    register: int,
    count: int = 1,
    *,
    unit_id: int = DEFAULT_UNIT_ID,
    func: int = FUNC_WRITE_MULTIPLE,
) -> bytes:
    """Build a valid CRC-protected write ACK frame (8 bytes)."""
    body = struct.pack(">BBHH", unit_id, func, register, count)
    return body + crc16(body)


def make_exception_frame(
    func: int,
    exc_code: int,
    *,
    unit_id: int = DEFAULT_UNIT_ID,
) -> bytes:
    """Build a valid CRC-protected Modbus exception frame (5 bytes)."""
    body = struct.pack(">BBB", unit_id, func | EXCEPTION_FLAG, exc_code)
    return body + crc16(body)


class FakeClock:
    """Controllable monotonic clock for deterministic timing tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@dataclass
class FakeCharacteristic:
    uuid: str
    properties: list[str]


@dataclass
class FakeServiceCollection:
    characteristics: dict[str, FakeCharacteristic] = field(default_factory=dict)

    def get_characteristic(self, uuid: str) -> FakeCharacteristic | None:
        return self.characteristics.get(uuid.lower())


class FakeBleakClient:
    """In-memory mock BleakClient supporting chunking, duplicate bursts, and drops."""

    def __init__(
        self,
        address: str = "25:08:00:00:0A:BA",
        *,
        disconnected_callback: Callable[[Any], None] | None = None,
        write_properties: list[str] | None = None,
        on_write: Callable[[FakeBleakClient, bytes, bool], Any] | None = None,
    ) -> None:
        self.address = address
        self.is_connected = True
        self.mtu_size = 247
        self._disconnected_callback = disconnected_callback
        self._notify_callback: Callable[[Any, bytearray], None] | None = None
        self._on_write = on_write
        self.writes: list[tuple[bytes, bool]] = []
        self.start_notify_calls = 0
        self.stop_notify_calls = 0
        self.disconnect_calls = 0

        props = (
            write_properties
            if write_properties is not None
            else ["write-without-response", "write"]
        )
        self.services = FakeServiceCollection(
            characteristics={
                NOTIFY_UUID: FakeCharacteristic(NOTIFY_UUID, ["notify"]),
                WRITE_UUID: FakeCharacteristic(WRITE_UUID, props),
            }
        )

    async def start_notify(self, uuid: str, callback: Callable[[Any, bytearray], None]) -> None:
        assert uuid.lower() == NOTIFY_UUID
        self.start_notify_calls += 1
        self._notify_callback = callback

    async def stop_notify(self, uuid: str) -> None:
        assert uuid.lower() == NOTIFY_UUID
        self.stop_notify_calls += 1
        self._notify_callback = None

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        assert uuid.lower() == WRITE_UUID
        self.writes.append((bytes(data), response))
        if self._on_write is not None:
            result = self._on_write(self, bytes(data), response)
            if asyncio.iscoroutine(result):
                await result

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False

    def emit_notify(self, data: bytes) -> None:
        """Deliver a notification chunk to the subscribed callback."""
        if self._notify_callback is not None:
            self._notify_callback(NOTIFY_UUID, bytearray(data))

    def schedule_notify(self, data: bytes, delay: float = 0.0) -> None:
        """Schedule a notification chunk asynchronously on the event loop."""
        loop = asyncio.get_running_loop()
        if delay <= 0:
            loop.call_soon(self.emit_notify, data)
        else:
            loop.call_later(delay, self.emit_notify, data)

    def simulate_disconnect(self) -> None:
        """Simulate an unexpected peripheral disconnect."""
        self.is_connected = False
        if self._disconnected_callback is not None:
            self._disconnected_callback(self)


@pytest.fixture
def sample_ble_device() -> BLEDevice:
    """Return a synthetic BLEDevice for testing."""
    return BLEDevice("25:08:00:00:0A:BA", "ZJBE-250800000ABA", details={})
