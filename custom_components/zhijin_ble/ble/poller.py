"""Adaptive polling policy shared by the Home Assistant coordinator and Mac CLI."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .const import (
    DEFAULT_OFFLINE_ISSUE_SECONDS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POWER_SAVE_INTERVAL,
    DEFAULT_SETTINGS_INTERVAL,
    DEFAULT_UNAVAILABLE_AFTER_FAILURES,
    PowerSaveMode,
)
from .models import TelemetryData


@dataclass(frozen=True, slots=True)
class PollPolicy:
    """Configuration for adaptive polling intervals, settings cadence, and health tracking."""

    poll_interval: int = DEFAULT_POLL_INTERVAL
    power_save_mode: PowerSaveMode = PowerSaveMode.NIGHT_IDLE
    power_save_interval: int = DEFAULT_POWER_SAVE_INTERVAL
    settings_interval: int = DEFAULT_SETTINGS_INTERVAL
    offline_issue_seconds: int = DEFAULT_OFFLINE_ISSUE_SECONDS
    unavailable_after_failures: int = DEFAULT_UNAVAILABLE_AFTER_FAILURES

    def __post_init__(self) -> None:
        if self.poll_interval < 5:
            raise ValueError("poll_interval must be at least 5 seconds")
        if self.power_save_interval < 0:
            raise ValueError("power_save_interval must be >= 0")
        if self.settings_interval < 0:
            raise ValueError("settings_interval must be >= 0")
        if self.offline_issue_seconds < 0:
            raise ValueError("offline_issue_seconds must be >= 0")
        if self.unavailable_after_failures < 1:
            raise ValueError("unavailable_after_failures must be >= 1")


@dataclass(frozen=True, slots=True)
class FailureStatus:
    """Health evaluation returned when a poll cycle fails."""

    consecutive_failures: int
    offline_seconds: float
    mark_unavailable: bool
    raise_offline_issue: bool


class AdaptivePollScheduler:
    """Tracks settings cadence, adaptive low-power polling intervals, and offline timers.

    Designed with an injectable monotonic clock so both Home Assistant and the
    standalone CLI soak tester share identical scheduling and health logic.
    """

    def __init__(
        self,
        policy: PollPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy or PollPolicy()
        self._clock = clock
        self._last_settings_fetch: float | None = None
        self._force_settings_refresh: bool = True
        self._consecutive_failures: int = 0
        self._offline_since: float | None = None
        self._offline_issue_active: bool = False
        self._power_save_active: bool = False

    @property
    def consecutive_failures(self) -> int:
        """Return count of consecutive failed poll cycles."""
        return self._consecutive_failures

    @property
    def power_save_active(self) -> bool:
        """Return True if adaptive power-saving polling is currently active."""
        return self._power_save_active

    @property
    def offline_issue_active(self) -> bool:
        """Return True if an offline alert threshold has been reached and not yet resolved."""
        return self._offline_issue_active

    def request_settings_refresh(self) -> None:
        """Force the next poll cycle to fetch controller settings."""
        self._force_settings_refresh = True

    def should_fetch_settings(self, *, has_settings: bool, now: float | None = None) -> bool:
        """Return True if controller settings should be read on the current poll."""
        if not has_settings or self._force_settings_refresh or self._last_settings_fetch is None:
            return True
        if self.policy.settings_interval == 0:
            return False
        current = self._clock() if now is None else now
        return (current - self._last_settings_fetch) >= self.policy.settings_interval

    def mark_settings_fetched(self, *, now: float | None = None) -> None:
        """Record that settings were freshly read from the controller."""
        self._last_settings_fetch = self._clock() if now is None else now
        self._force_settings_refresh = False

    def evaluate_power_save(self, telemetry: TelemetryData | None) -> bool:
        """Determine whether power-saving polling should be active given latest telemetry."""
        mode = self.policy.power_save_mode
        if (
            mode == PowerSaveMode.OFF
            or self.policy.power_save_interval <= self.policy.poll_interval
            or telemetry is None
        ):
            self._power_save_active = False
            return False

        if mode == PowerSaveMode.NIGHT_IDLE:
            active = (not telemetry.solar_status) and telemetry.is_idle
        elif mode == PowerSaveMode.NIGHT:
            active = not telemetry.solar_status
        elif mode == PowerSaveMode.IDLE:
            active = telemetry.is_idle
        else:
            active = False

        self._power_save_active = active
        return active

    def next_interval(self, telemetry: TelemetryData | None) -> int:
        """Compute the next poll interval in seconds based on power-save state."""
        if self.evaluate_power_save(telemetry):
            return self.policy.power_save_interval
        return self.policy.poll_interval

    def record_success(self) -> bool:
        """Record a successful poll.

        Returns True if an offline issue was active and should now be cleared.
        """
        was_offline_issue = self._offline_issue_active
        self._consecutive_failures = 0
        self._offline_since = None
        self._offline_issue_active = False
        return was_offline_issue

    def record_failure(self, *, now: float | None = None) -> FailureStatus:
        """Record a failed poll and evaluate availability & offline alert thresholds."""
        current = self._clock() if now is None else now
        self._consecutive_failures += 1
        if self._offline_since is None:
            self._offline_since = current

        offline_seconds = max(0.0, current - self._offline_since)
        mark_unavailable = self._consecutive_failures >= self.policy.unavailable_after_failures
        raise_issue = (
            self.policy.offline_issue_seconds > 0
            and offline_seconds >= self.policy.offline_issue_seconds
        )
        if raise_issue:
            self._offline_issue_active = True

        return FailureStatus(
            consecutive_failures=self._consecutive_failures,
            offline_seconds=offline_seconds,
            mark_unavailable=mark_unavailable,
            raise_offline_issue=raise_issue,
        )
