"""Protocol constants, BLE UUIDs, register addresses, and defaults for ZhiJin BLE."""

from __future__ import annotations

from enum import IntEnum, StrEnum

# Nordic UART Service (NUS)-style UUIDs used by ZhiJin BLE solar controllers.
# Note: Compared to standard Nordic NUS, TX (notify) and RX (write) are swapped:
# 6e400002 is Notify (controller -> host) and 6e400003 is Write (host -> controller).
SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
WRITE_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# Standard BLE advertising name prefix for ZhiJin controllers (e.g. ZJBE-240800XXXXXX).
DEVICE_NAME_PREFIX = "ZJ"

# Modbus RTU protocol parameters.
DEFAULT_UNIT_ID = 0x01

FUNC_READ_HOLDING = 0x03
FUNC_WRITE_SINGLE = 0x06
FUNC_WRITE_MULTIPLE = 0x10
EXCEPTION_FLAG = 0x80

VALID_FUNCTIONS = frozenset({FUNC_READ_HOLDING, FUNC_WRITE_SINGLE, FUNC_WRITE_MULTIPLE})

# Telemetry register block (0x0001..0x000A).
# Firmware quirk: the official app requests count=16, and the controller returns
# 10 holding registers (byte_count=20).
REG_TELEMETRY_START = 0x0001
REG_TELEMETRY_REQUEST_COUNT = 16
TELEMETRY_RESPONSE_BYTES = 20

# Settings register block (0x1001..0x1009).
# Firmware quirk: the official app requests count=15, and the controller returns
# 9 holding registers (byte_count=18).
REG_SETTINGS_START = 0x1001
REG_SETTINGS_REQUEST_COUNT = 15
SETTINGS_RESPONSE_BYTES = 18


class SettingsRegister(IntEnum):
    """Writable holding register addresses for controller settings."""

    BATTERY_TYPE = 0x1001
    TIME_HOUR = 0x1002
    TIME_MIN = 0x1003
    FULL_VOLTAGE = 0x1004
    OUTPUT_MODE = 0x1005
    CUTOFF_VOLTAGE = 0x1006
    LOAD_SWITCH = 0x1007
    VOLTAGE_DETECT_MODE = 0x1008
    RECOVERY_VOLTAGE = 0x1009


class BatteryType(IntEnum):
    """Supported battery chemistry modes (register 0x1001)."""

    LITHIUM = 1
    GEL = 2
    LEAD_ACID = 3


class OutputMode(IntEnum):
    """Load output control modes (register 0x1005)."""

    MANUAL = 0
    AUTO = 1
    TIMER = 2
    DIRECT = 3


class VoltageDetectMode(IntEnum):
    """System voltage detection modes (register 0x1008)."""

    AUTO = 0
    V12 = 1
    V24 = 2
    V36 = 3
    V48 = 4
    V60 = 5
    V72 = 6
    V84 = 7
    V96 = 8


class ConnectionMode(StrEnum):
    """BLE session management strategy."""

    ON_DEMAND = "on_demand"
    PERSISTENT = "persistent"


class PowerSaveMode(StrEnum):
    """Adaptive polling slowdown strategy to reduce controller radio and MCU power draw."""

    OFF = "off"
    NIGHT_IDLE = "night_idle"
    NIGHT = "night"
    IDLE = "idle"


BATTERY_TYPE_LABELS: dict[BatteryType, str] = {
    BatteryType.LITHIUM: "Lithium",
    BatteryType.GEL: "Gel",
    BatteryType.LEAD_ACID: "Lead-Acid",
}

OUTPUT_MODE_LABELS: dict[OutputMode, str] = {
    OutputMode.MANUAL: "Manual",
    OutputMode.AUTO: "Auto",
    OutputMode.TIMER: "Timer",
    OutputMode.DIRECT: "Direct",
}

