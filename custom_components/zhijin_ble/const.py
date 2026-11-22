"""Home Assistant integration constants for ZhiJin BLE."""

from __future__ import annotations

import logging
from typing import Any

from .ble.const import (
    DEFAULT_COMMAND_RETRIES,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_OFFLINE_ISSUE_SECONDS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POWER_SAVE_INTERVAL,
    DEFAULT_SETTINGS_INTERVAL,
    DEFAULT_TEMPERATURE_OFFSET,
    DEFAULT_UNAVAILABLE_AFTER_FAILURES,
    ConnectionMode,
    PowerSaveMode,
)

DOMAIN = "zhijin_ble"
LOGGER = logging.getLogger(__package__)

MANUFACTURER = "ZhiJin"
MODEL = "ZhiJin BLE Solar Controller"

# Config entry data keys
CONF_ADDRESS = "address"
CONF_NAME = "name"

# Config entry option keys
CONF_CONNECTION_MODE = "connection_mode"
CONF_POLL_INTERVAL = "poll_interval"
CONF_POWER_SAVE_MODE = "power_save_mode"
CONF_POWER_SAVE_INTERVAL = "power_save_interval"
CONF_SETTINGS_INTERVAL = "settings_interval"
CONF_COMMAND_TIMEOUT = "command_timeout"
CONF_COMMAND_RETRIES = "command_retries"
CONF_UNAVAILABLE_AFTER_FAILURES = "unavailable_after_failures"
CONF_OFFLINE_ISSUE_SECONDS = "offline_issue_seconds"
CONF_TEMPERATURE_OFFSET = "temperature_offset"

DEFAULT_OPTIONS: dict[str, Any] = {
    CONF_CONNECTION_MODE: ConnectionMode.ON_DEMAND.value,
    CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
    CONF_POWER_SAVE_MODE: PowerSaveMode.NIGHT_IDLE.value,
    CONF_POWER_SAVE_INTERVAL: DEFAULT_POWER_SAVE_INTERVAL,
    CONF_SETTINGS_INTERVAL: DEFAULT_SETTINGS_INTERVAL,
    CONF_COMMAND_TIMEOUT: DEFAULT_COMMAND_TIMEOUT,
    CONF_COMMAND_RETRIES: DEFAULT_COMMAND_RETRIES,
    CONF_UNAVAILABLE_AFTER_FAILURES: DEFAULT_UNAVAILABLE_AFTER_FAILURES,
    CONF_OFFLINE_ISSUE_SECONDS: DEFAULT_OFFLINE_ISSUE_SECONDS,
    CONF_TEMPERATURE_OFFSET: DEFAULT_TEMPERATURE_OFFSET,
}


def offline_issue_id(address: str) -> str:
    """Return the deterministic Repairs issue ID for a controller address."""
    return f"device_offline_{address.lower().replace(':', '_')}"
