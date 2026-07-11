"""Unit tests for the ``otto.suite.run`` library API.

Exercises the extracted suite-run engine as a plain library call — no Typer
context, no ``ctx.meta``. Covers the public surface (``RunOptions``,
``SuiteRunResult``, ``run_suite``, ``find_suite``, ``resolve_output_dir``) plus
the internal exit-code mapping (``_final_exit_code``) that folds a stability
threshold violation into the invocation's exit code.
"""

import dataclasses
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.suite.run import (
    NoTestsMatchedError,
    RunOptions,
    SuiteRunResult,
    _final_exit_code,
    find_suite,
    resolve_output_dir,
    run_selection,
    run_suite,
)


def test_suite_package_reexports_selection_api():
    """otto.suite is the documented library facade — run_selection and both
    selection exceptions must be reachable from it directly, not only from the
    internal otto.suite.run / otto.suite.selection submodules."""
    import otto.suite
    from otto.suite.run import NoTestsMatchedError as _NoTestsMatchedError
    from otto.suite.run import run_selection as _run_selection
    from otto.suite.selection import UnknownSelectionError as _UnknownSelectionError

    assert otto.suite.run_selection is _run_selection
    assert otto.suite.NoTestsMatchedError is _NoTestsMatchedError
    assert otto.suite.UnknownSelectionError is _UnknownSelectionError
    assert "run_selection" in otto.suite.__all__
    assert "NoTestsMatchedError" in otto.suite.__all__
    assert "UnknownSelectionError" in otto.suite.__all__


def test_run_options_defaults_match_cli():
    o = RunOptions()
    assert o.cov_clean is True
    assert o.threshold == 100.0
    assert o.project_name == "Coverage Report"


def test_suite_run_result_passed():
    r = SuiteRunResult(
        exit_code=0,
        junit_paths=[Path("j.xml")],
        stability_report=None,
        stability_unstable=False,
        output_dir=Path(),
    )
    assert r.passed
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.exit_code = 1  # type: ignore[misc]


def test_suite_run_result_failed_is_not_passed():
    r = SuiteRunResult(
        exit_code=1,
        junit_paths=[],
        stability_report=None,
        stability_unstable=False,
        output_dir=Path(),
    )
    assert not r.passed


def test_final_exit_code_stability_failure():
    # threshold violation on an otherwise-green run must fail the invocation
    assert _final_exit_code(rc=0, unstable=True) == 1
    assert _final_exit_code(rc=0, unstable=False) == 0
    assert _final_exit_code(rc=5, unstable=False) == 5  # NO_TESTS_COLLECTED stays a failure


def test_final_exit_code_pytest_rc_wins_over_stability():
    # A real pytest failure code is preserved even when also unstable.
    assert _final_exit_code(rc=1, unstable=True) == 1


def test_find_suite_unknown_lists_registered():
    with pytest.raises(LookupError, match="registered"):
        find_suite("TestNoSuchSuite")


def test_find_suite_returns_registered_class():
    from otto.suite.register import SUITES, register_suite_class
    from otto.suite.run import find_suite as _find

    class _FindMeSuite:
        pass

    # Explicit cleanup (matching the seeding convention in
    # tests/unit/cli/test_listing.py) on top of this directory's autouse
    # _isolate_suites fixture, so the global SUITES registry never leaks even
    # if this test runs outside that conftest.
    register_suite_class(_FindMeSuite)
    try:
        assert _find("_FindMeSuite") is _FindMeSuite
    finally:
        SUITES.unregister("_FindMeSuite")


def test_resolve_output_dir_explicit_wins(tmp_path):
    assert resolve_output_dir(tmp_path) == tmp_path


def test_resolve_output_dir_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # no explicit dir, no context output_dir
    assert resolve_output_dir(None) == tmp_path


def test_resolve_output_dir_uses_context_output_dir(tmp_path):
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    token = set_context(OttoContext(lab=Lab(name="test"), output_dir=tmp_path))
    try:
        assert resolve_output_dir(None) == tmp_path
    finally:
        reset_context(token)


