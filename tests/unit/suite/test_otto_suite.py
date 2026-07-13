"""
Unit tests for ``otto.suite.suite``.

Tests verify:
  - ``_sanitize_node_name`` replaces filesystem-unsafe characters
  - The two autouse fixtures (``_otto_test_dir``,
    ``_otto_monitor_events``) work correctly when split from the former
    monolithic ``_test_lifecycle``
  - ``@pytest.mark.parametrize`` produces distinct ``testDir`` per parameter
  - ``suite_options`` fixture injection works in test method parameters
  - ``teardown_method`` is called after each test
  - ``expect()`` records non-fatal failures without stopping execution
  - ``start_monitor(db_path=...)``/``stop_monitor()`` persist a real (not
    degraded) session archive
"""

import asyncio
import contextlib
import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.models import MonitorExport
from otto.monitor.collector import MetricCollector
from otto.monitor.db import read_sessions
from otto.monitor.export import build_db_export
from otto.suite.plugin import OttoPlugin
from otto.suite.pytest_plugin import OttoOptionsPlugin
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


def _run_inner_pytest(test_file: Path, tmp_path: Path, options: object | None = None) -> int:
    """Run an inner pytest session with OttoPlugin + OttoOptionsPlugin.

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
                "-o",
                "asyncio_default_fixture_loop_scope=function",
                "--no-cov",
                "--override-ini",
                "addopts=",
                "-p",
                "no:playwright",
                "-x",
            ],
            plugins=[OttoPlugin(), OttoOptionsPlugin(options)],
        )
    finally:
        sys.modules.pop(test_file.stem, None)
        reset_context(token)
    return exit_code


# ── Autouse fixtures ─────────────────────────────────────────────────────────


class TestOttoTestDir:
    def test_test_dir_created_per_test(self, tmp_path: Path) -> None:
        """Each test gets a unique testDir under suiteDir/tests/."""
        capture_file = tmp_path / "dirs.txt"
        test_file = tmp_path / "test_dirs.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestDirs(OttoSuite):
    async def test_alpha(self) -> None:
        CAPTURE.write_text(str(self.testDir))

    async def test_beta(self) -> None:
        with CAPTURE.open("a") as f:
            f.write("\\n" + str(self.testDir))
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        lines = capture_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert lines[0] != lines[1]
        assert "test_alpha" in lines[0]
        assert "test_beta" in lines[1]

    def test_parametrized_names_sanitized(self, tmp_path: Path) -> None:
        """Parametrized test names have brackets replaced in testDir."""
        capture_file = tmp_path / "param_dirs.txt"
        test_file = tmp_path / "test_param_dirs.py"
        test_file.write_text(f"""\
import pathlib
import pytest
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestParamDirs(OttoSuite):
    @pytest.mark.parametrize("val", ["a", "b"])
    async def test_param(self, val: str) -> None:
        with CAPTURE.open("a") as f:
            f.write(str(self.testDir) + "\\n")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        lines = [line for line in capture_file.read_text().strip().split("\n") if line]
        assert len(lines) == 2
        # Brackets should be sanitized
        for line in lines:
            assert "[" not in line
            assert "]" not in line


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


# ── teardown_method ──────────────────────────────────────────────────────────


class TestTeardownMethod:
    def test_teardown_method_called(self, tmp_path: Path) -> None:
        """teardown_method() is called after each test."""
        capture_file = tmp_path / "teardown.txt"
        test_file = tmp_path / "test_teardown.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestTeardown(OttoSuite):
    def teardown_method(self, method=None) -> None:
        with CAPTURE.open("a") as f:
            f.write("torn_down\\n")

    async def test_one(self) -> None:
        assert True

    async def test_two(self) -> None:
        assert True
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        lines = [line for line in capture_file.read_text().strip().split("\n") if line]
        assert len(lines) == 2
        assert all(line == "torn_down" for line in lines)


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


