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

from unittest.mock import MagicMock, patch

import pytest

from otto.suite.plugin import OttoPlugin, StabilityCollector

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_report(when: str = "call", passed: bool = True) -> MagicMock:
    """Create a mock pytest.TestReport."""
    report = MagicMock()
    report.when = when
    report.passed = passed
    report.failed = not passed
    return report


def _make_item() -> MagicMock:
    """Create a mock pytest.Item with the attributes the hook expects."""
    item = MagicMock()
    item.name = "test_x[param]"  # a parametrized name: the banner escapes its brackets
    item._request = True  # hasattr check passes, truthy so _initrequest not called
    item.config.getoption.return_value = False  # setupshow=False
    return item


def _call_and_report_side_effect(item, when, log=True, **kwds):
    """Return a passing report for the given phase."""
    return _make_report(when, passed=True)


def _call_and_report_setup_fails(item, when, log=True, **kwds):
    """Return a failing report for setup, passing for everything else."""
    if when == "setup":
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

        with patch(
            "otto.suite.plugin.call_and_report", side_effect=_call_and_report_side_effect
        ) as mock_car:
            result = plugin.pytest_runtest_protocol(item, nextitem=None)

        assert result is True
        # 1 setup + 3 calls + 1 teardown = 5 total
        assert mock_car.call_count == 5
        phases = [c.args[1] for c in mock_car.call_args_list]
        assert phases == ["setup", "call", "call", "call", "teardown"]

    def test_setupshow_invokes_show_test_item(self):
        """--setup-show routes through pytest's private ``show_test_item``.

        Since pytest 9.1 that helper requires the keyword-only ``add_space``
        argument. ``wraps`` calls the *real* helper, so this guards against
        both otto dropping the kwarg and a future pytest signature change
        (the path is otherwise never exercised by the suite).
        """
        from _pytest.runner import show_test_item as real_show_test_item

        plugin = OttoPlugin(iterations=1)
        item = _make_item()
        item.config.getoption.return_value = True  # setupshow=True

        with (
            patch("otto.suite.plugin.call_and_report", side_effect=_call_and_report_side_effect),
            patch("otto.suite.plugin.show_test_item", wraps=real_show_test_item) as mock_show,
        ):
            result = plugin.pytest_runtest_protocol(item, nextitem=None)

        assert result is True
        mock_show.assert_called_once_with(item, add_space=False)

    def test_setup_failure_skips_call_phase(self):
        """If setup fails, skip call iterations and go straight to teardown."""
        plugin = OttoPlugin(iterations=10)
        item = _make_item()

        with patch(
            "otto.suite.plugin.call_and_report", side_effect=_call_and_report_setup_fails
        ) as mock_car:
            result = plugin.pytest_runtest_protocol(item, nextitem=None)

        assert result is True
        # 1 setup (failed) + 1 teardown = 2 total, no call phase
        assert mock_car.call_count == 2
        phases = [c.args[1] for c in mock_car.call_args_list]
        assert phases == ["setup", "teardown"]

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

        with (
            patch(
                "otto.suite.plugin.call_and_report", side_effect=_call_and_report_side_effect
            ) as mock_car,
            patch("otto.suite.plugin.time") as mock_time,
        ):
            mock_time.monotonic = lambda: next(times)
            result = plugin.pytest_runtest_protocol(item, nextitem=None)

        assert result is True
        phases = [c.args[1] for c in mock_car.call_args_list]
        # 1 setup + 3 calls + 1 teardown
        assert phases == ["setup", "call", "call", "call", "teardown"]

    def test_combined_iterations_and_duration_iterations_first(self):
        """When both are set, stop at whichever limit hits first (iterations)."""
        plugin = OttoPlugin(iterations=2, duration=9999)
        item = _make_item()

        with patch(
            "otto.suite.plugin.call_and_report", side_effect=_call_and_report_side_effect
        ) as mock_car:
            result = plugin.pytest_runtest_protocol(item, nextitem=None)

        assert result is True
        phases = [c.args[1] for c in mock_car.call_args_list]
        assert phases == ["setup", "call", "call", "teardown"]

    def test_combined_iterations_and_duration_duration_first(self):
        """When both are set, stop at whichever limit hits first (duration)."""
        plugin = OttoPlugin(iterations=9999, duration=3)
        item = _make_item()

        # Time jumps past deadline after first call
        times = iter([100.0, 100.0, 104.0])

        with (
            patch(
                "otto.suite.plugin.call_and_report", side_effect=_call_and_report_side_effect
            ) as mock_car,
            patch("otto.suite.plugin.time") as mock_time,
        ):
            mock_time.monotonic = lambda: next(times)
            result = plugin.pytest_runtest_protocol(item, nextitem=None)

        assert result is True
        phases = [c.args[1] for c in mock_car.call_args_list]
        # 1 setup + 1 call (then time expired) + 1 teardown
        assert phases == ["setup", "call", "teardown"]

    def test_funcargs_cleaned_up(self):
        """After protocol completes, item._request and funcargs are cleared."""
        plugin = OttoPlugin(iterations=1)
        item = _make_item()

        with patch("otto.suite.plugin.call_and_report", side_effect=_call_and_report_side_effect):
            plugin.pytest_runtest_protocol(item, nextitem=None)

        assert item._request is False
        assert item.funcargs is None


