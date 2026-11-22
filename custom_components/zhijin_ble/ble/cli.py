"""Standalone macOS / desktop CLI for ZhiJin BLE testing and long debug sessions.

Runs directly via ``bleak`` with zero Home Assistant dependencies so you can
connect from a MacBook and run multi-hour reliability soak tests, inspect raw
Modbus/BLE packet bursts, and tune polling / power-saving parameters.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from .client import ClientConfig, ZhiJinBLEClient
from .const import (
    BATTERY_TYPE_LABELS,
    CUTOFF_VOLTAGE_RANGE,
    DEFAULT_COMMAND_RETRIES,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POWER_SAVE_INTERVAL,
    DEFAULT_RECONNECT_RETRIES,
    DEFAULT_SETTINGS_INTERVAL,
    DEFAULT_TEMPERATURE_OFFSET,
    FULL_VOLTAGE_RANGE,
    OUTPUT_MODE_LABELS,
    RECOVERY_VOLTAGE_RANGE,
    TEMPERATURE_OFFSET_RANGE,
    VOLTAGE_DETECT_MODE_LABELS,
    ConnectionMode,
    PowerSaveMode,
    SettingsRegister,
)
from .exceptions import ZhiJinError
from .models import ConnectionStats, SettingsData, TelemetryData
from .poller import AdaptivePollScheduler, PollPolicy
from .protocol import encode_voltage
from .scanner import DiscoveredController, find_controller, scan_controllers

LOGGER = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _local_ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _parse_int_auto(value: str) -> int:
    return int(value, 0)


def _label_lookup(mapping: dict[Any, str], raw: str) -> int:
    normalized = raw.strip().lower().replace("_", "-")
    for enum_val, label in mapping.items():
        if label.lower() == normalized or enum_val.name.lower().replace("_", "-") == normalized:
            return int(enum_val)
    choices = ", ".join(mapping.values())
    raise ValueError(f"Invalid option '{raw}'. Valid choices: {choices}")


class _JsonlSessionLogger:
    """Append-only JSONL writer for post-session analysis of long debug runs."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._fh: TextIO | None = None

    def __enter__(self) -> _JsonlSessionLogger:
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self._path.open("a", encoding="utf-8")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    def write(self, event: str, payload: dict[str, Any]) -> None:
        if self._fh is None:
            return
        record = {"ts": _iso_now(), "event": event, **payload}
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()


def format_telemetry_block(
    telemetry: TelemetryData,
    settings: SettingsData | None = None,
    *,
    temperature_offset: float = 0.0,
) -> str:
    """Render a human-readable summary of controller telemetry and settings."""
    calibrated_temp = round(telemetry.temperature + temperature_offset, 2)
    temp_line = (
        f"  Temperature            : {calibrated_temp:.2f} °C "
        f"(raw={telemetry.temperature:.2f} °C, offset={temperature_offset:+.2f} °C)"
        if temperature_offset != 0.0
        else f"  Temperature            : {telemetry.temperature:.2f} °C"
    )
    lines = [
        "Telemetry:",
        f"  Battery Voltage        : {telemetry.battery_voltage:.1f} V",
        f"  Battery Percent        : {telemetry.battery_percent:.1f} %",
        f"  Healthy Usable Battery : {telemetry.healthy_usable_battery:.1f} %",
        f"  Charge Current         : {telemetry.charge_current:.1f} A  (Solar Power: {telemetry.solar_power:.1f} W)",
        f"  Discharge Current      : {telemetry.discharge_current:.1f} A  (Load Power:  {telemetry.load_power:.1f} W)",
        temp_line,
        f"  Status                 : solar={'ON' if telemetry.solar_status else 'OFF'}, "
        f"load={'ON' if telemetry.load_status else 'OFF'}, "
        f"wind={'ON' if telemetry.wind_status else 'OFF'}",
        f"  Total Generation       : {telemetry.total_generation:.1f} Ah "
        f"(raw={telemetry.total_power_raw}, resets={telemetry.total_power_resets})",
        f"  Controller Model Type  : {telemetry.solar_model_type}",
    ]
    if settings is not None:
        lines.extend(
            [
                "Settings:",
                f"  Battery Type           : {settings.battery_type_label or settings.battery_type}",
                f"  Output Mode            : {settings.output_mode_label or settings.output_mode}",
                f"  Voltage Detect Mode    : {settings.voltage_detect_mode_label or settings.voltage_detect_mode}",
                f"  Manual Load Switch     : {'ON' if settings.load_switch else 'OFF'}",
                f"  Full Voltage           : {settings.full_voltage:.1f} V",
                f"  Recovery Voltage       : {settings.recovery_voltage:.1f} V",
                f"  Cutoff Voltage         : {settings.cutoff_voltage:.1f} V",
                f"  Timer                  : {settings.time_hour:02d}:{settings.time_min:02d}",
            ]
        )
    return "\n".join(lines)