def test_run_suite_returns_result(tmp_path, monkeypatch):
    """The library entrypoint runs a suite and returns a populated result."""
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)
    monkeypatch.setattr("pytest.main", lambda *a, **k: pytest.ExitCode.OK)

    class _LibSuite:
        pass

    result = run_suite(_LibSuite, output_dir=tmp_path)
    assert isinstance(result, SuiteRunResult)
    assert result.exit_code == 0
    assert result.passed
    assert result.output_dir == tmp_path
    assert result.junit_paths == [tmp_path / "junit.xml"]
    assert result.stability_unstable is False


# ── run_suite: pytest.main argument wiring ───────────────────────────────────
#
# Ported from the old CLI-wrapper tests (tests/unit/cli/test_test.py's
# TestRunSuiteInternals / TestRunSuiteReport) when the suite-run engine moved
# out of otto.cli.test: they now exercise the library run_suite directly.


def _capture_pytest_main(monkeypatch, rc=None):
    """Patch pytest.main to record its args list and return *rc* (default OK)."""
    captured: dict = {}

    def fake_main(args, **_kw):
        captured["args"] = args
        return rc if rc is not None else pytest.ExitCode.OK

    monkeypatch.setattr("pytest.main", fake_main)
    return captured


def test_run_suite_passes_suite_file_and_keyword(tmp_path, monkeypatch):
    """run_suite derives the suite's file (inspect.getfile) and keys pytest by class name."""
    import inspect

    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)
    captured = _capture_pytest_main(monkeypatch)

    class _KwSuite:
        pass

    run_suite(_KwSuite, output_dir=tmp_path)
    args = captured["args"]
    assert inspect.getfile(_KwSuite) in args
    assert "-k" in args
    assert args[args.index("-k") + 1] == "_KwSuite"


def test_run_suite_auto_junit_path_under_output_dir(tmp_path, monkeypatch):
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)
    captured = _capture_pytest_main(monkeypatch)

    class _JunitSuite:
        pass

    run_suite(_JunitSuite, output_dir=tmp_path)
    junit_arg = next((a for a in captured["args"] if "--junitxml" in a), None)
    assert junit_arg is not None
    assert str(tmp_path) in junit_arg


def test_run_suite_passes_markers(tmp_path, monkeypatch):
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)
    captured = _capture_pytest_main(monkeypatch)

    class _MarkerSuite:
        pass

    run_suite(_MarkerSuite, run_options=RunOptions(markers="not integration"), output_dir=tmp_path)
    args = captured["args"]
    assert "-m" in args
    assert args[args.index("-m") + 1] == "not integration"


def test_run_suite_monitor_flags_reach_plugin(tmp_path, monkeypatch):
    """--monitor settings flow to OttoPlugin; the output path defaults to monitor.json."""
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)
    _capture_pytest_main(monkeypatch)

    captured: dict = {}

    class _CapturingPlugin:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("otto.suite.plugin.OttoPlugin", _CapturingPlugin)

    class _MonSuite:
        pass

    run_suite(
        _MonSuite,
        run_options=RunOptions(monitor=True, monitor_interval=2.0, monitor_hosts="router"),
        output_dir=tmp_path,
    )
    assert captured["monitor"] is True
    assert captured["monitor_interval"] == 2.0
    assert captured["monitor_hosts"] == "router"
    assert captured["monitor_output"] == tmp_path / "monitor.json"


def test_run_suite_monitor_output_override(tmp_path, monkeypatch):
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)
    _capture_pytest_main(monkeypatch)

    captured: dict = {}

    class _CapturingPlugin:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("otto.suite.plugin.OttoPlugin", _CapturingPlugin)

    out = tmp_path / "somewhere.db"

    class _MonSuite2:
        pass

    run_suite(
        _MonSuite2,
        run_options=RunOptions(monitor=True, monitor_output=out),
        output_dir=tmp_path,
    )
    assert captured["monitor_output"] == out


@pytest.mark.parametrize(
    ("rc", "expected"),
    [
        (pytest.ExitCode.TESTS_FAILED, 1),
        (pytest.ExitCode.NO_TESTS_COLLECTED, 5),  # a named suite collecting nothing is a failure
        (pytest.ExitCode.INTERNAL_ERROR, 3),
    ],
)
def test_run_suite_exit_code_maps_pytest_rc(tmp_path, monkeypatch, rc, expected):
    """The library result carries the pytest rc; the runner (not the library) raises Exit."""
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)
    _capture_pytest_main(monkeypatch, rc=rc)

    class _RcSuite:
        pass

    result = run_suite(_RcSuite, output_dir=tmp_path)
    assert result.exit_code == expected
    assert not result.passed


