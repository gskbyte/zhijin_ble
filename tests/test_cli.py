"""Unit tests for the standalone macOS / desktop zhijin-ble CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.device import BLEDevice

from custom_components.zhijin_ble.ble.cli import (
    build_parser,
    format_session_summary,
    format_telemetry_block,
    main,
)
from custom_components.zhijin_ble.ble.models import ConnectionStats
from custom_components.zhijin_ble.ble.protocol import decode_settings, decode_telemetry
from custom_components.zhijin_ble.ble.scanner import DiscoveredController
from tests.conftest import REAL_SETTINGS, REAL_TELEMETRY_12_0V


def _discovered() -> DiscoveredController:
    dev = BLEDevice("UUID-MAC-1", "ZJBE-TEST", details={})
    return DiscoveredController(
        device=dev,
        name="ZJBE-TEST",
        address="UUID-MAC-1",
        rssi=-58,
        service_uuids=("6e400001-b5a3-f393-e0a9-e50e24dcca9e",),
    )


class TestCLIFormattingAndParsing:
    def test_format_telemetry_and_summary(self) -> None:
        telem = decode_telemetry(REAL_TELEMETRY_12_0V[3:-2], battery_type=1)
        settings = decode_settings(REAL_SETTINGS[3:-2])
        block = format_telemetry_block(telem, settings)
        assert "12.0 V" in block
        assert "13.20 °C" in block
        assert "Lithium" in block
        assert "12.6 V" in block

        calibrated_block = format_telemetry_block(telem, temperature_offset=-5.3)
        assert "7.90 °C (raw=13.20 °C, offset=-5.30 °C)" in calibrated_block

        stats = ConnectionStats(
            connect_attempts=3,
            connect_successes=3,
            commands_sent=6,
            commands_succeeded=5,
            commands_failed=1,
            duplicate_frames=14,
        )
        summary = format_session_summary(
            duration_s=120.5,
            total_polls=5,
            successful_polls=4,
            failed_polls=1,
            stats=stats,
            errors_by_type={"ZhiJinTimeoutError": 1},
        )
        assert "80.0%" in summary
        assert "duplicate_bursts=14" in summary
        assert "ZhiJinTimeoutError=1" in summary

    def test_parser_accepts_all_subcommands(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["monitor", "--mode", "persistent", "--interval", "30", "--power-save", "idle", "--raw"]
        )
        assert args.command == "monitor"
        assert args.mode == "persistent"
        assert args.interval == 30
        assert args.power_save == "idle"
        assert args.raw is True


class TestCLICommands:
    def test_scan_json_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch(
            "custom_components.zhijin_ble.ble.cli.scan_controllers",
            new=AsyncMock(return_value=[_discovered()]),
        ):
            rc = main(["scan", "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload) == 1
        assert payload[0]["name"] == "ZJBE-TEST"
        assert payload[0]["rssi"] == -58

    def test_read_json_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        telem = decode_telemetry(REAL_TELEMETRY_12_0V[3:-2], battery_type=1)
        settings = decode_settings(REAL_SETTINGS[3:-2])

        fake_client = MagicMock()
        fake_client.poll = AsyncMock(return_value=(telem, settings))
        fake_client.disconnect = AsyncMock()
        fake_client.stats = ConnectionStats(connect_attempts=1, connect_successes=1)

        with (
            patch(
                "custom_components.zhijin_ble.ble.cli.find_controller",
                new=AsyncMock(return_value=_discovered()),
            ),
            patch(
                "custom_components.zhijin_ble.ble.cli._build_client",
                return_value=fake_client,
            ),
        ):
            rc = main(["read", "--temp-offset", "-5.3", "--json"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["telemetry"]["battery_voltage"] == 12.0
        assert data["telemetry"]["temperature"] == 13.2
        assert data["telemetry"]["temperature_offset"] == -5.3
        assert data["telemetry"]["calibrated_temperature"] == 7.9
        assert data["settings"]["full_voltage"] == 12.6
        fake_client.disconnect.assert_awaited_once()

    def test_monitor_runs_max_polls_and_writes_jsonl_log(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        telem = decode_telemetry(REAL_TELEMETRY_12_0V[3:-2], battery_type=1)
        settings = decode_settings(REAL_SETTINGS[3:-2])
        log_file = tmp_path / "session.jsonl"

        fake_client = MagicMock()
        fake_client.poll = AsyncMock(side_effect=[(telem, settings), (telem, None)])
        fake_client.disconnect = AsyncMock()
        fake_client.stats = ConnectionStats(commands_sent=3, commands_succeeded=3)
        fake_client._cli_rssi_ref = {"value": -58}

        async def _instant_timeout(coro, timeout):
            coro.close()
            raise TimeoutError

        with (
            patch(
                "custom_components.zhijin_ble.ble.cli.find_controller",
                new=AsyncMock(return_value=_discovered()),
            ),
            patch(
                "custom_components.zhijin_ble.ble.cli._build_client",
                return_value=fake_client,
            ),
            patch("asyncio.wait_for", new=_instant_timeout),
        ):
            rc = main(
                [
                    "monitor",
                    "--mode",
                    "on_demand",
                    "--interval",
                    "10",
                    "--max-polls",
                    "2",
                    "--log-file",
                    str(log_file),
                ]
            )

        assert rc == 0
        out = capsys.readouterr().out
        assert "=== ZhiJin BLE Debug Session Summary ===" in out
        assert "2 total | 2 ok | 0 failed (100.0%)" in out

        records = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        events = [r["event"] for r in records]
        assert events == ["poll_ok", "poll_ok", "session_summary"]

    def test_write_validates_voltage_range_before_connecting(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["write", "--full-voltage", "99.9"])
        assert rc == 1
        assert "must be between" in capsys.readouterr().err

    def test_write_requires_at_least_one_option(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["write"])
        assert rc == 1
        assert "Specify at least one setting" in capsys.readouterr().err

    def test_write_happy_path_and_probe_command(self, capsys: pytest.CaptureFixture[str]) -> None:
        telem = decode_telemetry(REAL_TELEMETRY_12_0V[3:-2], battery_type=1)
        settings = decode_settings(REAL_SETTINGS[3:-2])

        fake_client = MagicMock()
        fake_client.write_register = AsyncMock()
        fake_client.read_settings = AsyncMock(return_value=settings)
        fake_client.read_telemetry = AsyncMock(return_value=telem)
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client._client = MagicMock(mtu_size=247, services=[])

        with (
            patch(
                "custom_components.zhijin_ble.ble.cli.find_controller",
                new=AsyncMock(return_value=_discovered()),
            ),
            patch(
                "custom_components.zhijin_ble.ble.cli._build_client",
                return_value=fake_client,
            ),
        ):
            rc_write = main(
                [
                    "write",
                    "--load",
                    "on",
                    "--full-voltage",
                    "12.6",
                    "--cutoff-voltage",
                    "10.0",
                    "--recovery-voltage",
                    "11.0",
                    "--battery-type",
                    "lithium",
                    "--output-mode",
                    "manual",
                    "--voltage-detect",
                    "auto",
                ]
            )
            assert rc_write == 0
            assert fake_client.write_register.await_count == 7
            assert "Write verified!" in capsys.readouterr().out

            rc_probe = main(["probe"])
            assert rc_probe == 0
            assert "MTU size: 247" in capsys.readouterr().out

    def test_monitor_records_poll_failures_in_summary_and_jsonl(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_file = tmp_path / "fail.jsonl"
        fake_client = MagicMock()
        fake_client.poll = AsyncMock(side_effect=TimeoutError("no reply"))
        fake_client.disconnect = AsyncMock()
        fake_client.stats = ConnectionStats(timeouts=1, commands_failed=1)
        fake_client._cli_rssi_ref = {"value": -70}

        with (
            patch(
                "custom_components.zhijin_ble.ble.cli.find_controller",
                new=AsyncMock(return_value=_discovered()),
            ),
            patch(
                "custom_components.zhijin_ble.ble.cli._build_client",
                return_value=fake_client,
            ),
        ):
            rc = main(["monitor", "--max-polls", "1", "--log-file", str(log_file)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "TimeoutError=1" in out
        records = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        assert [r["event"] for r in records] == ["poll_fail", "session_summary"]