def format_session_summary(
    *,
    duration_s: float,
    total_polls: int,
    successful_polls: int,
    failed_polls: int,
    stats: ConnectionStats,
    errors_by_type: dict[str, int],
) -> str:
    """Format a compact end-of-session reliability report."""
    poll_rate = (successful_polls / total_polls * 100.0) if total_polls else 0.0
    lines = [
        "",
        "=== ZhiJin BLE Debug Session Summary ===",
        f"Duration              : {duration_s:.1f} s",
        f"Polls                 : {total_polls} total | {successful_polls} ok | {failed_polls} failed ({poll_rate:.1f}%)",
        f"Connections           : {stats.connect_attempts} attempts | {stats.connect_successes} ok | "
        f"{stats.connect_failures} fail | {stats.unexpected_disconnects} unexpected drops",
        f"Connect Latency (ms)  : avg={stats.avg_connect_ms} | last={stats.last_connect_ms}",
        f"Commands              : {stats.commands_sent} sent | {stats.commands_succeeded} ok | "
        f"{stats.commands_failed} fail | {stats.command_retries} retries | "
        f"success={stats.command_success_rate}%",
        f"Command RTT (ms)      : avg={stats.avg_rtt_ms} | p95={stats.p95_rtt_ms} | last={stats.last_rtt_ms}",
        f"Packet Hygiene        : timeouts={stats.timeouts} | crc_errors={stats.crc_errors} | "
        f"duplicate_bursts={stats.duplicate_frames} | unsolicited={stats.unsolicited_frames} | "
        f"modbus_exc={stats.modbus_exceptions}",
    ]
    if errors_by_type:
        err_str = ", ".join(f"{name}={count}" for name, count in sorted(errors_by_type.items()))
        lines.append(f"Errors by Type        : {err_str}")
    return "\n".join(lines)


async def _resolve_target(
    address: str | None,
    name: str | None,
    scan_timeout: float,
    adapter: str | None,
) -> DiscoveredController:
    found = await find_controller(
        address=address,
        name=name,
        timeout=scan_timeout,
        adapter=adapter,
    )
    if found is None:
        target_desc = address or name or "any ZJ* controller"
        raise ZhiJinError(
            f"Could not find {target_desc} within {scan_timeout:.1f}s. "
            "Make sure the controller is powered on and no phone app is connected."
        )
    return found


def _build_client(
    target: DiscoveredController,
    *,
    mode: ConnectionMode,
    timeout: float,
    retries: int,
    reconnect_retries: int,
    scan_timeout: float,
    adapter: str | None,
    use_retry_connector: bool,
    raw_trace: bool,
    session_logger: _JsonlSessionLogger | None,
) -> ZhiJinBLEClient:
    """Create a ZhiJinBLEClient backed by live BleakScanner re-resolution."""
    latest_rssi: dict[str, int | None] = {"value": target.rssi}

    async def _device_provider():
        found = await find_controller(
            address=target.address,
            timeout=scan_timeout,
            adapter=adapter,
        )
        if found is not None:
            latest_rssi["value"] = found.rssi
            return found.device
        return target.device

    def _on_event(event: str, payload: dict[str, Any]) -> None:
        if session_logger is not None:
            session_logger.write(event, payload)
        if raw_trace and event in (
            "tx_frame",
            "rx_chunk",
            "rx_frame",
            "rx_duplicate",
            "rx_unsolicited",
            "crc_error",
            "command_timeout",
            "command_retry",
            "connect_start",
            "connect_success",
            "connect_error",
            "disconnect",
        ):
            detail = " ".join(f"{k}={v}" for k, v in payload.items())
            print(f"[{_local_ts()}] TRACE {event:<16} {detail}", flush=True)

    config = ClientConfig(
        connection_mode=mode,
        command_timeout=timeout,
        command_retries=retries,
        reconnect_retries=reconnect_retries,
        use_retry_connector=use_retry_connector,
    )
    client = ZhiJinBLEClient(
        address=target.address,
        device_provider=_device_provider,
        name=target.name,
        config=config,
        event_callback=_on_event,
    )
    client.update_device(target.device)
    client._cli_rssi_ref = latest_rssi  # type: ignore[attr-defined]
    return client