# ── run_suite: --cov-report wiring ───────────────────────────────────────────


def _run_suite_report(tmp_path, monkeypatch, *, run_options, log_dir):
    """Drive run_suite with a stubbed repo and mocked coverage tail; return the report mock."""
    import otto.config

    repo = MagicMock()
    repo.tests = [log_dir]
    repo.sut_dir = log_dir
    repo.name = "repo"
    # No [coverage] section → legacy gcda-only report path (what these pin).
    repo.settings = {}

    monkeypatch.setattr(otto.config, "get_repos", lambda: [repo])
    monkeypatch.setattr("pytest.main", lambda *a, **k: pytest.ExitCode.OK)
    monkeypatch.setattr("otto.coverage.collect.collect_coverage", AsyncMock())
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", AsyncMock())

    mock_store = MagicMock()
    mock_store.overall_pct.return_value = 50.0
    mock_store.file_count.return_value = 1
    mock_run_report = AsyncMock(return_value=mock_store)
    monkeypatch.setattr("otto.coverage.reporter.run_coverage_report", mock_run_report)

    class _RepSuite:
        pass

    run_suite(_RepSuite, run_options=run_options, output_dir=log_dir)
    return mock_run_report


def test_run_suite_no_cov_report_means_no_call(tmp_path, monkeypatch):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    mock = _run_suite_report(
        tmp_path,
        monkeypatch,
        run_options=RunOptions(cov=True, cov_clean=False, cov_report=False),
        log_dir=log_dir,
    )
    mock.assert_not_called()


def test_run_suite_default_report_dir_under_output_dir(tmp_path, monkeypatch):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    mock = _run_suite_report(
        tmp_path,
        monkeypatch,
        run_options=RunOptions(cov=True, cov_clean=False, cov_report=True),
        log_dir=log_dir,
    )
    mock.assert_called_once()
    args = mock.call_args.args
    assert args[0] == [log_dir / "cov"]
    assert args[1] == log_dir / "cov_report"
    assert (log_dir / "cov_report").is_dir()


def test_run_suite_explicit_report_dir_and_project_name(tmp_path, monkeypatch):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    report_dir = tmp_path / "my_report"
    report_dir.mkdir()
    mock = _run_suite_report(
        tmp_path,
        monkeypatch,
        run_options=RunOptions(
            cov=True,
            cov_clean=False,
            cov_report=True,
            cov_report_dir=report_dir,
            project_name="My App",
        ),
        log_dir=log_dir,
    )
    mock.assert_called_once()
    args = mock.call_args.args
    assert args[1] == report_dir
    assert mock.call_args.kwargs["project_name"] == "My App"


def test_run_suite_cov_report_into_reused_dir_warns_not_raises(tmp_path, monkeypatch, caplog):
    """A library run_suite(cov_report=True) into a pre-populated report dir warns and skips.

    Regression: _post_run_coverage's report-dir emptiness check used the CLI's
    typer-raising _prepare_empty_dir OUTSIDE the swallow, so a library run into a
    reused output_dir raised typer.BadParameter from a public library entrypoint.
    It now calls the neutral prepare_empty_dir INSIDE the swallow: a collision
    warns and skips the report, matching never-fail-a-successful-run.
    """
    import otto.config

    log_dir = tmp_path / "log"
    log_dir.mkdir()
    report_dir = log_dir / "cov_report"
    report_dir.mkdir()
    (report_dir / "stale.html").write_text("stale from a previous run")

    repo = MagicMock()
    repo.tests = [log_dir]
    repo.sut_dir = log_dir
    repo.name = "repo"
    repo.settings = {}
    monkeypatch.setattr(otto.config, "get_repos", lambda: [repo])
    monkeypatch.setattr("pytest.main", lambda *a, **k: pytest.ExitCode.OK)
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", AsyncMock())
    mock_report = AsyncMock()
    monkeypatch.setattr("otto.coverage.reporter.run_coverage_report", mock_report)

    class _ReuseSuite:
        pass

    with caplog.at_level("WARNING"):
        result = run_suite(
            _ReuseSuite,
            run_options=RunOptions(cov=False, cov_clean=False, cov_report=True),
            output_dir=log_dir,
        )
    # Completed with a result — no typer exception escaped.
    assert isinstance(result, SuiteRunResult)
    assert result.passed
    # The report was skipped: the dir collision was swallowed before rendering.
    mock_report.assert_not_called()
    assert any("not empty" in r.getMessage() for r in caplog.records)
    # We refused to clear — the stale artifact is preserved.
    assert (report_dir / "stale.html").exists()


