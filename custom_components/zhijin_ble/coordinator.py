"""DataUpdateCoordinator for ZhiJin BLE."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    async_last_service_info,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble.client import ClientConfig, ZhiJinBLEClient
from .ble.const import ConnectionMode, PowerSaveMode, SettingsRegister
from .ble.exceptions import ZhiJinError
from .ble.models import ControllerState
from .ble.poller import AdaptivePollScheduler, PollPolicy
from .ble.protocol import encode_voltage, recalculate_telemetry_for_battery_type
from .const import (
    CONF_ADDRESS,
    CONF_COMMAND_RETRIES,
    CONF_COMMAND_TIMEOUT,
    CONF_CONNECTION_MODE,
    CONF_NAME,
    CONF_OFFLINE_ISSUE_SECONDS,
    CONF_POLL_INTERVAL,
    CONF_POWER_SAVE_INTERVAL,
    CONF_POWER_SAVE_MODE,
    CONF_SETTINGS_INTERVAL,
    CONF_TEMPERATURE_OFFSET,
    CONF_UNAVAILABLE_AFTER_FAILURES,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOGGER,
    offline_issue_id,
)


def _merged_options(entry: ConfigEntry) -> dict[str, Any]:
    """Merge entry options over defaults."""
    return {**DEFAULT_OPTIONS, **entry.options}


class ZhiJinCoordinator(DataUpdateCoordinator[ControllerState]):
    """Coordinates BLE polling, adaptive power-saving intervals, and writes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.device_name: str = entry.data.get(CONF_NAME) or entry.title or self.address

        opts = _merged_options(entry)
        self._applied_ble_options: dict[str, Any] = {
            k: v for k, v in opts.items() if k != CONF_TEMPERATURE_OFFSET
        }
        self.client_config = ClientConfig(
            connection_mode=ConnectionMode(opts[CONF_CONNECTION_MODE]),
            command_timeout=float(opts[CONF_COMMAND_TIMEOUT]),
            command_retries=int(opts[CONF_COMMAND_RETRIES]),
        )
        self.policy = PollPolicy(
            poll_interval=int(opts[CONF_POLL_INTERVAL]),
            power_save_mode=PowerSaveMode(opts[CONF_POWER_SAVE_MODE]),
            power_save_interval=int(opts[CONF_POWER_SAVE_INTERVAL]),
            settings_interval=int(opts[CONF_SETTINGS_INTERVAL]),
            offline_issue_seconds=int(opts[CONF_OFFLINE_ISSUE_SECONDS]),
            unavailable_after_failures=int(opts[CONF_UNAVAILABLE_AFTER_FAILURES]),
        )
        self.scheduler = AdaptivePollScheduler(self.policy)
        self.client = ZhiJinBLEClient(
            address=self.address,
            device_provider=self._resolve_ble_device,
            name=self.device_name,
            config=self.client_config,
        )
        self.temperature_offset: float = float(opts[CONF_TEMPERATURE_OFFSET])
        self._state = ControllerState(
            temperature_offset=self.temperature_offset,
            stats=self.client.stats,
        )

        super().__init__(
            hass,
            LOGGER,
            name=f"{DOMAIN}_{self.address}",
            update_interval=timedelta(seconds=self.policy.poll_interval),
        )

    def _resolve_ble_device(self):
        """Fetch latest connectable BLEDevice from Home Assistant's Bluetooth stack."""
        return async_ble_device_from_address(self.hass, self.address, connectable=True)

    def _refresh_rssi(self) -> None:
        """Update cached RSSI from the latest Home Assistant Bluetooth advertisement."""
        service_info = async_last_service_info(self.hass, self.address, connectable=True)
        if service_info is not None and service_info.rssi is not None:
            self._state.rssi = service_info.rssi

    async def _async_update_data(self) -> ControllerState:
        """Fetch telemetry (and settings when due) from the ZhiJin controller."""
        self._refresh_rssi()
        started = time.monotonic()

        fetch_settings = self.scheduler.should_fetch_settings(
            has_settings=self._state.settings is not None
        )
        battery_type = self._state.settings.battery_type if self._state.settings else None

        try:
            telemetry, settings = await self.client.poll(
                include_settings=fetch_settings,
                battery_type=battery_type,
            )
        except Exception as err:
            return self._handle_poll_failure(err)

        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        self._state.telemetry = telemetry
        if settings is not None:
            self._state.settings = settings
            self.scheduler.mark_settings_fetched()

        self._state.last_poll_ms = elapsed_ms
        self._state.touch_updated()
        self._state.is_online = True
        self._state.consecutive_failures = 0

        if self.scheduler.record_success():
            async_delete_issue(self.hass, DOMAIN, offline_issue_id(self.address))

        next_seconds = self.scheduler.next_interval(telemetry)
        self._state.power_save_active = self.scheduler.power_save_active
        self._set_poll_interval(next_seconds)

        return self._state

    def _handle_poll_failure(self, err: Exception) -> ControllerState:
        """Track failure duration, manage Repairs issues, and tolerate transient blips."""
        status = self.scheduler.record_failure()
        self._state.is_online = False
        self._state.consecutive_failures = status.consecutive_failures
        self._state.power_save_active = False
        # Return to normal interval during recovery so reconnection isn't delayed by night mode.
        self._set_poll_interval(self.policy.poll_interval)

        if status.raise_offline_issue:
            async_create_issue(
                self.hass,
                DOMAIN,
                offline_issue_id(self.address),
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="device_offline",
                translation_placeholders={
                    "name": self.device_name,
                    "address": self.address,
                    "minutes": str(max(1, self.policy.offline_issue_seconds // 60)),
                },
            )

        if not status.mark_unavailable and self._state.telemetry is not None:
            LOGGER.warning(
                "Transient BLE poll failure %d/%d for %s (%s); preserving cached state: %s",
                status.consecutive_failures,
                self.policy.unavailable_after_failures,
                self.device_name,
                self.address,
                err,
            )
            return self._state

        raise UpdateFailed(f"Error communicating with ZhiJin BLE {self.address}: {err}") from err

    def _set_poll_interval(self, seconds: int) -> None:
        target = timedelta(seconds=seconds)
        if self.update_interval != target:
            LOGGER.debug(
                "Adjusting poll interval for %s from %s to %s (power_save=%s)",
                self.address,
                self.update_interval,
                target,
                self._state.power_save_active,
            )
            self.update_interval = target

    async def async_write_register(self, register: SettingsRegister | int, value: int) -> None:
        """Write a setting register safely, refresh settings in-session, and push state."""
        try:
            settings = await self.client.write_register(
                int(register),
                value,
                refresh_settings=True,
            )
        except ZhiJinError as err:
            raise HomeAssistantError(
                f"Failed to update setting 0x{int(register):04X} on {self.device_name}: {err}"
            ) from err

        if settings is not None:
            self._state.settings = settings
            if self._state.telemetry is not None:
                self._state.telemetry = recalculate_telemetry_for_battery_type(
                    self._state.telemetry,
                    settings.battery_type,
                )
            self.scheduler.mark_settings_fetched()
        else:
            self.scheduler.request_settings_refresh()

        self._state.is_online = True
        self._state.consecutive_failures = 0
        self._state.touch_updated()
        if self.scheduler.record_success():
            async_delete_issue(self.hass, DOMAIN, offline_issue_id(self.address))

        self.async_set_updated_data(self._state)

    async def async_write_voltage(self, register: SettingsRegister, volts: float) -> None:
        """Encode and write a voltage threshold register."""
        await self.async_write_register(register, encode_voltage(volts))

    def can_apply_options_live(self, new_options: dict[str, Any]) -> bool:
        """Return True if only live-updatable options (temperature_offset) changed."""
        merged = {**DEFAULT_OPTIONS, **new_options}
        new_ble_options = {k: v for k, v in merged.items() if k != CONF_TEMPERATURE_OFFSET}
        return new_ble_options == self._applied_ble_options

    def async_apply_temperature_offset(self, offset: float) -> None:
        """Apply a new temperature calibration offset immediately without reconnecting BLE."""
        rounded = round(float(offset), 2)
        self.temperature_offset = rounded
        self._state.temperature_offset = rounded
        self.async_set_updated_data(self._state)

    async def async_set_temperature_offset(self, offset: float) -> None:
        """Update and persist the temperature calibration offset."""
        rounded = round(float(offset), 2)
        self.async_apply_temperature_offset(rounded)
        if self.entry.options.get(CONF_TEMPERATURE_OFFSET) != rounded:
            self.hass.config_entries.async_update_entry(
                self.entry,
                options={**self.entry.options, CONF_TEMPERATURE_OFFSET: rounded},
            )

    async def async_shutdown_ble(self) -> None:
        """Cleanly disconnect the BLE client on unload or Home Assistant shutdown."""
        await self.client.disconnect()
