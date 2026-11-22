"""Exception hierarchy for the ZhiJin BLE library."""

from __future__ import annotations

MODBUS_EXCEPTION_NAMES: dict[int, str] = {
    0x01: "Illegal Function",
    0x02: "Illegal Data Address",
    0x03: "Illegal Data Value",
    0x04: "Server Device Failure",
    0x05: "Acknowledge",
    0x06: "Server Device Busy",
}


class ZhiJinError(Exception):
    """Base exception for all ZhiJin BLE errors."""


class ZhiJinConnectionError(ZhiJinError):
    """Raised when establishing or maintaining a BLE connection fails."""


class ZhiJinDeviceNotFoundError(ZhiJinConnectionError):
    """Raised when the target BLE device cannot be resolved or discovered."""


class ZhiJinDisconnectedError(ZhiJinConnectionError):
    """Raised when the BLE link drops while an operation is in progress."""


class ZhiJinTimeoutError(ZhiJinError):
    """Raised when the controller does not respond within the configured timeout."""


class ZhiJinProtocolError(ZhiJinError):
    """Raised when a received frame violates expected Modbus/ZhiJin protocol structure."""


class ZhiJinCRCError(ZhiJinProtocolError):
    """Raised when a received Modbus frame fails CRC-16 verification."""


class ZhiJinModbusException(ZhiJinProtocolError):
    """Raised when the controller returns a valid Modbus exception response."""

    def __init__(self, function_code: int, exception_code: int) -> None:
        self.function_code = function_code
        self.exception_code = exception_code
        desc = MODBUS_EXCEPTION_NAMES.get(exception_code, f"Unknown (0x{exception_code:02X})")
        super().__init__(
            f"Modbus exception for function 0x{function_code:02X}: "
            f"{desc} (code 0x{exception_code:02X})"
        )
