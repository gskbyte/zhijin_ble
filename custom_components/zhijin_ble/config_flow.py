"""Config and options flows for ZhiJin BLE."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .ble.const import (
    DEVICE_NAME_PREFIX,
    TEMPERATURE_OFFSET_RANGE,
    ConnectionMode,
    PowerSaveMode,
)
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
)

MANUAL_ADDRESS_OPTION = "__manual__"

CONNECTION_MODE_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[m.value for m in ConnectionMode],
        mode=SelectSelectorMode.LIST,
        translation_key="connection_mode",
    )
)

POWER_SAVE_MODE_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[m.value for m in PowerSaveMode],
        mode=SelectSelectorMode.LIST,
        translation_key="power_save_mode",
    )
)


def _normalize_address(address: str) -> str:
    return address.strip().upper()


def _primary_option_fields(current: dict[str, Any] | None = None) -> dict[vol.Marker, Any]:
    """Primary connection & power-saving fields shared across setup and options steps."""
    merged = {**DEFAULT_OPTIONS, **(current or {})}
    return {
        vol.Required(
            CONF_CONNECTION_MODE,
            default=merged[CONF_CONNECTION_MODE],
        ): CONNECTION_MODE_SELECTOR,
        vol.Required(
            CONF_POLL_INTERVAL,
            default=int(merged[CONF_POLL_INTERVAL]),
        ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
        vol.Required(
            CONF_POWER_SAVE_MODE,
            default=merged[CONF_POWER_SAVE_MODE],
        ): POWER_SAVE_MODE_SELECTOR,
        vol.Required(
            CONF_POWER_SAVE_INTERVAL,
            default=int(merged[CONF_POWER_SAVE_INTERVAL]),
        ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
    }


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    """Build voluptuous schema for connection, power-saving, and calibration options."""
    merged = {**DEFAULT_OPTIONS, **current}
    return vol.Schema(
        {
            **_primary_option_fields(merged),
            vol.Required(
                CONF_SETTINGS_INTERVAL,
                default=int(merged[CONF_SETTINGS_INTERVAL]),
            ): vol.All(vol.Coerce(int), vol.Range(min=60, max=86400)),
            vol.Required(
                CONF_COMMAND_TIMEOUT,
                default=float(merged[CONF_COMMAND_TIMEOUT]),
            ): vol.All(vol.Coerce(float), vol.Range(min=2.0, max=15.0)),
            vol.Required(
                CONF_COMMAND_RETRIES,
                default=int(merged[CONF_COMMAND_RETRIES]),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
            vol.Required(
                CONF_UNAVAILABLE_AFTER_FAILURES,
                default=int(merged[CONF_UNAVAILABLE_AFTER_FAILURES]),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
            vol.Required(
                CONF_OFFLINE_ISSUE_SECONDS,
                default=int(merged[CONF_OFFLINE_ISSUE_SECONDS]),
            ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
            vol.Required(
                CONF_TEMPERATURE_OFFSET,
                default=float(merged[CONF_TEMPERATURE_OFFSET]),
            ): vol.All(
                vol.Coerce(float),
                vol.Range(min=TEMPERATURE_OFFSET_RANGE[0], max=TEMPERATURE_OFFSET_RANGE[1]),
            ),
        }
    )


def _extract_initial_options(user_input: dict[str, Any]) -> dict[str, Any]:
    """Extract primary option keys from form input merged over defaults."""
    primary_keys = (
        CONF_CONNECTION_MODE,
        CONF_POLL_INTERVAL,
        CONF_POWER_SAVE_MODE,
        CONF_POWER_SAVE_INTERVAL,
    )
    return {
        **DEFAULT_OPTIONS,
        **{k: user_input[k] for k in primary_keys if k in user_input},
    }


class ZhiJinConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ZhiJin BLE."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ZhiJinOptionsFlow:
        """Return the options flow handler."""
        return ZhiJinOptionsFlow(config_entry)

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> config_entries.ConfigFlowResult:
        """Handle automatic Home Assistant Bluetooth discovery."""
        address = _normalize_address(discovery_info.address)
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        name = discovery_info.name or address
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm adding an automatically discovered ZhiJin controller."""
        assert self._discovery_info is not None
        address = _normalize_address(self._discovery_info.address)
        name = self._discovery_info.name or address

        if user_input is not None:
            return self.async_create_entry(
                title=name,
                data={CONF_ADDRESS: address, CONF_NAME: name},
                options=_extract_initial_options(user_input),
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(_primary_option_fields()),
            description_placeholders={"name": name, "address": address},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle user-initiated setup via Bluetooth discovery or manual address entry."""
        if user_input is not None:
            selected = user_input[CONF_ADDRESS]
            if selected == MANUAL_ADDRESS_OPTION:
                return await self.async_step_manual()

            info = self._discovered_devices[selected]
            address = _normalize_address(info.address)
            name = info.name or address

            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=name,
                data={CONF_ADDRESS: address, CONF_NAME: name},
                options=_extract_initial_options(user_input),
            )

        current_addresses = {_normalize_address(a) for a in self._async_current_ids() if a}
        candidates: list[BluetoothServiceInfoBleak] = []
        for info in async_discovered_service_info(self.hass, connectable=True):
            address = _normalize_address(info.address)
            if address in current_addresses:
                continue
            if info.name and info.name.upper().startswith(DEVICE_NAME_PREFIX):
                candidates.append(info)

        candidates.sort(
            key=lambda item: item.rssi if item.rssi is not None else -999,
            reverse=True,
        )
        self._discovered_devices = {_normalize_address(info.address): info for info in candidates}

        if not self._discovered_devices:
            return await self.async_step_manual()

        device_choices: dict[str, str] = {}
        for address, info in self._discovered_devices.items():
            rssi_suffix = f" [{info.rssi} dBm]" if info.rssi is not None else ""
            device_choices[address] = f"{info.name} ({address}){rssi_suffix}"
        device_choices[MANUAL_ADDRESS_OPTION] = "Enter Bluetooth address manually..."

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(device_choices),
                **_primary_option_fields(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Allow manual entry of a controller Bluetooth address when not advertising."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = _normalize_address(user_input[CONF_ADDRESS])
            if len(address) < 6:
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                name = (user_input.get(CONF_NAME) or "").strip() or f"ZhiJin ({address})"
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=name,
                    data={CONF_ADDRESS: address, CONF_NAME: name},
                    options=_extract_initial_options(user_input),
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): str,
                vol.Optional(CONF_NAME, default=""): str,
                **_primary_option_fields(),
            }
        )
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)


class ZhiJinOptionsFlow(config_entries.OptionsFlow):
    """Handle runtime tuning of ZhiJin BLE polling, connection, and power-save options."""

    def __init__(self, config_entry: config_entries.ConfigEntry | None = None) -> None:
        self._entry = config_entry

    def _current_entry(self) -> config_entries.ConfigEntry:
        return self._entry if self._entry is not None else self.config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show and save the options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        entry = self._current_entry()
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(dict(entry.options)),
        )