VOLTAGE_DETECT_MODE_LABELS: dict[VoltageDetectMode, str] = {
    VoltageDetectMode.AUTO: "Auto",
    VoltageDetectMode.V12: "12V",
    VoltageDetectMode.V24: "24V",
    VoltageDetectMode.V36: "36V",
    VoltageDetectMode.V48: "48V",
    VoltageDetectMode.V60: "60V",
    VoltageDetectMode.V72: "72V",
    VoltageDetectMode.V84: "84V",
    VoltageDetectMode.V96: "96V",
}

# Voltage limits matching controller UI ranges.
FULL_VOLTAGE_RANGE = (10.0, 30.0)
CUTOFF_VOLTAGE_RANGE = (9.0, 25.0)
RECOVERY_VOLTAGE_RANGE = (10.0, 28.0)

# Temperature calibration offset defaults and bounds (°C).
DEFAULT_TEMPERATURE_OFFSET = 0.0
TEMPERATURE_OFFSET_RANGE = (-25.0, 25.0)

# Threshold above which a system is treated as 24V (normalized to 12V scale by /2).
SYSTEM_24V_THRESHOLD = 17.0

# Current threshold (A) above which charging/discharging is considered active.
ACTIVE_CURRENT_THRESHOLD = 0.1

# Normalized 12V full-voltage threshold distinguishing 3S Li-ion (<= 13.0V, e.g. 12.6V)
# from 4S LiFePO4 (> 13.0V, e.g. 14.4V / 14.6V) when battery_type == LITHIUM.
LITHIUM_3S_FULL_VOLTAGE_THRESHOLD = 13.0

# 3S Li-ion (NMC, 11.1V nominal / 12.6V full) safe usable curve:
# 10.5V (3.50V/cell safe floor above 10.0V cutoff) -> 0%, 12.6V (4.20V/cell) -> 100%.
HEALTHY_BATTERY_CURVE_LITHIUM_3S: tuple[tuple[float, float], ...] = (
    (10.5, 0.0),
    (11.1, 20.0),
    (11.4, 40.0),
    (11.8, 70.0),
    (12.3, 90.0),
    (12.6, 100.0),
)

# Piecewise linear curves mapping normalized 12V voltage -> healthy usable %.
HEALTHY_BATTERY_CURVES: dict[BatteryType, tuple[tuple[float, float], ...]] = {
    # LiFePO4 4S baseline: safe usable range ~12.2V - 13.4V
    BatteryType.LITHIUM: (
        (12.2, 0.0),
        (13.0, 30.0),
        (13.2, 70.0),
        (13.4, 100.0),
    ),
    # Gel battery: safe 50-60% DoD limit around 11.9V, full rest ~12.8V
    BatteryType.GEL: (
        (11.9, 0.0),
        (12.1, 15.0),
        (12.3, 45.0),
        (12.5, 75.0),
        (12.8, 100.0),
    ),
    # Lead-Acid: safe 50% DoD limit at 12.0V, full rest ~12.7V
    BatteryType.LEAD_ACID: (
        (12.0, 0.0),
        (12.1, 20.0),
        (12.3, 50.0),
        (12.5, 80.0),
        (12.7, 100.0),
    ),
}

# Communication timing defaults tuned from real controller traffic captures.
DEFAULT_COMMAND_TIMEOUT = 5.0
DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_DISCONNECT_TIMEOUT = 3.0
DEFAULT_COMMAND_RETRIES = 2
DEFAULT_RECONNECT_RETRIES = 1
DEFAULT_RETRY_DELAY = 0.35
DEFAULT_POST_SUBSCRIBE_DELAY = 0.2
DEFAULT_MIN_COMMAND_INTERVAL = 0.3
DEFAULT_DUPLICATE_WINDOW = 1.0
DEFAULT_MAX_BUFFER_BYTES = 1024

# Adaptive polling defaults (favoring controller power savings and reliability).
DEFAULT_POLL_INTERVAL = 60
DEFAULT_POWER_SAVE_INTERVAL = 300
DEFAULT_SETTINGS_INTERVAL = 600
DEFAULT_OFFLINE_ISSUE_SECONDS = 180
DEFAULT_UNAVAILABLE_AFTER_FAILURES = 2
