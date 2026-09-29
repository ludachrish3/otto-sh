"""The ExpectCollector (the conformance engine) and the ``expect`` fixture built on it."""

import logging

import pytest

from otto.suite.expect import ExpectCollector, expect
from tests.unit.suite._inner import run_inner

pytest_plugins = ["pytester"]


class TestExpectCollector:
    def test_passing_expect_records_nothing(self):
        c = ExpectCollector()
        c.expect(True)
        c.expect(True, "should not record")
        assert c.failures == []

    def test_failing_expect_records_report(self):
        c = ExpectCollector()
        x = 42
        c.expect(x == 99, "math is broken")
        assert len(c.failures) == 1
        report = c.failures[0]
        assert "math is broken" in report
        assert "x = 42" in report  # locals captured

    def test_multiple_failures_accumulate_in_order(self):
        c = ExpectCollector()
        c.expect(False, "first")
        c.expect(False, "second")
        assert len(c.failures) == 2
        assert "first" in c.failures[0]
        assert "second" in c.failures[1]

    def test_reset_clears_failures(self):
        c = ExpectCollector()
        c.expect(False, "boom")
        assert c.failures
        c.reset()
        assert c.failures == []

    def test_raise_if_failures_raises_with_aggregate_report(self):
        c = ExpectCollector()
        c.expect(False, "alpha")
        c.expect(False, "beta")
        with pytest.raises(AssertionError) as exc:
            c.raise_if_failures()
        msg = str(exc.value)
        assert "2 expectation(s) failed" in msg
        assert "alpha" in msg
        assert "beta" in msg

    def test_raise_if_failures_no_raise_when_clean(self):
        c = ExpectCollector()
        c.expect(True)
        c.raise_if_failures()  # must not raise

    def test_logger_warns_on_failure(self, caplog):
        logger = logging.getLogger("otto.test.expect")
        c = ExpectCollector(logger=logger)
        with caplog.at_level(logging.WARNING, logger="otto.test.expect"):
            c.expect(False, "logged failure")
        assert any("logged failure" in r.message for r in caplog.records)

    def test_module_level_expect_uses_explicit_collector(self):
        c = ExpectCollector()
        expect(2 + 2 == 5, "via module fn", collector=c)
        assert len(c.failures) == 1
        assert "via module fn" in c.failures[0]


# ── the callable form: ``expect(cond, msg)`` (spec §5.4) ─────────────────────


def test_call_records_like_expect_and_points_at_the_caller() -> None:
    collector = ExpectCollector()
    answer = 41
    collector(answer == 42, "off by one")
    assert len(collector.failures) == 1
    report = collector.failures[0]
    assert "Message: off by one" in report
    assert "answer = 41" in report, report  # the CALLER's locals, not __call__'s
    assert 'collector(answer == 42, "off by one")' in report


def test_call_with_a_truthy_condition_records_nothing() -> None:
    collector = ExpectCollector()
    collector(True)
    assert collector.failures == []


# ── the ``expect`` fixture, through a real inner session (spec §5.4) ────────


class _PhaseRecorder:
    """Inner-session plugin: remembers (when, outcome) per test report."""

    def __init__(self) -> None:
        self.reports: list[tuple[str, str]] = []

    def pytest_runtest_logreport(self, report) -> None:
        self.reports.append((report.when, report.outcome))


def _run_recording(pytester, otto_plugins, **files):
    """``run_inner`` with a phase recorder registered alongside otto's plugins."""
    recorder = _PhaseRecorder()
    result = run_inner(pytester, [*otto_plugins, recorder], **files)
    return result, recorder.reports


def test_a_passing_expect_does_not_fail(pytester, otto_plugins) -> None:
    body = "async def test_ok(expect):\n    expect(True)\n    expect(1 == 1)\n    expect('x')\n"
    run_inner(pytester, otto_plugins, test_pass=body).assert_outcomes(passed=1)


