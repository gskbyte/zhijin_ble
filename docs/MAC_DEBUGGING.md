# macOS Direct BLE Debugging & Soak-Testing Guide

The `custom_components/zhijin_ble/ble/` library has **zero Home Assistant dependencies** and ships with the `zhijin-ble` CLI so you can connect directly from your Mac to run multi-hour reliability tests, inspect raw BLE notification bursts, and compare connection modes.

---

## 1. Setup on macOS

1. Install dependencies using [`uv`](https://docs.astral.sh/uv/) (or `pip install -e ".[dev]"`):
   ```bash
   uv sync --extra dev
   ```
2. Grant Bluetooth access to your terminal app if prompted (**System Settings → Privacy & Security → Bluetooth → Terminal / iTerm2 / VS Code**).
3. Ensure Home Assistant and the official ZhiJin phone app are **not** holding an active connection to the controller (the controller accepts only one BLE connection at a time).

> **macOS CoreBluetooth Note**: macOS does not expose raw Bluetooth MAC addresses (e.g. `25:08:00:00:0A:BA`). Instead, CoreBluetooth assigns a per-Mac peripheral UUID (e.g. `A1B2C3D4-E5F6-...`). Pass `--name ZJ` or omit `--address` to auto-discover, or pass the UUID shown by `zhijin-ble scan`.

---

## 2. Discovering & Probing the Controller

```bash
# Discover nearby ZhiJin controllers (sorted by RSSI)
uv run zhijin-ble scan

# Inspect GATT services, characteristic properties, MTU, and test a raw read
uv run zhijin-ble probe --start-reg 0x0001 --count 16

# Perform a single read of telemetry + settings (with optional temperature calibration offset)
uv run zhijin-ble read --raw --temp-offset -5.3
```

---

## 3. Running Long Soak Tests (`monitor`)

### Test A: Low-Power On-Demand Mode (Recommended)
Connects for each poll, fetches telemetry (and settings every 10 min), and disconnects immediately:

```bash
uv run zhijin-ble monitor \
  --mode on_demand \
  --interval 60 \
  --power-save night_idle \
  --night-interval 300 \
  --settings-interval 600 \
  --log-file logs/soak_on_demand.jsonl
```

### Test B: Stress Test Persistent Mode (Compare Against HA)
Holds a single BLE link open and logs every raw packet and duplicate burst:

```bash
uv run zhijin-ble monitor \
  --mode persistent \
  --interval 15 \
  --power-save off \
  --raw \
  --log-file logs/soak_persistent.jsonl
```

### Test C: Direct CoreBluetooth vs `bleak-retry-connector`
Add `--direct-bleak` to bypass `bleak-retry-connector` if you want to compare direct CoreBluetooth `BleakClient` behavior:

```bash
uv run zhijin-ble monitor --mode on_demand --interval 30 --direct-bleak --raw
```

### Stopping & Reading the Report
Press `Ctrl+C` (or pass `--duration 3600` / `--max-polls 100`) at any time. The CLI cleanly unsubscribes, disconnects the BLE link, flushes the JSONL log, and prints a full reliability report:

- Total / OK / Failed poll counts and success percentage
- Connect attempts, successes, failures, unexpected drops, avg/last connect latency (ms)
- Command attempts, retries, timeouts, CRC errors, duplicate bursts, unsolicited frames, avg/p95 RTT (ms)
- Error breakdown by exception type

---

## 4. Testing Setting Writes from Mac

```bash
# Toggle manual load switch and read back settings to confirm
uv run zhijin-ble write --load on
uv run zhijin-ble write --load off

# Adjust a voltage threshold (0.1V precision with proper rounding)
uv run zhijin-ble write --full-voltage 12.6 --cutoff-voltage 10.0 --recovery-voltage 11.0
```
