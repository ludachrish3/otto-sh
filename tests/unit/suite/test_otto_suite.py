"""
Unit tests for ``otto.suite.suite`` and the fixtures otto hands every suite.

Tests verify:
  - ``_sanitize_node_name`` replaces filesystem-unsafe characters
  - ``suite_dir`` / ``test_dir`` land at ``<run>/<Suite>/<node>``, sanitized,
    and exist only when requested
  - ``suite_options`` fixture injection works in test method parameters
  - nothing otto-specific lives on the suite instance; the xunit hooks are gone
  - ``expect`` records non-fatal failures, fails the CALL phase, composes with retry
  - ``start_monitor(db_path=...)``/``stop_monitor()`` persist a real (not
    degraded) session archive
"""

import asyncio
import contextlib
import json
import socket
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.models import MonitorExport
from otto.models.monitor import MonitorSessionFragment
from otto.monitor.collector import MetricCollector
from otto.monitor.db import MetricDB, read_sessions
from otto.monitor.export import build_db_export
from otto.suite.plugin import OttoPlugin
from otto.suite.pytest_plugin import OttoOptionsPlugin
from otto.suite.run import ASYNCIO_LOOP_ARGS
from otto.suite.suite import OttoSuite, _sanitize_node_name

# ── _sanitize_node_name ──────────────────────────────────────────────────────


class TestSanitizeNodeName:
    def test_brackets_replaced(self):
        assert _sanitize_node_name("test_foo[router-True]") == "test_foo_router-True_"

    def test_slashes_replaced(self):
        assert _sanitize_node_name("test/foo") == "test_foo"

    def test_multiple_unsafe_chars(self):
        assert _sanitize_node_name('a[b]<c>d:e"f|g?h*i\\j/k') == "a_b__c_d_e_f_g_h_i_j_k"

    def test_plain_name_unchanged(self):
        assert _sanitize_node_name("test_simple_name") == "test_simple_name"

    def test_empty_string(self):
        assert _sanitize_node_name("") == ""

    def test_hyphens_and_underscores_preserved(self):
        assert _sanitize_node_name("test_foo-bar_baz") == "test_foo-bar_baz"


# ── Inner pytest session helpers ─────────────────────────────────────────────


def _run_inner_pytest(
    test_file: Path,
    tmp_path: Path,
    options: object | None = None,
    extra_plugins: tuple[object, ...] = (),
) -> int:
    """Run an inner pytest session with OttoPlugin + OttoOptionsPlugin.

    *extra_plugins* are registered alongside otto's own, so a caller that needs
    to observe the inner session (e.g. ``_run_inner_recording``'s phase
    recorder) gets the SAME session shape every other test here measures rather
    than a second, drifting copy of the argument list.

    The inner session runs in-process via ``pytest.main()``, so it shares the
    interpreter (and ``sys.modules``) with the outer run. The callers all
    generate their test files under fixed basenames (``test_pass.py``,
    ``test_reset.py``, ...) imported as top-level modules keyed by stem. Once
    the same outer test runs more than once in a process -- e.g. under
    ``pytest --count`` on a shared xdist worker -- the second run hits the
    cached module: pytest either raises "import file mismatch" or, worse,
    silently reuses it so the stale module-level ``CAPTURE`` constant still
    points at the first run's tmp dir.

    Evicting the module (and any cached bytecode) after the session keeps the
    filename intact -- some tests assert on it -- while ensuring the next
    invocation imports a genuinely fresh module.

    The inner session leaks pytest-asyncio event loops, but those no longer
    need closing here: the root-conftest loop reaper (see
    ``tests/_loop_reaper.py``) closes any orphaned harness loop at the outer
    test's teardown boundary.

    ``-p no:playwright`` disables pytest-playwright for the inner session.
    That plugin installs a session-wide ``pytest_runtest_call`` wrapper
    (used for its soft-assertion ``expect()``) that runs for *every* test,
    not just ones using its fixtures. Since this inner session shares the
    interpreter with the outer one, the outer test's own call is already
    wrapped by that same hook; entering it a second time here raises
    "nested soft assertion scopes are not supported". None of the inner
    fixture files need Playwright, so disabling it here is a no-op for
    behavior and just avoids the collision.
    """
    ctx = OttoContext(lab=Lab(name="_test_stub"), output_dir=tmp_path)
    token = set_context(ctx)
    try:
        exit_code = pytest.main(
            [
                str(test_file),
                "-o",
                "asyncio_mode=auto",
                *ASYNCIO_LOOP_ARGS,
                "--no-cov",
                "--override-ini",
                "addopts=",
                "-p",
                "no:playwright",
                "-x",
            ],
            plugins=[OttoPlugin(), OttoOptionsPlugin(options), *extra_plugins],
        )
    finally:
        sys.modules.pop(test_file.stem, None)
        reset_context(token)
    return exit_code