def test_run_suite_cov_dir_override_used_as_report_source(tmp_path, monkeypatch):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    cov_dir = tmp_path / "custom_cov"
    cov_dir.mkdir()
    mock = _run_suite_report(
        tmp_path,
        monkeypatch,
        run_options=RunOptions(cov=True, cov_clean=False, cov_report=True, cov_dir=cov_dir),
        log_dir=log_dir,
    )
    mock.assert_called_once()
    args = mock.call_args.args
    assert args[0] == [cov_dir]


# ── run_suite: cov_dir empty/overwrite guard ─────────────────────────────────


def test_run_suite_nonempty_cov_dir_without_overwrite_raises(tmp_path, monkeypatch):
    """A non-empty ``cov_dir`` without ``overwrite_cov_dir`` raises before any
    host I/O — the same guard the CLI's ``--cov-dir``/``--overwrite-cov-dir``
    pair already enforces, now also applied to a library caller that hands
    ``RunOptions.cov_dir`` straight to ``run_suite``."""
    import otto.config

    log_dir = tmp_path / "log"
    log_dir.mkdir()
    cov_dir = tmp_path / "cov_dir"
    cov_dir.mkdir()
    (cov_dir / "stale.txt").write_text("stale")

    monkeypatch.setattr(otto.config, "get_repos", list)
    monkeypatch.setattr("pytest.main", lambda *a, **k: pytest.ExitCode.OK)
    clean_mock = AsyncMock()
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", clean_mock)

    class _CovDirSuite:
        pass

    with pytest.raises(ValueError, match="cov_dir"):
        run_suite(
            _CovDirSuite,
            run_options=RunOptions(cov=True, cov_dir=cov_dir),
            output_dir=log_dir,
        )
    # Failed before the pre-run remote clean ever ran, and the stale contents
    # were never touched.
    clean_mock.assert_not_awaited()
    assert (cov_dir / "stale.txt").exists()


def test_run_suite_overwrite_cov_dir_true_clears_and_proceeds(tmp_path, monkeypatch):
    """``overwrite_cov_dir=True`` clears a non-empty ``cov_dir`` and the run proceeds."""
    import otto.config

    log_dir = tmp_path / "log"
    log_dir.mkdir()
    cov_dir = tmp_path / "cov_dir"
    cov_dir.mkdir()
    (cov_dir / "stale.txt").write_text("stale")

    monkeypatch.setattr(otto.config, "get_repos", list)
    monkeypatch.setattr("pytest.main", lambda *a, **k: pytest.ExitCode.OK)
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", AsyncMock())
    monkeypatch.setattr("otto.coverage.collect.collect_coverage", AsyncMock())

    class _CovDirSuite2:
        pass

    result = run_suite(
        _CovDirSuite2,
        run_options=RunOptions(cov=True, cov_dir=cov_dir, cov_clean=False, overwrite_cov_dir=True),
        output_dir=log_dir,
    )
    assert result.passed
    assert not (cov_dir / "stale.txt").exists()


def test_run_selection_raises_value_error_when_nothing_matches(monkeypatch):
    """A --tests selection that matches nothing raises ValueError, not typer.Exit.

    No repos means no test universe to search — resolve_selection() returns an
    empty per-repo mapping rather than a did-you-mean UnknownSelectionError
    (there is nothing to suggest), so run_selection() reaches its own "nothing
    to run" check and raises a plain ValueError, matching the library's
    no-typer contract.
    """
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)

    with pytest.raises(ValueError, match="No tests matched"):
        run_selection(run_options=RunOptions(tests="test_nonexistent_zzz"))


