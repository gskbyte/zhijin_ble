"""Unit tests for Modbus RTU framing, CRC-16, stream parser, and register codecs."""

from __future__ import annotations

import struct

import pytest

from custom_components.zhijin_ble.ble.const import (
    FUNC_READ_HOLDING,
    FUNC_WRITE_MULTIPLE,
    FUNC_WRITE_SINGLE,
    BatteryType,
    OutputMode,
    VoltageDetectMode,
)
from custom_components.zhijin_ble.ble.exceptions import ZhiJinProtocolError
from custom_components.zhijin_ble.ble.protocol import (
    ModbusFrameParser,
    build_read_holding,
    build_settings_request,
    build_telemetry_request,
    build_write_multiple,
    build_write_single,
    calculate_battery_percent,
    calculate_healthy_usable_percent,
    crc16,
    decode_settings,
    decode_telemetry,
    decode_write_ack,
    encode_voltage,
    is_lithium_3s,
    recalculate_telemetry_for_battery_type,
    verify_crc,
)
from tests.conftest import (
    REAL_SETTINGS,
    REAL_TELEMETRY_12_0V,
    REAL_TELEMETRY_12_1V,
    make_exception_frame,
    make_settings_frame,
    make_telemetry_frame,
    make_write_ack_frame,
)


class TestCRC16AndBuilders:
    """Verify CRC-16 calculation and Modbus request builders."""

    @pytest.mark.parametrize(
        "raw_frame",
        [REAL_TELEMETRY_12_0V, REAL_TELEMETRY_12_1V, REAL_SETTINGS],
    )
    def test_verify_crc_on_real_hardware_frames(self, raw_frame: bytes) -> None:
        assert verify_crc(raw_frame) is True
        corrupted = bytearray(raw_frame)
        corrupted[5] ^= 0x01
        assert verify_crc(corrupted) is False

    def test_verify_crc_rejects_short_frames(self) -> None:
        assert verify_crc(b"") is False
        assert verify_crc(b"\x01\x03\x00") is False

    def test_build_telemetry_and_settings_requests(self) -> None:
        telem_req = build_telemetry_request()
        assert len(telem_req) == 8
        assert telem_req[:6] == bytes.fromhex("010300010010")
        assert verify_crc(telem_req)

        settings_req = build_settings_request()
        assert len(settings_req) == 8
        assert settings_req[:6] == bytes.fromhex("01031001000f")
        assert verify_crc(settings_req)

    def test_build_write_multiple_single_register(self) -> None:
        req = build_write_multiple(0x1007, [1])
        assert req[:9] == struct.pack(">BBHHBH", 1, FUNC_WRITE_MULTIPLE, 0x1007, 1, 2, 1)
        assert len(req) == 11
        assert verify_crc(req)

    def test_build_write_single(self) -> None:
        req = build_write_single(0x1007, 0)
        assert len(req) == 8
        assert req[:6] == struct.pack(">BBHH", 1, FUNC_WRITE_SINGLE, 0x1007, 0)
        assert verify_crc(req)

    def test_builder_range_validation(self) -> None:
        with pytest.raises(ValueError):
            build_read_holding(0x0001, 0)
        with pytest.raises(ValueError):
            build_read_holding(0x0001, 126)
        with pytest.raises(ValueError):
            build_read_holding(0x10000, 1)
        with pytest.raises(ValueError):
            build_read_holding(0x0001, 1, unit_id=256)
        with pytest.raises(ValueError):
            build_write_multiple(0x1001, [])
        with pytest.raises(ValueError):
            build_write_multiple(0x1001, [1] * 124)
        with pytest.raises(ValueError):
            build_write_multiple(0x1001, [70000])
        with pytest.raises(ValueError):
            build_write_single(-1, 1)
        with pytest.raises(ValueError):
            build_write_single(0x1001, 70000)

    @pytest.mark.parametrize(
        ("volts", "expected"),
        [
            (11.3, 113),  # IEEE-754 int(11.3 * 10) would wrongly produce 112
            (12.6, 126),
            (14.3, 143),
            (19.9, 199),
            (28.8, 288),
            (0.0, 0),
        ],
    )
    def test_encode_voltage_avoids_float_truncation(self, volts: float, expected: int) -> None:
        assert encode_voltage(volts) == expected

    def test_encode_voltage_validation(self) -> None:
        with pytest.raises(ValueError):
            encode_voltage(-0.1)
        with pytest.raises(ValueError):
            encode_voltage(7000.0)


