"""Resilient async BLE Modbus client for ZhiJin solar controllers.

Key connectivity features:
- Supports both ``on_demand`` (connect -> poll -> disconnect; frees ESPHome
  Bluetooth proxy slots, avoids long-lived link stagnation, lowers controller
  radio duty cycle) and ``persistent`` (low-latency keep-alive) modes.
- Uses ``BleakClientWithServiceCache`` via ``bleak-retry-connector`` when
  available so GATT service discovery is cached across reconnects.
- Absorbs the controller's multi-frame duplicate notification bursts cleanly
  without flooding warnings or colliding with subsequent commands.
- Enforces post-subscribe settle delay and minimum inter-command spacing to
  protect the controller's UART bridge buffer.
- Retries transient packet timeouts in-session before tearing down the BLE link,
  and performs one clean reconnect retry on transport loss.
- Race-safe disconnect callback that immediately wakes pending waiters instead
  of stalling until timeout, and ignores callbacks from superseded clients.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from bleak import BleakClient, BleakError
from bleak.backends.device import BLEDevice

from .const import (
    DEFAULT_COMMAND_RETRIES,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_DISCONNECT_TIMEOUT,
    DEFAULT_DUPLICATE_WINDOW,
    DEFAULT_MIN_COMMAND_INTERVAL,
    DEFAULT_POST_SUBSCRIBE_DELAY,
    DEFAULT_RECONNECT_RETRIES,
    DEFAULT_RETRY_DELAY,
    DEFAULT_UNIT_ID,
    FUNC_READ_HOLDING,
    FUNC_WRITE_MULTIPLE,
    FUNC_WRITE_SINGLE,
    NOTIFY_UUID,
    SETTINGS_RESPONSE_BYTES,
    TELEMETRY_RESPONSE_BYTES,
    WRITE_UUID,
    BatteryType,
    ConnectionMode,
    SettingsRegister,
)
from .exceptions import (
    ZhiJinConnectionError,
    ZhiJinDeviceNotFoundError,
    ZhiJinDisconnectedError,
    ZhiJinModbusException,
    ZhiJinProtocolError,
    ZhiJinTimeoutError,
)
from .models import ConnectionStats, ModbusFrame, SettingsData, TelemetryData
from .protocol import (
    ModbusFrameParser,
    build_read_holding,
    build_settings_request,
    build_telemetry_request,
    build_write_multiple,
    decode_settings,
    decode_telemetry,
    decode_write_ack,
    encode_voltage,
    recalculate_telemetry_for_battery_type,
)

LOGGER = logging.getLogger(__name__)

BLEDeviceProvider = Callable[[], Awaitable[BLEDevice | None] | BLEDevice | None]
ConnectorFunc = Callable[
    [BLEDevice, str, Callable[[Any], None], Callable[[], BLEDevice | None] | None, float],
    Awaitable[Any],
]
EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class ClientConfig:
    """Tuning parameters for ``ZhiJinBLEClient``."""

    unit_id: int = DEFAULT_UNIT_ID
    connection_mode: ConnectionMode = ConnectionMode.ON_DEMAND
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    disconnect_timeout: float = DEFAULT_DISCONNECT_TIMEOUT
    command_retries: int = DEFAULT_COMMAND_RETRIES
    reconnect_retries: int = DEFAULT_RECONNECT_RETRIES
    retry_delay: float = DEFAULT_RETRY_DELAY
    post_subscribe_delay: float = DEFAULT_POST_SUBSCRIBE_DELAY
    min_command_interval: float = DEFAULT_MIN_COMMAND_INTERVAL
    duplicate_window: float = DEFAULT_DUPLICATE_WINDOW
    use_retry_connector: bool = True
    write_response: bool | None = None

    def __post_init__(self) -> None:
        if self.command_timeout <= 0:
            raise ValueError("command_timeout must be > 0")
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be > 0")
        if self.command_retries < 1:
            raise ValueError("command_retries must be >= 1")
        if self.reconnect_retries < 0:
            raise ValueError("reconnect_retries must be >= 0")


@dataclass(slots=True)
class _PendingRequest:
    """In-flight Modbus request awaiting a matching CRC-verified response frame."""

    function_code: int
    expected_byte_count: int | None
    expected_write_echo: tuple[int, int] | None
    future: asyncio.Future[ModbusFrame]


async def _default_connector(
    device: BLEDevice,
    name: str,
    disconnected_callback: Callable[[Any], None],
    ble_device_callback: Callable[[], BLEDevice | None] | None,
    timeout: float,
    *,
    use_retry_connector: bool = True,
) -> BleakClient:
    """Establish a BLE connection using bleak-retry-connector if enabled and available."""
    if use_retry_connector:
        try:
            from bleak_retry_connector import (
                BleakClientWithServiceCache,
                establish_connection,
            )

            kwargs: dict[str, Any] = {
                "client_class": BleakClientWithServiceCache,
                "device": device,
                "name": name,
                "disconnected_callback": disconnected_callback,
                "use_services_cache": True,
            }
            if ble_device_callback is not None:
                kwargs["ble_device_callback"] = lambda: ble_device_callback() or device

            return await asyncio.wait_for(establish_connection(**kwargs), timeout=timeout)
        except ImportError:
            LOGGER.debug("bleak-retry-connector unavailable; falling back to direct BleakClient")

    client = BleakClient(device, disconnected_callback=disconnected_callback, timeout=timeout)
    await asyncio.wait_for(client.connect(), timeout=timeout)
    return client


class ZhiJinBLEClient:
    """Thread-safe / coroutine-serialized BLE client for ZhiJin solar controllers."""

    def __init__(
        self,
        address: str,
        device_provider: BLEDeviceProvider | BLEDevice,
        *,
        name: str | None = None,
        config: ClientConfig | None = None,
        connector: ConnectorFunc | None = None,
        event_callback: EventCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.address = address
        self.name = name or address
        self.config = config or ClientConfig()
        self._event_callback = event_callback
        self._clock = clock

        if isinstance(device_provider, BLEDevice):
            self._cached_device: BLEDevice | None = device_provider
            self._device_provider: BLEDeviceProvider = lambda: self._cached_device
            self._sync_provider: Callable[[], BLEDevice | None] | None = (
                lambda: self._cached_device
            )
        else:
            self._cached_device = None
            self._device_provider = device_provider
            self._sync_provider = (
                None if inspect.iscoroutinefunction(device_provider) else device_provider  # type: ignore[assignment]
            )

        self._connector = connector
        self._client: Any | None = None
        self._notifying = False
        self._disconnecting = False
        self._resolved_write_response: bool | None = None
        self._lock = asyncio.Lock()

        self._parser = ModbusFrameParser(unit_id=self.config.unit_id)
        self._pending: _PendingRequest | None = None
        self._last_completed_frame: bytes | None = None
        self._last_completed_at: float = 0.0
        self._last_command_finished_at: float = 0.0
        self._last_battery_type: int = BatteryType.LEAD_ACID
        self._last_full_voltage: float | None = None

        self.stats = ConnectionStats()

    @property
    def is_connected(self) -> bool:
        """Return True if the underlying BLE transport is currently connected."""
        return bool(self._client is not None and self._client.is_connected)

    def update_device(self, device: BLEDevice) -> None:
        """Update cached BLEDevice reference (e.g., from a fresh advertisement)."""
        self._cached_device = device
        if device.name and self.name == self.address:
            self.name = device.name

    def _emit(self, event: str, **payload: Any) -> None:
        if self._event_callback is not None:
            try:
                self._event_callback(event, payload)
            except Exception:
                LOGGER.debug("Event callback failed for %s", event, exc_info=True)

    async def _resolve_device(self) -> BLEDevice:
        """Obtain the freshest BLEDevice from provider."""
        provided = self._device_provider()
        if inspect.isawaitable(provided):
            device = await provided
        else:
            device = provided

        if device is not None:
            self._cached_device = device
            if device.name and self.name == self.address:
                self.name = device.name
            return device

        raise ZhiJinDeviceNotFoundError(
            f"ZhiJin BLE device {self.address} could not be discovered or resolved"
        )

    async def connect(self) -> None:
        """Ensure an active BLE connection with notifications subscribed."""
        if self.is_connected and self._notifying:
            return

        # Clean up any half-open client before establishing a new connection.
        if self._client is not None:
            await self._disconnect_unlocked()

        device = await self._resolve_device()
        self.stats.connect_attempts += 1
        started = self._clock()
        self._emit("connect_start", address=self.address)

        client: Any | None = None
        try:
            if self._connector is not None:
                client = await self._connector(
                    device,
                    self.name,
                    self._on_disconnected,
                    self._sync_provider,
                    self.config.connect_timeout,
                )
            else:
                client = await _default_connector(
                    device,
                    self.name,
                    self._on_disconnected,
                    self._sync_provider,
                    self.config.connect_timeout,
                    use_retry_connector=self.config.use_retry_connector,
                )

            self._client = client
            self._parser.clear()
            self._resolved_write_response = self._detect_write_response(client)

            await asyncio.wait_for(
                client.start_notify(NOTIFY_UUID, self._on_notification),
                timeout=self.config.command_timeout,
            )
            self._notifying = True

            if self.config.post_subscribe_delay > 0:
                await asyncio.sleep(self.config.post_subscribe_delay)

            elapsed_ms = (self._clock() - started) * 1000.0
            self.stats.connect_successes += 1
            self.stats.record_connect_latency(elapsed_ms)
            self._emit("connect_success", address=self.address, latency_ms=round(elapsed_ms, 1))
            LOGGER.debug("Connected to %s in %.1f ms", self.name, elapsed_ms)

        except Exception as err:
            self.stats.connect_failures += 1
            self._emit("connect_error", address=self.address, error=str(err))
            if client is not None:
                self._client = client
                await self._disconnect_unlocked()
            else:
                self._client = None
                self._notifying = False
            if isinstance(err, ZhiJinConnectionError):
                raise
            raise ZhiJinConnectionError(f"Failed to connect to {self.address}: {err}") from err

    async def disconnect(self) -> None:
        """Cleanly unsubscribe notifications and disconnect the BLE link."""
        async with self._lock:
            await self._disconnect_unlocked()

    async def _disconnect_unlocked(self) -> None:
        """Internal disconnect helper (must be called under ``self._lock`` or during cleanup)."""
        client = self._client
        was_notifying = self._notifying
        self._client = None
        self._notifying = False
        self._resolved_write_response = None
        self._parser.clear()

        if self._pending is not None and not self._pending.future.done():
            self._pending.future.set_exception(ZhiJinDisconnectedError("Disconnected locally"))
        self._pending = None

        if client is None:
            return

        self._disconnecting = True
        try:
            if was_notifying and getattr(client, "is_connected", False):
                try:
                    await asyncio.wait_for(
                        client.stop_notify(NOTIFY_UUID),
                        timeout=min(2.0, self.config.disconnect_timeout),
                    )
                except Exception:
                    LOGGER.debug("Ignoring stop_notify error during disconnect", exc_info=True)

            try:
                await asyncio.wait_for(client.disconnect(), timeout=self.config.disconnect_timeout)
            except Exception:
                LOGGER.debug("Ignoring disconnect error", exc_info=True)
            self.stats.disconnects += 1
            self._emit("disconnect", address=self.address, expected=True)
        finally:
            self._disconnecting = False

    def _on_disconnected(self, client: Any) -> None:
        """Bleak disconnect callback — ignores stale clients and wakes pending waiters."""
        if self._client is not client:
            LOGGER.debug("Ignoring disconnect callback from superseded BLE client")
            return

        self._client = None
        self._notifying = False
        self._resolved_write_response = None
        self._parser.clear()

        if not self._disconnecting:
            self.stats.unexpected_disconnects += 1
            self._emit("disconnect", address=self.address, expected=False)
            LOGGER.debug("Unexpected BLE disconnect from %s", self.address)

        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_exception(
                ZhiJinDisconnectedError(f"BLE link dropped by {self.address} while awaiting reply")
            )

    def _detect_write_response(self, client: Any) -> bool:
        """Choose write-without-response vs write-with-response from GATT properties."""
        if self.config.write_response is not None:
            return self.config.write_response

        services = getattr(client, "services", None)
        if services is not None:
            try:
                char = services.get_characteristic(WRITE_UUID)
            except Exception:
                char = None
            if char is not None:
                props = {p.lower() for p in getattr(char, "properties", ())}
                if "write-without-response" in props:
                    return False
                if "write" in props:
                    return True
        return False

    def _on_notification(self, _sender: Any, data: bytearray) -> None:
        """Reassemble incoming BLE notification chunks and dispatch matched frames."""
        raw_chunk = bytes(data)
        self._emit("rx_chunk", hex=raw_chunk.hex(), length=len(raw_chunk))

        prev_crc_errors = self._parser.crc_errors
        frames = self._parser.feed(raw_chunk)
        new_crc_errors = self._parser.crc_errors - prev_crc_errors
        if new_crc_errors:
            self.stats.crc_errors += new_crc_errors
            self._emit("crc_error", count=new_crc_errors, chunk_hex=raw_chunk.hex())
            LOGGER.debug("CRC error(s) (%d) while parsing chunk %s", new_crc_errors, raw_chunk.hex())

        now = self._clock()
        for frame in frames:
            self._handle_frame(frame, now)

    def _handle_frame(self, frame: ModbusFrame, now: float) -> None:
        """Route a CRC-verified frame to the pending request or absorb duplicates."""
        self._emit(
            "rx_frame",
            hex=frame.raw.hex(),
            func=frame.function_code,
            byte_count=frame.byte_count,
            is_exception=frame.is_exception,
            exception_code=frame.exception_code,
        )

        pending = self._pending
        if pending is not None and not pending.future.done():
            if frame.is_exception and frame.function_code == pending.function_code:
                self.stats.modbus_exceptions += 1
                pending.future.set_exception(
                    ZhiJinModbusException(frame.function_code, frame.exception_code or 0)
                )
                return

            if self._frame_matches_pending(frame, pending):
                self._last_completed_frame = frame.raw
                self._last_completed_at = now
                pending.future.set_result(frame)
                return

        # Frame did not match an active request: classify duplicate bursts vs unsolicited traffic.
        if (
            self._last_completed_frame is not None
            and frame.raw == self._last_completed_frame
            and (now - self._last_completed_at) <= self.config.duplicate_window
        ):
            self.stats.duplicate_frames += 1
            self._emit("rx_duplicate", hex=frame.raw.hex())
            LOGGER.debug("Absorbed duplicate burst frame: %s", frame.raw.hex())
        else:
            self.stats.unsolicited_frames += 1
            self._emit("rx_unsolicited", hex=frame.raw.hex())
            LOGGER.debug("Ignored unsolicited Modbus frame: %s", frame.raw.hex())

    @staticmethod
    def _frame_matches_pending(frame: ModbusFrame, pending: _PendingRequest) -> bool:
        if frame.is_exception or frame.function_code != pending.function_code:
            return False
        if (
            pending.function_code == FUNC_READ_HOLDING
            and pending.expected_byte_count is not None
            and frame.byte_count != pending.expected_byte_count
        ):
            return False
        if (
            pending.function_code in (FUNC_WRITE_SINGLE, FUNC_WRITE_MULTIPLE)
            and pending.expected_write_echo is not None
        ):
            try:
                echo = decode_write_ack(frame)
            except ZhiJinProtocolError:
                return False
            if echo != pending.expected_write_echo:
                return False
        return True

    async def _respect_command_spacing(self) -> None:
        """Wait for any remaining inter-command quiet period so trailing bursts drain."""
        if self.config.min_command_interval <= 0 or self._last_command_finished_at <= 0:
            return
        elapsed = self._clock() - self._last_command_finished_at
        remaining = self.config.min_command_interval - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _send_frame_once(
        self,
        payload: bytes,
        *,
        expected_func: int,
        expected_byte_count: int | None = None,
        expected_write_echo: tuple[int, int] | None = None,
    ) -> ModbusFrame:
        """Transmit one Modbus frame and wait for its matched response."""
        if not self.is_connected or not self._notifying:
            await self.connect()

        await self._respect_command_spacing()
        self._parser.clear()

        loop = asyncio.get_running_loop()
        pending = _PendingRequest(
            function_code=expected_func,
            expected_byte_count=expected_byte_count,
            expected_write_echo=expected_write_echo,
            future=loop.create_future(),
        )
        self._pending = pending
        self.stats.commands_sent += 1
        started = self._clock()
        self._emit("tx_frame", hex=payload.hex(), func=expected_func)

        try:
            write_resp = bool(self._resolved_write_response)
            await self._client.write_gatt_char(WRITE_UUID, payload, response=write_resp)
            frame = await asyncio.wait_for(pending.future, timeout=self.config.command_timeout)
            rtt_ms = (self._clock() - started) * 1000.0
            self.stats.record_rtt_latency(rtt_ms)
            return frame
        except TimeoutError as err:
            self.stats.timeouts += 1
            self._emit("command_timeout", hex=payload.hex(), timeout=self.config.command_timeout)
            raise ZhiJinTimeoutError(
                f"Timed out after {self.config.command_timeout:.1f}s waiting for "
                f"function 0x{expected_func:02X} response from {self.address}"
            ) from err
        except (BleakError, OSError) as err:
            raise ZhiJinDisconnectedError(f"BLE write failed for {self.address}: {err}") from err
        finally:
            self._pending = None
            self._last_command_finished_at = self._clock()

    async def _execute_command(
        self,
        payload: bytes,
        *,
        expected_func: int,
        expected_byte_count: int | None = None,
        expected_write_echo: tuple[int, int] | None = None,
    ) -> ModbusFrame:
        """Execute a Modbus command with in-session retries and optional reconnect recovery."""
        last_error: Exception | None = None
        total_sessions = 1 + self.config.reconnect_retries

        for session_idx in range(total_sessions):
            if session_idx > 0:
                LOGGER.debug(
                    "Reconnecting to %s (recovery attempt %d/%d)",
                    self.address,
                    session_idx,
                    self.config.reconnect_retries,
                )
                await self._disconnect_unlocked()
                await self.connect()

            for attempt in range(self.config.command_retries):
                if attempt > 0:
                    self.stats.command_retries += 1
                    self._emit("command_retry", attempt=attempt + 1, session=session_idx + 1)
                    if self.config.retry_delay > 0:
                        await asyncio.sleep(self.config.retry_delay)

                try:
                    frame = await self._send_frame_once(
                        payload,
                        expected_func=expected_func,
                        expected_byte_count=expected_byte_count,
                        expected_write_echo=expected_write_echo,
                    )
                    self.stats.commands_succeeded += 1
                    return frame
                except ZhiJinModbusException:
                    # Protocol-level rejection from the controller will not be fixed by retrying.
                    self.stats.commands_failed += 1
                    raise
                except ZhiJinDisconnectedError as err:
                    # Link is dead; break out to reconnect loop immediately.
                    last_error = err
                    break
                except ZhiJinTimeoutError as err:
                    last_error = err
                    LOGGER.debug(
                        "Command timeout on %s (session %d/%d, attempt %d/%d)",
                        self.address,
                        session_idx + 1,
                        total_sessions,
                        attempt + 1,
                        self.config.command_retries,
                    )

        self.stats.commands_failed += 1
        await self._disconnect_unlocked()
        assert last_error is not None
        raise last_error

    @asynccontextmanager
    async def _operation_session(self):
        """Serialize access and enforce on_demand disconnect after an operation finishes."""
        async with self._lock:
            try:
                yield
            finally:
                if self.config.connection_mode == ConnectionMode.ON_DEMAND:
                    await self._disconnect_unlocked()

    async def read_telemetry(
        self,
        *,
        battery_type: int | None = None,
        full_voltage: float | None = None,
    ) -> TelemetryData:
        """Read and decode real-time telemetry registers (0x0001..0x000A)."""
        async with self._operation_session():
            return await self._read_telemetry_unlocked(
                battery_type=battery_type,
                full_voltage=full_voltage,
            )

    async def _read_telemetry_unlocked(
        self,
        *,
        battery_type: int | None = None,
        full_voltage: float | None = None,
    ) -> TelemetryData:
        frame = await self._execute_command(
            build_telemetry_request(unit_id=self.config.unit_id),
            expected_func=FUNC_READ_HOLDING,
            expected_byte_count=TELEMETRY_RESPONSE_BYTES,
        )
        effective_type = self._last_battery_type if battery_type is None else battery_type
        effective_full_v = self._last_full_voltage if full_voltage is None else full_voltage
        return decode_telemetry(
            frame.register_data,
            battery_type=effective_type,
            full_voltage=effective_full_v,
        )

    async def read_settings(self) -> SettingsData:
        """Read and decode controller settings registers (0x1001..0x1009)."""
        async with self._operation_session():
            return await self._read_settings_unlocked()

    async def _read_settings_unlocked(self) -> SettingsData:
        frame = await self._execute_command(
            build_settings_request(unit_id=self.config.unit_id),
            expected_func=FUNC_READ_HOLDING,
            expected_byte_count=SETTINGS_RESPONSE_BYTES,
        )
        settings = decode_settings(frame.register_data)
        self._last_battery_type = settings.battery_type
        self._last_full_voltage = settings.full_voltage
        return settings

    async def poll(
        self,
        *,
        include_settings: bool = False,
        battery_type: int | None = None,
        full_voltage: float | None = None,
    ) -> tuple[TelemetryData, SettingsData | None]:
        """Read telemetry (and optionally settings) within a single BLE session.

        In ``on_demand`` mode both reads reuse the same connection and cleanly
        disconnect once complete.
        """
        async with self._operation_session():
            settings: SettingsData | None = None
            if include_settings:
                # Fetch telemetry first, then settings (matching official controller sequence).
                telemetry = await self._read_telemetry_unlocked(
                    battery_type=battery_type,
                    full_voltage=full_voltage,
                )
                settings = await self._read_settings_unlocked()
                telemetry = recalculate_telemetry_for_battery_type(
                    telemetry,
                    settings.battery_type,
                    full_voltage=settings.full_voltage,
                )
            else:
                telemetry = await self._read_telemetry_unlocked(
                    battery_type=battery_type,
                    full_voltage=full_voltage,
                )
            return telemetry, settings

    async def read_raw_registers(
        self,
        start_reg: int,
        count: int,
        *,
        expected_byte_count: int | None = None,
    ) -> ModbusFrame:
        """Send a raw function 0x03 Read Holding Registers request (for probing/debugging)."""
        async with self._operation_session():
            return await self._execute_command(
                build_read_holding(start_reg, count, unit_id=self.config.unit_id),
                expected_func=FUNC_READ_HOLDING,
                expected_byte_count=expected_byte_count,
            )

    async def write_register(
        self,
        register: int,
        value: int,
        *,
        refresh_settings: bool = False,
    ) -> SettingsData | None:
        """Write a single holding register via Modbus function 0x10.

        Optionally reads back the full settings block within the same BLE session.
        """
        async with self._operation_session():
            await self._execute_command(
                build_write_multiple(register, [value], unit_id=self.config.unit_id),
                expected_func=FUNC_WRITE_MULTIPLE,
                expected_write_echo=(register, 1),
            )
            if refresh_settings:
                return await self._read_settings_unlocked()
            return None

    async def set_load_switch(
        self, enabled: bool, *, refresh_settings: bool = False
    ) -> SettingsData | None:
        """Toggle the manual load output switch (register 0x1007)."""
        return await self.write_register(
            SettingsRegister.LOAD_SWITCH,
            1 if enabled else 0,
            refresh_settings=refresh_settings,
        )

    async def set_voltage(
        self,
        register: SettingsRegister,
        volts: float,
        *,
        refresh_settings: bool = False,
    ) -> SettingsData | None:
        """Write a voltage threshold register using proper 0.1V rounding."""
        return await self.write_register(
            register,
            encode_voltage(volts),
            refresh_settings=refresh_settings,
        )