# ── StabilityCollector ───────────────────────────────────────────────────────


class TestStabilityCollector:
    def test_records_pass_and_fail(self):
        collector = StabilityCollector()
        collector.record("test_a", passed=True)
        collector.record("test_a", passed=True)
        collector.record("test_a", passed=False)
        assert collector.results["test_a"] == (2, 3)

    def test_multiple_tests_tracked_independently(self):
        collector = StabilityCollector()
        collector.record("test_a", passed=True)
        collector.record("test_b", passed=False)
        assert collector.results["test_a"] == (1, 1)
        assert collector.results["test_b"] == (0, 1)


# ── _print_stability_report ─────────────────────────────────────────────────


class TestStabilityReport:
    def test_report_uses_percentage_threshold(self, tmp_path):
        from otto.suite.run import _print_stability_report

        collector = StabilityCollector()
        for _ in range(9):
            collector.record("test_a", passed=True)
        collector.record("test_a", passed=False)  # 90% pass rate

        # threshold=90 → exactly meets threshold → STABLE
        with patch("otto.suite.run.logger"):
            _print_stability_report("MySuite", collector, 10, 0, 90.0, tmp_path)

        report_text = (tmp_path / "stability_report.txt").read_text()
        assert "STABLE" in report_text
        assert "90%" in report_text
        assert "PASS" in report_text

    def test_report_fails_below_threshold(self, tmp_path):
        from otto.suite.run import _print_stability_report

        collector = StabilityCollector()
        for _ in range(8):
            collector.record("test_a", passed=True)
        for _ in range(2):
            collector.record("test_a", passed=False)  # 80% pass rate

        # Report-as-data: a sub-threshold run returns the unstable verdict
        # (True) rather than raising SystemExit; the caller folds it into the
        # invocation's exit code (see _final_exit_code).
        with patch("otto.suite.run.logger"):
            unstable = _print_stability_report("MySuite", collector, 10, 0, 90.0, tmp_path)

        assert unstable is True
        report_text = (tmp_path / "stability_report.txt").read_text()
        assert "UNSTABLE" in report_text

    def test_report_header_shows_iterations(self, tmp_path):
        from otto.suite.run import _print_stability_report

        collector = StabilityCollector()
        collector.record("test_a", passed=True)

        with patch("otto.suite.run.logger"):
            _print_stability_report("MySuite", collector, 5, 0, 100.0, tmp_path)

        report_text = (tmp_path / "stability_report.txt").read_text()
        assert "5 iterations" in report_text

    def test_report_header_shows_duration(self, tmp_path):
        from otto.suite.run import _print_stability_report

        collector = StabilityCollector()
        collector.record("test_a", passed=True)

        with patch("otto.suite.run.logger"):
            _print_stability_report("MySuite", collector, 0, 300, 100.0, tmp_path)

        report_text = (tmp_path / "stability_report.txt").read_text()
        assert "300s duration" in report_text

    def test_report_header_shows_both(self, tmp_path):
        from otto.suite.run import _print_stability_report

        collector = StabilityCollector()
        collector.record("test_a", passed=True)

        with patch("otto.suite.run.logger"):
            _print_stability_report("MySuite", collector, 50, 120, 100.0, tmp_path)

        report_text = (tmp_path / "stability_report.txt").read_text()
        assert "50 iterations" in report_text
        assert "120s duration" in report_text