async def _cmd_scan(args: argparse.Namespace) -> int:
    controllers = await scan_controllers(
        timeout=args.scan_timeout,
        include_service_uuid_match=args.all,
        adapter=args.adapter,
    )
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": c.name,
                        "address": c.address,
                        "rssi": c.rssi,
                        "service_uuids": list(c.service_uuids),
                    }
                    for c in controllers
                ],
                indent=2,
            )
        )
        return 0

    if not controllers:
        print(f"No ZhiJin BLE controllers found in {args.scan_timeout:.1f}s.")
        return 1

    print(f"Found {len(controllers)} controller(s):")
    for idx, ctrl in enumerate(controllers, start=1):
        rssi_str = f"{ctrl.rssi} dBm" if ctrl.rssi is not None else "n/a"
        print(f"  {idx}. {ctrl.name:<24} address={ctrl.address}  rssi={rssi_str}")
        if ctrl.service_uuids:
            print(f"     services: {', '.join(ctrl.service_uuids)}")
    return 0


async def _cmd_read(args: argparse.Namespace) -> int:
    _check_range("temp-offset", args.temp_offset, TEMPERATURE_OFFSET_RANGE)
    target = await _resolve_target(args.address, args.name, args.scan_timeout, args.adapter)
    with _JsonlSessionLogger(Path(args.log_file) if args.log_file else None) as slog:
        client = _build_client(
            target,
            mode=ConnectionMode.ON_DEMAND,
            timeout=args.timeout,
            retries=args.retries,
            reconnect_retries=args.reconnect_retries,
            scan_timeout=args.scan_timeout,
            adapter=args.adapter,
            use_retry_connector=not args.direct_bleak,
            raw_trace=args.raw,
            session_logger=slog,
        )
        try:
            telemetry, settings = await client.poll(include_settings=True)
        finally:
            await client.disconnect()

    calibrated_temp = round(telemetry.temperature + args.temp_offset, 2)
    if args.json:
        telem_dict = telemetry.to_dict()
        telem_dict["calibrated_temperature"] = calibrated_temp
        telem_dict["temperature_offset"] = args.temp_offset
        print(
            json.dumps(
                {
                    "device": {"name": target.name, "address": target.address, "rssi": target.rssi},
                    "telemetry": telem_dict,
                    "settings": settings.to_dict() if settings else None,
                    "stats": client.stats.to_dict(),
                },
                indent=2,
            )
        )
    else:
        print(f"Device: {target.name} ({target.address})  RSSI={target.rssi} dBm")
        print(
            format_telemetry_block(
                telemetry,
                settings,
                temperature_offset=args.temp_offset,
            )
        )
        print(
            f"\nStats: connect={client.stats.last_connect_ms} ms, "
            f"avg_rtt={client.stats.avg_rtt_ms} ms, "
            f"duplicates={client.stats.duplicate_frames}, "
            f"retries={client.stats.command_retries}"
        )
    return 0


