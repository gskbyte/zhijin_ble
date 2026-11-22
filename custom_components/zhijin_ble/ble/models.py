"""Typed data models for ZhiJin BLE telemetry, settings, and connection metrics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .const import (
    ACTIVE_CURRENT_THRESHOLD,
    BATTERY_TYPE_LABELS,
    OUTPUT_MODE_LABELS,
    VOLTAGE_DETECT_MODE_LABELS,
    BatteryType,
    OutputMode,
    VoltageDetectMode,
)


@dataclass(frozen=True, slots=True)
class ModbusFrame:
    """A CRC-validated Modbus RTU frame parsed from the BLE notification stream."""

    unit_id: int
    function_code: int
    payload: bytes
    raw: bytes
    is_exception: bool = False
    exception_code: int | None = None

    @property
    def byte_count(self) -> int | None:
        """Return the payload byte count for Read Holding (0x03) responses."""
        if not self.is_exception and self.function_code == 0x03 and self.payload:
            return self.payload[0]
        return None

    @property
    def register_data(self) -> bytes:
        """Return register data bytes (excluding byte_count prefix) for 0x03 responses."""
        if not self.is_exception and self.function_code == 0x03 and len(self.payload) >= 1:
            return self.payload[1:]
        return b""


@dataclass(frozen=True, slots=True)
class TelemetryData:
    """Decoded real-time telemetry snapshot from registers 0x0001..0x000A."""

    solar_model_type: int
    battery_voltage: float
    charge_current: float
    discharge_current: float
    temperature: float
    solar_status: bool
    load_status: bool
    wind_status: bool
    total_power_raw: int
    total_power_resets: int
    total_generation: float
    battery_percent: float
    healthy_usable_battery: float
    solar_power: float
    load_power: float

    @property
    def is_charging(self) -> bool:
        """Return True if the battery is actively receiving charge current."""
        return self.charge_current > ACTIVE_CURRENT_THRESHOLD

    @property
    def is_discharging(self) -> bool:
        """Return True if a load is actively drawing current."""
        return self.discharge_current > ACTIVE_CURRENT_THRESHOLD

    @property
    def is_idle(self) -> bool:
        """Return True if neither charging nor discharging current is flowing."""
        return not self.is_charging and not self.is_discharging

    def to_dict(self) -> dict[str, Any]:
        """Serialize telemetry to a JSON-friendly dictionary."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SettingsData:
    """Decoded controller configuration snapshot from registers 0x1001..0x1009."""

    battery_type: int
    time_hour: int
    time_min: int
    full_voltage: float
    output_mode: int
    cutoff_voltage: float
    load_switch: bool
    voltage_detect_mode: int
    recovery_voltage: float

    @property
    def battery_type_label(self) -> str | None:
        """Return human-readable battery chemistry label."""
        try:
            return BATTERY_TYPE_LABELS[BatteryType(self.battery_type)]
        except ValueError:
            return None

    @property
    def output_mode_label(self) -> str | None:
        """Return human-readable output mode label."""
        try:
            return OUTPUT_MODE_LABELS[OutputMode(self.output_mode)]
        except ValueError:
            return None

    @property
    def voltage_detect_mode_label(self) -> str | None:
        """Return human-readable voltage detection mode label."""
        try:
            return VOLTAGE_DETECT_MODE_LABELS[VoltageDetectMode(self.voltage_detect_mode)]
        except ValueError:
            return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize settings to a JSON-friendly dictionary."""
        data = asdict(self)
        data["battery_type_label"] = self.battery_type_label
        data["output_mode_label"] = self.output_mode_label
        data["voltage_detect_mode_label"] = self.voltage_detect_mode_label
        return data


@dataclass(slots=True)
class ConnectionStats:
    """Live reliability and latency telemetry for a BLE client session."""

    connect_attempts: int = 0
    connect_successes: int = 0
    connect_failures: int = 0
    disconnects: int = 0
    unexpected_disconnects: int = 0
    commands_sent: int = 0
    commands_succeeded: int = 0
    commands_failed: int = 0
    command_retries: int = 0
    timeouts: int = 0
    crc_errors: int = 0
    duplicate_frames: int = 0
    unsolicited_frames: int = 0
    modbus_exceptions: int = 0
    last_connect_ms: float | None = None
    last_rtt_ms: float | None = None
    _connect_samples_ms: list[float] = field(default_factory=list, repr=False)
    _rtt_samples_ms: list[float] = field(default_factory=list, repr=False)

    MAX_LATENCY_SAMPLES: int = 200

    def record_connect_latency(self, ms: float) -> None:
        """Record a connect duration measurement in milliseconds."""
        self.last_connect_ms = round(ms, 1)
        self._append_sample(self._connect_samples_ms, ms)

    def record_rtt_latency(self, ms: float) -> None:
        """Record a command round-trip duration in milliseconds."""
        self.last_rtt_ms = round(ms, 1)
        self._append_sample(self._rtt_samples_ms, ms)

    def _append_sample(self, samples: list[float], value: float) -> None:
        samples.append(value)
        if len(samples) > self.MAX_LATENCY_SAMPLES:
            del samples[: len(samples) - self.MAX_LATENCY_SAMPLES]

    @property
    def command_success_rate(self) -> float | None:
        """Return command success percentage (0.0-100.0), or None if no commands finished."""
        completed = self.commands_succeeded + self.commands_failed
        if completed == 0:
            return None
        return round((self.commands_succeeded / completed) * 100.0, 1)

    @property
    def avg_rtt_ms(self) -> float | None:
        """Return rolling mean command RTT in milliseconds."""
        if not self._rtt_samples_ms:
            return None
        return round(sum(self._rtt_samples_ms) / len(self._rtt_samples_ms), 1)

    @property
    def p95_rtt_ms(self) -> float | None:
        """Return rolling 95th percentile command RTT in milliseconds."""
        return self._percentile(self._rtt_samples_ms, 95)

    @property
    def avg_connect_ms(self) -> float | None:
        """Return rolling mean connect latency in milliseconds."""
        if not self._connect_samples_ms:
            return None
        return round(sum(self._connect_samples_ms) / len(self._connect_samples_ms), 1)

    @staticmethod
    def _percentile(samples: list[float], pct: float) -> float | None:
        if not samples:
            return None
        ordered = sorted(samples)
        idx = min(len(ordered) - 1, max(0, int((pct / 100.0) * len(ordered) + 0.9999) - 1))
        return round(ordered[idx], 1)

    def to_dict(self) -> dict[str, Any]:
        """Serialize statistics to a JSON-friendly dictionary."""
        return {
            "connect_attempts": self.connect_attempts,
            "connect_successes": self.connect_successes,
            "connect_failures": self.connect_failures,
            "disconnects": self.disconnects,
            "unexpected_disconnects": self.unexpected_disconnects,
            "commands_sent": self.commands_sent,
            "commands_succeeded": self.commands_succeeded,
            "commands_failed": self.commands_failed,
            "command_retries": self.command_retries,
            "command_success_rate": self.command_success_rate,
            "timeouts": self.timeouts,
            "crc_errors": self.crc_errors,
            "duplicate_frames": self.duplicate_frames,
            "unsolicited_frames": self.unsolicited_frames,
            "modbus_exceptions": self.modbus_exceptions,
            "last_connect_ms": self.last_connect_ms,
            "avg_connect_ms": self.avg_connect_ms,
            "last_rtt_ms": self.last_rtt_ms,
            "avg_rtt_ms": self.avg_rtt_ms,
            "p95_rtt_ms": self.p95_rtt_ms,
        }


@dataclass(slots=True)
class ControllerState:
    """Combined latest telemetry, settings, and connection metadata for a controller."""

    telemetry: TelemetryData | None = None
    settings: SettingsData | None = None
    rssi: int | None = None
    last_updated: datetime | None = None
    last_poll_ms: float | None = None
    power_save_active: bool = False
    is_online: bool = False
    consecutive_failures: int = 0
    temperature_offset: float = 0.0
    stats: ConnectionStats = field(default_factory=ConnectionStats)

    @property
    def calibrated_temperature(self) -> float | None:
        """Return temperature adjusted by ``temperature_offset`` (°C)."""
        if self.telemetry is None:
            return None
        return round(self.telemetry.temperature + self.temperature_offset, 2)

    def touch_updated(self) -> None:
        """Stamp last_updated with current UTC time."""
        self.last_updated = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        """Serialize complete controller state to a JSON-friendly dictionary."""
        return {
            "telemetry": self.telemetry.to_dict() if self.telemetry else None,
            "settings": self.settings.to_dict() if self.settings else None,
            "rssi": self.rssi,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "last_poll_ms": self.last_poll_ms,
            "power_save_active": self.power_save_active,
            "is_online": self.is_online,
            "consecutive_failures": self.consecutive_failures,
            "temperature_offset": self.temperature_offset,
            "calibrated_temperature": self.calibrated_temperature,
            "stats": self.stats.to_dict(),
        }