def test_a_failing_expect_lets_the_body_continue(pytester, otto_plugins) -> None:
    body = """\
import pathlib

async def test_continues(expect):
    expect(False)
    pathlib.Path(__file__).with_name("reached").write_text("yes")
"""
    run_inner(pytester, otto_plugins, test_continue=body).assert_outcomes(failed=1)
    assert (pytester.path / "reached").read_text() == "yes"


def test_failures_fail_the_call_phase_not_teardown(pytester, otto_plugins) -> None:
    """``1 failed``, in the CALL phase. Red if the summary is raised from a
    fixture's teardown (a ``passed`` call plus a ``failed`` teardown)."""
    body = "async def test_soft(expect):\n    expect(False, 'soft')\n"
    result, reports = _run_recording(pytester, otto_plugins, test_phase=body)
    result.assert_outcomes(failed=1)
    assert ("call", "failed") in reports, reports
    assert ("teardown", "failed") not in reports, reports
    assert ("setup", "passed") in reports, reports


def test_a_hard_assert_in_the_body_wins(pytester, otto_plugins) -> None:
    """The body's own AssertionError is the failure; the soft one was already logged."""
    body = """\
async def test_hard(expect):
    expect(False, "soft failure recorded")
    raise AssertionError("hard failure wins")
"""
    result = run_inner(pytester, otto_plugins, test_hard=body)
    result.assert_outcomes(failed=1)
    out = result.stdout.str()
    assert "hard failure wins" in out
    assert "1 expectation(s) failed" not in out


def test_every_failure_is_recorded_with_its_source_and_message(pytester, otto_plugins) -> None:
    """Three soft failures among passes, each report naming its file, line, locals and msg."""
    body = """\
import json
import pathlib

async def test_mixed(expect):
    val = 42
    expect(True)
    expect(val == 99, "hostname missing from config")
    expect(True)
    expect(False, "two")
    expect(False, "three")
    pathlib.Path(__file__).with_name("failures.json").write_text(json.dumps(expect.failures))
"""
    run_inner(pytester, otto_plugins, test_mix=body).assert_outcomes(failed=1)
    import json

    failures = json.loads((pytester.path / "failures.json").read_text())
    assert len(failures) == 3
    first = failures[0]
    assert "test_mix.py" in first
    assert "expect(val == 99" in first
    assert "val = 42" in first
    assert "hostname missing from config" in first


def test_failures_reset_between_tests(pytester, otto_plugins) -> None:
    body = """\
import pathlib

async def test_first(expect):
    expect(False, "only mine")

async def test_second(expect):
    pathlib.Path(__file__).with_name("count").write_text(str(len(expect.failures)))
"""
    run_inner(pytester, otto_plugins, test_reset=body).assert_outcomes(passed=1, failed=1)
    assert (pytester.path / "count").read_text() == "0"


def test_a_method_of_a_plain_class_may_use_expect(pytester, otto_plugins) -> None:
    body = """\
class TestPlain:
    async def test_soft(self, expect):
        expect(False, "class soft failure")
"""
    result, reports = _run_recording(pytester, otto_plugins, test_plain_class=body)
    result.assert_outcomes(failed=1)
    assert ("call", "failed") in reports


def test_expect_composes_with_retry(pytester, otto_plugins) -> None:
    """The collector resets per attempt, so a retried body whose second attempt is
    clean PASSES. Red if the check lives in ``pytest_runtest_call`` (the retry
    never re-enters it) or the collector is not reset."""
    body = """\
import pathlib

import pytest

COUNTER = pathlib.Path(__file__).with_name("attempts")

@pytest.mark.retry(2)
async def test_flaky_soft(expect):
    attempts = int(COUNTER.read_text()) if COUNTER.exists() else 0
    COUNTER.write_text(str(attempts + 1))
    expect(attempts >= 1, "first attempt is soft-red")
"""
    result, reports = _run_recording(pytester, otto_plugins, test_retry_expect=body)
    assert (pytester.path / "attempts").read_text() == "2"
    result.assert_outcomes(passed=1)
    assert ("call", "passed") in reports