class TestTelemetryAndSettingsDecoders:
    """Verify binary register decoding and derived solar/battery calculations."""

    def test_decode_real_hardware_telemetry_12_0v(self) -> None:
        telem = decode_telemetry(REAL_TELEMETRY_12_0V[3:-2], battery_type=BatteryType.LITHIUM)
        assert telem.solar_model_type == 1
        assert telem.battery_voltage == 12.0
        assert telem.charge_current == 0.0
        assert telem.discharge_current == 0.0
        assert telem.temperature == 13.2
        assert telem.solar_status is True
        assert telem.load_status is True
        assert telem.wind_status is False
        assert telem.total_power_raw == 82
        assert telem.total_power_resets == 0
        assert telem.total_generation == 8.2
        assert telem.solar_power == 0.0
        assert telem.load_power == 0.0
        assert telem.battery_percent == pytest.approx(83.3, abs=0.1)
        assert telem.healthy_usable_battery == 0.0  # 12.0V below LiFePO4 12.2V floor
        assert telem.is_idle is True
        assert telem.is_charging is False
        assert telem.is_discharging is False

    def test_decode_real_hardware_settings(self) -> None:
        settings = decode_settings(REAL_SETTINGS[3:-2])
        assert settings.battery_type == BatteryType.LITHIUM
        assert settings.battery_type_label == "Lithium"
        assert settings.time_hour == 0
        assert settings.time_min == 0
        assert settings.full_voltage == 12.6
        assert settings.output_mode == OutputMode.MANUAL
        assert settings.output_mode_label == "Manual"
        assert settings.cutoff_voltage == 10.0
        assert settings.load_switch is True
        assert settings.voltage_detect_mode == VoltageDetectMode.AUTO
        assert settings.voltage_detect_mode_label == "Auto"
        assert settings.recovery_voltage == 11.0

    def test_decode_negative_temperature_and_rollover_generation(self) -> None:
        frame = make_telemetry_frame(
            voltage=25.6,
            charge_current=10.5,
            discharge_current=2.5,
            temperature=-12.34,
            solar_status=1,
            load_status=1,
            wind_status=1,
            total_power_raw=456,
            total_power_resets=3,
        )
        telem = decode_telemetry(frame[3:-2], battery_type=BatteryType.GEL)
        assert telem.battery_voltage == 25.6
        assert telem.temperature == -12.34
        assert telem.solar_power == 268.8
        assert telem.load_power == 64.0
        assert telem.wind_status is True
        assert telem.total_generation == 3045.6
        assert telem.is_charging is True
        assert telem.is_discharging is True
        assert telem.is_idle is False
        # 25.6V / 2 = 12.8V -> 100% for Gel
        assert telem.healthy_usable_battery == 100.0

    def test_decode_short_payloads_raise_protocol_error(self) -> None:
        with pytest.raises(ZhiJinProtocolError):
            decode_telemetry(b"\x00" * 19)
        with pytest.raises(ZhiJinProtocolError):
            decode_settings(b"\x00" * 17)

    def test_battery_percent_12v_and_24v_bounds(self) -> None:
        assert calculate_battery_percent(8.5) == 0.0
        assert calculate_battery_percent(9.0) == 0.0
        assert calculate_battery_percent(10.8) == 50.0
        assert calculate_battery_percent(12.6) == 100.0
        assert calculate_battery_percent(14.4) == 100.0
        # 24V system (> 17.0V)
        assert calculate_battery_percent(18.0) == 0.0
        assert calculate_battery_percent(21.6) == 50.0
        assert calculate_battery_percent(25.2) == 100.0
        assert calculate_battery_percent(28.0) == 100.0

    @pytest.mark.parametrize(
        ("voltage", "btype", "expected"),
        [
            # Lithium
            (12.0, BatteryType.LITHIUM, 0.0),
            (12.2, BatteryType.LITHIUM, 0.0),
            (12.6, BatteryType.LITHIUM, 15.0),
            (13.0, BatteryType.LITHIUM, 30.0),
            (13.1, BatteryType.LITHIUM, 50.0),
            (13.2, BatteryType.LITHIUM, 70.0),
            (13.3, BatteryType.LITHIUM, 85.0),
            (13.4, BatteryType.LITHIUM, 100.0),
            (26.6, BatteryType.LITHIUM, 85.0),  # 24V -> 13.3V normalized
            # Gel
            (11.8, BatteryType.GEL, 0.0),
            (12.0, BatteryType.GEL, 7.5),
            (12.2, BatteryType.GEL, 30.0),
            (12.4, BatteryType.GEL, 60.0),
            (12.65, BatteryType.GEL, 87.5),
            (12.8, BatteryType.GEL, 100.0),
            # Lead-Acid
            (11.9, BatteryType.LEAD_ACID, 0.0),
            (12.05, BatteryType.LEAD_ACID, 10.0),
            (12.2, BatteryType.LEAD_ACID, 35.0),
            (12.4, BatteryType.LEAD_ACID, 65.0),
            (12.6, BatteryType.LEAD_ACID, 90.0),
            (12.7, BatteryType.LEAD_ACID, 100.0),
            # Unknown chemistry falls back to Lead-Acid
            (12.3, 99, 50.0),
        ],
    )
    def test_healthy_usable_battery_curves(
        self, voltage: float, btype: int, expected: float
    ) -> None:
        assert calculate_healthy_usable_percent(voltage, btype) == pytest.approx(expected, abs=0.1)

    def test_recalculate_telemetry_for_battery_type(self) -> None:
        telem = decode_telemetry(REAL_TELEMETRY_12_1V[3:-2], battery_type=BatteryType.LITHIUM)
        assert telem.healthy_usable_battery == 0.0
        updated = recalculate_telemetry_for_battery_type(telem, BatteryType.LEAD_ACID)
        assert updated.healthy_usable_battery == 20.0
        assert updated.battery_voltage == telem.battery_voltage

        # Recalculating with 3S Li-ion full_voltage (12.6V) switches to 3S Li-ion curve
        updated_3s = recalculate_telemetry_for_battery_type(
            telem, BatteryType.LITHIUM, full_voltage=12.6
        )
        assert updated_3s.healthy_usable_battery == 82.0

    @pytest.mark.parametrize(
        ("voltage", "full_voltage", "expected"),
        [
            # 3S Li-ion (12.6V full)
            (10.0, 12.6, 0.0),
            (10.5, 12.6, 0.0),
            (11.1, 12.6, 20.0),
            (11.4, 12.6, 40.0),
            (11.8, 12.6, 70.0),
            (12.0, 12.6, 78.0),
            (12.1, 12.6, 82.0),
            (12.3, 12.6, 90.0),
            (12.4, 12.6, 93.3),
            (12.6, 12.6, 100.0),
            # 6S Li-ion 24V system (25.2V full -> normalized 12.6V <= 13.0V)
            (24.8, 25.2, 93.3),
            (25.2, 25.2, 100.0),
            # 4S LiFePO4 (14.4V full -> normalized 14.4V > 13.0V)
            (12.4, 14.4, 7.5),
            (13.2, 14.4, 70.0),
            (13.4, 14.4, 100.0),
            # 8S LiFePO4 24V system (28.8V full -> normalized 14.4V > 13.0V)
            (26.4, 28.8, 70.0),
        ],
    )
    def test_healthy_usable_battery_lithium_3s_vs_4s(
        self, voltage: float, full_voltage: float, expected: float
    ) -> None:
        assert calculate_healthy_usable_percent(
            voltage, BatteryType.LITHIUM, full_voltage=full_voltage
        ) == pytest.approx(expected, abs=0.1)

    def test_is_lithium_3s_detection(self) -> None:
        assert is_lithium_3s(BatteryType.LITHIUM, 12.6) is True
        assert is_lithium_3s(BatteryType.LITHIUM, 13.0) is True
        assert is_lithium_3s(BatteryType.LITHIUM, 25.2) is True
        assert is_lithium_3s(BatteryType.LITHIUM, 14.4) is False
        assert is_lithium_3s(BatteryType.LITHIUM, 28.8) is False
        assert is_lithium_3s(BatteryType.LITHIUM, None) is False
        assert is_lithium_3s(BatteryType.GEL, 12.6) is False
        assert is_lithium_3s(BatteryType.LEAD_ACID, 12.6) is False

    def test_decode_write_ack_validation(self) -> None:
        parser = ModbusFrameParser()
        ack = parser.feed(make_write_ack_frame(0x1007, 1))[0]
        assert decode_write_ack(ack) == (0x1007, 1)

        telem_frame = parser.feed(REAL_TELEMETRY_12_0V)[0]
        with pytest.raises(ZhiJinProtocolError):
            decode_write_ack(telem_frame)


