# ZhiJin BLE

> [!NOTE]
> **Disclaimer**: This project was created with the assistance of **Gemini**. The BLE GATT UUIDs and Modbus RTU frame decoding are based on the reverse-engineering work in [wescom-code/zhijinpower_ble](https://github.com/wescom-code/zhijinpower_ble).

A **Home Assistant custom integration** and **standalone macOS/Linux BLE library & debug CLI** for cheap, alleged MPPT Bluetooth solar charge controllers based on ZhiJinPower (`ZJ*`) BLE modules — such as [this controller on AliExpress](https://de.aliexpress.com/item/1005007077300633.html).

---

## Features

- **Flexible Connection Modes**:
  - **On-Demand mode *(default)***: Connects, reads telemetry (and settings when due), and disconnects cleanly — reducing controller radio duty cycle, keeping ESPHome Bluetooth Proxy slots free, and allowing the mobile app to connect between polls.
  - **Persistent mode**: Keeps a single BLE link open for fastest switch/setting response and short poll intervals.
- **Adaptive Power-Saving Polling**: Configurable telemetry and settings intervals (default `60s` / `600s`) with automatic slowdown (default `300s`) during idle or nighttime periods (`night_idle`, `night`, `idle`, or `off`).
- **Resilient BLE & Modbus Transport**:
  - Cached GATT service discovery via `BleakClientWithServiceCache` (`bleak-retry-connector`) with fresh `BLEDevice` callbacks.
  - Sliding-window `ModbusFrameParser` supporting fragmented chunks, coalesced frames, 5-byte Modbus exception frames (`0x83`/`0x86`/`0x90`), bounded buffers, and 1-byte resynchronization on CRC mismatches.
  - Duplicate notification burst absorption and inter-command drain spacing so trailing burst copies are handled cleanly without collisions.
  - In-session command retries, clean reconnect recovery, race-safe disconnect handling with immediate waiter wakeup, and configurable transient failure tolerance.
- **Full Home Assistant Integration**:
  - Automatic Bluetooth discovery (`ZJBE*` / `ZJ-*`), manual `ZJ*` scan & address fallback, and a full **Options Flow** for runtime tuning (including **Temperature Calibration Offset**).
  - Telemetry sensors, chemistry-aware **Healthy Usable Battery (%)** with dynamic charging icons, load/solar power sensors, diagnostic link metrics (RSSI, poll latency, command success rate, connectivity, power-save state), Repairs alerts for prolonged outages, and one-click Diagnostics download.
  - Serialized setting writes (`0x10`) with accurate voltage rounding and in-session readback verification.
- **Standalone macOS / Desktop CLI (`zhijin-ble`)**: Pure-Python `ble/` package with zero Home Assistant dependencies for direct scanning, one-shot reads, setting writes, GATT probing, and multi-hour soak tests with raw hex tracing and JSONL session logs.

---

## Architecture

```text
custom_components/zhijin_ble/
├── ble/                 # Zero-HA-dependency BLE & Modbus library (runs on macOS & HA)
│   ├── const.py         # UUIDs, registers, enums, battery curves, timing defaults
│   ├── exceptions.py    # Structured error hierarchy (timeout, CRC, disconnect, Modbus exc)
│   ├── models.py        # Frozen TelemetryData, SettingsData, ControllerState, ConnectionStats
│   ├── protocol.py      # CRC-16, frame builders, sliding-window ModbusFrameParser, codecs
│   ├── client.py        # ZhiJinBLEClient (on-demand/persistent, retries, burst absorption)
│   ├── poller.py        # AdaptivePollScheduler shared by HA coordinator & Mac CLI
│   ├── scanner.py       # Standalone BleakScanner discovery (macOS UUID & Linux MAC)
│   └── cli.py           # `zhijin-ble` CLI (scan, read, monitor soak test, write, probe)
├── __init__.py          # Lazy HA setup/unload, runtime_data, HA stop cleanup
├── config_flow.py       # Auto Bluetooth discovery, manual fallback, full Options Flow
├── coordinator.py       # Dynamic-interval DataUpdateCoordinator + serialized writes + Repairs
├── entity.py            # Shared ZhiJinEntity base class (DRY DeviceInfo / unique_id)
├── sensor.py            # Telemetry & BLE link diagnostic sensors
├── binary_sensor.py     # Solar/Load/Wind status, Connectivity, Power-Save diagnostic
├── switch.py            # Manual load switch
├── number.py            # Full / Cutoff / Recovery voltage configuration entities
├── select.py            # Battery type / Output mode / Voltage detect configuration entities
├── diagnostics.py       # One-click Home Assistant diagnostic dump
├── brand/               # Local brand icons (icon.png 256px, icon@2x.png 512px)
└── translations/        # English translations (en.json)
```

---

## Exposed Home Assistant Entities

### Telemetry Sensors
| Entity | Unit | Device Class | State Class | Notes |
| --- | --- | --- | --- | --- |
| Battery Voltage | `V` | Voltage | Measurement | Register `0x0002 / 10` |
| Battery | `%` | Battery | Measurement | Matches official app curve (12V / 24V auto) |
| Healthy Usable Battery | `%` | Battery | Measurement | Chemistry-aware safe DoD curve with dynamic charging icons |
| Charge Current | `A` | Current | Measurement | Register `0x0003 / 10` |
| Solar Power | `W` | Power | Measurement | `battery_voltage * charge_current` (Energy Dashboard ready) |
| Discharge Current | `A` | Current | Measurement | Register `0x0004 / 10` |
| Load Power | `W` | Power | Measurement | `battery_voltage * discharge_current` |
| Temperature | `°C` | Temperature | Measurement | Signed int16 register `0x0005 / 100` |
| Total Generation | `Ah` | — | Total Increasing | `rollover_count * 1000 + raw / 10` |

### Binary Sensors
| Entity | Category | Device Class | Notes |
| --- | --- | --- | --- |
| Solar Status | Telemetry | Light | Day/PV input active (`0x0006`) |
| Load Status | Telemetry | Power | Load output active (`0x0007`) |
| Wind Status | Telemetry | Power | Wind input active (`0x0008`) |
| Bluetooth Connectivity | Diagnostic | Connectivity | Always available; reflects live link health + stats attributes |
| Power-Saving Polling | Diagnostic | — | Indicates when adaptive slow polling is active |

### Diagnostic Sensors
| Entity | Unit | Enabled by Default | Notes |
| --- | --- | --- | --- |
| Bluetooth Signal Strength | `dBm` | Yes | Latest advertisement RSSI |
| Poll Latency | `ms` | Yes | Total poll round-trip duration |
| Command Success Rate | `%` | No | Rolling command success percentage |

### Controls & Configuration
| Platform | Entity | Register | Range / Options |
| --- | --- | --- | --- |
| Switch | Manual Load Switch | `0x1007` | On / Off |
| Number (`config`) | Full Voltage | `0x1004` | `10.0 – 30.0 V` (step `0.1`) |
| Number (`config`) | Cutoff Voltage | `0x1006` | `9.0 – 25.0 V` (step `0.1`) |
| Number (`config`) | Recovery Voltage | `0x1009` | `10.0 – 28.0 V` (step `0.1`) |
| Number (`config`) | Temperature Offset | Local | `-25.0 – +25.0 °C` (step `0.1`, synced with Options) |
| Select (`config`) | Battery Type | `0x1001` | Lithium, Gel, Lead-Acid |
| Select (`config`) | Output Mode | `0x1005` | Manual, Auto, Timer, Direct |
| Select (`config`) | Voltage Detect Mode | `0x1008` | Auto, 12V – 96V |

---

## Power Saving & Polling Options

Every parameter can be adjusted anytime from **Settings → Devices & Services → ZhiJin BLE → Configure**:

- **Connection Mode**:
  - `on_demand` *(default, lowest controller power & highest stability)*: connects per poll, reads registers, immediately disconnects. Controller returns to low-power BLE advertising between polls, ESPHome proxy slots stay free, and the mobile app works between polls.
  - `persistent`: keeps a single BLE link open for fastest switch/setting response and fast polling (`10–30s`).
- **Poll Interval**: Default `60s` (`10–3600s`).
- **Power-Saving Polling Mode**:
  - `night_idle` *(default)*: slows to **Power-Saving Poll Interval** (default `300s`) when `solar_status` is off AND both charge and discharge currents are `< 0.1 A`.
  - `night`: slows whenever `solar_status` is off.
  - `idle`: slows whenever both charge and discharge currents are `< 0.1 A` (including overcast daytime idle periods).
  - `off`: always uses the normal poll interval.
- **Settings Refresh Interval**: Default `600s` (10 min). Controller settings are also refreshed immediately after any change made from Home Assistant.
- **Temperature Calibration Offset**: Default `0.0 °C` (`-25.0` to `+25.0 °C`). Added to the raw temperature register to compensate for internal enclosure self-heating (raw value remains available as an entity attribute when non-zero).

---

## Home Assistant Installation (2026.11+)

### Via Marketplace (Recommended)
1. Open **Settings → Marketplace → ⋮ → Custom repositories**.
2. Add this repository URL with category **Integration**.
3. Install **ZhiJin BLE**.
4. Go to **Settings → Devices & Services** — your `ZJ*` controller should be auto-discovered, or click **Add Integration → ZhiJin BLE**.

### Manual Installation
Copy `custom_components/zhijin_ble/` into your Home Assistant `config/custom_components/zhijin_ble/` directory and restart Home Assistant.

---

## Running Directly from macOS (Debug & Soak Testing)

No Home Assistant installation is required! Using [`uv`](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev

# 1. Discover nearby controllers
uv run zhijin-ble scan

# 2. One-shot telemetry + settings read
uv run zhijin-ble read

# 3. Long-running soak test with raw packet tracing & JSONL session log
uv run zhijin-ble monitor --mode on_demand --interval 60 --power-save night_idle --raw --log-file soak.jsonl
```

See [docs/MAC_DEBUGGING.md](docs/MAC_DEBUGGING.md) for detailed macOS debugging workflows and [docs/PROTOCOL.md](docs/PROTOCOL.md) for the full BLE/Modbus register specification.

---

## Development & Tests

```bash
uv sync --extra dev
uv run pytest
uv run ruff check custom_components tests
```

---

## Acknowledgements

Many thanks to [wescom-code/zhijinpower_ble](https://github.com/wescom-code/zhijinpower_ble) for reverse-engineering and documenting the ZhiJinPower BLE GATT UUIDs, Modbus RTU frame structure, and register mappings that made decoding the controller protocol possible.
