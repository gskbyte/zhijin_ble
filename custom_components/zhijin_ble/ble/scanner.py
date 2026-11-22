"""Standalone BLE device discovery helpers for macOS, Linux, and Windows.

Uses ``bleak.BleakScanner`` directly so the library and debug CLI work outside
Home Assistant (including CoreBluetooth UUID addressing on macOS).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .const import DEVICE_NAME_PREFIX, SERVICE_UUID


@dataclass(frozen=True, slots=True)
class DiscoveredController:
    """Summary of a discovered ZhiJin BLE controller."""

    device: BLEDevice
    name: str
    address: str
    rssi: int | None
    service_uuids: tuple[str, ...]


def is_zhijin_advertisement(
    device: BLEDevice,
    adv_data: AdvertisementData | None = None,
    *,
    name_prefix: str = DEVICE_NAME_PREFIX,
    match_service_uuid: bool = True,
) -> bool:
    """Return True if a BLE advertisement appears to be a ZhiJin solar controller."""
    name = (adv_data.local_name if adv_data and adv_data.local_name else None) or device.name or ""
    if name_prefix and name.upper().startswith(name_prefix.upper()):
        return True

    if match_service_uuid and adv_data and adv_data.service_uuids:
        uuids = {u.lower() for u in adv_data.service_uuids}
        if SERVICE_UUID.lower() in uuids:
            return True

    return False


async def scan_controllers(
    *,
    timeout: float = 8.0,
    name_prefix: str = DEVICE_NAME_PREFIX,
    include_service_uuid_match: bool = False,
    adapter: str | None = None,
) -> list[DiscoveredController]:
    """Scan for nearby ZhiJin BLE controllers sorted by strongest RSSI first.

    By default matches devices whose advertised name starts with ``ZJ`` to avoid
    false positives from unrelated Nordic UART devices. Set
    ``include_service_uuid_match=True`` to also include devices advertising the
    NUS service UUID.
    """
    kwargs: dict[str, Any] = {"timeout": timeout, "return_adv": True}
    if adapter:
        kwargs["adapter"] = adapter

    discovered: dict[str, tuple[BLEDevice, AdvertisementData]] = await BleakScanner.discover(
        **kwargs
    )

    results: list[DiscoveredController] = []
    for device, adv in discovered.values():
        if not is_zhijin_advertisement(
            device,
            adv,
            name_prefix=name_prefix,
            match_service_uuid=include_service_uuid_match,
        ):
            continue
        name = adv.local_name or device.name or device.address
        uuids = tuple(u.lower() for u in (adv.service_uuids or ()))
        results.append(
            DiscoveredController(
                device=device,
                name=name,
                address=device.address,
                rssi=adv.rssi,
                service_uuids=uuids,
            )
        )

    results.sort(key=lambda item: item.rssi if item.rssi is not None else -999, reverse=True)
    return results


async def find_controller(
    *,
    address: str | None = None,
    name: str | None = None,
    timeout: float = 8.0,
    adapter: str | None = None,
) -> DiscoveredController | None:
    """Find a specific ZhiJin controller by address/UUID, name, or strongest signal."""
    kwargs: dict[str, Any] = {"timeout": timeout, "return_adv": True}
    if adapter:
        kwargs["adapter"] = adapter

    discovered: dict[str, tuple[BLEDevice, AdvertisementData]] = await BleakScanner.discover(
        **kwargs
    )

    target_addr = address.upper() if address else None
    target_name = name.upper() if name else None

    matches: list[DiscoveredController] = []
    for device, adv in discovered.values():
        dev_name = adv.local_name or device.name or ""
        if target_addr:
            if device.address.upper() != target_addr:
                continue
        elif target_name:
            if target_name not in dev_name.upper():
                continue
        elif not is_zhijin_advertisement(device, adv, match_service_uuid=False):
            continue

        matches.append(
            DiscoveredController(
                device=device,
                name=dev_name or device.address,
                address=device.address,
                rssi=adv.rssi,
                service_uuids=tuple(u.lower() for u in (adv.service_uuids or ())),
            )
        )

    if not matches:
        return None

    matches.sort(key=lambda item: item.rssi if item.rssi is not None else -999, reverse=True)
    return matches[0]
