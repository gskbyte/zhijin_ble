"""Diagnostics support for ZhiJin BLE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ZhiJinConfigEntry
from .const import CONF_ADDRESS

TO_REDACT = {CONF_ADDRESS}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ZhiJinConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a ZhiJin BLE config entry."""
    coordinator = entry.runtime_data
    state = coordinator.data

    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "client": {
            "connection_mode": coordinator.client_config.connection_mode.value,
            "command_timeout": coordinator.client_config.command_timeout,
            "command_retries": coordinator.client_config.command_retries,
            "is_connected": coordinator.client.is_connected,
        },
        "scheduler": {
            "poll_interval": coordinator.policy.poll_interval,
            "power_save_mode": coordinator.policy.power_save_mode.value,
            "power_save_interval": coordinator.policy.power_save_interval,
            "settings_interval": coordinator.policy.settings_interval,
            "current_update_interval_s": (
                int(coordinator.update_interval.total_seconds())
                if coordinator.update_interval
                else None
            ),
            "power_save_active": coordinator.scheduler.power_save_active,
            "consecutive_failures": coordinator.scheduler.consecutive_failures,
            "offline_issue_active": coordinator.scheduler.offline_issue_active,
        },
        "state": state.to_dict() if state is not None else None,
    }