def test_run_selection_no_match_raises_no_tests_matched_error(monkeypatch):
    """The no-match case raises the specific NoTestsMatchedError, not a bare ValueError.

    The dedicated subclass lets the CLI adapter catch *only* the no-match case,
    so an unrelated pipeline ValueError can never be misreported as "No tests
    matched the selection."
    """
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)

    with pytest.raises(NoTestsMatchedError, match="No tests matched"):
        run_selection(run_options=RunOptions(tests="test_nonexistent_zzz"))


def test_run_selection_empty_options_raises(monkeypatch):
    """Default RunOptions (no tests AND no markers) must refuse, not run every test.

    The CLI callback guards this (it only calls through when --tests/-m is set);
    the library must guard it too so a bare run_selection() can never silently
    match every test in every repo.
    """
    import otto.config

    # Guard fires before get_repos, but stub it so a regression can't run pytest.
    monkeypatch.setattr(otto.config, "get_repos", list)

    with pytest.raises(ValueError, match="run_selection requires run_options"):
        run_selection(run_options=RunOptions())


def test_run_selection_marker_alone_raises_when_no_repo_matches(monkeypatch):
    """The -m-alone path funnels through the same "nothing matched" ValueError."""
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)

    with pytest.raises(ValueError, match="No tests matched"):
        run_selection(run_options=RunOptions(markers="not-a-real-marker"))


def test_run_selection_typo_raises_unknown_selection_error(tmp_path, monkeypatch):
    """A typo against a real test universe raises the library's own exception.

    UnknownSelectionError (never typer.BadParameter — the library speaks
    library exceptions) propagates from resolve_selection through
    run_selection, carrying the did-you-mean message and the param_hint the
    CLI adapter needs to reconstruct an identical typer.BadParameter.
    """
    import otto.config
    from otto.config.repo import CollectedTest
    from otto.suite.selection import UnknownSelectionError

    class _FakeRepo:
        name = "fixture-repo"
        sut_dir = tmp_path
        tests: ClassVar[list] = []

        def collect_tests(self, markers=None, suite=None, tests=None):
            return [
                CollectedTest(
                    nodeid="tests/t.py::test_alpha",
                    name="test_alpha",
                    path=tmp_path / "tests" / "t.py",
                    cls_name=None,
                )
            ]

    monkeypatch.setattr(otto.config, "get_repos", lambda: [_FakeRepo()])

    with pytest.raises(UnknownSelectionError, match="did you mean: test_alpha") as excinfo:
        run_selection(run_options=RunOptions(tests="test_alpah"))
    assert excinfo.value.param_hint == "--tests"


def test_run_selection_returns_result_single_repo(tmp_path, monkeypatch):
    """A single matching repo runs one pytest session and returns its junit path."""
    import otto.config
    from otto.config.repo import CollectedTest

    class _FakeRepo:
        name = "fixture-repo"
        sut_dir = tmp_path
        tests: ClassVar[list] = []

        def collect_tests(self, markers=None, suite=None, tests=None):
            return [
                CollectedTest(
                    nodeid="tests/t.py::test_alpha",
                    name="test_alpha",
                    path=tmp_path / "tests" / "t.py",
                    cls_name=None,
                )
            ]

    monkeypatch.setattr(otto.config, "get_repos", lambda: [_FakeRepo()])
    monkeypatch.setattr("pytest.main", lambda *a, **k: pytest.ExitCode.OK)

    result = run_selection(
        run_options=RunOptions(tests="test_alpha"),
        output_dir=tmp_path,
    )
    assert isinstance(result, SuiteRunResult)
    assert result.exit_code == 0
    assert result.junit_paths == [tmp_path / "junit.xml"]


def test_run_selection_multi_repo_junit_fan_out(tmp_path, monkeypatch):
    """Two matching repos fan the default junit name out to junit_<repo>.xml each."""
    import otto.config
    from otto.config.repo import CollectedTest

    def _make_repo(name: str) -> object:
        class _FakeRepo:
            def collect_tests(self, markers=None, suite=None, tests=None):
                return [
                    CollectedTest(
                        nodeid="tests/t.py::test_alpha",
                        name="test_alpha",
                        path=tmp_path / "tests" / "t.py",
                        cls_name=None,
                    )
                ]

        repo = _FakeRepo()
        repo.name = name
        repo.sut_dir = tmp_path
        repo.tests = []
        return repo

    repos = [_make_repo("repoA"), _make_repo("repoB")]
    monkeypatch.setattr(otto.config, "get_repos", lambda: repos)
    monkeypatch.setattr("pytest.main", lambda *a, **k: pytest.ExitCode.OK)

    result = run_selection(
        run_options=RunOptions(tests="test_alpha"),
        output_dir=tmp_path,
    )
    assert result.exit_code == 0
    assert result.junit_paths == [
        tmp_path / "junit_repoA.xml",
        tmp_path / "junit_repoB.xml",
    ]


