# ZhiJin BLE Protocol Specification

This document describes the BLE GATT transport and Modbus RTU application protocol spoken by ZhiJinPower solar charge controllers (tested with Product ID 8 / `ZJBE-*` modules).

---

## 1. BLE Advertisement & GATT Profile

- **Advertised Local Name**: Starts with `ZJ` (e.g., `ZJBE-240800XXXXXX`).
- **Connection Exclusivity**: The BLE module supports only **one** active central connection at a time. While connected, the device stops advertising.
- **Nordic UART Service (NUS) Variant** *(TX/RX swapped vs standard Nordic NUS)*:

| Role | UUID | Direction | Notes |
| --- | --- | --- | --- |
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | — | UART bridge service |
| Notify (TX) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` | Controller → Host | Subscribe via CCCD before writing |
| Write (RX) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` | Host → Controller | Uses `write-without-response` when advertised |

---

## 2. Modbus RTU Framing over BLE

All payloads are standard **Modbus RTU** frames with **little-endian CRC-16** (polynomial `0xA001`, init `0xFFFF`).

- **Unit ID**: `0x01` (Solar controller).
- **Supported Function Codes**:
  - `0x03` — Read Holding Registers
    - Request (8 bytes): `[unit_id, 0x03, start_hi, start_lo, count_hi, count_lo, crc_lo, crc_hi]`
    - Response (`byte_count + 5` bytes): `[unit_id, 0x03, byte_count, data..., crc_lo, crc_hi]`
  - `0x10` — Write Multiple Registers *(used by official app even for single-register writes)*
    - Request: `[unit_id, 0x10, start_hi, start_lo, count_hi, count_lo, byte_count, values..., crc_lo, crc_hi]`
    - Response (8 bytes): `[unit_id, 0x10, start_hi, start_lo, count_hi, count_lo, crc_lo, crc_hi]`
  - `0x06` — Write Single Register (response 8 bytes)
  - `0x83`, `0x86`, `0x90` — Modbus Exception Responses (5 bytes): `[unit_id, func | 0x80, exc_code, crc_lo, crc_hi]`

---

## 3. Observed Hardware Quirks

1. **Asymmetric Register Counts**:
   - **Telemetry read** requests `start=0x0001, count=16` (`01 03 00 01 00 10 15 c6`), and the controller replies with **10 registers (`byte_count = 20`, 25-byte frame)**.
   - **Settings read** requests `start=0x1001, count=15` (`01 03 10 01 00 0f 50 ce`), and the controller replies with **9 registers (`byte_count = 18`, 23-byte frame)**.
2. **Duplicate Notification Bursts**:
   - Real hardware captures show that after responding to a single read request (typically within `25–160 ms`), the controller's BLE-UART bridge frequently emits **0 to 7 additional identical copies** of the exact same response frame within `1–10 ms`.
   - `ZhiJinBLEClient` absorbs matching duplicates arriving within `duplicate_window` (default `1.0s`) at `DEBUG` level and enforces `min_command_interval` (default `0.3s`) before the next command so trailing bursts drain cleanly.
3. **Post-Subscribe Settle Window**:
   - Waiting ~200 ms after enabling notifications on `6e400002` prevents the first command from being dropped on cold connections.

---

## 4. Register Maps

### 4.1 Telemetry Block (`0x0001..0x000A`)

Request: `01 03 00 01 00 10 15 c6` → Response `byte_count = 0x14` (20 bytes)

