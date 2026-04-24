"""
Unit tests for the stability testing feature (``--iterations`` / ``--duration``).

Tests verify:
  - ``pytest_runtest_protocol`` repeats the call phase the correct number of times
  - Duration mode stops after the specified time
  - Both modes combine (whichever limit hits first)
  - Setup failures skip iteration and go straight to teardown
  - Default (iterations=0, duration=0) defers to normal pytest behaviour
  - ``StabilityCollector`` accumulates per-test pass/fail counts
  - ``_print_stability_report`` uses percentage-based thresholds
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from otto.suite.plugin import OttoPlugin, StabilityCollector


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_report(when: str = 'call', passed: bool = True) -> MagicMock:
    """Create a mock pytest.TestReport."""
    report = MagicMock()
    report.when = when
    report.passed = passed
    report.failed = not passed
    return report


def _make_item() -> MagicMock:
    """Create a mock pytest.Item with the attributes the hook expects."""
    item = MagicMock()
    item._request = True  # hasattr check passes, truthy so _initrequest not called
    item.config.getoption.return_value = False  # setupshow=False
    return item


def _call_and_report_side_effect(item, when, log=True, **kwds):
    """Return a passing report for the given phase."""
    return _make_report(when, passed=True)


def _call_and_report_setup_fails(item, when, log=True, **kwds):
    """Return a failing report for setup, passing for everything else."""
    if when == 'setup':
        return _make_report(when, passed=False)
    return _make_report(when, passed=True)


# ── pytest_runtest_protocol ──────────────────────────────────────────────────

class TestRunTestProtocol:

    def test_no_stability_defers_to_default(self):
        """iterations=0, duration=0 returns None (fall through)."""
        plugin = OttoPlugin(iterations=0, duration=0)
        result = plugin.pytest_runtest_protocol(MagicMock(), nextitem=None)
        assert result is None

    def test_iterations_repeats_call_n_times(self):
        """--iterations 3 calls the call phase 3 times, setup/teardown once each."""
        plugin = OttoPlugin(iterations=3)
        item = _make_item()

        with patch('otto.suite.plugin.call_and_report', side_effect=_call_and_report_side_effect) as mock_car:
            result = plugin.pytest_runtest_protocol(item, nextitem=None)

        assert result is True
        # 1 setup + 3 calls + 1 teardown = 5 total
        assert mock_car.call_count == 5
        phases = [c.args[1] for c in mock_car.call_args_list]
        assert phases == ['setup', 'call', 'call', 'call', 'teardown']

    def test_setup_failure_skips_call_phase(self):
        """If setup fails, skip call iterations and go straight to teardown."""
        plugin = OttoPlugin(iterations=10)
        item = _make_item()

        with patch('otto.suite.plugin.call_and_report', side_effect=_call_and_report_setup_fails) as mock_car:
            result = plugin.pytest_runtest_protocol(item, nextitem=None)

        assert result is True
        # 1 setup (failed) + 1 teardown = 2 total, no call phase
        assert mock_car.call_count == 2
        phases = [c.args[1] for c in mock_car.call_args_list]
        assert phases == ['setup', 'teardown']

    def test_duration_stops_after_time(self):
        """--duration should stop after the specified time elapses."""
        plugin = OttoPlugin(duration=5)
        item = _make_item()

        # Time sequence: deadline calc uses first two, then loop checks before each call
        # monotonic()=100 for deadline calc -> deadline=105
        # monotonic()=100 -> 100<105 -> call #1
        # monotonic()=102 -> 102<105 -> call #2
        # monotonic()=104 -> 104<105 -> call #3
        # monotonic()=106 -> 106>=105 -> stop
        times = iter([100.0, 100.0, 102.0, 104.0, 106.0])

        with patch('otto.suite.plugin.call_and_report', side_effect=_call_and_report_side_effect) as mock_car, \
             patch('otto.suite.plugin.time') as mock_time:
            mock_time.monotonic = lambda: next(times)
            result = plugin.pytest_runtest_protocol(item, nextitem=None)

        assert result is True
        phases = [c.args[1] for c in mock_car.call_args_list]
        # 1 setup + 3 calls + 1 teardown
        assert phases == ['setup', 'call', 'call', 'call', 'teardown']

    def test_combined_iterations_and_duration_iterations_first(self):
        """When both are set, stop at whichever limit hits first (iterations)."""
        plugin = OttoPlugin(iterations=2, duration=9999)
        item = _make_item()

        with patch('otto.suite.plugin.call_and_report', side_effect=_call_and_report_side_effect) as mock_car:
            result = plugin.pytest_runtest_protocol(item, nextitem=None)

        assert result is True
        phases = [c.args[1] for c in mock_car.call_args_list]
        assert phases == ['setup', 'call', 'call', 'teardown']

    def test_combined_iterations_and_duration_duration_first(self):
        """When both are set, stop at whichever limit hits first (duration)."""
        plugin = OttoPlugin(iterations=9999, duration=3)
        item = _make_item()

        # Time jumps past deadline after first call
        times = iter([100.0, 100.0, 104.0])

        with patch('otto.suite.plugin.call_and_report', side_effect=_call_and_report_side_effect) as mock_car, \
             patch('otto.suite.plugin.time') as mock_time:
            mock_time.monotonic = lambda: next(times)
            result = plugin.pytest_runtest_protocol(item, nextitem=None)

        assert result is True
        phases = [c.args[1] for c in mock_car.call_args_list]
        # 1 setup + 1 call (then time expired) + 1 teardown
        assert phases == ['setup', 'call', 'teardown']

    def test_funcargs_cleaned_up(self):
        """After protocol completes, item._request and funcargs are cleared."""
        plugin = OttoPlugin(iterations=1)
        item = _make_item()

        with patch('otto.suite.plugin.call_and_report', side_effect=_call_and_report_side_effect):
            plugin.pytest_runtest_protocol(item, nextitem=None)

        assert item._request is False
        assert item.funcargs is None


# ── StabilityCollector ───────────────────────────────────────────────────────

class TestStabilityCollector:

    def test_records_pass_and_fail(self):
        collector = StabilityCollector()
        collector.record('test_a', passed=True)
        collector.record('test_a', passed=True)
        collector.record('test_a', passed=False)
        assert collector.results['test_a'] == (2, 3)

    def test_multiple_tests_tracked_independently(self):
        collector = StabilityCollector()
        collector.record('test_a', passed=True)
        collector.record('test_b', passed=False)
        assert collector.results['test_a'] == (1, 1)
        assert collector.results['test_b'] == (0, 1)


# ── _print_stability_report ─────────────────────────────────────────────────

class TestStabilityReport:

    def test_report_uses_percentage_threshold(self, tmp_path):
        from otto.cli.test import _print_stability_report

        collector = StabilityCollector()
        for _ in range(9):
            collector.record('test_a', passed=True)
        collector.record('test_a', passed=False)  # 90% pass rate

        # threshold=90 → exactly meets threshold → STABLE
        with patch('otto.cli.test.logger') as mock_logger:
            _print_stability_report('MySuite', collector, 10, 0, 90.0, tmp_path)

        report_text = (tmp_path / 'stability_report.txt').read_text()
        assert 'STABLE' in report_text
        assert '90%' in report_text
        assert 'PASS' in report_text

    def test_report_fails_below_threshold(self, tmp_path):
        from otto.cli.test import _print_stability_report

        collector = StabilityCollector()
        for _ in range(8):
            collector.record('test_a', passed=True)
        for _ in range(2):
            collector.record('test_a', passed=False)  # 80% pass rate

        with patch('otto.cli.test.logger'), \
             pytest.raises(SystemExit, match='1'):
            _print_stability_report('MySuite', collector, 10, 0, 90.0, tmp_path)

        report_text = (tmp_path / 'stability_report.txt').read_text()
        assert 'UNSTABLE' in report_text

    def test_report_header_shows_iterations(self, tmp_path):
        from otto.cli.test import _print_stability_report

        collector = StabilityCollector()
        collector.record('test_a', passed=True)

        with patch('otto.cli.test.logger'):
            _print_stability_report('MySuite', collector, 5, 0, 100.0, tmp_path)

        report_text = (tmp_path / 'stability_report.txt').read_text()
        assert '5 iterations' in report_text

    def test_report_header_shows_duration(self, tmp_path):
        from otto.cli.test import _print_stability_report

        collector = StabilityCollector()
        collector.record('test_a', passed=True)

        with patch('otto.cli.test.logger'):
            _print_stability_report('MySuite', collector, 0, 300, 100.0, tmp_path)

        report_text = (tmp_path / 'stability_report.txt').read_text()
        assert '300s duration' in report_text

    def test_report_header_shows_both(self, tmp_path):
        from otto.cli.test import _print_stability_report

        collector = StabilityCollector()
        collector.record('test_a', passed=True)

        with patch('otto.cli.test.logger'):
            _print_stability_report('MySuite', collector, 50, 120, 100.0, tmp_path)

        report_text = (tmp_path / 'stability_report.txt').read_text()
        assert '50 iterations' in report_text
        assert '120s duration' in report_text
