"""The ZhiJin BLE Home Assistant integration.

Home Assistant runtime imports are kept inside setup/unload functions so the
standalone ``custom_components.zhijin_ble.ble`` library and CLI can run on
macOS/Linux without installing Home Assistant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import CONF_ADDRESS, DOMAIN, offline_issue_id

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import ZhiJinCoordinator

    ZhiJinConfigEntry = ConfigEntry[ZhiJinCoordinator]
else:
    ZhiJinConfigEntry = object


async def async_setup_entry(hass: HomeAssistant, entry: ZhiJinConfigEntry) -> bool:
    """Set up ZhiJin BLE from a config entry."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform

    from .coordinator import ZhiJinCoordinator

    platforms = [
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.SWITCH,
    ]

    coordinator = ZhiJinCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    async def _async_stop(_event: object) -> None:
        await coordinator.async_shutdown_ble()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ZhiJinConfigEntry) -> bool:
    """Unload a ZhiJin BLE config entry."""
    from homeassistant.const import Platform
    from homeassistant.helpers.issue_registry import async_delete_issue

    platforms = [
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.SWITCH,
    ]

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok:
        coordinator: ZhiJinCoordinator = entry.runtime_data
        await coordinator.async_shutdown_ble()
        async_delete_issue(hass, DOMAIN, offline_issue_id(entry.data[CONF_ADDRESS]))

    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ZhiJinConfigEntry) -> None:
    """Reload config entry when user updates integration options."""
    await hass.config_entries.async_reload(entry.entry_id)
