"""Pure-Python Modbus RTU framing, CRC-16, stream parser, and register codecs.

This module performs all binary encoding/decoding for ZhiJin solar controllers
without any BLE transport or Home Assistant dependencies.
"""

from __future__ import annotations

import struct
from dataclasses import replace
from itertools import pairwise

from .const import (
    DEFAULT_MAX_BUFFER_BYTES,
    DEFAULT_UNIT_ID,
    EXCEPTION_FLAG,
    FUNC_READ_HOLDING,
    FUNC_WRITE_MULTIPLE,
    FUNC_WRITE_SINGLE,
    HEALTHY_BATTERY_CURVE_LITHIUM_3S,
    HEALTHY_BATTERY_CURVES,
    LITHIUM_3S_FULL_VOLTAGE_THRESHOLD,
    REG_SETTINGS_REQUEST_COUNT,
    REG_SETTINGS_START,
    REG_TELEMETRY_REQUEST_COUNT,
    REG_TELEMETRY_START,
    SETTINGS_RESPONSE_BYTES,
    SYSTEM_24V_THRESHOLD,
    TELEMETRY_RESPONSE_BYTES,
    VALID_FUNCTIONS,
    BatteryType,
)
from .exceptions import ZhiJinProtocolError
from .models import ModbusFrame, SettingsData, TelemetryData

# Maximum legal Modbus RTU data payload byte count for function 0x03 (125 registers).
_MAX_READ_BYTE_COUNT = 250


