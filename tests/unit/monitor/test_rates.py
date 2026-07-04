"""Unit tests for the shared counter->rate helpers."""

from datetime import datetime, timedelta, timezone

from otto.monitor.rates import RateTracker, compute_rate

T0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


class TestComputeRate:
    def test_positive_delta(self):
        assert compute_rate(100.0, 250.0, 5.0) == 30.0

    def test_zero_delta_is_zero_rate(self):
        assert compute_rate(100.0, 100.0, 5.0) == 0.0

    def test_negative_delta_returns_none(self):
        """Counter reset (reboot) or wrap: skip the tick, never a spike."""
        assert compute_rate(100.0, 50.0, 5.0) is None

    def test_zero_dt_returns_none(self):
        assert compute_rate(100.0, 200.0, 0.0) is None

    def test_negative_dt_returns_none(self):
        assert compute_rate(100.0, 200.0, -1.0) is None


class TestRateTracker:
    def test_first_sighting_returns_none(self):
        tracker = RateTracker()
        assert tracker.update("k", 100.0, T0) is None

    def test_second_sighting_returns_rate(self):
        tracker = RateTracker()
        tracker.update("k", 100.0, T0)
        assert tracker.update("k", 250.0, T0 + timedelta(seconds=5)) == 30.0

    def test_rate_uses_actual_elapsed_not_nominal(self):
        tracker = RateTracker()
        tracker.update("k", 0.0, T0)
        # 10 s elapsed, not "the interval": 100/10 = 10
        assert tracker.update("k", 100.0, T0 + timedelta(seconds=10)) == 10.0

    def test_negative_delta_rebaselines(self):
        """Reset tick returns None; the NEXT tick rates against the new baseline."""
        tracker = RateTracker()
        tracker.update("k", 1000.0, T0)
        assert tracker.update("k", 10.0, T0 + timedelta(seconds=5)) is None
        assert tracker.update("k", 60.0, T0 + timedelta(seconds=10)) == 10.0

    def test_keys_are_independent(self):
        tracker = RateTracker()
        tracker.update("a", 0.0, T0)
        tracker.update("b", 0.0, T0)
        assert tracker.update("a", 50.0, T0 + timedelta(seconds=5)) == 10.0
        assert tracker.update("b", 100.0, T0 + timedelta(seconds=5)) == 20.0

    def test_prune_drops_stale_keys(self):
        """A vanished interface's state is dropped; re-appearance re-baselines."""
        tracker = RateTracker()
        tracker.update("gone", 100.0, T0)
        tracker.update("kept", 100.0, T0)
        tracker.prune({"kept"})
        assert tracker.update("gone", 200.0, T0 + timedelta(seconds=5)) is None
        assert tracker.update("kept", 200.0, T0 + timedelta(seconds=5)) == 20.0