async def _cmd_monitor(args: argparse.Namespace) -> int:
    _check_range("temp-offset", args.temp_offset, TEMPERATURE_OFFSET_RANGE)
    target = await _resolve_target(args.address, args.name, args.scan_timeout, args.adapter)
    mode = ConnectionMode(args.mode)
    policy = PollPolicy(
        poll_interval=args.interval,
        power_save_mode=PowerSaveMode(args.power_save),
        power_save_interval=args.night_interval,
        settings_interval=args.settings_interval,
    )
    scheduler = AdaptivePollScheduler(policy)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    print(
        f"Starting monitor on {target.name} ({target.address}) | mode={mode.value} | "
        f"interval={policy.poll_interval}s | power_save={policy.power_save_mode.value} "
        f"({policy.power_save_interval}s) | settings={policy.settings_interval}s"
    )
    if args.log_file:
        print(f"Logging structured session events to: {args.log_file}")
    print("Press Ctrl+C to stop and print the reliability summary.\n", flush=True)

    start_mono = time.monotonic()
    total_polls = 0
    successful_polls = 0
    failed_polls = 0
    errors_by_type: dict[str, int] = {}
    last_telemetry: TelemetryData | None = None
    last_settings: SettingsData | None = None

    with _JsonlSessionLogger(Path(args.log_file) if args.log_file else None) as slog:
        client = _build_client(
            target,
            mode=mode,
            timeout=args.timeout,
            retries=args.retries,
            reconnect_retries=args.reconnect_retries,
            scan_timeout=args.scan_timeout,
            adapter=args.adapter,
            use_retry_connector=not args.direct_bleak,
            raw_trace=args.raw,
            session_logger=slog,
        )

        try:
            while not stop_event.is_set():
                if args.max_polls and total_polls >= args.max_polls:
                    break
                if args.duration and (time.monotonic() - start_mono) >= args.duration:
                    break

                total_polls += 1
                poll_started = time.monotonic()
                fetch_settings = scheduler.should_fetch_settings(
                    has_settings=last_settings is not None
                )

                try:
                    telemetry, settings = await client.poll(
                        include_settings=fetch_settings,
                        battery_type=last_settings.battery_type if last_settings else None,
                        full_voltage=last_settings.full_voltage if last_settings else None,
                    )
                    elapsed_ms = (time.monotonic() - poll_started) * 1000.0
                    last_telemetry = telemetry
                    if settings is not None:
                        last_settings = settings
                        scheduler.mark_settings_fetched()
                    scheduler.record_success()
                    successful_polls += 1

                    next_wait = scheduler.next_interval(last_telemetry)
                    rssi = getattr(client, "_cli_rssi_ref", {}).get("value")
                    ok_pct = (successful_polls / total_polls) * 100.0
                    ps_tag = "PS" if scheduler.power_save_active else "NORM"
                    settings_tag = "+SET" if settings is not None else ""

                    calibrated_temp = round(telemetry.temperature + args.temp_offset, 2)
                    print(
                        f"[{_local_ts()}] #{total_polls:04d} OK{settings_tag:<4} "
                        f"{elapsed_ms:6.0f}ms | V={telemetry.battery_voltage:4.1f}V "
                        f"({telemetry.battery_percent:5.1f}% / H:{telemetry.healthy_usable_battery:5.1f}%) "
                        f"I_chg={telemetry.charge_current:4.1f}A P={telemetry.solar_power:5.1f}W "
                        f"I_dis={telemetry.discharge_current:4.1f}A T={calibrated_temp:5.2f}C "
                        f"SLW={int(telemetry.solar_status)}{int(telemetry.load_status)}{int(telemetry.wind_status)} "
                        f"Ah={telemetry.total_generation:5.1f} | "
                        f"RSSI={rssi} ok={ok_pct:5.1f}% dup={client.stats.duplicate_frames} "
                        f"[{ps_tag} next={next_wait}s]",
                        flush=True,
                    )
                    slog.write(
                        "poll_ok",
                        {
                            "poll": total_polls,
                            "elapsed_ms": round(elapsed_ms, 1),
                            "rssi": rssi,
                            "power_save": scheduler.power_save_active,
                            "next_interval_s": next_wait,
                            "temperature_offset": args.temp_offset,
                            "calibrated_temperature": calibrated_temp,
                            "telemetry": telemetry.to_dict(),
                            "settings": settings.to_dict() if settings else None,
                        },
                    )
                except Exception as err:
                    elapsed_ms = (time.monotonic() - poll_started) * 1000.0
                    failed_polls += 1
                    err_name = type(err).__name__
                    errors_by_type[err_name] = errors_by_type.get(err_name, 0) + 1
                    status = scheduler.record_failure()
                    next_wait = policy.poll_interval
                    ok_pct = (successful_polls / total_polls) * 100.0

                    print(
                        f"[{_local_ts()}] #{total_polls:04d} FAIL {elapsed_ms:6.0f}ms | "
                        f"{err_name}: {err} (consec={status.consecutive_failures}, ok={ok_pct:5.1f}%)",
                        flush=True,
                    )
                    slog.write(
                        "poll_fail",
                        {
                            "poll": total_polls,
                            "elapsed_ms": round(elapsed_ms, 1),
                            "error_type": err_name,
                            "error": str(err),
                            "consecutive_failures": status.consecutive_failures,
                        },
                    )

                if args.max_polls and total_polls >= args.max_polls:
                    break

                remaining_duration = (
                    max(0.0, args.duration - (time.monotonic() - start_mono))
                    if args.duration
                    else None
                )
                sleep_for = (
                    min(float(next_wait), remaining_duration)
                    if remaining_duration is not None
                    else float(next_wait)
                )
                if sleep_for <= 0:
                    break
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
        finally:
            await client.disconnect()
            duration_s = time.monotonic() - start_mono
            slog.write(
                "session_summary",
                {
                    "duration_s": round(duration_s, 1),
                    "total_polls": total_polls,
                    "successful_polls": successful_polls,
                    "failed_polls": failed_polls,
                    "errors_by_type": errors_by_type,
                    "stats": client.stats.to_dict(),
                },
            )
            print(
                format_session_summary(
                    duration_s=duration_s,
                    total_polls=total_polls,
                    successful_polls=successful_polls,
                    failed_polls=failed_polls,
                    stats=client.stats,
                    errors_by_type=errors_by_type,
                )
            )
    return 0