class TestModbusFrameParser:
    """Verify sliding-window stream reassembly, resync, and exception handling."""

    def test_parses_single_complete_frame(self) -> None:
        parser = ModbusFrameParser()
        frames = parser.feed(REAL_TELEMETRY_12_0V)
        assert len(frames) == 1
        assert frames[0].unit_id == 1
        assert frames[0].function_code == FUNC_READ_HOLDING
        assert frames[0].byte_count == 20
        assert frames[0].raw == REAL_TELEMETRY_12_0V
        assert parser.buffered_bytes == 0

    def test_parses_arbitrarily_fragmented_chunks(self) -> None:
        parser = ModbusFrameParser()
        collected = []
        # Feed 1 byte at a time
        for idx in range(len(REAL_TELEMETRY_12_1V)):
            collected.extend(parser.feed(REAL_TELEMETRY_12_1V[idx : idx + 1]))
        assert len(collected) == 1
        assert collected[0].raw == REAL_TELEMETRY_12_1V

    def test_parses_coalesced_multiple_frames(self) -> None:
        parser = ModbusFrameParser()
        frames = parser.feed(REAL_TELEMETRY_12_0V + REAL_SETTINGS + make_write_ack_frame(0x1004, 1))
        assert len(frames) == 3
        assert frames[0].byte_count == 20
        assert frames[1].byte_count == 18
        assert frames[2].function_code == FUNC_WRITE_MULTIPLE
        assert parser.buffered_bytes == 0

    def test_resyncs_past_leading_noise_including_stray_unit_id_and_bad_length(self) -> None:
        parser = ModbusFrameParser()
        # Noise contains unit_id 0x01 followed by bogus func 0xFF and bogus odd/huge byte counts
        noise = bytes([0xAA, 0xBB, 0x01, 0xFF, 0xFE, 0x01, 0x03, 0xFF, 0x01, 0x03, 0x05])
        frames = parser.feed(noise + REAL_TELEMETRY_12_0V)
        assert len(frames) == 1
        assert frames[0].raw == REAL_TELEMETRY_12_0V
        assert parser.discarded_bytes == len(noise)

    def test_resyncs_by_one_byte_on_crc_mismatch_without_eating_next_frame(self) -> None:
        parser = ModbusFrameParser()
        # Fake 0x01 0x03 0x14 prefix (claims 25-byte frame) immediately followed by real frame!
        # Sliding by 1 byte on CRC failure ensures the valid frame inside the window is not discarded.
        corrupt_prefix = bytes([0x01, 0x03, 0x14, 0x99])
        frames = parser.feed(corrupt_prefix + REAL_TELEMETRY_12_0V)
        assert len(frames) == 1
        assert frames[0].raw == REAL_TELEMETRY_12_0V
        assert parser.crc_errors >= 1

    def test_parses_modbus_exception_frame(self) -> None:
        parser = ModbusFrameParser()
        exc_raw = make_exception_frame(FUNC_READ_HOLDING, 0x02)
        frames = parser.feed(exc_raw)
        assert len(frames) == 1
        assert frames[0].is_exception is True
        assert frames[0].function_code == FUNC_READ_HOLDING
        assert frames[0].exception_code == 0x02

    def test_caps_buffer_growth_under_continuous_garbage(self) -> None:
        parser = ModbusFrameParser(max_buffer_bytes=64)
        # Feed 200 bytes starting with 0x01 0x03 0xF0 (240 bytes) — won't complete inside cap
        garbage = bytes([0x01, 0x03, 0xF0]) + bytes([0x55] * 200)
        assert parser.feed(garbage) == []
        assert parser.buffered_bytes <= 64
        parser.clear()
        assert parser.buffered_bytes == 0

    def test_settings_frame_with_trailing_crc_helper(self) -> None:
        frame = make_settings_frame(full_voltage=14.6, cutoff_voltage=11.3)
        assert verify_crc(frame)
        assert crc16(frame[:-2]) == frame[-2:]