def test_a_library_stability_run_with_redirected_junit_writes_its_report(tmp_path, monkeypatch):
    """With JUnit sent elsewhere and no output dir, the report still has a dir to land in.

    ``stability_report.txt`` is written straight into the run's directory, so a
    library run creates its default run dir (#758) even when nothing else of
    its output lands there.
    """
    import contextvars

    from otto import layout
    from otto.suite.run import RunOptions, run_tests
    from tests._fixtures.bootstrap_seam import patch_bootstrap
    from tests._fixtures.run_projects import DOUBLE_TEST_NAME, project_double, pytest_main_returning

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    patch_bootstrap(monkeypatch, [project_double(tmp_path / "proj")])
    monkeypatch.setattr("pytest.main", pytest_main_returning())
    opts = RunOptions(iterations=1, results=str(tmp_path / "elsewhere" / "junit.xml"))

    result = contextvars.Context().run(run_tests, names=[DOUBLE_TEST_NAME], run_options=opts)

    assert result.stability_report == result.output_dir / "stability_report.txt"
    assert result.stability_report.is_file()
    assert layout.is_run_dir(result.output_dir)


# ── a repeated run's exit code: the threshold decides (#1058) ───────────────

_ONE_ITERATION_IN_FOUR_FAILS = """
CALLS = []


def test_flaky():
    CALLS.append(1)
    assert len(CALLS) != 2
"""

_ONE_ITERATION_IN_FOUR_SKIPS = """
import pytest

CALLS = []


def test_skips_once():
    CALLS.append(1)
    if len(CALLS) == 2:
        pytest.skip("not this time")
"""

_SETUP_FIXTURE_RAISES = """
import pytest


@pytest.fixture
def broken():
    raise RuntimeError("setup is broken")


def test_needs_broken(broken):
    pass
"""

_TEARDOWN_FIXTURE_RAISES = """
import pytest


@pytest.fixture
def broken():
    yield
    raise RuntimeError("teardown is broken")


def test_needs_broken(broken):
    pass
"""

_ALWAYS_PASSES = "def test_steady():\n    pass\n"


def _repeated_run(one_project, tmp_path, source: str, *, threshold: float):
    """Run *source* for four iterations through ``run_tests``, which ``otto test`` calls."""
    import re

    from otto.suite import RunOptions, run_tests

    one_project(files={"tests/test_repeat.py": source})
    return run_tests(
        names=[re.search(r"def (test_\w+)", source)[1]],
        run_options=RunOptions(iterations=4, threshold=threshold, random_order=False),
        output_dir=tmp_path / "out",
    )