def crc16(data: bytes | bytearray | memoryview) -> bytes:
    """Compute little-endian Modbus RTU CRC-16 (polynomial 0xA001, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack("<H", crc)


def verify_crc(frame: bytes | bytearray | memoryview) -> bool:
    """Return True if frame is at least 4 bytes long and its trailing CRC-16 matches."""
    if len(frame) < 4:
        return False
    return crc16(frame[:-2]) == bytes(frame[-2:])


def build_read_holding(
    start_reg: int,
    count: int,
    *,
    unit_id: int = DEFAULT_UNIT_ID,
) -> bytes:
    """Build a Modbus function 0x03 (Read Holding Registers) request frame."""
    if not 0 <= unit_id <= 0xFF:
        raise ValueError(f"unit_id out of range: {unit_id}")
    if not 0 <= start_reg <= 0xFFFF:
        raise ValueError(f"start_reg out of range: {start_reg}")
    if not 1 <= count <= 125:
        raise ValueError(f"count out of range: {count}")

    body = struct.pack(">BBHH", unit_id, FUNC_READ_HOLDING, start_reg, count)
    return body + crc16(body)


def build_write_single(
    register: int,
    value: int,
    *,
    unit_id: int = DEFAULT_UNIT_ID,
) -> bytes:
    """Build a Modbus function 0x06 (Write Single Register) request frame."""
    if not 0 <= register <= 0xFFFF:
        raise ValueError(f"register out of range: {register}")
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"value out of range: {value}")

    body = struct.pack(">BBHH", unit_id, FUNC_WRITE_SINGLE, register, value)
    return body + crc16(body)


def build_write_multiple(
    start_reg: int,
    values: list[int] | tuple[int, ...],
    *,
    unit_id: int = DEFAULT_UNIT_ID,
) -> bytes:
    """Build a Modbus function 0x10 (Write Multiple Registers) request frame.

    ZhiJin solar controllers expect function 0x10 even when writing a single setting.
    """
    if not values:
        raise ValueError("values must contain at least one register value")
    if len(values) > 123:
        raise ValueError(f"too many registers for write multiple: {len(values)}")
    if not 0 <= start_reg <= 0xFFFF:
        raise ValueError(f"start_reg out of range: {start_reg}")

    parts = [struct.pack(">BBHHB", unit_id, FUNC_WRITE_MULTIPLE, start_reg, len(values), len(values) * 2)]
    for val in values:
        if not 0 <= val <= 0xFFFF:
            raise ValueError(f"register value out of range: {val}")
        parts.append(struct.pack(">H", val))
    body = b"".join(parts)
    return body + crc16(body)


def build_telemetry_request(*, unit_id: int = DEFAULT_UNIT_ID) -> bytes:
    """Build the telemetry read command (0x0001, count 16 -> expects 20 data bytes)."""
    return build_read_holding(REG_TELEMETRY_START, REG_TELEMETRY_REQUEST_COUNT, unit_id=unit_id)


def build_settings_request(*, unit_id: int = DEFAULT_UNIT_ID) -> bytes:
    """Build the settings read command (0x1001, count 15 -> expects 18 data bytes)."""
    return build_read_holding(REG_SETTINGS_START, REG_SETTINGS_REQUEST_COUNT, unit_id=unit_id)


def encode_voltage(volts: float) -> int:
    """Encode a voltage float (0.1V resolution) into its integer register value.

    Uses ``round()`` rather than ``int()`` to avoid IEEE-754 truncation bugs
    (e.g. ``int(11.3 * 10) == 112`` instead of ``113``).
    """
    if volts < 0:
        raise ValueError(f"voltage must be non-negative: {volts}")
    encoded = round(volts * 10)
    if encoded > 0xFFFF:
        raise ValueError(f"encoded voltage exceeds uint16: {volts}")
    return encoded


def _interpolate_curve(value: float, curve: tuple[tuple[float, float], ...]) -> float:
    """Piecewise linear interpolation over sorted (x, y) breakpoints, clamped at ends."""
    if value <= curve[0][0]:
        return curve[0][1]
    if value >= curve[-1][0]:
        return curve[-1][1]

    for (x0, y0), (x1, y1) in pairwise(curve):
        if x0 <= value <= x1:
            ratio = (value - x0) / (x1 - x0)
            return y0 + ratio * (y1 - y0)
    return curve[-1][1]


def calculate_battery_percent(voltage: float) -> float:
    """Calculate raw voltage-based battery percentage (matches official ZhiJin app)."""
    multiplier = 1 if voltage <= SYSTEM_24V_THRESHOLD else 2
    min_v = 9.0 * multiplier
    max_v = 12.6 * multiplier
    pct = ((voltage - min_v) / (max_v - min_v)) * 100.0
    return round(max(0.0, min(100.0, pct)), 1)


def is_lithium_3s(battery_type: int, full_voltage: float | None) -> bool:
    """Return True when Lithium chemistry is configured for a 3S (or 6S @ 24V) Li-ion pack.

    Determined from the controller's ``full_voltage`` register normalized to a 12V
    unit (<= 13.0 V -> 3S Li-ion such as 12.6 V; > 13.0 V -> 4S LiFePO4 such as 14.4 V).
    """
    if battery_type != BatteryType.LITHIUM or full_voltage is None or full_voltage <= 0:
        return False
    multiplier = 1 if full_voltage <= SYSTEM_24V_THRESHOLD else 2
    return (full_voltage / multiplier) <= LITHIUM_3S_FULL_VOLTAGE_THRESHOLD


def calculate_healthy_usable_percent(
    voltage: float,
    battery_type: int,
    *,
    full_voltage: float | None = None,
) -> float:
    """Calculate chemistry-aware health-safe usable battery percentage.

    12V systems (voltage <= 17V) use the 12V curve directly; 24V systems are
    normalized to the 12V scale by dividing by 2. For ``BatteryType.LITHIUM``,
    when ``full_voltage`` is known and <= 13.0V (per 12V unit), the 3S Li-ion
    curve is used instead of the 4S LiFePO4 curve.
    """
    multiplier = 1 if voltage <= SYSTEM_24V_THRESHOLD else 2
    v_norm = voltage / multiplier
    if is_lithium_3s(battery_type, full_voltage):
        curve = HEALTHY_BATTERY_CURVE_LITHIUM_3S
    else:
        try:
            chemistry = BatteryType(battery_type)
        except ValueError:
            chemistry = BatteryType.LEAD_ACID
        curve = HEALTHY_BATTERY_CURVES[chemistry]
    return round(_interpolate_curve(v_norm, curve), 1)


def decode_telemetry(
    data: bytes,
    *,
    battery_type: int = BatteryType.LEAD_ACID,
    full_voltage: float | None = None,
) -> TelemetryData:
    """Decode the 20-byte telemetry payload from registers 0x0001..0x000A."""
    if len(data) < TELEMETRY_RESPONSE_BYTES:
        raise ZhiJinProtocolError(
            f"Telemetry payload too short: expected >= {TELEMETRY_RESPONSE_BYTES} bytes, got {len(data)}"
        )

    (
        solar_model_type,
        voltage_raw,
        charge_raw,
        discharge_raw,
        temp_raw,
        solar_raw,
        load_raw,
        wind_raw,
        total_power_raw,
        total_power_resets,
    ) = struct.unpack_from(">HHHHhHHHHH", data, 0)

    voltage = round(voltage_raw / 10.0, 1)
    charge_current = round(charge_raw / 10.0, 1)
    discharge_current = round(discharge_raw / 10.0, 1)
    temperature = round(temp_raw / 100.0, 2)
    total_generation = round(total_power_resets * 1000.0 + total_power_raw / 10.0, 1)

    return TelemetryData(
        solar_model_type=solar_model_type,
        battery_voltage=voltage,
        charge_current=charge_current,
        discharge_current=discharge_current,
        temperature=temperature,
        solar_status=solar_raw == 1,
        load_status=load_raw == 1,
        wind_status=wind_raw == 1,
        total_power_raw=total_power_raw,
        total_power_resets=total_power_resets,
        total_generation=total_generation,
        battery_percent=calculate_battery_percent(voltage),
        healthy_usable_battery=calculate_healthy_usable_percent(
            voltage, battery_type, full_voltage=full_voltage
        ),
        solar_power=round(voltage * charge_current, 1),
        load_power=round(voltage * discharge_current, 1),
    )


def recalculate_telemetry_for_battery_type(
    telemetry: TelemetryData,
    battery_type: int,
    *,
    full_voltage: float | None = None,
) -> TelemetryData:
    """Return a copy of telemetry with healthy_usable_battery updated for battery settings."""
    return replace(
        telemetry,
        healthy_usable_battery=calculate_healthy_usable_percent(
            telemetry.battery_voltage, battery_type, full_voltage=full_voltage
        ),
    )


def decode_settings(data: bytes) -> SettingsData:
    """Decode the 18-byte settings payload from registers 0x1001..0x1009."""
    if len(data) < SETTINGS_RESPONSE_BYTES:
        raise ZhiJinProtocolError(
            f"Settings payload too short: expected >= {SETTINGS_RESPONSE_BYTES} bytes, got {len(data)}"
        )

    (
        battery_type,
        time_hour,
        time_min,
        full_voltage_raw,
        output_mode,
        cutoff_voltage_raw,
        load_switch_raw,
        voltage_detect_mode,
        recovery_voltage_raw,
    ) = struct.unpack_from(">HHHHHHHHH", data, 0)

    return SettingsData(
        battery_type=battery_type,
        time_hour=time_hour,
        time_min=time_min,
        full_voltage=round(full_voltage_raw / 10.0, 1),
        output_mode=output_mode,
        cutoff_voltage=round(cutoff_voltage_raw / 10.0, 1),
        load_switch=load_switch_raw == 1,
        voltage_detect_mode=voltage_detect_mode,
        recovery_voltage=round(recovery_voltage_raw / 10.0, 1),
    )


def decode_write_ack(frame: ModbusFrame) -> tuple[int, int]:
    """Decode the (start_reg, count_or_value) echo from a 0x06 or 0x10 response frame."""
    if frame.function_code not in (FUNC_WRITE_SINGLE, FUNC_WRITE_MULTIPLE):
        raise ZhiJinProtocolError(f"Expected write ACK frame, got function 0x{frame.function_code:02X}")
    if len(frame.payload) != 4:
        raise ZhiJinProtocolError(f"Invalid write ACK payload length: {len(frame.payload)}")
    return struct.unpack(">HH", frame.payload)


class ModbusFrameParser:
    """Resilient sliding-window Modbus RTU stream reassembler.

    Handles:
    - BLE notification fragmentation across arbitrary chunk boundaries.
    - Multiple coalesced frames within a single BLE notification.
    - Leading/interleaved noise bytes without eating subsequent valid frames.
    - CRC-16 failure resynchronization (advances by 1 byte instead of discarding
      the candidate length).
    - Modbus exception responses (function | 0x80, fixed 5-byte length).
    - Bounded buffer growth under continuous garbage input.
    """

    def __init__(
        self,
        *,
        unit_id: int = DEFAULT_UNIT_ID,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
    ) -> None:
        self._unit_id = unit_id
        self._max_buffer_bytes = max_buffer_bytes
        self._buffer = bytearray()
        self.crc_errors = 0
        self.discarded_bytes = 0

    @property
    def buffered_bytes(self) -> int:
        """Return number of bytes currently held in the reassembly buffer."""
        return len(self._buffer)

    def clear(self) -> None:
        """Clear any buffered partial bytes."""
        self._buffer.clear()

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[ModbusFrame]:
        """Feed incoming BLE notification bytes and return all complete valid frames."""
        if chunk:
            self._buffer.extend(chunk)
            if len(self._buffer) > self._max_buffer_bytes:
                overflow = len(self._buffer) - self._max_buffer_bytes
                del self._buffer[:overflow]
                self.discarded_bytes += overflow

        frames: list[ModbusFrame] = []

        while len(self._buffer) >= 5:
            if self._buffer[0] != self._unit_id:
                del self._buffer[0]
                self.discarded_bytes += 1
                continue

            func = self._buffer[1]
            expected_len = self._expected_frame_length(func)
            if expected_len is None:
                # Unknown function code or impossible byte_count: resync by 1 byte.
                del self._buffer[0]
                self.discarded_bytes += 1
                continue

            if len(self._buffer) < expected_len:
                # Wait for remaining chunks of this candidate frame.
                break

            candidate = bytes(self._buffer[:expected_len])
            if verify_crc(candidate):
                del self._buffer[:expected_len]
                is_exception = bool(func & EXCEPTION_FLAG)
                frames.append(
                    ModbusFrame(
                        unit_id=candidate[0],
                        function_code=func & ~EXCEPTION_FLAG if is_exception else func,
                        payload=candidate[2:-2],
                        raw=candidate,
                        is_exception=is_exception,
                        exception_code=candidate[2] if is_exception else None,
                    )
                )
            else:
                # Candidate failed CRC: slide forward by 1 byte so a real frame
                # starting inside the candidate window is never dropped.
                self.crc_errors += 1
                del self._buffer[0]
                self.discarded_bytes += 1

        return frames

    def _expected_frame_length(self, func: int) -> int | None:
        """Determine candidate frame length from header bytes, or None if invalid."""
        if func in (FUNC_WRITE_SINGLE, FUNC_WRITE_MULTIPLE):
            return 8

        if func == FUNC_READ_HOLDING:
            byte_count = self._buffer[2]
            if byte_count == 0 or byte_count > _MAX_READ_BYTE_COUNT or byte_count % 2 != 0:
                return None
            return byte_count + 5

        if (func & EXCEPTION_FLAG) and (func & ~EXCEPTION_FLAG) in VALID_FUNCTIONS:
            return 5

        return None