# ── run_suite: context installation for library callers ─────────────────────
#
# OttoSuite internals (setup_method / setup_class / the ctx fixture) call
# get_context(); only the CLI preamble ever installed an OttoContext, so the
# documented library path (bootstrap() → find_suite → run_suite) failed for
# EVERY real OttoSuite with "RuntimeError: No active OttoContext". These tests
# run a real minimal OttoSuite through the library entrypoint, in-process.


@pytest.fixture
def _inner_session_env(monkeypatch):
    """Neutralize outer-test interference for the REAL inner pytest sessions below.

    ``PYTEST_ADDOPTS`` reaches the inner ``pytest.main`` even though
    ``_run_pytest_session`` overrides ini ``addopts`` (env addopts are
    prepended to argv, not read from the ini):

    - ``-p no:playwright``: the outer session's pytest-playwright wraps every
      ``pytest_runtest_call``; letting the inner session load it again nests
      its soft-assertion scope and errors (same reason
      test_otto_suite._run_inner_pytest disables it).
    - ``asyncio_default_fixture_loop_scope=function``: silences pytest-asyncio's
      unset-option deprecation warning at inner configure time, which the outer
      session's ``filterwarnings=error`` would otherwise escalate into an inner
      INTERNALERROR. A plain library caller (no outer pytest) never hits either.
    """
    monkeypatch.setenv(
        "PYTEST_ADDOPTS",
        "-p no:playwright -o asyncio_default_fixture_loop_scope=function",
    )


def _probe_suite_src(cls_name: str) -> str:
    """Source for a minimal real OttoSuite that exercises the context-backed dirs."""
    return (
        '"""Minimal real OttoSuite probe for the library run path."""\n\n'
        "from otto.suite import OttoSuite\n\n\n"
        f"class {cls_name}(OttoSuite):\n"
        f'    """Library-run probe: writes a marker into its per-test dir."""\n\n'
        "    def test_marker(self):\n"
        "        # suiteDir/testDir come from get_context().output_dir\n"
        "        self.testDir.mkdir(parents=True, exist_ok=True)\n"
        '        (self.testDir / "marker.txt").write_text("ok")\n'
    )


def _register_probe_suite(tmp_path: Path, tag: str) -> type:
    """Write, import, and auto-register a real Test* OttoSuite; return the class.

    Mirrors ``Repo.import_test_file``'s spec_from_file_location shape (see
    tests/unit/suite/test_register.py) so ``inspect.getfile`` resolves. The
    ``Test*`` name auto-registers via ``OttoSuite.__init_subclass__``; callers
    must clean up with :func:`_cleanup_probe_suite` in a ``finally``.
    """
    import importlib.util
    import sys

    cls_name = f"TestCtxProbe{tag}"
    suite_file = tmp_path / f"test_ctx_probe_{tag.lower()}.py"
    suite_file.write_text(_probe_suite_src(cls_name))
    mod_name = f"_otto_ctx_probe_{tag.lower()}"
    spec = importlib.util.spec_from_file_location(mod_name, suite_file)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return getattr(mod, cls_name)


def _cleanup_probe_suite(tag: str) -> None:
    """Unregister the probe suite and drop its modules (incl. pytest's re-import)."""
    import sys

    from otto.suite.register import SUITES

    cls_name = f"TestCtxProbe{tag}"
    if cls_name in SUITES:
        SUITES.unregister(cls_name)
    sys.modules.pop(f"_otto_ctx_probe_{tag.lower()}", None)
    sys.modules.pop(f"test_ctx_probe_{tag.lower()}", None)