| Register | Payload Offset | Type | Scale | Field |
| --- | --- | --- | --- | --- |
| `0x0001` | `0..1` | `uint16` | `1` | `solar_model_type` |
| `0x0002` | `2..3` | `uint16` | `/ 10.0` | `battery_voltage` (V) |
| `0x0003` | `4..5` | `uint16` | `/ 10.0` | `charge_current` (A) |
| `0x0004` | `6..7` | `uint16` | `/ 10.0` | `discharge_current` (A) |
| `0x0005` | `8..9` | `int16` | `/ 100.0` | `temperature` (°C, signed) |
| `0x0006` | `10..11` | `uint16` | `== 1` | `solar_status` (Day/PV active) |
| `0x0007` | `12..13` | `uint16` | `== 1` | `load_status` (Load active) |
| `0x0008` | `14..15` | `uint16` | `== 1` | `wind_status` (Wind active) |
| `0x0009` | `16..17` | `uint16` | `/ 10.0` | `total_power_raw` (Ah) |
| `0x000A` | `18..19` | `uint16` | `* 1000.0` | `total_power_resets` (rollover count) |

**Derived Telemetry Metrics**:
- `total_generation (Ah) = total_power_resets * 1000.0 + total_power_raw / 10.0`
- `solar_power (W) = round(battery_voltage * charge_current, 1)`
- `load_power (W) = round(battery_voltage * discharge_current, 1)`
- `battery_percent (%)`: `sys = 1 if V <= 17.0 else 2`; linear clamp between `9.0 * sys` (0%) and `12.6 * sys` (100%).
- `healthy_usable_battery (%)`: chemistry-aware piecewise linear curve on `V / sys`:
  - **Lithium — 3S Li-ion (`1`, `full_voltage / sys <= 13.0V`, e.g. `12.6V`)**: `10.5V → 0%`, `11.1V → 20%`, `11.4V → 40%`, `11.8V → 70%`, `12.3V → 90%`, `12.6V → 100%`
  - **Lithium — 4S LiFePO4 (`1`, `full_voltage / sys > 13.0V`, e.g. `14.4V`)**: `12.2V → 0%`, `13.0V → 30%`, `13.2V → 70%`, `13.4V → 100%`
  - **Gel (`2`)**: `11.9V → 0%`, `12.1V → 15%`, `12.3V → 45%`, `12.5V → 75%`, `12.8V → 100%`
  - **Lead-Acid (`3`)**: `12.0V → 0%`, `12.1V → 20%`, `12.3V → 50%`, `12.5V → 80%`, `12.7V → 100%`

**Real Capture Example**:
`01031400010078000000000528000100010000005200000daf`
→ Model `1`, `12.0 V`, `0.0 A` charge, `0.0 A` discharge, `13.20 °C`, Solar ON, Load ON, Wind OFF, `8.2 Ah` (`raw=82, resets=0`).

---

### 4.2 Settings Block (`0x1001..0x1009`)

Request: `01 03 10 01 00 0f 50 ce` → Response `byte_count = 0x12` (18 bytes)

| Register | Payload Offset | Type | Scale / Values | Field |
| --- | --- | --- | --- | --- |
| `0x1001` | `0..1` | `uint16` | `1=Lithium, 2=Gel, 3=Lead-Acid` | `battery_type` |
| `0x1002` | `2..3` | `uint16` | `0..12` | `time_hour` |
| `0x1003` | `4..5` | `uint16` | `0..60` | `time_min` |
| `0x1004` | `6..7` | `uint16` | `/ 10.0` (V) | `full_voltage` |
| `0x1005` | `8..9` | `uint16` | `0=Manual, 1=Auto, 2=Timer, 3=Direct` | `output_mode` |
| `0x1006` | `10..11` | `uint16` | `/ 10.0` (V) | `cutoff_voltage` |
| `0x1007` | `12..13` | `uint16` | `0=Off, 1=On` | `load_switch` |
| `0x1008` | `14..15` | `uint16` | `0=Auto, 1=12V, ..., 8=96V` | `voltage_detect_mode` |
| `0x1009` | `16..17` | `uint16` | `/ 10.0` (V) | `recovery_voltage` |

**Real Capture Example**:
`010312000100000000007e0000006400010000006ea4d1`
→ Lithium (`1`), timer `00:00`, Full `12.6 V`, Manual (`0`), Cutoff `10.0 V`, Load Switch ON (`1`), Voltage Detect Auto (`0`), Recovery `11.0 V`.