class TestExpect:
    def test_passing_expect_does_not_fail(self, tmp_path: Path) -> None:
        """A truthy expect() should not cause the test to fail."""
        test_file = tmp_path / "test_pass.py"
        test_file.write_text("""\
from otto.suite.suite import OttoSuite

class TestPass(OttoSuite):
    async def test_ok(self) -> None:
        self.expect(True)
        self.expect(1 == 1)
        self.expect("hello")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK

    def test_failing_expect_continues_execution(self, tmp_path: Path) -> None:
        """A failing expect() does not stop the test; later code still runs."""
        capture_file = tmp_path / "continued.txt"
        test_file = tmp_path / "test_continue.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestContinue(OttoSuite):
    async def test_continues(self) -> None:
        self.expect(False)
        CAPTURE.write_text("reached")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        assert capture_file.read_text() == "reached"

    def test_multiple_failures_all_reported(self, tmp_path: Path) -> None:
        """All failing expects appear in the final error message."""
        capture_file = tmp_path / "count.txt"
        test_file = tmp_path / "test_multi.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestMulti(OttoSuite):
    async def test_three_failures(self) -> None:
        self.expect(False, "first")
        self.expect(False, "second")
        self.expect(False, "third")
        CAPTURE.write_text(str(len(self._expect_failures)))
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        assert capture_file.read_text() == "3"

    def test_failure_includes_source_line(self, tmp_path: Path) -> None:
        """The failure report includes the source filename and line."""
        capture_file = tmp_path / "report.txt"
        test_file = tmp_path / "test_source.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestSource(OttoSuite):
    async def test_source_info(self) -> None:
        x = 42
        self.expect(x == 99)
        CAPTURE.write_text(self._expect_failures[0])
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        report = capture_file.read_text()
        assert "test_source.py" in report
        assert "self.expect(x == 99)" in report
        assert "x = 42" in report

    def test_custom_msg_alongside_source(self, tmp_path: Path) -> None:
        """A custom msg appears alongside (not instead of) source info."""
        capture_file = tmp_path / "msg.txt"
        test_file = tmp_path / "test_msg.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestMsg(OttoSuite):
    async def test_custom_msg(self) -> None:
        val = 42
        self.expect(val == 99, "hostname missing from config")
        CAPTURE.write_text(self._expect_failures[0])
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        report = capture_file.read_text()
        # msg is present
        assert "hostname missing from config" in report
        # source info is also present (not replaced by msg)
        assert "self.expect(val == 99" in report
        assert "val = 42" in report

    def test_mix_of_pass_and_fail(self, tmp_path: Path) -> None:
        """Only failing expects are recorded; passing ones are ignored."""
        capture_file = tmp_path / "mix.txt"
        test_file = tmp_path / "test_mix.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestMix(OttoSuite):
    async def test_mixed(self) -> None:
        self.expect(True)
        self.expect(False, "one")
        self.expect(True)
        self.expect(False, "two")
        self.expect(True)
        CAPTURE.write_text(str(len(self._expect_failures)))
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        assert capture_file.read_text() == "2"

    def test_failures_reset_between_tests(self, tmp_path: Path) -> None:
        """Each test starts with a fresh _expect_failures list."""
        capture_file = tmp_path / "reset.txt"
        test_file = tmp_path / "test_reset.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestReset(OttoSuite):
    async def test_first(self) -> None:
        self.expect(True)

    async def test_second(self) -> None:
        with CAPTURE.open("w") as f:
            f.write(str(len(self._expect_failures)))
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        assert capture_file.read_text() == "0"


# ── _active_monitor_collector accessor ─────────────────────────────────────────


class TestActiveMonitorCollector:
    """Per-suite ``_monitor_collector`` takes precedence; falls back to the
    class-level session collector set by ``OttoPlugin._otto_session_monitor``."""

    @staticmethod
    def _make_suite(tmp_path: Path):
        from otto.suite.suite import OttoSuite

        class _Suite(OttoSuite):
            pass

        ctx = OttoContext(lab=Lab(name="_test_stub"), output_dir=tmp_path)
        token = set_context(ctx)
        try:
            s = _Suite()
            s.setup_method()
        finally:
            reset_context(token)
        return s

    def test_returns_none_when_no_monitor_active(self, tmp_path: Path):
        s = self._make_suite(tmp_path)
        assert s._active_monitor_collector() is None

    def test_per_suite_collector_takes_precedence(self, tmp_path: Path):
        from otto.suite.suite import OttoSuite

        per_suite = MagicMock(name="per_suite")
        session = MagicMock(name="session")
        try:
            OttoSuite._session_monitor_collector = session
            s = self._make_suite(tmp_path)
            s._monitor_collector = per_suite
            assert s._active_monitor_collector() is per_suite
        finally:
            OttoSuite._session_monitor_collector = None

    def test_falls_back_to_session_collector(self, tmp_path: Path):
        from otto.suite.suite import OttoSuite

        session = MagicMock(name="session")
        try:
            OttoSuite._session_monitor_collector = session
            s = self._make_suite(tmp_path)
            assert s._active_monitor_collector() is session
        finally:
            OttoSuite._session_monitor_collector = None


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


def _make_suite(tmp_path: Path) -> OttoSuite:
    class _Suite(OttoSuite):
        pass

    ctx = OttoContext(lab=Lab(name="_test_stub"), output_dir=tmp_path)
    token = set_context(ctx)
    try:
        suite = _Suite()
        suite.setup_method()
    finally:
        reset_context(token)
    return suite


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
            url = await suite.start_monitor(
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
                    urllib.request.urlopen, f"{url}/api/monitor_sessions", timeout=10
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
