"""Unit tests for the shared AdaptivePollScheduler and PollPolicy."""

from __future__ import annotations

import pytest

from custom_components.zhijin_ble.ble.const import PowerSaveMode
from custom_components.zhijin_ble.ble.poller import AdaptivePollScheduler, PollPolicy
from custom_components.zhijin_ble.ble.protocol import decode_telemetry
from tests.conftest import FakeClock, make_telemetry_frame


def _telem(solar: int, charge: float, discharge: float):
    frame = make_telemetry_frame(
        solar_status=solar,
        charge_current=charge,
        discharge_current=discharge,
    )
    return decode_telemetry(frame[3:-2])


class TestPollPolicyValidation:
    def test_rejects_invalid_values(self) -> None:
        with pytest.raises(ValueError):
            PollPolicy(poll_interval=4)
        with pytest.raises(ValueError):
            PollPolicy(power_save_interval=-1)
        with pytest.raises(ValueError):
            PollPolicy(settings_interval=-1)
        with pytest.raises(ValueError):
            PollPolicy(offline_issue_seconds=-1)
        with pytest.raises(ValueError):
            PollPolicy(unavailable_after_failures=0)


class TestAdaptivePollScheduler:
    def test_settings_fetch_cadence_and_forced_refresh(self) -> None:
        clock = FakeClock(100.0)
        scheduler = AdaptivePollScheduler(PollPolicy(settings_interval=600), clock=clock)

        assert scheduler.should_fetch_settings(has_settings=False) is True
        scheduler.mark_settings_fetched()
        assert scheduler.should_fetch_settings(has_settings=True) is False

        clock.advance(599.0)
        assert scheduler.should_fetch_settings(has_settings=True) is False

        clock.advance(1.0)
        assert scheduler.should_fetch_settings(has_settings=True) is True
        scheduler.mark_settings_fetched()

        scheduler.request_settings_refresh()
        assert scheduler.should_fetch_settings(has_settings=True) is True

    def test_settings_interval_zero_only_fetches_initially_or_on_demand(self) -> None:
        clock = FakeClock(100.0)
        scheduler = AdaptivePollScheduler(PollPolicy(settings_interval=0), clock=clock)

        assert scheduler.should_fetch_settings(has_settings=False) is True
        scheduler.mark_settings_fetched()
        clock.advance(99999.0)
        assert scheduler.should_fetch_settings(has_settings=True) is False

        scheduler.request_settings_refresh()
        assert scheduler.should_fetch_settings(has_settings=True) is True

    @pytest.mark.parametrize(
        ("mode", "solar", "charge", "discharge", "expected_interval", "expected_active"),
        [
            # NIGHT_IDLE: active only when solar=0 AND idle
            (PowerSaveMode.NIGHT_IDLE, 0, 0.0, 0.0, 300, True),
            (PowerSaveMode.NIGHT_IDLE, 0, 0.0, 1.5, 60, False),
            (PowerSaveMode.NIGHT_IDLE, 1, 0.0, 0.0, 60, False),
            (PowerSaveMode.NIGHT_IDLE, 1, 5.0, 0.0, 60, False),
            # NIGHT: active whenever solar=0
            (PowerSaveMode.NIGHT, 0, 0.0, 2.0, 300, True),
            (PowerSaveMode.NIGHT, 1, 0.0, 0.0, 60, False),
            # IDLE: active whenever currents are ~0 even in daytime
            (PowerSaveMode.IDLE, 1, 0.0, 0.0, 300, True),
            (PowerSaveMode.IDLE, 0, 0.0, 0.0, 300, True),
            (PowerSaveMode.IDLE, 1, 0.5, 0.0, 60, False),
            (PowerSaveMode.IDLE, 0, 0.0, 0.5, 60, False),
            # OFF: never slows down
            (PowerSaveMode.OFF, 0, 0.0, 0.0, 60, False),
        ],
    )
    def test_power_save_modes(
        self,
        mode: PowerSaveMode,
        solar: int,
        charge: float,
        discharge: float,
        expected_interval: int,
        expected_active: bool,
    ) -> None:
        scheduler = AdaptivePollScheduler(
            PollPolicy(poll_interval=60, power_save_mode=mode, power_save_interval=300)
        )
        interval = scheduler.next_interval(_telem(solar, charge, discharge))
        assert interval == expected_interval
        assert scheduler.power_save_active is expected_active

    def test_power_save_disabled_when_interval_not_greater(self) -> None:
        scheduler = AdaptivePollScheduler(
            PollPolicy(poll_interval=120, power_save_mode=PowerSaveMode.NIGHT, power_save_interval=60)
        )
        assert scheduler.next_interval(_telem(0, 0.0, 0.0)) == 120
        assert scheduler.power_save_active is False
        assert scheduler.next_interval(None) == 120

    def test_failure_tracking_and_time_based_offline_issue(self) -> None:
        clock = FakeClock(1000.0)
        scheduler = AdaptivePollScheduler(
            PollPolicy(unavailable_after_failures=2, offline_issue_seconds=180),
            clock=clock,
        )

        # First failure: tolerated (not yet unavailable, no repair issue)
        s1 = scheduler.record_failure()
        assert s1.consecutive_failures == 1
        assert s1.mark_unavailable is False
        assert s1.raise_offline_issue is False
        assert scheduler.offline_issue_active is False

        # Second failure after 60s: unavailable, still below 180s issue threshold
        clock.advance(60.0)
        s2 = scheduler.record_failure()
        assert s2.consecutive_failures == 2
        assert s2.mark_unavailable is True
        assert s2.raise_offline_issue is False

        # Third failure at +180s total offline: raises repair issue
        clock.advance(120.0)
        s3 = scheduler.record_failure()
        assert s3.offline_seconds == 180.0
        assert s3.raise_offline_issue is True
        assert scheduler.offline_issue_active is True

        # Recovery returns True (clear issue) and resets counters
        assert scheduler.record_success() is True
        assert scheduler.consecutive_failures == 0
        assert scheduler.offline_issue_active is False
        # Subsequent success returns False (no active issue to clear)
        assert scheduler.record_success() is False
