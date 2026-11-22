"""Unit tests for ZhiJinBLEClient transport resilience, retries, and burst handling."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from bleak import BleakError
from bleak.backends.device import BLEDevice

from custom_components.zhijin_ble.ble.client import ClientConfig, ZhiJinBLEClient
from custom_components.zhijin_ble.ble.const import (
    FUNC_READ_HOLDING,
    FUNC_WRITE_MULTIPLE,
    BatteryType,
    ConnectionMode,
    SettingsRegister,
)
from custom_components.zhijin_ble.ble.exceptions import (
    ZhiJinConnectionError,
    ZhiJinDeviceNotFoundError,
    ZhiJinModbusException,
    ZhiJinTimeoutError,
)
from custom_components.zhijin_ble.ble.models import ConnectionStats
from custom_components.zhijin_ble.ble.protocol import (
    build_settings_request,
    build_telemetry_request,
)
from tests.conftest import (
    REAL_SETTINGS,
    REAL_TELEMETRY_12_0V,
    REAL_TELEMETRY_12_1V,
    FakeBleakClient,
    make_exception_frame,
    make_settings_frame,
    make_write_ack_frame,
)


def _fast_config(**overrides: Any) -> ClientConfig:
    defaults = {
        "command_timeout": 0.25,
        "connect_timeout": 1.0,
        "disconnect_timeout": 0.5,
        "command_retries": 2,
        "reconnect_retries": 1,
        "retry_delay": 0.01,
        "post_subscribe_delay": 0.0,
        "min_command_interval": 0.01,
        "duplicate_window": 1.0,
    }
    defaults.update(overrides)
    return ClientConfig(**defaults)


class TestClientConfigValidation:
    def test_rejects_invalid_config(self) -> None:
        with pytest.raises(ValueError):
            ClientConfig(command_timeout=0)
        with pytest.raises(ValueError):
            ClientConfig(connect_timeout=-1)
        with pytest.raises(ValueError):
            ClientConfig(command_retries=0)
        with pytest.raises(ValueError):
            ClientConfig(reconnect_retries=-1)


class TestZhiJinBLEClient:
    async def test_on_demand_poll_connects_reads_both_and_disconnects(
        self, sample_ble_device: BLEDevice
    ) -> None:
        created: list[FakeBleakClient] = []

        def on_write(fc: FakeBleakClient, payload: bytes, _resp: bool) -> None:
            if payload == build_telemetry_request():
                fc.schedule_notify(REAL_TELEMETRY_12_0V)
            elif payload == build_settings_request():
                fc.schedule_notify(REAL_SETTINGS)

        async def connector(device, name, disc_cb, _dev_cb, _timeout):
            fc = FakeBleakClient(device.address, disconnected_callback=disc_cb, on_write=on_write)
            created.append(fc)
            return fc

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(connection_mode=ConnectionMode.ON_DEMAND),
            connector=connector,
        )

        telem, settings = await client.poll(include_settings=True)
        assert telem.battery_voltage == 12.0
        assert settings is not None
        assert settings.battery_type == BatteryType.LITHIUM
        assert settings.full_voltage == 12.6

        # Both reads happened on a single BLE connection, then disconnected cleanly
        assert len(created) == 1
        assert created[0].start_notify_calls == 1
        assert created[0].stop_notify_calls == 1
        assert created[0].disconnect_calls == 1
        assert client.is_connected is False
        assert client.stats.connect_successes == 1
        assert client.stats.commands_succeeded == 2
        assert client.stats.command_success_rate == 100.0

    async def test_persistent_mode_reuses_single_connection(
        self, sample_ble_device: BLEDevice
    ) -> None:
        created: list[FakeBleakClient] = []

        def on_write(fc: FakeBleakClient, payload: bytes, _resp: bool) -> None:
            if payload == build_telemetry_request():
                fc.schedule_notify(REAL_TELEMETRY_12_1V)

        async def connector(device, name, disc_cb, _dev_cb, _timeout):
            fc = FakeBleakClient(device.address, disconnected_callback=disc_cb, on_write=on_write)
            created.append(fc)
            return fc

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(connection_mode=ConnectionMode.PERSISTENT),
            connector=connector,
        )

        t1, _ = await client.poll()
        t2, _ = await client.poll()
        assert t1.battery_voltage == 12.1
        assert t2.battery_voltage == 12.1
        assert len(created) == 1
        assert client.is_connected is True
        await client.disconnect()
        assert client.is_connected is False

    async def test_absorbs_seven_duplicate_burst_frames_without_breaking_next_read(
        self, sample_ble_device: BLEDevice
    ) -> None:
        """Reproduces the exact 7-duplicate burst pattern observed in real HA logs."""
        events: list[tuple[str, dict[str, Any]]] = []

        def on_write(fc: FakeBleakClient, payload: bytes, _resp: bool) -> None:
            if payload == build_telemetry_request():
                # 1 primary response + 7 duplicate copies arriving 1-3ms later
                fc.schedule_notify(REAL_TELEMETRY_12_0V, delay=0.0)
                for idx in range(7):
                    fc.schedule_notify(REAL_TELEMETRY_12_0V, delay=0.002 + idx * 0.001)
            elif payload == build_settings_request():
                # 1 primary + 2 duplicate copies
                fc.schedule_notify(REAL_SETTINGS, delay=0.0)
                fc.schedule_notify(REAL_SETTINGS, delay=0.002)
                fc.schedule_notify(REAL_SETTINGS, delay=0.004)

        async def connector(device, name, disc_cb, _dev_cb, _timeout):
            return FakeBleakClient(device.address, disconnected_callback=disc_cb, on_write=on_write)

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(
                connection_mode=ConnectionMode.PERSISTENT,
                min_command_interval=0.03,
            ),
            connector=connector,
            event_callback=lambda ev, data: events.append((ev, data)),
        )

        telem, settings = await client.poll(include_settings=True)
        await asyncio.sleep(0.02)  # Allow trailing settings duplicates to arrive
        assert telem.battery_voltage == 12.0
        assert settings is not None
        assert settings.cutoff_voltage == 10.0
        assert client.stats.duplicate_frames == 9
        assert client.stats.unsolicited_frames == 0
        assert any(ev == "rx_duplicate" for ev, _ in events)
        await client.disconnect()

    async def test_ignores_unsolicited_frame_and_accepts_matching_response(
        self, sample_ble_device: BLEDevice
    ) -> None:
        def on_write(fc: FakeBleakClient, _payload: bytes, _resp: bool) -> None:
            # Controller sends wrong byte-count frame first (settings instead of telemetry), then telemetry
            fc.schedule_notify(REAL_SETTINGS, delay=0.0)
            fc.schedule_notify(REAL_TELEMETRY_12_1V, delay=0.005)

        async def connector(device, name, disc_cb, _dev_cb, _timeout):
            return FakeBleakClient(device.address, disconnected_callback=disc_cb, on_write=on_write)

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(),
            connector=connector,
        )

        telem = await client.read_telemetry()
        assert telem.battery_voltage == 12.1
        assert client.stats.unsolicited_frames == 1

    async def test_in_session_timeout_retry_recovers_without_reconnect(
        self, sample_ble_device: BLEDevice
    ) -> None:
        connect_count = 0
        write_count = 0

        def on_write(fc: FakeBleakClient, _payload: bytes, _resp: bool) -> None:
            nonlocal write_count
            write_count += 1
            if write_count == 2:
                fc.schedule_notify(REAL_TELEMETRY_12_0V)
            # First write intentionally gets no reply -> timeout

        async def connector(device, name, disc_cb, _dev_cb, _timeout):
            nonlocal connect_count
            connect_count += 1
            return FakeBleakClient(device.address, disconnected_callback=disc_cb, on_write=on_write)

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(
                connection_mode=ConnectionMode.PERSISTENT,
                command_timeout=0.05,
                command_retries=2,
            ),
            connector=connector,
        )

        telem = await client.read_telemetry()
        assert telem.battery_voltage == 12.0
        assert connect_count == 1
        assert write_count == 2
        assert client.stats.timeouts == 1
        assert client.stats.command_retries == 1
        assert client.stats.commands_succeeded == 1
        await client.disconnect()

    async def test_mid_command_disconnect_wakes_immediately_and_recovers_on_reconnect(
        self, sample_ble_device: BLEDevice
    ) -> None:
        """Verifies link drop wakes pending waiter immediately (not after 10s timeout!)."""
        connect_count = 0

        async def connector(device, name, disc_cb, _dev_cb, _timeout):
            nonlocal connect_count
            connect_count += 1
            session = connect_count

            def on_write(fc: FakeBleakClient, _payload: bytes, _resp: bool) -> None:
                if session == 1:
                    asyncio.get_running_loop().call_later(0.01, fc.simulate_disconnect)
                else:
                    fc.schedule_notify(REAL_TELEMETRY_12_1V)

            return FakeBleakClient(device.address, disconnected_callback=disc_cb, on_write=on_write)

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(
                connection_mode=ConnectionMode.PERSISTENT,
                command_timeout=10.0,  # Large timeout proves immediate wakeup
                reconnect_retries=1,
            ),
            connector=connector,
        )

        telem = await asyncio.wait_for(client.read_telemetry(), timeout=1.0)
        assert telem.battery_voltage == 12.1
        assert connect_count == 2
        assert client.stats.unexpected_disconnects == 1
        await client.disconnect()

    async def test_stale_disconnect_callback_does_not_clobber_active_client(
        self, sample_ble_device: BLEDevice
    ) -> None:
        clients: list[FakeBleakClient] = []

        async def connector(device, name, disc_cb, _dev_cb, _timeout):
            fc = FakeBleakClient(device.address, disconnected_callback=disc_cb)
            clients.append(fc)
            return fc

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(connection_mode=ConnectionMode.PERSISTENT),
            connector=connector,
        )

        await client.connect()
        first_client = clients[0]
        await client.disconnect()
        await client.connect()
        second_client = clients[1]

        # Late callback from superseded first client must be ignored
        first_client.simulate_disconnect()
        assert client.is_connected is True
        assert client._client is second_client
        assert client.stats.unexpected_disconnects == 0
        await client.disconnect()

    async def test_modbus_exception_fails_immediately_without_retry(
        self, sample_ble_device: BLEDevice
    ) -> None:
        writes = 0

        def on_write(fc: FakeBleakClient, _payload: bytes, _resp: bool) -> None:
            nonlocal writes
            writes += 1
            fc.schedule_notify(make_exception_frame(FUNC_READ_HOLDING, 0x02))

        async def connector(device, name, disc_cb, _dev_cb, _timeout):
            return FakeBleakClient(device.address, disconnected_callback=disc_cb, on_write=on_write)

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(command_retries=3, reconnect_retries=2),
            connector=connector,
        )

        with pytest.raises(ZhiJinModbusException) as exc_info:
            await client.read_telemetry()

        assert exc_info.value.function_code == FUNC_READ_HOLDING
        assert exc_info.value.exception_code == 0x02
        assert writes == 1
        assert client.stats.modbus_exceptions == 1
        assert client.stats.commands_failed == 1

    async def test_write_register_verifies_echo_and_refreshes_settings(
        self, sample_ble_device: BLEDevice
    ) -> None:
        written_payloads: list[bytes] = []

        def on_write(fc: FakeBleakClient, payload: bytes, _resp: bool) -> None:
            written_payloads.append(payload)
            if payload[1] == FUNC_WRITE_MULTIPLE:
                # Send wrong register echo first (should be ignored), then correct ACK
                fc.schedule_notify(make_write_ack_frame(0x1001, 1), delay=0.0)
                fc.schedule_notify(make_write_ack_frame(0x1006, 1), delay=0.005)
            elif payload == build_settings_request():
                fc.schedule_notify(make_settings_frame(cutoff_voltage=11.3))

        async def connector(device, name, disc_cb, _dev_cb, _timeout):
            return FakeBleakClient(device.address, disconnected_callback=disc_cb, on_write=on_write)

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(connection_mode=ConnectionMode.ON_DEMAND),
            connector=connector,
        )

        settings = await client.set_voltage(
            SettingsRegister.CUTOFF_VOLTAGE, 11.3, refresh_settings=True
        )
        assert settings is not None
        assert settings.cutoff_voltage == 11.3
        # Verify 11.3V was encoded as 113 (0x0071), NOT 112!
        assert written_payloads[0][7:9] == bytes([0x00, 0x71])
        assert client.is_connected is False

    async def test_write_mode_autodetection_and_override(
        self, sample_ble_device: BLEDevice
    ) -> None:
        recorded_responses: list[bool] = []

        def on_write(fc: FakeBleakClient, _payload: bytes, resp: bool) -> None:
            recorded_responses.append(resp)
            fc.schedule_notify(make_write_ack_frame(0x1007, 1))

        # Characteristic only supports write-with-response -> auto-selects response=True
        async def connector_write_only(device, name, disc_cb, _dev_cb, _timeout):
            return FakeBleakClient(
                device.address,
                disconnected_callback=disc_cb,
                write_properties=["write"],
                on_write=on_write,
            )

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(),
            connector=connector_write_only,
        )
        await client.set_load_switch(True)
        assert recorded_responses == [True]

        # Explicit override forces response=False even on write-only properties
        client_override = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(write_response=False),
            connector=connector_write_only,
        )
        await client_override.set_load_switch(False)
        assert recorded_responses == [True, False]

    async def test_device_not_found_and_exhausted_timeouts(
        self, sample_ble_device: BLEDevice
    ) -> None:
        client_missing = ZhiJinBLEClient(
            "AA:BB:CC:DD:EE:FF",
            lambda: None,
            config=_fast_config(),
        )
        with pytest.raises(ZhiJinDeviceNotFoundError):
            await client_missing.read_telemetry()

        # Exhausted timeouts across all retries and reconnects
        async def silent_connector(device, name, disc_cb, _dev_cb, _timeout):
            return FakeBleakClient(device.address, disconnected_callback=disc_cb)

        client_timeout = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(command_timeout=0.03, command_retries=2, reconnect_retries=1),
            connector=silent_connector,
        )
        with pytest.raises(ZhiJinTimeoutError):
            await client_timeout.read_telemetry()
        assert client_timeout.stats.timeouts == 4  # 2 sessions x 2 retries
        assert client_timeout.stats.commands_failed == 1
        assert client_timeout.is_connected is False

    async def test_bleak_error_on_connect_wraps_as_connection_error(
        self, sample_ble_device: BLEDevice
    ) -> None:
        async def failing_connector(*_args):
            raise BleakError("Adapter busy")

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(),
            connector=failing_connector,
        )
        with pytest.raises(ZhiJinConnectionError):
            await client.connect()
        assert client.stats.connect_failures == 1

    async def test_async_device_provider_and_concurrent_lock_serialization(
        self, sample_ble_device: BLEDevice
    ) -> None:
        active_writes = 0
        max_concurrent = 0

        async def on_write(fc: FakeBleakClient, payload: bytes, _resp: bool) -> None:
            nonlocal active_writes, max_concurrent
            active_writes += 1
            max_concurrent = max(max_concurrent, active_writes)
            await asyncio.sleep(0.01)
            active_writes -= 1
            if payload == build_telemetry_request():
                fc.schedule_notify(REAL_TELEMETRY_12_0V)
            elif payload == build_settings_request():
                fc.schedule_notify(REAL_SETTINGS)

        async def provider():
            return sample_ble_device

        async def connector(device, name, disc_cb, _dev_cb, _timeout):
            return FakeBleakClient(device.address, disconnected_callback=disc_cb, on_write=on_write)

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            provider,
            config=_fast_config(connection_mode=ConnectionMode.PERSISTENT),
            connector=connector,
        )

        res_t, res_s = await asyncio.gather(client.read_telemetry(), client.read_settings())
        assert max_concurrent == 1
        assert res_t.battery_voltage == 12.0
        assert res_s.full_voltage == 12.6
        await client.disconnect()

    async def test_crc_error_recovery_and_raw_register_read(
        self, sample_ble_device: BLEDevice
    ) -> None:
        corrupt = bytearray(REAL_TELEMETRY_12_0V)
        corrupt[-1] ^= 0xFF

        def on_write(fc: FakeBleakClient, _payload: bytes, _resp: bool) -> None:
            # First emit corrupt frame, then valid frame
            fc.schedule_notify(bytes(corrupt), delay=0.0)
            fc.schedule_notify(REAL_TELEMETRY_12_0V, delay=0.005)

        async def connector(device, name, disc_cb, _dev_cb, _timeout):
            return FakeBleakClient(device.address, disconnected_callback=disc_cb, on_write=on_write)

        client = ZhiJinBLEClient(
            sample_ble_device.address,
            sample_ble_device,
            config=_fast_config(),
            connector=connector,
        )
        frame = await client.read_raw_registers(0x0001, 16, expected_byte_count=20)
        assert frame.raw == REAL_TELEMETRY_12_0V
        assert client.stats.crc_errors >= 1


class TestConnectionStats:
    def test_latency_rolling_window_and_percentiles(self) -> None:
        stats = ConnectionStats()
        assert stats.command_success_rate is None
        assert stats.avg_rtt_ms is None
        assert stats.p95_rtt_ms is None

        for i in range(1, 251):
            stats.record_rtt_latency(float(i))
            stats.record_connect_latency(float(i * 2))

        # Capped at MAX_LATENCY_SAMPLES (200), holding 51..250
        assert stats.last_rtt_ms == 250.0
        assert stats.avg_rtt_ms == 150.5
        assert stats.p95_rtt_ms == 240.0
        assert stats.avg_connect_ms == 301.0

        stats.commands_succeeded = 9
        stats.commands_failed = 1
        assert stats.command_success_rate == 90.0
        assert stats.to_dict()["command_success_rate"] == 90.0