@pytest.mark.usefixtures("_inner_session_env")
def test_run_suite_installs_minimal_context_when_none_active(tmp_path, monkeypatch):
    """The documented library path works with NO active context (the CLI-preamble gap).

    run_suite must install a minimal lab-less OttoContext for the session so
    OttoSuite's own get_context()-backed fixtures work, and restore the prior
    (no-context) state afterwards.
    """
    import otto.config
    from otto.context import _active, try_get_context

    monkeypatch.setattr(otto.config, "get_repos", list)
    out = tmp_path / "out"
    out.mkdir()

    suite_cls = _register_probe_suite(tmp_path, "A")
    token = _active.set(None)  # hermetic: guarantee the no-context precondition
    try:
        assert try_get_context() is None
        result = run_suite(suite_cls, output_dir=out)
        assert result.passed, f"exit_code={result.exit_code}"
        assert (out / "junit.xml").exists()
        # The suite's per-test dir was created under output_dir via the
        # temporary context (suiteDir = get_context().output_dir).
        markers = list(out.rglob("marker.txt"))
        assert markers, f"no per-test marker under {out}"
        # The temporary context never leaks out of run_suite.
        assert try_get_context() is None
    finally:
        _active.reset(token)
        _cleanup_probe_suite("A")


@pytest.mark.usefixtures("_inner_session_env")
def test_run_suite_sets_and_restores_output_dir_on_active_context(tmp_path, monkeypatch):
    """An active context with output_dir=None gets log_dir for the session, then restored."""
    import otto.config
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    monkeypatch.setattr(otto.config, "get_repos", list)
    out = tmp_path / "out"
    out.mkdir()

    suite_cls = _register_probe_suite(tmp_path, "B")
    ctx = OttoContext(lab=Lab(name="test"))
    assert ctx.output_dir is None
    token = set_context(ctx)
    try:
        result = run_suite(suite_cls, output_dir=out)
        assert result.passed, f"exit_code={result.exit_code}"
        assert list(out.rglob("marker.txt")), f"no per-test marker under {out}"
        # The session-scoped assignment is rolled back afterwards.
        assert ctx.output_dir is None
    finally:
        reset_context(token)
        _cleanup_probe_suite("B")


@pytest.mark.usefixtures("_inner_session_env")
def test_run_suite_leaves_active_context_output_dir_untouched(tmp_path, monkeypatch):
    """A context that already has an output_dir is never mutated by run_suite."""
    import otto.config
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    monkeypatch.setattr(otto.config, "get_repos", list)
    ctx_dir = tmp_path / "ctx_dir"
    ctx_dir.mkdir()
    out = tmp_path / "out"
    out.mkdir()

    suite_cls = _register_probe_suite(tmp_path, "C")
    ctx = OttoContext(lab=Lab(name="test"), output_dir=ctx_dir)
    token = set_context(ctx)
    try:
        result = run_suite(suite_cls, output_dir=out)
        assert result.passed, f"exit_code={result.exit_code}"
        # junit honors the explicit output_dir; the context is untouched, so
        # the suite's own dirs still follow the context's output_dir (exactly
        # the CLI-equivalent behavior, where the two are the same dir).
        assert (out / "junit.xml").exists()
        assert ctx.output_dir == ctx_dir
    finally:
        reset_context(token)
        _cleanup_probe_suite("C")


# ── _session_context hardening: restore on exception ─────────────────────────
#
# _run_pytest_session raising mid-session must not leak the temporary state
# _session_context installs — the finally in each branch must still run.