async def _cmd_write(args: argparse.Namespace) -> int:
    writes: list[tuple[int, int, str]] = []

    if args.load is not None:
        writes.append((SettingsRegister.LOAD_SWITCH, 1 if args.load == "on" else 0, f"load={args.load}"))
    if args.full_voltage is not None:
        _check_range("full-voltage", args.full_voltage, FULL_VOLTAGE_RANGE)
        writes.append(
            (SettingsRegister.FULL_VOLTAGE, encode_voltage(args.full_voltage), f"full_voltage={args.full_voltage}V")
        )
    if args.cutoff_voltage is not None:
        _check_range("cutoff-voltage", args.cutoff_voltage, CUTOFF_VOLTAGE_RANGE)
        writes.append(
            (SettingsRegister.CUTOFF_VOLTAGE, encode_voltage(args.cutoff_voltage), f"cutoff_voltage={args.cutoff_voltage}V")
        )
    if args.recovery_voltage is not None:
        _check_range("recovery-voltage", args.recovery_voltage, RECOVERY_VOLTAGE_RANGE)
        writes.append(
            (SettingsRegister.RECOVERY_VOLTAGE, encode_voltage(args.recovery_voltage), f"recovery_voltage={args.recovery_voltage}V")
        )
    if args.battery_type is not None:
        val = _label_lookup(BATTERY_TYPE_LABELS, args.battery_type)
        writes.append((SettingsRegister.BATTERY_TYPE, val, f"battery_type={args.battery_type}"))
    if args.output_mode is not None:
        val = _label_lookup(OUTPUT_MODE_LABELS, args.output_mode)
        writes.append((SettingsRegister.OUTPUT_MODE, val, f"output_mode={args.output_mode}"))
    if args.voltage_detect is not None:
        val = _label_lookup(VOLTAGE_DETECT_MODE_LABELS, args.voltage_detect)
        writes.append((SettingsRegister.VOLTAGE_DETECT_MODE, val, f"voltage_detect={args.voltage_detect}"))
    if args.register is not None:
        if args.value is None:
            raise ValueError("--value is required when using --register")
        writes.append((args.register, args.value, f"reg=0x{args.register:04X} val={args.value}"))

    if not writes:
        raise ValueError("Specify at least one setting to write (see `zhijin-ble write --help`).")

    target = await _resolve_target(args.address, args.name, args.scan_timeout, args.adapter)
    client = _build_client(
        target,
        mode=ConnectionMode.PERSISTENT,
        timeout=args.timeout,
        retries=args.retries,
        reconnect_retries=args.reconnect_retries,
        scan_timeout=args.scan_timeout,
        adapter=args.adapter,
        use_retry_connector=not args.direct_bleak,
        raw_trace=args.raw,
        session_logger=None,
    )
    try:
        for reg, val, desc in writes:
            print(f"Writing {desc} (register 0x{reg:04X} = {val})...")
            await client.write_register(reg, val)
        settings = await client.read_settings()
        print("Write verified! Current settings:")
        print(
            format_telemetry_block(
                await client.read_telemetry(
                    battery_type=settings.battery_type,
                    full_voltage=settings.full_voltage,
                ),
                settings,
            )
        )
    finally:
        await client.disconnect()
    return 0