# ── Autouse fixtures ─────────────────────────────────────────────────────────


class TestArtifactDirs:
    def test_test_dir_is_per_test_under_the_suite_dir(self, tmp_path: Path) -> None:
        """Spec §5.3: ``<run>/<Suite>/<node>``; each test its own; both exist when requested.

        Both inner tests APPEND and the assertions are set-shaped: the inner
        run inherits pytest-randomly, so alpha-then-beta is a coin flip.
        """
        capture_file = tmp_path / "dirs.txt"
        test_file = tmp_path / "test_dirs.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestDirs(OttoSuite):
    async def test_alpha(self, suite_dir, test_dir) -> None:
        with CAPTURE.open("a") as f:
            f.write(f"{{suite_dir}} {{test_dir}} {{test_dir.is_dir()}}\\n")

    async def test_beta(self, suite_dir, test_dir) -> None:
        with CAPTURE.open("a") as f:
            f.write(f"{{suite_dir}} {{test_dir}} {{test_dir.is_dir()}}\\n")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        rows = [line.split() for line in capture_file.read_text().strip().split("\n") if line]
        assert len(rows) == 2, rows
        suite_dirs = {row[0] for row in rows}
        assert suite_dirs == {str(tmp_path / "TestDirs")}, suite_dirs
        test_dirs = {row[1] for row in rows}
        assert test_dirs == {
            str(tmp_path / "TestDirs" / "test_alpha"),
            str(tmp_path / "TestDirs" / "test_beta"),
        }
        assert all(row[2] == "True" for row in rows), rows

    def test_parametrized_names_sanitized(self, tmp_path: Path) -> None:
        capture_file = tmp_path / "param_dirs.txt"
        test_file = tmp_path / "test_param_dirs.py"
        test_file.write_text(f"""\
import pathlib
import pytest
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestParamDirs(OttoSuite):
    @pytest.mark.parametrize("val", ["a", "b"])
    async def test_param(self, val: str, test_dir) -> None:
        with CAPTURE.open("a") as f:
            f.write(str(test_dir) + "\\n")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        lines = sorted(line for line in capture_file.read_text().strip().split("\n") if line)
        assert lines == [
            str(tmp_path / "TestParamDirs" / "test_param_a_"),
            str(tmp_path / "TestParamDirs" / "test_param_b_"),
        ]

    def test_dirs_exist_only_when_requested(self, tmp_path: Path) -> None:
        """Like ``tmp_path``: a test that never names ``test_dir`` leaves no directory.
        Red if the plugin creates directories eagerly (an autouse mkdir)."""
        test_file = tmp_path / "test_silent.py"
        test_file.write_text("""\
from otto.suite.suite import OttoSuite

class TestSilent(OttoSuite):
    async def test_quiet(self) -> None:
        assert True
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        assert not (tmp_path / "TestSilent").exists()

    def test_plain_function_gets_the_module_stem_as_its_suite_dir(self, tmp_path: Path) -> None:
        capture_file = tmp_path / "plain.txt"
        test_file = tmp_path / "test_plain_dirs.py"
        test_file.write_text(f"""\
import pathlib

CAPTURE = pathlib.Path({str(capture_file)!r})

async def test_plain(suite_dir, test_dir) -> None:
    CAPTURE.write_text(f"{{suite_dir}} {{test_dir}}")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        suite_dir, test_dir = capture_file.read_text().split()
        assert suite_dir == str(tmp_path / "test_plain_dirs")
        assert test_dir == str(tmp_path / "test_plain_dirs" / "test_plain")


# ── suite_options fixture ────────────────────────────────────────────────────


class TestSuiteOptionsFixture:
    def test_suite_options_injected_via_fixture(self, tmp_path: Path) -> None:
        """Tests can request suite_options as a fixture parameter."""

        @dataclass
        class Opts:
            device_type: str = "router"

        opts = Opts(device_type="switch")
        capture_file = tmp_path / "opts.txt"
        test_file = tmp_path / "test_opts.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

class TestOpts(OttoSuite):
    async def test_get_options(self, suite_options) -> None:
        pathlib.Path({str(capture_file)!r}).write_text(suite_options.device_type)
""")
        exit_code = _run_inner_pytest(test_file, tmp_path, options=opts)
        assert exit_code == pytest.ExitCode.OK
        assert capture_file.read_text() == "switch"

    def test_suite_options_none_when_no_plugin_options(self, tmp_path: Path) -> None:
        """suite_options is None when OttoOptionsPlugin has no options."""
        capture_file = tmp_path / "none_opts.txt"
        test_file = tmp_path / "test_none_opts.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

class TestNoneOpts(OttoSuite):
    async def test_none_options(self, suite_options) -> None:
        pathlib.Path({str(capture_file)!r}).write_text(str(suite_options))
""")
        exit_code = _run_inner_pytest(test_file, tmp_path, options=None)
        assert exit_code == pytest.ExitCode.OK
        assert capture_file.read_text() == "None"


# ── nothing otto-specific on the instance ────────────────────────────────────


class TestNothingOttoSpecificOnTheInstance:
    """Spec §5.6/§10: the xunit hooks and the attributes they set are gone — loudly."""

    def test_hooks_are_not_defined_on_the_base(self) -> None:
        for name in (
            "setup_method",
            "teardown_method",
            "setup_class",
            "teardown_class",
            "expect",
        ):
            assert name not in OttoSuite.__dict__, name

    def test_a_live_instance_carries_no_otto_attributes(self, tmp_path: Path) -> None:
        capture_file = tmp_path / "attrs.txt"
        test_file = tmp_path / "test_attrs.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestAttrs(OttoSuite):
    async def test_probe(self) -> None:
        gone = ("testDir", "suiteDir", "logger", "expect", "_expect_failures")
        present = [n for n in gone if hasattr(self, n)]
        CAPTURE.write_text(",".join(present))
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        assert capture_file.read_text() == ""

    def test_monitor_slots_default_to_none_on_the_class(self) -> None:
        assert OttoSuite._monitor_collector is None
        assert OttoSuite._monitor_server is None
        assert OttoSuite._monitor_task is None
        assert OttoSuite._monitor_db is None


# ── Parametrize ──────────────────────────────────────────────────────────────


class TestParametrize:
    def test_parametrize_runs_all_variants(self, tmp_path: Path) -> None:
        """@pytest.mark.parametrize produces one test per parameter value."""
        capture_file = tmp_path / "params.txt"
        test_file = tmp_path / "test_parametrize.py"
        test_file.write_text(f"""\
import pathlib
import pytest
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestParams(OttoSuite):
    @pytest.mark.parametrize("val", ["alpha", "beta", "gamma"])
    async def test_values(self, val: str) -> None:
        with CAPTURE.open("a") as f:
            f.write(val + "\\n")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        lines = sorted(line for line in capture_file.read_text().strip().split("\n") if line)
        assert lines == ["alpha", "beta", "gamma"]

    def test_parametrize_with_options(self, tmp_path: Path) -> None:
        """Parametrized tests can also receive suite_options fixture."""

        @dataclass
        class Opts:
            prefix: str = "hello"

        opts = Opts(prefix="world")
        capture_file = tmp_path / "param_opts.txt"
        test_file = tmp_path / "test_param_opts.py"
        test_file.write_text(f"""\
import pathlib
import pytest
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestParamOpts(OttoSuite):
    @pytest.mark.parametrize("suffix", ["1", "2"])
    async def test_combined(self, suite_options, suffix: str) -> None:
        with CAPTURE.open("a") as f:
            f.write(f"{{suite_options.prefix}}-{{suffix}}\\n")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path, options=opts)
        assert exit_code == pytest.ExitCode.OK
        lines = sorted(line for line in capture_file.read_text().strip().split("\n") if line)
        assert lines == ["world-1", "world-2"]


# ── expect() non-fatal assertions ───────────────────────────────────────────


class _PhaseRecorder:
    """Inner-session plugin: remembers (when, outcome) per test report."""

    def __init__(self) -> None:
        self.reports: list[tuple[str, str]] = []

    def pytest_runtest_logreport(self, report) -> None:
        self.reports.append((report.when, report.outcome))


def _run_inner_recording(test_file: Path, tmp_path: Path) -> tuple[int, list[tuple[str, str]]]:
    """``_run_inner_pytest`` plus a phase recorder registered alongside otto's plugins."""
    recorder = _PhaseRecorder()
    exit_code = _run_inner_pytest(test_file, tmp_path, extra_plugins=(recorder,))
    return exit_code, recorder.reports


class TestExpect:
    def test_passing_expect_does_not_fail(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test_pass.py"
        test_file.write_text("""\
from otto.suite.suite import OttoSuite

class TestPass(OttoSuite):
    async def test_ok(self, expect) -> None:
        expect(True)
        expect(1 == 1)
        expect("hello")
""")
        assert _run_inner_pytest(test_file, tmp_path) == pytest.ExitCode.OK

    def test_failing_expect_continues_execution(self, tmp_path: Path) -> None:
        capture_file = tmp_path / "continued.txt"
        test_file = tmp_path / "test_continue.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestContinue(OttoSuite):
    async def test_continues(self, expect) -> None:
        expect(False)
        CAPTURE.write_text("reached")
""")
        assert _run_inner_pytest(test_file, tmp_path) == pytest.ExitCode.TESTS_FAILED
        assert capture_file.read_text() == "reached"

    def test_failures_fail_the_call_phase_not_teardown(self, tmp_path: Path) -> None:
        """Spec §5.4: `1 failed`, in the CALL phase. Red if the summary is raised from a
        fixture's teardown (today's shape: `passed` call + `failed` teardown)."""
        test_file = tmp_path / "test_phase.py"
        test_file.write_text("""\
from otto.suite.suite import OttoSuite

class TestPhase(OttoSuite):
    async def test_soft(self, expect) -> None:
        expect(False, "soft")
""")
        exit_code, reports = _run_inner_recording(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        assert ("call", "failed") in reports, reports
        assert ("teardown", "failed") not in reports, reports
        assert ("setup", "passed") in reports, reports

    def test_a_hard_assert_in_the_body_wins(self, tmp_path: Path, capsys) -> None:
        """The body's own AssertionError is the failure; the soft one was already logged."""
        test_file = tmp_path / "test_hard.py"
        test_file.write_text("""\
from otto.suite.suite import OttoSuite

class TestHard(OttoSuite):
    async def test_hard(self, expect) -> None:
        expect(False, "soft failure recorded")
        raise AssertionError("hard failure wins")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        out = capsys.readouterr().out
        assert "hard failure wins" in out
        assert "1 expectation(s) failed" not in out

    def test_multiple_failures_all_reported(self, tmp_path: Path) -> None:
        capture_file = tmp_path / "count.txt"
        test_file = tmp_path / "test_multi.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestMulti(OttoSuite):
    async def test_three_failures(self, expect) -> None:
        expect(False, "first")
        expect(False, "second")
        expect(False, "third")
        CAPTURE.write_text(str(len(expect.failures)))
""")
        assert _run_inner_pytest(test_file, tmp_path) == pytest.ExitCode.TESTS_FAILED
        assert capture_file.read_text() == "3"

    def test_failure_includes_source_line(self, tmp_path: Path) -> None:
        capture_file = tmp_path / "report.txt"
        test_file = tmp_path / "test_source.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestSource(OttoSuite):
    async def test_source_info(self, expect) -> None:
        x = 42
        expect(x == 99)
        CAPTURE.write_text(expect.failures[0])
""")
        assert _run_inner_pytest(test_file, tmp_path) == pytest.ExitCode.TESTS_FAILED
        report = capture_file.read_text()
        assert "test_source.py" in report
        assert "expect(x == 99)" in report
        assert "x = 42" in report

    def test_custom_msg_alongside_source(self, tmp_path: Path) -> None:
        capture_file = tmp_path / "msg.txt"
        test_file = tmp_path / "test_msg.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestMsg(OttoSuite):
    async def test_custom_msg(self, expect) -> None:
        val = 42
        expect(val == 99, "hostname missing from config")
        CAPTURE.write_text(expect.failures[0])
""")
        assert _run_inner_pytest(test_file, tmp_path) == pytest.ExitCode.TESTS_FAILED
        report = capture_file.read_text()
        assert "hostname missing from config" in report
        assert "expect(val == 99" in report
        assert "val = 42" in report

    def test_mix_of_pass_and_fail(self, tmp_path: Path) -> None:
        capture_file = tmp_path / "mix.txt"
        test_file = tmp_path / "test_mix.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestMix(OttoSuite):
    async def test_mixed(self, expect) -> None:
        expect(True)
        expect(False, "one")
        expect(True)
        expect(False, "two")
        expect(True)
        CAPTURE.write_text(str(len(expect.failures)))
""")
        assert _run_inner_pytest(test_file, tmp_path) == pytest.ExitCode.TESTS_FAILED
        assert capture_file.read_text() == "2"

    def test_failures_reset_between_tests(self, tmp_path: Path) -> None:
        capture_file = tmp_path / "reset.txt"
        test_file = tmp_path / "test_reset.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestReset(OttoSuite):
    async def test_first(self, expect) -> None:
        expect(True)

    async def test_second(self, expect) -> None:
        with CAPTURE.open("w") as f:
            f.write(str(len(expect.failures)))
""")
        assert _run_inner_pytest(test_file, tmp_path) == pytest.ExitCode.OK
        assert capture_file.read_text() == "0"

    def test_a_plain_function_may_use_expect(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test_plain_expect.py"
        test_file.write_text("""\
async def test_plain(expect) -> None:
    expect(False, "plain function soft failure")
""")
        exit_code, reports = _run_inner_recording(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        assert ("call", "failed") in reports

    def test_expect_composes_with_retry(self, tmp_path: Path) -> None:
        """Spec §5.4 / ruling R2: the collector resets per attempt, so a retried body
        whose second attempt is clean PASSES. Red if the check lives in
        pytest_runtest_call (the retry never re-enters it) or the collector is not reset."""
        counter = tmp_path / "attempts.txt"
        test_file = tmp_path / "test_retry_expect.py"
        test_file.write_text(f"""\
import pathlib
import pytest
from otto.suite.suite import OttoSuite

COUNTER = pathlib.Path({str(counter)!r})

class TestRetryExpect(OttoSuite):
    @pytest.mark.retry(2)
    async def test_flaky_soft(self, expect) -> None:
        attempts = int(COUNTER.read_text()) if COUNTER.exists() else 0
        COUNTER.write_text(str(attempts + 1))
        expect(attempts >= 1, "first attempt is soft-red")
""")
        exit_code, reports = _run_inner_recording(test_file, tmp_path)
        assert counter.read_text() == "2"
        assert exit_code == pytest.ExitCode.OK, reports
        assert ("call", "passed") in reports


# ── _active_monitor_collector accessor ─────────────────────────────────────────


def _make_suite(tmp_path: Path):
    """A bare ``OttoSuite`` subclass instance — no hooks exist to call any more; the
    monitor slots are class-level ``None`` defaults."""
    from otto.suite.suite import OttoSuite

    class _Suite(OttoSuite):
        pass

    ctx = OttoContext(lab=Lab(name="_test_stub"), output_dir=tmp_path)
    token = set_context(ctx)
    try:
        return _Suite()
    finally:
        reset_context(token)


class TestActiveMonitorCollector:
    """Per-suite ``_monitor_collector`` takes precedence; falls back to the
    class-level session collector set by ``OttoPlugin._otto_session_monitor``."""

    def test_returns_none_when_no_monitor_active(self, tmp_path: Path):
        s = _make_suite(tmp_path)
        assert s._active_monitor_collector() is None

    def test_per_suite_collector_takes_precedence(self, tmp_path: Path):
        from otto.suite.suite import OttoSuite

        per_suite = MagicMock(name="per_suite")
        session = MagicMock(name="session")
        try:
            OttoSuite._session_monitor_collector = session
            s = _make_suite(tmp_path)
            s._monitor_collector = per_suite
            assert s._active_monitor_collector() is per_suite
        finally:
            OttoSuite._session_monitor_collector = None

    def test_falls_back_to_session_collector(self, tmp_path: Path):
        from otto.suite.suite import OttoSuite

        session = MagicMock(name="session")
        try:
            OttoSuite._session_monitor_collector = session
            s = _make_suite(tmp_path)
            assert s._active_monitor_collector() is session
        finally:
            OttoSuite._session_monitor_collector = None


# ── add_monitor_event: validates through the shared event seam (Plan 5c Task 5b) ──


class TestAddMonitorEvent:
    """Library marks (``suite.add_monitor_event``) obey the same validation as
    web marks — ``EventCreateBody`` is the one seam (Chris's dedup directive).

    Validation must fire synchronously, at the call site, before the
    collector is ever touched: ``add_monitor_event`` is normally awaited
    (``await self.add_monitor_event(...)``, see ``docs/guide/cli/monitor/dashboard.md``),
    but a bad label/color/dash must raise even for a caller that never gets
    that far — calling it and discarding the result without awaiting is
    exactly what a fire-and-forget mistake looks like, and it must still be
    loud.
    """

    def test_add_monitor_event_rejects_invalid_dash_loud(self, tmp_path: Path) -> None:
        """Library marks obey the same validation as web marks (one seam)."""
        s = _make_suite(tmp_path)
        collector = MagicMock(name="collector")
        s._monitor_collector = collector
        with pytest.raises(ValueError, match="dash"):
            s.add_monitor_event("checkpoint", dash="wavy")
        collector.add_event.assert_not_called()

    def test_add_monitor_event_rejects_non_hex_color(self, tmp_path: Path) -> None:
        s = _make_suite(tmp_path)
        collector = MagicMock(name="collector")
        s._monitor_collector = collector
        with pytest.raises(ValueError, match="color"):
            s.add_monitor_event("checkpoint", color="red")
        collector.add_event.assert_not_called()

    def test_add_monitor_event_rejects_blank_label(self, tmp_path: Path) -> None:
        s = _make_suite(tmp_path)
        collector = MagicMock(name="collector")
        s._monitor_collector = collector
        with pytest.raises(ValueError, match="label"):
            s.add_monitor_event("   ")
        collector.add_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_monitor_event_valid_call_still_records(self, tmp_path: Path) -> None:
        """Behaviorally unchanged for valid input: the collector still records it."""
        s = _make_suite(tmp_path)
        collector = MagicMock(name="collector")
        collector.add_event = AsyncMock(return_value=None)
        s._monitor_collector = collector
        await s.add_monitor_event("checkpoint", color="#112233", dash="dot")
        collector.add_event.assert_awaited_once_with(
            label="checkpoint", color="#112233", dash="dot", source="user_code"
        )

    @pytest.mark.asyncio
    async def test_suite_monitor_event_reaches_stream_subscribers(self, tmp_path: Path) -> None:
        """A suite-emitted event must arrive as a format:1 fragment in real time
        -- the acceptance criterion behind "events appear while otto test
        --monitor runs" (spec 2026-07-18 Real-time suite events).

        Unlike the MagicMock arrangement above (which only proves
        ``add_event`` was *called* the right way), this uses a REAL
        ``MetricCollector`` so the assertion runs through its actual
        ``subscribe()``/``_publish()`` plumbing -- the same path a real
        dashboard SSE client reads from.
        """
        s = _make_suite(tmp_path)
        collector = MetricCollector(hosts=[])
        collector.session_id = "s-suite-live"
        s._monitor_collector = collector

        q = collector.subscribe()
        await s.add_monitor_event("checkpoint", color="#112233", dash="dot")
        payload = q.get_nowait()
        frag = MonitorSessionFragment.model_validate(payload)
        assert frag.events
        assert frag.events[0].label == "checkpoint"
        assert frag.session == collector.session_id


# ── start_monitor(db_path=...) / stop_monitor(): real archive shape ─────────
#
# Spec 2026-07-12: a --db-backed session must never persist the degraded
# lab_json="{}"/meta_json="{}" scaffold — that renders with no chart specs,
# no units, and no lab topology on replay. Mirrors otto.suite.plugin's own
# db-output test (test_session_monitor_db_output_persists_real_lab_and_meta):
# asserts on the round-tripped artifact via build_db_export, not on the
# MetricDB constructor args.


def _make_unconnected_host(host_id: str = "router1") -> UnixHost:
    """A real UnixHost that makes no connection at construction.

    ``snapshot_lab`` (called unconditionally by ``start_monitor``, spec
    2026-07-12) validates its result against pydantic's ``HostSnapshot`` — a
    bare ``MagicMock(spec=UnixHost)`` fails that validation because its unset
    attributes are auto-vivified ``Mock`` objects, not strings (see
    ``tests/unit/suite/test_plugin.py``'s identical helper).
    """
    return UnixHost(ip="10.0.0.1", element=host_id, creds=[Cred(login="admin", password="secret")])


async def _fake_collector_run(collector: MetricCollector, interval, duration=None) -> None:
    """Stand in for ``MetricCollector.run``: open the real DB, then idle.

    Exercises the exact same DB-opening call site (``init_db()``) a real run
    does — so the session row really gets INSERTed/UPDATEd — without
    attempting any host I/O: the host in these tests is a real, unconnected
    UnixHost pointed at a bogus IP, and a genuine collection tick would try
    to SSH to it. Blocks until ``stop_monitor()`` cancels the task that owns
    this coroutine (see ``start_monitor``'s ``_run()`` wrapper).
    """
    await collector.init_db()
    await asyncio.Event().wait()


class TestStartMonitorArchive:
    @pytest.mark.asyncio
    async def test_db_output_persists_real_lab_meta_and_end_stamp(
        self, tmp_path: Path, hermetic_monitor_dist: Path
    ) -> None:
        # start_monitor() launches the real dashboard server, which refuses to
        # start without a built React dist — hence the hermetic one. pytest does
        # not run `make web`, so without this the test passes on any developer
        # checkout (which has a dist) and fails in CI (which does not).
        del hermetic_monitor_dist
        out_path = tmp_path / "monitor.db"
        suite = _make_suite(tmp_path)

        with patch.object(MetricCollector, "run", _fake_collector_run):
            await suite.start_monitor(
                hosts=[_make_unconnected_host("router1")],
                db_path=str(out_path),
                interval=1.0,
            )
            await suite.stop_monitor()

        (session,) = build_db_export(str(out_path)).sessions
        # lab: the real snapshot, not "{}"
        assert [h.id for h in session.lab.hosts] == ["router1"]
        # meta: the real parser catalog, not "{}" — chart specs carry the
        # units and grouping the review shell renders from.
        assert session.meta.charts, "session meta persisted with no chart specs"
        assert session.meta.interval == 1.0
        # end: a clean stop_monitor() must stamp the RAW column, not rely on
        # the producer's crash-tolerant fallback to paper over a null one —
        # build_db_export()'s SessionRecord.end is NEVER None (_fallback_end
        # always synthesizes one: row.end, else the last sample, else start),
        # so it can't tell a finalized session from a crashed one. Only the
        # archive's own column can (mirrors test_plugin.py's identical check).
        (raw_session,) = read_sessions(str(out_path))
        assert raw_session.end is not None, "a clean stop_monitor() left end unstamped"

    @pytest.mark.asyncio
    async def test_start_monitor_returns_with_session_archive_committed(
        self, tmp_path: Path, hermetic_monitor_dist: Path
    ) -> None:
        """Regression: nightly/CI flake (issues #136/#137/#142/#143/#144).

        ``MetricDB.open()`` used to run only inside the collector task spawned
        by ``start_monitor``'s ``_run()``, racing uvicorn startup — the only
        thing ``start_monitor()`` awaits. A prompt ``stop_monitor()`` then
        cancelled ``open()`` at whichever await point it had reached, leaving
        the archive in one of three partial states (no tables / user_version
        0 / no session row) while ``finalize()`` silently no-oped on the
        never-opened connection. The slowed ``open()`` here turns that
        CI-load coin flip into a certainty: pre-fix, the collector task is
        still sleeping when ``start_monitor()`` returns, so the read below
        finds no committed session. The invariant pinned: when
        ``start_monitor(db_path=...)`` returns, the session row is already
        committed — before any cancellable task can be torn down.
        """
        del hermetic_monitor_dist
        out_path = tmp_path / "monitor.db"
        suite = _make_suite(tmp_path)

        real_open = MetricDB.open

        async def slow_open(db: MetricDB) -> None:
            await asyncio.sleep(0.5)
            await real_open(db)

        with (
            patch.object(MetricCollector, "run", _fake_collector_run),
            patch.object(MetricDB, "open", slow_open),
        ):
            await suite.start_monitor(
                hosts=[_make_unconnected_host("router1")],
                db_path=str(out_path),
                interval=1.0,
            )
            try:
                (session,) = build_db_export(str(out_path)).sessions
                assert [h.id for h in session.lab.hosts] == ["router1"]
            finally:
                await suite.stop_monitor()

        (raw_session,) = read_sessions(str(out_path))
        assert raw_session.end is not None, "a clean stop_monitor() left end unstamped"


class TestStartMonitorStartupFailure:
    @pytest.mark.asyncio
    async def test_failure_reaps_monitor_task_and_reraises(
        self, tmp_path: Path, hermetic_monitor_dist: Path
    ) -> None:
        """A startup failure must not leave the dead serve task parked.

        ``start_monitor()`` inherits the server's startup failure through
        ``wait_started()`` (gate G7). On that path it must also reap the
        ``_monitor_task`` it just spawned: left behind, a later
        ``stop_monitor()`` would await the same dead task and surface the
        identical failure a second time, and an unawaited dead task fires
        "exception was never retrieved" at GC.
        """
        del hermetic_monitor_dist
        suite = _make_suite(tmp_path)

        # Hold a bound, listening socket so uvicorn's bind fails with the
        # SystemExit the server translates to RuntimeError.
        blocker = socket.socket()
        try:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]
            with patch.object(MetricCollector, "run", _fake_collector_run):
                with pytest.raises(RuntimeError, match="already in use"):
                    await suite.start_monitor(
                        hosts=[_make_unconnected_host("router1")],
                        interval=1.0,
                        port=port,
                    )
                assert suite._monitor_task is None, (
                    "start_monitor() re-raised but left the dead serve task in _monitor_task"
                )
                # A cleanup-path stop_monitor() after the failure must be a
                # quiet no-op for the task, not a second raise.
                await suite.stop_monitor()
        finally:
            blocker.close()


class TestStartMonitorLiveSessionWiring:
    @pytest.mark.asyncio
    async def test_no_db_path_still_stamps_session_id_and_serves_monitor_sessions(
        self, tmp_path: Path, hermetic_monitor_dist: Path
    ) -> None:
        """Pins an escaped defect found building Task 2: ``start_monitor()``
        used to build ``frame``/``lab`` only inside its ``if db_path is not
        None:`` branch, so the in-memory-only (``db_path=None``) path — the
        one every suite/pytest-plugin caller actually uses — passed neither
        to ``MonitorServer``. Two silent consequences: (a) ``collector.
        session_id`` stayed ``""``, so every SSE fragment published on this
        path is addressed to a session the browser never holds and is
        dropped; (b) ``/api/monitor_sessions`` in live mode requires
        ``frame``/``lab`` and 500s (``RuntimeError``) without them. This
        boots a real MonitorServer (hence ``hermetic_monitor_dist`` — pytest
        never runs `make web`) and hits the live endpoint over a real socket
        rather than only inspecting private attributes, so a regression that
        broke serving (not just the id stamp) would fail it too.
        """
        del hermetic_monitor_dist
        suite = _make_suite(tmp_path)

        with patch.object(MetricCollector, "run", _fake_collector_run):
            await suite.start_monitor(
                hosts=[_make_unconnected_host("router1")],
                interval=1.0,
            )
            try:
                assert suite._monitor_collector is not None
                assert suite._monitor_collector.session_id != "", (
                    "collector.session_id was never stamped — MonitorServer "
                    "was built without frame= on the db_path=None path"
                )

                resp = await asyncio.to_thread(
                    urllib.request.urlopen,
                    f"{suite._monitor_server.origin}/api/monitor_sessions?key={suite._monitor_server.key}",
                    timeout=10,
                )
                with contextlib.closing(resp) as opened:
                    payload = json.loads(opened.read())
                export = MonitorExport.model_validate(payload)
                assert export.format == 1
                (session,) = export.sessions
                assert session.id == suite._monitor_collector.session_id
                assert session.end is None, "a live session is one whose end is still open"
            finally:
                await suite.stop_monitor()


# ── ctx at session scope; logger capture (spec §5.2, §5.5) ────────────────────


def test_a_class_scoped_fixture_may_request_ctx(tmp_path: Path) -> None:
    """Spec §5.2: ``ctx`` is session-scoped so suite-wide fixtures can take it.
    Red at function scope (ScopeMismatch at setup)."""
    capture_file = tmp_path / "ctx.txt"
    test_file = tmp_path / "test_ctx_scope.py"
    test_file.write_text(f"""\
import pathlib
import pytest
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestCtxScope(OttoSuite):
    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def uses_ctx(cls, ctx):
        CAPTURE.write_text(ctx.lab.name)

    async def test_ran(self) -> None:
        assert True
""")
    assert _run_inner_pytest(test_file, tmp_path) == pytest.ExitCode.OK
    assert capture_file.read_text() == "_test_stub"


def test_collected_suite_modules_are_captured_into_ottos_logging(
    tmp_path: Path, monkeypatch
) -> None:
    """Spec §5.5: after collection every item's top-level module name reaches
    capture_external_loggers, so a suite's ``logging.getLogger(__name__)`` lands
    in otto's sinks. Red if the collection hook is dropped."""
    seen: list[set[str]] = []
    monkeypatch.setattr(
        "otto.logger.management.capture_external_loggers",
        lambda prefixes: seen.append(set(prefixes)),
    )
    test_file = tmp_path / "test_logcap.py"
    test_file.write_text("""\
from otto.suite.suite import OttoSuite

class TestLogCap(OttoSuite):
    async def test_a(self) -> None:
        assert True

async def test_plain() -> None:
    assert True
""")
    assert _run_inner_pytest(test_file, tmp_path) == pytest.ExitCode.OK
    assert seen, "capture_external_loggers was never called after collection"
    assert "test_logcap" in seen[0], seen