def test_run_suite_restores_no_context_state_on_exception(tmp_path, monkeypatch):
    """No-active-context branch: an exception mid-session still resets the
    contextvar, leaving no active OttoContext behind."""
    import otto.config
    from otto.context import _active, try_get_context

    monkeypatch.setattr(otto.config, "get_repos", list)

    def _raise(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr("otto.suite.run._run_pytest_session", _raise)

    class _ExcNoCtxSuite:
        pass

    token = _active.set(None)  # hermetic: guarantee the no-context precondition
    try:
        assert try_get_context() is None
        with pytest.raises(RuntimeError, match="boom"):
            run_suite(_ExcNoCtxSuite, output_dir=tmp_path)
        assert try_get_context() is None
    finally:
        _active.reset(token)


def test_run_suite_restores_prior_output_dir_on_exception(tmp_path, monkeypatch):
    """Active-context-with-no-output_dir branch: an exception mid-session still
    rolls back the session-scoped output_dir assignment to its prior value."""
    import otto.config
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    monkeypatch.setattr(otto.config, "get_repos", list)

    def _raise(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr("otto.suite.run._run_pytest_session", _raise)

    class _ExcOutDirSuite:
        pass

    ctx = OttoContext(lab=Lab(name="test"))
    assert ctx.output_dir is None
    token = set_context(ctx)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            run_suite(_ExcOutDirSuite, output_dir=tmp_path)
        assert ctx.output_dir is None
    finally:
        reset_context(token)


# ── run_selection: context installation for library callers ─────────────────
#
# The context-installation tests above only ever drove run_suite; run_selection
# shares the exact same _session_context call but had no direct coverage.


def test_run_selection_installs_and_restores_minimal_context(tmp_path, monkeypatch):
    """run_selection installs the same minimal lab-less context run_suite does
    for a no-context library caller, and restores the prior (no-context) state
    afterwards."""
    import otto.config
    from otto.config.repo import CollectedTest
    from otto.context import LIBRARY_LAB_NAME, _active, try_get_context

    class _FakeRepo:
        name = "fixture-repo"
        sut_dir = tmp_path
        tests: ClassVar[list] = []

        def collect_tests(self, markers=None, suite=None, tests=None):
            return [
                CollectedTest(
                    nodeid="tests/t.py::test_alpha",
                    name="test_alpha",
                    path=tmp_path / "tests" / "t.py",
                    cls_name=None,
                )
            ]

    monkeypatch.setattr(otto.config, "get_repos", lambda: [_FakeRepo()])

    captured: dict = {}

    def fake_main(_args, **_kw):
        ctx = try_get_context()
        captured["lab_name"] = ctx.lab.name if ctx is not None else None
        captured["output_dir"] = ctx.output_dir if ctx is not None else None
        return pytest.ExitCode.OK

    monkeypatch.setattr("pytest.main", fake_main)

    token = _active.set(None)  # hermetic: guarantee the no-context precondition
    try:
        assert try_get_context() is None
        result = run_selection(run_options=RunOptions(tests="test_alpha"), output_dir=tmp_path)
        assert result.passed, f"exit_code={result.exit_code}"
        # A context WAS installed for the duration of the session...
        assert captured["lab_name"] == LIBRARY_LAB_NAME
        assert captured["output_dir"] == tmp_path
        # ...and torn down afterwards.
        assert try_get_context() is None
    finally:
        _active.reset(token)


# ── run_selection: cov_dir empty/overwrite guard (carried from Task 3) ──────


def test_run_selection_nonempty_cov_dir_without_overwrite_raises(tmp_path, monkeypatch):
    """The cov_dir empty/overwrite guard run_suite enforces (Task 3) applies
    identically on the run_selection path — a library caller handing
    RunOptions.cov_dir straight to run_selection gets the same guard, before
    any host I/O."""
    import otto.config
    from otto.config.repo import CollectedTest

    log_dir = tmp_path / "log"
    log_dir.mkdir()
    cov_dir = tmp_path / "cov_dir"
    cov_dir.mkdir()
    (cov_dir / "stale.txt").write_text("stale")

    class _FakeRepo:
        name = "fixture-repo"
        sut_dir = tmp_path
        tests: ClassVar[list] = []

        def collect_tests(self, markers=None, suite=None, tests=None):
            return [
                CollectedTest(
                    nodeid="tests/t.py::test_alpha",
                    name="test_alpha",
                    path=tmp_path / "tests" / "t.py",
                    cls_name=None,
                )
            ]

    monkeypatch.setattr(otto.config, "get_repos", lambda: [_FakeRepo()])
    monkeypatch.setattr("pytest.main", lambda *a, **k: pytest.ExitCode.OK)
    clean_mock = AsyncMock()
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", clean_mock)

    with pytest.raises(ValueError, match="cov_dir"):
        run_selection(
            run_options=RunOptions(tests="test_alpha", cov=True, cov_dir=cov_dir),
            output_dir=log_dir,
        )
    # Failed before the pre-run remote clean ever ran, and the stale contents
    # were never touched.
    clean_mock.assert_not_awaited()
    assert (cov_dir / "stale.txt").exists()