def _check_range(label: str, value: float, bounds: tuple[float, float]) -> None:
    low, high = bounds
    if not low <= value <= high:
        raise ValueError(f"--{label} must be between {low} and {high} (got {value})")


async def _cmd_probe(args: argparse.Namespace) -> int:
    target = await _resolve_target(args.address, args.name, args.scan_timeout, args.adapter)
    client = _build_client(
        target,
        mode=ConnectionMode.PERSISTENT,
        timeout=args.timeout,
        retries=args.retries,
        reconnect_retries=args.reconnect_retries,
        scan_timeout=args.scan_timeout,
        adapter=args.adapter,
        use_retry_connector=not args.direct_bleak,
        raw_trace=True,
        session_logger=None,
    )
    try:
        await client.connect()
        bleak_client = client._client
        print(f"Connected to {target.name} ({target.address})")
        mtu = getattr(bleak_client, "mtu_size", None)
        if mtu is not None:
            print(f"MTU size: {mtu}")
        services = getattr(bleak_client, "services", None)
        if services is not None:
            print("GATT Services:")
            for service in services:
                print(f"  Service {service.uuid} ({service.description})")
                for char in service.characteristics:
                    props = ", ".join(char.properties)
                    print(f"    Char {char.uuid} [{props}]")

        if args.start_reg is not None:
            frame = await client.read_raw_registers(args.start_reg, args.count)
            print(
                f"\nRaw Read 0x{args.start_reg:04X} (count={args.count}): "
                f"byte_count={frame.byte_count}, hex={frame.raw.hex()}"
            )
    finally:
        await client.disconnect()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="zhijin-ble",
        description="ZhiJin BLE solar controller diagnostic & soak-testing CLI (macOS / Linux / Windows).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable DEBUG logging.")
    parser.add_argument("--adapter", help="Optional BLE adapter name (Linux hci0, etc.).")
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=8.0,
        help="BLE discovery scan timeout in seconds (default: 8.0).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # scan
    p_scan = sub.add_parser("scan", help="Scan for nearby ZhiJin BLE controllers.")
    p_scan.add_argument(
        "--all",
        action="store_true",
        help="Also include devices advertising the Nordic UART Service UUID.",
    )
    p_scan.add_argument("--json", action="store_true", help="Output JSON.")

    def _add_connection_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("-a", "--address", help="Target BLE address (Linux MAC or macOS UUID).")
        p.add_argument("-n", "--name", help="Target BLE device name substring (e.g. ZJBE).")
        p.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_COMMAND_TIMEOUT,
            help=f"Per-command response timeout in seconds (default: {DEFAULT_COMMAND_TIMEOUT}).",
        )
        p.add_argument(
            "--retries",
            type=int,
            default=DEFAULT_COMMAND_RETRIES,
            help=f"In-session command attempts before reconnect (default: {DEFAULT_COMMAND_RETRIES}).",
        )
        p.add_argument(
            "--reconnect-retries",
            type=int,
            default=DEFAULT_RECONNECT_RETRIES,
            help=f"Reconnect attempts per failed command (default: {DEFAULT_RECONNECT_RETRIES}).",
        )
        p.add_argument(
            "--direct-bleak",
            action="store_true",
            help="Use direct BleakClient instead of bleak-retry-connector.",
        )
        p.add_argument(
            "--raw",
            action="store_true",
            help="Print millisecond-timestamped TX/RX hex frames and BLE lifecycle events.",
        )

    # read
    p_read = sub.add_parser("read", help="Connect once and read telemetry + settings.")
    _add_connection_args(p_read)
    p_read.add_argument(
        "--temp-offset",
        type=float,
        default=DEFAULT_TEMPERATURE_OFFSET,
        help="Calibration offset in °C added to reported temperature (default: 0.0).",
    )
    p_read.add_argument("--json", action="store_true", help="Output JSON.")
    p_read.add_argument("--log-file", help="Optional JSONL trace log path.")

    # monitor
    p_mon = sub.add_parser(
        "monitor",
        help="Run a long-duration debug / soak-test polling session with reliability statistics.",
    )
    _add_connection_args(p_mon)
    p_mon.add_argument(
        "--temp-offset",
        type=float,
        default=DEFAULT_TEMPERATURE_OFFSET,
        help="Calibration offset in °C added to reported temperature (default: 0.0).",
    )
    p_mon.add_argument(
        "--mode",
        choices=[m.value for m in ConnectionMode],
        default=ConnectionMode.ON_DEMAND.value,
        help="Connection mode: on_demand (connect-poll-disconnect) or persistent (default: on_demand).",
    )
    p_mon.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Normal polling interval in seconds (default: {DEFAULT_POLL_INTERVAL}).",
    )
    p_mon.add_argument(
        "--power-save",
        choices=[m.value for m in PowerSaveMode],
        default=PowerSaveMode.NIGHT_IDLE.value,
        help=f"Adaptive power-saving mode (default: {PowerSaveMode.NIGHT_IDLE.value}).",
    )
    p_mon.add_argument(
        "--night-interval",
        type=int,
        default=DEFAULT_POWER_SAVE_INTERVAL,
        help=f"Slow poll interval when power-save is active (default: {DEFAULT_POWER_SAVE_INTERVAL}).",
    )
    p_mon.add_argument(
        "--settings-interval",
        type=int,
        default=DEFAULT_SETTINGS_INTERVAL,
        help=f"Settings refresh interval in seconds (default: {DEFAULT_SETTINGS_INTERVAL}).",
    )
    p_mon.add_argument(
        "--duration",
        type=float,
        help="Optional session length in seconds (runs until Ctrl+C if omitted).",
    )
    p_mon.add_argument("--max-polls", type=int, help="Optional maximum number of polls to run.")
    p_mon.add_argument("--log-file", help="Append structured JSONL session events to this file.")

    # write
    p_write = sub.add_parser("write", help="Write one or more controller settings and verify.")
    _add_connection_args(p_write)
    p_write.add_argument("--load", choices=["on", "off"], help="Manual load switch.")
    p_write.add_argument("--full-voltage", type=float, help="Full charge cutoff voltage (V).")
    p_write.add_argument("--cutoff-voltage", type=float, help="Low-voltage disconnect threshold (V).")
    p_write.add_argument("--recovery-voltage", type=float, help="Low-voltage recovery threshold (V).")
    p_write.add_argument("--battery-type", help="Battery chemistry: lithium, gel, lead-acid.")
    p_write.add_argument("--output-mode", help="Output mode: manual, auto, timer, direct.")
    p_write.add_argument("--voltage-detect", help="Voltage detection mode: auto, 12V..96V.")
    p_write.add_argument("--register", type=_parse_int_auto, help="Raw register address (e.g. 0x1007).")
    p_write.add_argument("--value", type=_parse_int_auto, help="Raw uint16 value for --register.")

    # probe
    p_probe = sub.add_parser("probe", help="Inspect GATT services/characteristics and test raw reads.")
    _add_connection_args(p_probe)
    p_probe.add_argument("--start-reg", type=_parse_int_auto, help="Optional register address to read.")
    p_probe.add_argument("--count", type=int, default=16, help="Register count for --start-reg.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    )

    dispatch = {
        "scan": _cmd_scan,
        "read": _cmd_read,
        "monitor": _cmd_monitor,
        "write": _cmd_write,
        "probe": _cmd_probe,
    }

    try:
        return asyncio.run(dispatch[args.command](args))
    except KeyboardInterrupt:
        return 130
    except (ZhiJinError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