@pytest.mark.parametrize(
    ("source", "threshold", "exit_code", "unstable"),
    [
        # 3 of 4 iterations pass: 75%
        (_ONE_ITERATION_IN_FOUR_FAILS, 50.0, 0, False),
        (_ONE_ITERATION_IN_FOUR_FAILS, 95.0, 3, True),
        # the boundary: a rate equal to the threshold clears it
        (_ONE_ITERATION_IN_FOUR_FAILS, 75.0, 0, False),
        (_ONE_ITERATION_IN_FOUR_FAILS, 76.0, 3, True),
        # a skipped iteration is not a pass: green for pytest, below the threshold
        (_ONE_ITERATION_IN_FOUR_SKIPS, 100.0, 3, True),
        (_ALWAYS_PASSES, 100.0, 0, False),
        # not an iteration's pass/fail: still a failed run, whatever the threshold says
        (_SETUP_FIXTURE_RAISES, 0.0, 1, False),
        (_TEARDOWN_FIXTURE_RAISES, 0.0, 1, False),
        (_TEARDOWN_FIXTURE_RAISES, 100.0, 1, False),
    ],
    ids=[
        "flaky-above-threshold",
        "flaky-below-threshold",
        "flaky-at-threshold",
        "flaky-just-below-threshold",
        "skipped-iteration-below-threshold",
        "always-passes",
        "setup-error",
        "teardown-error-lenient",
        "teardown-error-strict",
    ],
)
def test_a_repeated_runs_exit_code_follows_its_threshold(
    one_project, tmp_path, source, threshold, exit_code, unstable
):
    """Failed iterations are judged by ``--threshold`` alone; any other failure exits 1."""
    result = _repeated_run(one_project, tmp_path, source, threshold=threshold)

    assert result.exit_code == exit_code
    assert result.stability_unstable is unstable


def test_a_repeated_run_cut_short_by_maxfail_is_not_judged_by_its_threshold(
    one_project, tmp_path, monkeypatch
):
    """`-x` stops at the first failed iteration: the run is incomplete, so pytest's code stands."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x")

    result = _repeated_run(one_project, tmp_path, _ONE_ITERATION_IN_FOUR_FAILS, threshold=50.0)

    assert result.exit_code == 1


def test_a_failing_test_in_a_run_that_is_not_repeated_still_exits_one(one_project, tmp_path):
    """No ``--iterations`` and no ``--duration``: the threshold has no say."""
    from otto.suite import RunOptions, run_tests

    one_project(files={"tests/test_plain.py": "def test_fails():\n    assert False\n"})
    result = run_tests(
        names=["test_fails"],
        run_options=RunOptions(threshold=0.0, random_order=False),
        output_dir=tmp_path / "out",
    )

    assert result.exit_code == 1
    assert result.stability_unstable is False


def test_a_repeated_run_says_why_failed_iterations_did_not_fail_it(one_project, tmp_path, caplog):
    """pytest's summary says "1 failed" and the run exits 0, so a line explains it."""
    with caplog.at_level("INFO"):
        result = _repeated_run(one_project, tmp_path, _ONE_ITERATION_IN_FOUR_FAILS, threshold=50.0)

    assert result.exit_code == 0
    assert "within --threshold 50%" in caplog.text


def test_a_failed_iteration_stays_a_failure_in_the_junit_file(one_project, tmp_path):
    """Only the exit code changes: the report and the JUnit file still show the failed iteration."""
    import xml.etree.ElementTree as ET

    result = _repeated_run(one_project, tmp_path, _ONE_ITERATION_IN_FOUR_FAILS, threshold=50.0)

    assert result.exit_code == 0
    suite = ET.parse(result.junit_paths[0]).getroot().find("testsuite")  # noqa: S314 — this run's own JUnit file
    assert suite is not None
    assert suite.get("failures") == "1"


@pytest.mark.parametrize(
    ("b_source", "b_name", "exit_code"),
    [
        (_ONE_ITERATION_IN_FOUR_FAILS.replace("test_flaky", "test_flaky_b"), "test_flaky_b", 0),
        (_SETUP_FIXTURE_RAISES, "test_needs_broken", 1),
    ],
    ids=["both-only-failed-iterations", "other-project-has-a-setup-error"],
)
def test_a_repeated_run_over_two_projects_is_decided_once(
    two_projects, tmp_path, b_source, b_name, exit_code
):
    """One project's failed iterations are not a failure, and another project's setup error is."""
    from otto.suite import RunOptions, run_tests

    two_projects(
        a={"tests/test_repeat_a.py": _ONE_ITERATION_IN_FOUR_FAILS},
        b={"tests/test_repeat_b.py": b_source},
    )
    result = run_tests(
        names=["test_flaky", b_name],
        run_options=RunOptions(iterations=4, threshold=50.0, random_order=False),
        output_dir=tmp_path / "out",
    )

    assert result.exit_code == exit_code
