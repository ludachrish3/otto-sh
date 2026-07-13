"""A collection interval below 1s is not meaningful — a host must have time to answer."""

import pytest

from otto.models import MIN_INTERVAL_SECONDS, validate_interval
from otto.monitor.collector import MetricCollector
from otto.suite.suite import OttoSuite


class TestValidator:
    def test_accepts_the_floor_and_above(self) -> None:
        assert validate_interval(1.0) == 1.0
        assert validate_interval(5.0) == 5.0

    def test_rejects_below_the_floor_naming_the_value_and_the_reason(self) -> None:
        with pytest.raises(ValueError, match="monitor interval"):
            validate_interval(0.5)

    def test_floor_is_one_second(self) -> None:
        assert MIN_INTERVAL_SECONDS == 1.0


class TestLibraryBoundary:
    @pytest.mark.asyncio
    async def test_start_monitor_rejects_a_sub_second_interval(self) -> None:
        suite = OttoSuite()
        with pytest.raises(ValueError, match="interval"):
            await suite.start_monitor(hosts=[], interval=0.1)


class TestEngineIsExempt:
    def test_metric_collector_is_not_floored(self) -> None:
        """The engine is a mechanism, not a human-facing knob. Monitor tests drive
        it at 0.01s against FAKE hosts; flooring it would cost real seconds per
        tick and protect nobody — no real host is polled on that path."""
        import inspect

        src = inspect.getsource(MetricCollector.run)
        assert "validate_interval" not in src
