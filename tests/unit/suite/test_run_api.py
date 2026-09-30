"""Unit tests for the ``otto.suite.run`` library API.

Exercises the test-run engine as a plain library call — no Typer context, no
``ctx.meta``. Covers the public surface (``RunOptions``, ``SuiteRunResult``,
``run_tests``, ``resolve_output_dir``) plus the internal exit-code mapping
(``_final_exit_code``) that folds a stability threshold violation into the
invocation's exit code.
"""

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.coverage.config import DestinationError
from otto.suite.run import (
    NoTestsMatchedError,
    RunOptions,
    SuiteRunResult,
    _final_exit_code,
    resolve_output_dir,
    run_tests,
)
from tests._fixtures.gitrepo import TmpGitRepo
from tests._fixtures.sut_repos import DOUBLE_TEST_NAME as _ALPHA
from tests._fixtures.sut_repos import collected, pytest_main_returning
from tests._fixtures.sut_repos import repo_double as _stub_repo


def _use_repo(monkeypatch, repo: MagicMock) -> MagicMock:
    """Make *repo* the lab's only repo."""
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", lambda: [repo])
    return repo


def test_suite_package_reexports_selection_api():
    """otto.suite is the documented library facade — run_tests and both
    selection exceptions must be reachable from it directly, not only from the
    internal otto.suite.run / otto.suite.selection submodules."""
    import otto
    import otto.suite
    from otto.suite.run import NoTestsMatchedError as _NoTestsMatchedError
    from otto.suite.run import run_tests as _run_tests
    from otto.suite.selection import UnknownSelectionError as _UnknownSelectionError

    assert otto.suite.run_tests is _run_tests
    assert otto.run_tests is _run_tests
    assert otto.suite.NoTestsMatchedError is _NoTestsMatchedError
    assert otto.suite.UnknownSelectionError is _UnknownSelectionError
    assert "run_tests" in otto.suite.__all__
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


# ── run_tests: pytest.main argument wiring ───────────────────────────────────


def _capture_pytest_main(monkeypatch, rc=None):
    """Patch pytest.main to record its args list and return *rc* (default OK)."""
    captured: dict = {}

    def fake_main(args, plugins=(), **_kw):
        captured["args"] = args
        collected(plugins)
        return rc if rc is not None else pytest.ExitCode.OK

    monkeypatch.setattr("pytest.main", fake_main)
    return captured


def test_run_tests_targets_the_test_directories_and_passes_no_keyword(tmp_path, monkeypatch):
    """The session's targets are the repo's test directories; the names ride on the plugin.

    Never a file or node id argument (it would bypass a conftest's
    ``collect_ignore``), and never ``-k`` (substring semantics).
    """
    _use_repo(monkeypatch, _stub_repo(tmp_path))
    captured = _capture_pytest_main(monkeypatch)
    plugin_kwargs = _capture_otto_plugin(monkeypatch)

    run_tests([_ALPHA], output_dir=tmp_path)
    args = captured["args"]
    assert [a for a in args if a.startswith(str(tmp_path))] == [str(tmp_path / "tests")]
    assert "-k" not in args
    assert plugin_kwargs["names"] == [_ALPHA]


def test_run_tests_auto_junit_path_under_output_dir(tmp_path, monkeypatch):
    _use_repo(monkeypatch, _stub_repo(tmp_path))
    captured = _capture_pytest_main(monkeypatch)

    run_tests([_ALPHA], output_dir=tmp_path)
    junit_arg = next((a for a in captured["args"] if "--junitxml" in a), None)
    assert junit_arg is not None
    assert str(tmp_path) in junit_arg


def test_run_tests_passes_markers(tmp_path, monkeypatch):
    _use_repo(monkeypatch, _stub_repo(tmp_path))
    captured = _capture_pytest_main(monkeypatch)

    run_tests([_ALPHA], run_options=RunOptions(markers="not integration"), output_dir=tmp_path)
    args = captured["args"]
    assert "-m" in args
    assert args[args.index("-m") + 1] == "not integration"


def _capture_otto_plugin(monkeypatch) -> dict:
    """Make ``OttoPlugin`` record its constructor kwargs (the plugin itself is unchanged)."""
    from otto.suite.plugin import OttoPlugin

    captured: dict = {}

    class _CapturingPlugin(OttoPlugin):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("otto.suite.plugin.OttoPlugin", _CapturingPlugin)
    return captured


def test_run_tests_monitor_flags_reach_plugin(tmp_path, monkeypatch):
    """--monitor settings flow to OttoPlugin; the output path defaults to monitor.json."""
    _use_repo(monkeypatch, _stub_repo(tmp_path))
    _capture_pytest_main(monkeypatch)
    captured = _capture_otto_plugin(monkeypatch)

    run_tests(
        [_ALPHA],
        run_options=RunOptions(monitor=True, monitor_interval=2.0, monitor_hosts="router"),
        output_dir=tmp_path,
    )
    assert captured["monitor"] is True
    assert captured["monitor_interval"] == 2.0
    assert captured["monitor_hosts"] == "router"
    assert captured["monitor_output"] == tmp_path / "monitor.json"


def test_run_tests_monitor_output_override(tmp_path, monkeypatch):
    _use_repo(monkeypatch, _stub_repo(tmp_path))
    _capture_pytest_main(monkeypatch)
    captured = _capture_otto_plugin(monkeypatch)

    out = tmp_path / "somewhere.db"
    run_tests(
        [_ALPHA], run_options=RunOptions(monitor=True, monitor_output=out), output_dir=tmp_path
    )
    assert captured["monitor_output"] == out


@pytest.mark.parametrize(
    ("rc", "expected"),
    [
        (pytest.ExitCode.TESTS_FAILED, 1),
        (pytest.ExitCode.INTERNAL_ERROR, 3),
    ],
)
def test_run_tests_exit_code_maps_pytest_rc(tmp_path, monkeypatch, rc, expected):
    """The library result carries the pytest rc; the CLI adapter (not the library) exits."""
    _use_repo(monkeypatch, _stub_repo(tmp_path))
    _capture_pytest_main(monkeypatch, rc=rc)

    result = run_tests([_ALPHA], output_dir=tmp_path)
    assert result.exit_code == expected
    assert not result.passed


def test_run_tests_a_session_with_nothing_to_run_is_no_match(tmp_path, monkeypatch):
    """pytest's "no tests collected" (5) from every repo is no match, not an exit code."""
    _use_repo(monkeypatch, _stub_repo(tmp_path))
    _capture_pytest_main(monkeypatch, rc=pytest.ExitCode.NO_TESTS_COLLECTED)

    with pytest.raises(NoTestsMatchedError):
        run_tests([_ALPHA], output_dir=tmp_path)


# ── resolve_coverage: the tri-state --cov/--no-cov decision ──────────────────


def _stub_instrumented_lab(monkeypatch, *, instrumented=True):
    """Stub the local instrumentation scan with a single-product report.

    ``resolve_coverage`` runs ``detect_for_lab`` over the coverage hosts
    before anything executes; every ``RunOptions(cov=True)`` run in this file
    passes through it, so the scan is stubbed rather than pointed at a lab.

    A forced ``--cov`` also refuses a missing ``[coverage]`` table, and these
    tests' repo doubles mostly carry no settings — so ``get_cov_config`` is
    wrapped, not replaced: a double that declares a real ``[coverage]`` keeps
    it (the ``[coverage.tickets]`` wiring tests depend on that), and one that
    declares none is handed a minimal stand-in table.
    """
    from otto.config.coverage_settings import get_cov_config as _real_get_cov_config
    from otto.coverage.instrumentation import InstrumentationReport, InstrumentationRow

    rows = [InstrumentationRow("h1", "app", True)] if instrumented else []
    monkeypatch.setattr(
        "otto.coverage.instrumentation.detect_for_lab",
        lambda repos: InstrumentationReport(rows),
    )
    monkeypatch.setattr(
        "otto.config.coverage_settings.get_cov_config",
        lambda repos: _real_get_cov_config(repos) or {"hosts": ".*"},
    )


def test_resolve_coverage_auto_turns_on_when_instrumented(monkeypatch):
    from otto.coverage.instrumentation import InstrumentationReport, InstrumentationRow
    from otto.suite.run import RunOptions, resolve_coverage

    monkeypatch.setattr(
        "otto.coverage.instrumentation.detect_for_lab",
        lambda repos: InstrumentationReport([InstrumentationRow("h1", "app", True)]),
    )
    monkeypatch.setattr(
        "otto.config.coverage_settings.get_cov_config", lambda repos: {"hosts": ".*"}
    )
    assert resolve_coverage(RunOptions(), [], command="otto test").cov is True


def test_resolve_coverage_auto_stays_off_when_nothing_instrumented(monkeypatch):
    from otto.coverage.instrumentation import InstrumentationReport, InstrumentationRow
    from otto.suite.run import RunOptions, resolve_coverage

    monkeypatch.setattr(
        "otto.coverage.instrumentation.detect_for_lab",
        lambda repos: InstrumentationReport([InstrumentationRow("h1", "app", False)]),
    )
    monkeypatch.setattr(
        "otto.config.coverage_settings.get_cov_config", lambda repos: {"hosts": ".*"}
    )
    assert resolve_coverage(RunOptions(), [], command="otto test").cov is False


def test_resolve_coverage_forced_on_with_nothing_raises(monkeypatch):
    from otto.coverage.errors import CoverageNotInstrumentedError
    from otto.coverage.instrumentation import InstrumentationReport
    from otto.suite.run import RunOptions, resolve_coverage

    monkeypatch.setattr(
        "otto.coverage.instrumentation.detect_for_lab", lambda repos: InstrumentationReport([])
    )
    monkeypatch.setattr(
        "otto.config.coverage_settings.get_cov_config", lambda repos: {"hosts": ".*"}
    )
    with pytest.raises(CoverageNotInstrumentedError):
        resolve_coverage(RunOptions(cov=True), [], command="otto test --cov")


def test_resolve_coverage_forced_off_never_detects(monkeypatch):
    from otto.suite.run import RunOptions, resolve_coverage

    monkeypatch.setattr(
        "otto.coverage.instrumentation.detect_for_lab",
        lambda repos: (_ for _ in ()).throw(AssertionError("must not detect")),
    )
    assert resolve_coverage(RunOptions(cov=False), [], command="otto test").cov is False


def test_resolve_coverage_returns_a_copy_keeping_every_other_field(monkeypatch):
    """The decision is a copy: the caller's RunOptions is frozen and untouched."""
    from otto.suite.run import RunOptions, resolve_coverage

    _stub_instrumented_lab(monkeypatch)
    monkeypatch.setattr(
        "otto.config.coverage_settings.get_cov_config", lambda repos: {"hosts": ".*"}
    )
    opts = RunOptions(markers="smoke", project_name="P")
    resolved = resolve_coverage(opts, [], command="otto test")
    assert opts.cov is None
    assert resolved.cov is True
    assert (resolved.markers, resolved.cov_report, resolved.project_name) == ("smoke", False, "P")


def test_run_tests_forced_cov_with_nothing_instrumented_raises(tmp_path, monkeypatch):
    """``--cov`` against a lab with no instrumented product refuses before the
    tests run — the typed error reaches the caller (the CLI frames it)."""
    from otto.coverage.errors import CoverageNotInstrumentedError

    _use_repo(monkeypatch, _stub_repo(tmp_path))
    monkeypatch.setattr("pytest.main", pytest_main_returning())
    pre_clean = AsyncMock()
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", pre_clean)
    _stub_instrumented_lab(monkeypatch, instrumented=False)

    with pytest.raises(CoverageNotInstrumentedError):
        run_tests([_ALPHA], run_options=RunOptions(cov=True), output_dir=tmp_path)
    # Refused before any host was touched.
    pre_clean.assert_not_awaited()


def test_resolve_coverage_forced_on_refuses_a_missing_coverage_table(monkeypatch):
    """A forced ``--cov`` with no ``[coverage]`` table refuses up front.

    There is nothing to collect into, and the collection stage's own refusal
    arrives *after* the suite has run, where ``_post_run_coverage`` swallows it
    — so an explicit request must die here, naming the remedy.
    """
    from otto.config.coverage_settings import CoverageConfigError
    from otto.coverage.instrumentation import InstrumentationReport, InstrumentationRow
    from otto.suite.run import RunOptions, resolve_coverage

    monkeypatch.setattr(
        "otto.coverage.instrumentation.detect_for_lab",
        lambda repos: InstrumentationReport([InstrumentationRow("h1", "app", True)]),
    )
    monkeypatch.setattr("otto.config.coverage_settings.get_cov_config", lambda repos: {})
    with pytest.raises(CoverageConfigError, match=r"\[coverage\] table"):
        resolve_coverage(RunOptions(cov=True), [], command="otto test --cov")


def test_run_tests_forced_cov_without_coverage_table_refuses_before_the_run(tmp_path, monkeypatch):
    """The refusal reaches the caller before any host is touched."""
    from otto.config.coverage_settings import CoverageConfigError
    from otto.coverage.instrumentation import InstrumentationReport, InstrumentationRow

    _use_repo(monkeypatch, _stub_repo(tmp_path))
    monkeypatch.setattr("pytest.main", pytest_main_returning())
    pre_clean = AsyncMock()
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", pre_clean)
    monkeypatch.setattr(
        "otto.coverage.instrumentation.detect_for_lab",
        lambda repos: InstrumentationReport([InstrumentationRow("h1", "app", True)]),
    )
    monkeypatch.setattr("otto.config.coverage_settings.get_cov_config", lambda repos: {})

    with pytest.raises(CoverageConfigError, match=r"\[coverage\] table"):
        run_tests([_ALPHA], run_options=RunOptions(cov=True), output_dir=tmp_path)
    pre_clean.assert_not_awaited()


def test_resolve_coverage_auto_with_no_coverage_table_does_not_raise(monkeypatch):
    """Auto is the sibling of the refusal above: no table, no request, no error
    — retrieval simply stays off (the instrumentation warning is logged)."""
    from otto.coverage.instrumentation import InstrumentationReport, InstrumentationRow
    from otto.suite.run import RunOptions, resolve_coverage

    monkeypatch.setattr(
        "otto.coverage.instrumentation.detect_for_lab",
        lambda repos: InstrumentationReport([InstrumentationRow("h1", "app", True)]),
    )
    monkeypatch.setattr("otto.config.coverage_settings.get_cov_config", lambda repos: {})
    assert resolve_coverage(RunOptions(), [], command="otto test").cov is False


def _raise_empty_selection(_repos):
    from otto.config.scope import EmptySelectionError

    raise EmptySelectionError("sensor", 3)


def test_resolve_coverage_auto_survives_an_empty_hosts_selection(monkeypatch, caplog):
    """A ``[coverage].hosts`` selector matching nothing must not kill a plain
    ``otto test``: it is a coverage-only misconfiguration, so retrieval goes
    off with one warning naming the command."""
    from otto.suite.run import RunOptions, resolve_coverage

    monkeypatch.setattr("otto.coverage.instrumentation.detect_for_lab", _raise_empty_selection)
    monkeypatch.setattr(
        "otto.config.coverage_settings.get_cov_config", lambda repos: {"hosts": "sensor"}
    )

    with caplog.at_level("WARNING"):
        resolved = resolve_coverage(RunOptions(), [], command="otto test")

    assert resolved.cov is False
    messages = [r.getMessage() for r in caplog.records]
    assert any("otto test: coverage stays off" in m for m in messages), messages


def test_resolve_coverage_auto_survives_a_malformed_hosts_selector(monkeypatch, caplog):
    """The other half of the same guard: a malformed selector warns, not raises.

    The warning is also markup-escaped. Both errors carry a literal bracket
    (``[coverage].hosts must be a string``, or a user regex like ``test[123]``
    quoted back by ``EmptySelectionError``), and the console handler plus both
    log files render log messages as Rich markup — unescaped, the bracketed
    text is parsed as a style tag and silently eaten. ``getMessage()`` cannot
    see that, so the LOGGED ARG is what this asserts.
    """
    from otto.config.coverage_settings import CoverageConfigError
    from otto.suite.run import RunOptions, resolve_coverage

    def _raise_config(_repos):
        raise CoverageConfigError("[coverage].hosts must be a string")

    monkeypatch.setattr("otto.coverage.instrumentation.detect_for_lab", _raise_config)
    monkeypatch.setattr(
        "otto.config.coverage_settings.get_cov_config", lambda repos: {"hosts": ["a"]}
    )

    with caplog.at_level("WARNING"):
        resolved = resolve_coverage(RunOptions(), [], command="otto test")

    assert resolved.cov is False
    record = caplog.records[-1]
    assert "coverage stays off" in record.getMessage()
    # The escaped form reaches the handler, so the bracket survives rendering.
    assert record.args[-1] == r"\[coverage].hosts must be a string"


def test_resolve_coverage_auto_warning_renders_its_brackets_literally(monkeypatch):
    """End of the escaping chain: through a real Rich markup handler, the
    ``[coverage]`` token is still in the rendered output."""
    import io
    import logging as _logging

    from rich.console import Console
    from rich.highlighter import NullHighlighter
    from rich.logging import RichHandler

    from otto.config.coverage_settings import CoverageConfigError
    from otto.suite.run import RunOptions, resolve_coverage
    from otto.suite.run import logger as run_logger

    def _raise_config(_repos):
        raise CoverageConfigError("[coverage].hosts must be a string")

    monkeypatch.setattr("otto.coverage.instrumentation.detect_for_lab", _raise_config)
    monkeypatch.setattr(
        "otto.config.coverage_settings.get_cov_config", lambda repos: {"hosts": ["a"]}
    )

    buf = io.StringIO()
    handler = RichHandler(
        console=Console(file=buf, width=200, force_terminal=False),
        markup=True,
        highlighter=NullHighlighter(),
        show_time=False,
        show_path=False,
    )
    monkeypatch.setattr(run_logger, "handlers", [handler])
    monkeypatch.setattr(run_logger, "propagate", False)
    monkeypatch.setattr(run_logger, "level", _logging.WARNING)

    assert resolve_coverage(RunOptions(), [], command="otto test").cov is False

    rendered = buf.getvalue()
    assert "[coverage].hosts must be a string" in rendered


def test_resolve_coverage_forced_on_propagates_an_empty_hosts_selection(monkeypatch):
    """Forced on, the same selection error is fatal — the user asked for coverage."""
    from otto.config.scope import EmptySelectionError
    from otto.suite.run import RunOptions, resolve_coverage

    monkeypatch.setattr("otto.coverage.instrumentation.detect_for_lab", _raise_empty_selection)
    monkeypatch.setattr(
        "otto.config.coverage_settings.get_cov_config", lambda repos: {"hosts": "sensor"}
    )

    with pytest.raises(EmptySelectionError):
        resolve_coverage(RunOptions(cov=True), [], command="otto test --cov")


# ── run_tests: --cov-report wiring ───────────────────────────────────────────


def _run_tests_report(tmp_path, monkeypatch, *, run_options, log_dir):
    """Drive run_tests with a stubbed repo and mocked coverage tail; return the report mock."""
    # No [coverage] section → legacy gcda-only report path (what these pin).
    _use_repo(monkeypatch, _stub_repo(tmp_path, sut_dir=log_dir, tests=[log_dir]))
    monkeypatch.setattr("pytest.main", pytest_main_returning())
    monkeypatch.setattr("otto.coverage.collect.collect_coverage", AsyncMock())
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", AsyncMock())
    _stub_instrumented_lab(monkeypatch)

    mock_store = MagicMock()
    mock_store.overall_pct.return_value = 50.0
    mock_store.file_count.return_value = 1
    mock_run_report = AsyncMock(return_value=mock_store)
    monkeypatch.setattr("otto.coverage.reporter.run_coverage_report", mock_run_report)

    run_tests([_ALPHA], run_options=run_options, output_dir=log_dir)
    return mock_run_report


def test_run_tests_no_cov_report_means_no_call(tmp_path, monkeypatch):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    mock = _run_tests_report(
        tmp_path,
        monkeypatch,
        run_options=RunOptions(cov=True, cov_clean=False, cov_report=False),
        log_dir=log_dir,
    )
    mock.assert_not_called()


def test_run_tests_default_report_dir_under_output_dir(tmp_path, monkeypatch):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    mock = _run_tests_report(
        tmp_path,
        monkeypatch,
        run_options=RunOptions(cov=True, cov_clean=False, cov_report=True),
        log_dir=log_dir,
    )
    mock.assert_called_once()
    args = mock.call_args.args
    assert args[0] == [log_dir / "cov"]
    assert args[1] == log_dir / "cov_report"
    # Directory creation is run_coverage_report's own prepare_destination
    # call; it is mocked out here, so it is proven directly in
    # tests/unit/cov/test_report_inputs.py::test_run_coverage_report_creates_a_missing_output_dir
    # instead.


def test_run_tests_explicit_report_dir_and_project_name(tmp_path, monkeypatch):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    report_dir = tmp_path / "my_report"
    report_dir.mkdir()
    mock = _run_tests_report(
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


def test_run_tests_cov_report_into_reused_dir_warns_not_raises(tmp_path, monkeypatch, caplog):
    """A library run_tests(cov_report=True) into a pre-populated report dir warns and skips.

    Regression: _post_run_coverage's report-dir emptiness check used the CLI's
    typer-raising equivalent OUTSIDE the swallow, so a library run into a
    reused output_dir raised typer.BadParameter from a public library entrypoint.
    It now proves the swallow around a DestinationError from
    run_coverage_report itself (whose own prepare_destination call is the
    gate, given the pre-populated default report dir): a collision warns and
    skips the report, matching never-fail-a-successful-run. The warning
    speaks in RunOptions field names (cov_report_dir/overwrite_cov_report_dir),
    not run_coverage_report's own (output_dir/overwrite) — _post_run_coverage
    rebuilds the DestinationError in the caller's field names before logging
    it, the same translation the CLI does for its own flags.
    """
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    report_dir = log_dir / "cov_report"
    report_dir.mkdir()
    (report_dir / "stale.html").write_text("stale from a previous run")

    _use_repo(monkeypatch, _stub_repo(tmp_path, sut_dir=log_dir, tests=[log_dir]))
    monkeypatch.setattr("pytest.main", pytest_main_returning())
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", AsyncMock())
    _stub_instrumented_lab(monkeypatch)
    # cov_report=True now forces cov=True too (construction-time rule), so the
    # fetch machinery runs; stub it out — this test's subject is the report path.
    monkeypatch.setattr("otto.coverage.collect.collect_coverage", AsyncMock())
    # run_coverage_report itself is NOT mocked: its own prepare_destination
    # call is the gate under test. _run_legacy_report is mocked only to prove
    # it is never reached (the gate raises before it).
    mock_legacy = AsyncMock()
    monkeypatch.setattr("otto.coverage.reporter._run_legacy_report", mock_legacy)

    with caplog.at_level("WARNING"):
        result = run_tests(
            [_ALPHA],
            run_options=RunOptions(cov_clean=False, cov_report=True),
            output_dir=log_dir,
        )
    # Completed with a result — no typer exception escaped.
    assert isinstance(result, SuiteRunResult)
    assert result.passed
    # The report was skipped: the dir collision was swallowed before rendering.
    mock_legacy.assert_not_called()
    assert any(
        "Coverage report generation failed" in r.getMessage()
        and "cov_report_dir target" in r.getMessage()
        and "overwrite_cov_report_dir" in r.getMessage()
        for r in caplog.records
    )
    # We refused to clear — the stale artifact is preserved.
    assert (report_dir / "stale.html").exists()


def test_run_tests_cov_dir_override_used_as_report_source(tmp_path, monkeypatch):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    cov_dir = tmp_path / "custom_cov"
    cov_dir.mkdir()
    mock = _run_tests_report(
        tmp_path,
        monkeypatch,
        run_options=RunOptions(cov=True, cov_clean=False, cov_report=True, cov_dir=cov_dir),
        log_dir=log_dir,
    )
    mock.assert_called_once()
    args = mock.call_args.args
    assert args[0] == [cov_dir]


# ── run_tests / _post_run_coverage: [coverage.tickets] wiring ───────────────
#
# Task 7 wired ticket_spec only into `otto cov report`'s path (resolved via
# resolve_report_inputs); `otto test --cov-report` went through
# _post_run_coverage, which never
# called load_ticket_spec, so its store never carried ticket data no matter
# what [coverage.tickets] said — and --cov-tickets-json there would have hit
# build_ticket_export's own loud-fail. These two tests close that gap: one
# pins the settings -> run_coverage_report(inputs.ticket_spec) wiring
# (mirrors TestCovReportCollectionModel.test_ticket_spec_threaded_from_settings
# in tests/unit/cli/test_cov.py); the other proves it end to end with a real
# git commit, a real (non-mocked) run_coverage_report, and a real
# --cov-tickets-json write.


def test_run_tests_ticket_spec_threaded_from_settings(tmp_path, monkeypatch):
    """[coverage.tickets] on the repo's settings must reach
    run_coverage_report's ReportInputs.ticket_spec via the otto-test path,
    exactly as it already does via otto cov report's resolve_report_inputs."""
    log_dir = tmp_path / "log"
    log_dir.mkdir()

    settings = {
        "coverage": {
            "tiers": {"system": {"kind": "e2e", "precedence": 1}},
            "tickets": {"pattern": r"[A-Z]{2,10}-[0-9]+"},
        }
    }
    _use_repo(
        monkeypatch, _stub_repo(tmp_path, sut_dir=log_dir, tests=[log_dir], settings=settings)
    )
    monkeypatch.setattr("pytest.main", pytest_main_returning())
    monkeypatch.setattr("otto.coverage.collect.collect_coverage", AsyncMock())
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", AsyncMock())
    _stub_instrumented_lab(monkeypatch)

    mock_store = MagicMock()
    mock_store.overall_pct.return_value = 50.0
    mock_store.file_count.return_value = 1
    mock_run_report = AsyncMock(return_value=mock_store)
    monkeypatch.setattr("otto.coverage.reporter.run_coverage_report", mock_run_report)

    run_tests(
        [_ALPHA],
        run_options=RunOptions(cov=True, cov_clean=False, cov_report=True),
        output_dir=log_dir,
    )

    mock_run_report.assert_called_once()
    ticket_spec = mock_run_report.call_args.args[2].ticket_spec
    assert ticket_spec is not None
    assert ticket_spec.extract("fix PROJ-7") == ["PROJ-7"]


def test_run_tests_no_coverage_section_leaves_ticket_spec_none(tmp_path, monkeypatch):
    """A repo with no [coverage] section resolves ticket_spec=None (the
    feature-absent default) rather than raising — mirrors the legacy
    gcda-only report path staying unchanged."""
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    mock = _run_tests_report(
        tmp_path,
        monkeypatch,
        run_options=RunOptions(cov=True, cov_clean=False, cov_report=True),
        log_dir=log_dir,
    )
    assert mock.call_args.args[2].ticket_spec is None


def test_post_run_coverage_populates_ticket_data_end_to_end(tmp_path, monkeypatch):
    """A real git repo whose sole commit names PROJ-7, harvested through a
    real (non-mocked) run_coverage_report and written via --cov-tickets-json,
    must produce a tickets.json naming PROJ-7 — proving the otto-test path
    now populates ticket data, not just that a mock received a kwarg."""
    import asyncio
    import json

    from otto.coverage.merge import merger as merger_mod
    from otto.suite.run import _post_run_coverage

    sut = TmpGitRepo(tmp_path / "sut")
    repo_root = sut.root
    sut.write("f.c", "int a;\nint b;\n")
    sut.commit("fix PROJ-7")

    hdir = tmp_path / "unit_build"
    hdir.mkdir()
    (hdir / "f.gcda").write_bytes(b"")
    (hdir / "f.gcno").write_bytes(b"")

    async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
        output.write_text(f"TN:\nSF:{repo_root / 'f.c'}\nDA:1,5\nend_of_record\n")
        return output

    monkeypatch.setattr(merger_mod.LcovMerger, "capture", fake_capture)
    # cov_tickets_json now forces cov=True too (construction-time rule), so the
    # fetch machinery runs; stub it out — this test's subject is ticket harvesting
    # from the report path, not collection.
    monkeypatch.setattr("otto.coverage.collect.collect_coverage", AsyncMock())

    repo = MagicMock()
    repo.sut_dir = repo_root
    repo.name = "repo"
    repo.settings = {
        "coverage": {
            "tiers": {"unit": {"kind": "unit", "precedence": 1, "harvest_dirs": [str(hdir)]}},
            "tickets": {"pattern": r"[A-Z]{2,10}-[0-9]+"},
        }
    }

    log_dir = tmp_path / "log"
    log_dir.mkdir()
    tickets_json = tmp_path / "tickets.json"

    opts = RunOptions(
        cov_clean=False,
        cov_report=True,
        cov_tickets_json=tickets_json,
    )
    asyncio.run(_post_run_coverage([repo], log_dir, opts))

    assert tickets_json.exists(), (
        "tickets.json was not written — ticket_spec never reached the store"
    )
    payload = json.loads(tickets_json.read_text())
    assert [t["id"] for t in payload["tickets"]] == ["PROJ-7"]
    assert payload["tickets"][0]["lines"]["owned"] >= 1


# ── run_tests / _post_run_coverage: overrides wiring ─────────────────────────
#
# Task 7 wired the override file into `otto cov report`'s path (resolved via
# resolve_report_inputs) and into `otto test --cov-report`'s
# _post_run_coverage — but that second wiring had
# no dedicated test, exactly the gap the [coverage.tickets] comment block
# above once described for ticket_spec. These two mirror that pair: one pins
# the settings -> run_coverage_report(inputs.overrides) wiring for a
# well-formed file; the other proves a malformed file logs a warning and
# never fails an otherwise-successful run — the entire reason
# resolve_report_inputs was placed inside _post_run_coverage's existing
# try/except swallow.


def test_run_tests_overrides_threaded_from_settings(tmp_path, monkeypatch):
    """A well-formed override file on the repo's settings must reach
    run_coverage_report's ReportInputs.overrides via the otto-test path,
    exactly as it already does via otto cov report's resolve_report_inputs."""
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    sut = TmpGitRepo(tmp_path / "sut")
    repo_root = sut.root
    sut.write("f.c", "int a;\n")
    sha = sut.commit("fix #1")

    overrides_dir = repo_root / ".otto"
    overrides_dir.mkdir()
    (overrides_dir / "coverage-overrides.toml").write_text(
        f'[[bench]]\ncommit = "{sha}"\nreason = "manual pass"\n'
    )

    settings = {
        "coverage": {
            "tiers": {
                "system": {"kind": "e2e", "precedence": 1},
                "bench": {"kind": "manual", "precedence": 2},
            },
            "tickets": {"pattern": "#(?P<n>[0-9]+)"},
        }
    }
    _use_repo(
        monkeypatch, _stub_repo(tmp_path, sut_dir=repo_root, tests=[log_dir], settings=settings)
    )
    monkeypatch.setattr("pytest.main", pytest_main_returning())
    monkeypatch.setattr("otto.coverage.collect.collect_coverage", AsyncMock())
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", AsyncMock())
    _stub_instrumented_lab(monkeypatch)

    mock_store = MagicMock()
    mock_store.overall_pct.return_value = 50.0
    mock_store.file_count.return_value = 1
    mock_run_report = AsyncMock(return_value=mock_store)
    monkeypatch.setattr("otto.coverage.reporter.run_coverage_report", mock_run_report)

    run_tests(
        [_ALPHA],
        run_options=RunOptions(cov=True, cov_clean=False, cov_report=True),
        output_dir=log_dir,
    )

    mock_run_report.assert_called_once()
    overrides = mock_run_report.call_args.args[2].overrides
    assert overrides is not None
    assert [e.key for e in overrides.asserted] == [f"commit:{sha}"]


def test_run_tests_malformed_overrides_file_warns_and_run_still_succeeds(
    tmp_path, monkeypatch, caplog
):
    """A malformed override file must not fail an otherwise-successful test
    run: load_override_config sits inside _post_run_coverage's existing
    try/except swallow (moved there specifically for this reason), so a bad
    file warns and the run still passes — the same never-fail-a-successful-
    run contract test_run_tests_cov_report_into_reused_dir_warns_not_raises
    pins for the report-dir collision case."""
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    repo_root = tmp_path / "sut"
    repo_root.mkdir()
    overrides_dir = repo_root / ".otto"
    overrides_dir.mkdir()
    (overrides_dir / "coverage-overrides.toml").write_text("not valid toml {{{")

    settings = {
        "coverage": {
            "tiers": {"bench": {"kind": "manual", "precedence": 1}},
            "tickets": {"pattern": "#(?P<n>[0-9]+)"},
        }
    }
    _use_repo(
        monkeypatch, _stub_repo(tmp_path, sut_dir=repo_root, tests=[log_dir], settings=settings)
    )
    monkeypatch.setattr("pytest.main", pytest_main_returning())
    monkeypatch.setattr("otto.coverage.collect.collect_coverage", AsyncMock())
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", AsyncMock())
    _stub_instrumented_lab(monkeypatch)
    mock_run_report = AsyncMock()
    monkeypatch.setattr("otto.coverage.reporter.run_coverage_report", mock_run_report)

    with caplog.at_level("WARNING"):
        result = run_tests(
            [_ALPHA],
            run_options=RunOptions(cov=True, cov_clean=False, cov_report=True),
            output_dir=log_dir,
        )

    assert isinstance(result, SuiteRunResult)
    assert result.passed
    mock_run_report.assert_not_called()
    assert any("not valid TOML" in r.getMessage() for r in caplog.records)


def test_run_tests_raises_value_error_when_nothing_matches(monkeypatch):
    """A name selection that matches nothing raises ValueError, not typer.Exit.

    No repos means no test universe to search: there is nothing to suggest,
    so run_tests() raises its "nothing to run" error (a ValueError) rather
    than a did-you-mean UnknownSelectionError, matching the library's
    no-typer contract.
    """
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)

    with pytest.raises(ValueError, match="No tests matched"):
        run_tests(["test_nonexistent_zzz"])


def test_run_tests_no_match_raises_no_tests_matched_error(monkeypatch):
    """The no-match case raises the specific NoTestsMatchedError, not a bare ValueError.

    The dedicated subclass lets the CLI adapter catch *only* the no-match case,
    so an unrelated pipeline ValueError can never be misreported as "No tests
    matched the selection."
    """
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)

    with pytest.raises(NoTestsMatchedError, match="No tests matched"):
        run_tests(["test_nonexistent_zzz"])


def test_run_tests_empty_options_raises(monkeypatch):
    """No names AND no markers must refuse, not run every test.

    The CLI guards this (it refuses at parse time without a name or -m);
    the library must guard it too so a bare run_tests() can never silently
    match every test in every repo.
    """
    import otto.config

    # Guard fires before get_repos, but stub it so a regression can't run pytest.
    monkeypatch.setattr(otto.config, "get_repos", list)

    with pytest.raises(ValueError, match=r"at least one test name or run_options\.markers"):
        run_tests()


def test_run_tests_marker_alone_raises_when_no_repo_matches(monkeypatch):
    """The -m-alone path funnels through the same "nothing matched" ValueError."""
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)

    with pytest.raises(ValueError, match="No tests matched"):
        run_tests(run_options=RunOptions(markers="not-a-real-marker"))


def test_run_tests_typo_raises_unknown_selection_error(sut_repo, tmp_path):
    """A typo against a real test universe raises the library's own exception.

    UnknownSelectionError (never typer.BadParameter — the library speaks
    library exceptions) propagates from run_tests, carrying the did-you-mean
    message and the param_hint the CLI adapter needs to reconstruct an
    identical typer.BadParameter.
    """
    from otto.suite.selection import UnknownSelectionError

    sut_repo(files={"tests/test_t.py": "def test_alpha():\n    pass\n"})

    with pytest.raises(UnknownSelectionError, match="did you mean: test_alpha") as excinfo:
        run_tests(["test_alpah"], output_dir=tmp_path / "out")
    assert excinfo.value.param_hint == "NAMES"


def test_run_tests_returns_result_single_repo(tmp_path, monkeypatch):
    """A single repo runs one pytest session and returns its junit path."""
    _use_repo(monkeypatch, _stub_repo(tmp_path))
    monkeypatch.setattr("pytest.main", pytest_main_returning())

    result = run_tests([_ALPHA], output_dir=tmp_path)
    assert isinstance(result, SuiteRunResult)
    assert result.exit_code == 0
    assert result.junit_paths == [tmp_path / "junit.xml"]


def _captured_layouts(monkeypatch: pytest.MonkeyPatch) -> list:
    """Stub ``pytest.main`` to return OK; record each running session's fixtures-plugin layout.

    ``plugins`` is a keyword pytest.main receives from ``_run_pytest_session`` --
    one ``OttoFixturesPlugin`` per session -- so its ``_layout`` is exactly what
    that session's ``module_dir``/``test_dir`` fixtures will resolve against:
    ``.root`` for where artifacts land, ``.test_roots`` for the directories a
    module's path is mirrored from. A ``--collect-only`` session runs no
    test and is left out. The stub's sessions record no file: several cold
    repos asked for a name would each be collected first, and find none.
    Returns the list the stub appends to, in call order.
    """
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    layouts: list = []

    def _fake_main(args: list[str], plugins: tuple = (), **kwargs: object) -> object:
        del kwargs
        collected(plugins)
        if "--collect-only" not in args:
            layouts.extend(p._layout for p in plugins if isinstance(p, OttoFixturesPlugin))
        return pytest.ExitCode.OK

    monkeypatch.setattr("pytest.main", _fake_main)
    return layouts


def test_run_tests_multi_repo_junit_fan_out(tmp_path, monkeypatch):
    """Two searched repos fan the default junit name out to junit_<repo>.xml each, and each
    session's ArtifactLayout root gets the repo layer (spec §5.3) and mirrors module paths
    from THAT repo's own test roots, never the other repo's."""
    import otto.config

    repos = [
        _stub_repo(
            tmp_path,
            name=name,
            sut_dir=tmp_path / name,
            tests=[tmp_path / name / "tests", tmp_path / name / "more_tests"],
        )
        for name in ("repoA", "repoB")
    ]
    monkeypatch.setattr(otto.config, "get_repos", lambda: repos)
    layouts = _captured_layouts(monkeypatch)

    # A marker run: each repo's one session, with no collection before it.
    result = run_tests(run_options=RunOptions(markers="smoke"), output_dir=tmp_path)
    assert result.exit_code == 0
    assert result.junit_paths == [
        tmp_path / "junit_repoA.xml",
        tmp_path / "junit_repoB.xml",
    ]
    assert [layout.root for layout in layouts] == [tmp_path / "repoA", tmp_path / "repoB"]
    assert [layout.test_roots for layout in layouts] == [
        [tmp_path / "repoA" / "tests", tmp_path / "repoA" / "more_tests"],
        [tmp_path / "repoB" / "tests", tmp_path / "repoB" / "more_tests"],
    ]


def test_run_tests_single_repo_layout_keeps_the_plain_output_dir(tmp_path, monkeypatch):
    """A run searching only ONE repo keeps the shorter layout: no repo layer."""
    _use_repo(monkeypatch, _stub_repo(tmp_path))
    layouts = _captured_layouts(monkeypatch)

    result = run_tests([_ALPHA], output_dir=tmp_path)
    assert result.exit_code == 0
    assert [layout.root for layout in layouts] == [tmp_path]


def test_run_tests_a_repo_with_no_test_directory_is_not_searched(tmp_path, monkeypatch):
    """Only a repo with a test directory on disk counts toward the repo layer, or gets a session."""
    import otto.config

    searched = _stub_repo(
        tmp_path, name="repoA", sut_dir=tmp_path / "repoA", tests=[tmp_path / "repoA" / "tests"]
    )
    bare = _stub_repo(tmp_path, name="repoB", sut_dir=tmp_path / "repoB", tests=[])
    bare.tests = [tmp_path / "repoB" / "tests"]
    monkeypatch.setattr(otto.config, "get_repos", lambda: [searched, bare])
    layouts = _captured_layouts(monkeypatch)

    result = run_tests([_ALPHA], output_dir=tmp_path)
    assert result.junit_paths == [tmp_path / "junit.xml"]
    assert [layout.root for layout in layouts] == [tmp_path]


# ── run_tests: context installation for library callers ─────────────────────
#
# Otto's own ctx fixture calls get_context() (module_dir/test_dir read their
# ArtifactLayout from the session's plugins instead); only the CLI preamble
# ever installs an OttoContext, so run_tests installs one for a library caller
# that has none. The first three tests run a REAL inner session over a
# generated repo, through run_tests itself.

_PROBE = """\
def test_marker(test_dir):
    # test_dir comes from the session's ArtifactLayout
    (test_dir / "marker.txt").write_text("ok")
"""


def test_run_tests_installs_minimal_context_when_none_active(sut_repo, tmp_path):
    """The documented library path works with NO active context (the CLI-preamble gap).

    run_tests must install a minimal lab-less OttoContext for the session so
    otto's own get_context()-backed fixtures work, and restore the prior
    (no-context) state afterwards.
    """
    from otto.context import _active, try_get_context

    sut_repo(files={"tests/test_ctx_probe_a.py": _PROBE})
    out = tmp_path / "out"
    out.mkdir()
    token = _active.set(None)  # hermetic: guarantee the no-context precondition
    try:
        assert try_get_context() is None
        result = run_tests(["test_marker"], output_dir=out)
        assert result.passed, f"exit_code={result.exit_code}"
        assert (out / "junit.xml").exists()
        # The per-test dir was created under output_dir via the run's
        # ArtifactLayout (module_dir = <output_dir>/<module stem>).
        assert (out / "test_ctx_probe_a" / "test_marker" / "marker.txt").exists()
        # The temporary context never leaks out of run_tests.
        assert try_get_context() is None
    finally:
        _active.reset(token)


def test_run_tests_sets_and_restores_output_dir_on_active_context(sut_repo, tmp_path):
    """An active context with output_dir=None gets log_dir for the session, then restored."""
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    sut_repo(files={"tests/test_ctx_probe_b.py": _PROBE})
    out = tmp_path / "out"
    out.mkdir()
    ctx = OttoContext(lab=Lab(name="test"))
    assert ctx.output_dir is None
    token = set_context(ctx)
    try:
        result = run_tests(["test_marker"], output_dir=out)
        assert result.passed, f"exit_code={result.exit_code}"
        assert list(out.rglob("marker.txt")), f"no per-test marker under {out}"
        # The session-scoped assignment is rolled back afterwards.
        assert ctx.output_dir is None
    finally:
        reset_context(token)


def test_run_tests_leaves_active_context_output_dir_untouched(sut_repo, tmp_path):
    """A context that already has an output_dir is never mutated by run_tests."""
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    sut_repo(files={"tests/test_ctx_probe_c.py": _PROBE})
    ctx_dir = tmp_path / "ctx_dir"
    ctx_dir.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    ctx = OttoContext(lab=Lab(name="test"), output_dir=ctx_dir)
    token = set_context(ctx)
    try:
        result = run_tests(["test_marker"], output_dir=out)
        assert result.passed, f"exit_code={result.exit_code}"
        # junit and the artifact layout honor the explicit output_dir; the
        # context's own output_dir is left exactly as the caller set it.
        assert (out / "junit.xml").exists()
        assert ctx.output_dir == ctx_dir
    finally:
        reset_context(token)


def test_run_tests_installs_and_restores_minimal_context(tmp_path, monkeypatch):
    """The installed context is the LIBRARY_LAB_NAME sentinel lab, pointed at the output dir."""
    from otto.context import LIBRARY_LAB_NAME, _active, try_get_context

    _use_repo(monkeypatch, _stub_repo(tmp_path))
    captured: dict = {}

    def fake_main(_args, plugins=(), **_kw):
        collected(plugins)
        ctx = try_get_context()
        captured["lab_name"] = ctx.lab.name if ctx is not None else None
        captured["output_dir"] = ctx.output_dir if ctx is not None else None
        return pytest.ExitCode.OK

    monkeypatch.setattr("pytest.main", fake_main)

    token = _active.set(None)  # hermetic: guarantee the no-context precondition
    try:
        assert try_get_context() is None
        result = run_tests([_ALPHA], output_dir=tmp_path)
        assert result.passed, f"exit_code={result.exit_code}"
        # A context WAS installed for the duration of the session...
        assert captured["lab_name"] == LIBRARY_LAB_NAME
        assert captured["output_dir"] == tmp_path
        # ...and torn down afterwards.
        assert try_get_context() is None
    finally:
        _active.reset(token)


@pytest.mark.parametrize("session_raises", [False, True], ids=["returns", "raises"])
def test_run_tests_restores_the_callers_verb_binding(tmp_path, monkeypatch, session_raises):
    """``run_tests`` binds ``test`` on the caller's active context for the run only.

    An enclosing ``otto run`` bound ``run``; after ``run_tests`` returns -- or
    raises out of its session -- the context still answers for ``run``: the
    same verb, the same built instances, the same parsed flags.
    """
    from otto import options
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context
    from otto.params import register_options

    @options
    class RunOpts:
        lab_env: str = "staging"

    @options
    class FirmwareOpts:
        fw: str = "latest"

    register_options(RunOpts, verbs=["run"])
    register_options(FirmwareOpts, verbs=["test"])
    _use_repo(monkeypatch, _stub_repo(tmp_path))
    seen: list = []

    def fake_main(_args, plugins=(), **_kwargs):
        from otto.context import get_context

        collected(plugins)
        seen.append(get_context().options(FirmwareOpts))
        if session_raises:
            raise RuntimeError("session blew up")
        return pytest.ExitCode.OK

    monkeypatch.setattr("pytest.main", fake_main)

    ctx = OttoContext(lab=Lab(name="test"))
    ctx.bind_verb_options("run", {"lab_env": "prod"})
    before = ctx.options(RunOpts)
    token = set_context(ctx)
    try:
        if session_raises:
            with pytest.raises(RuntimeError, match="session blew up"):
                run_tests(["test_alpha"], options=[FirmwareOpts(fw="2.1")], output_dir=tmp_path)
        else:
            run_tests(["test_alpha"], options=[FirmwareOpts(fw="2.1")], output_dir=tmp_path)
    finally:
        reset_context(token)
    # The run itself saw the test verb's options...
    assert [o.fw for o in seen] == ["2.1"]
    # ...and the caller gets its own binding back, untouched.
    assert ctx.verb == "run"
    assert ctx.options(RunOpts) is before
    assert ctx.verb_option_source().build(RunOpts).lab_env == "prod"


# ── _session_context hardening: restore on exception ─────────────────────────
#
# _run_pytest_session raising mid-session must not leak the temporary state
# _session_context installs — the finally in each branch must still run.


def _raising_session(monkeypatch) -> None:
    def _raise(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr("otto.suite.run._run_pytest_session", _raise)


def test_run_tests_restores_no_context_state_on_exception(tmp_path, monkeypatch):
    """No-active-context branch: an exception mid-session still resets the
    contextvar, leaving no active OttoContext behind."""
    from otto.context import _active, try_get_context

    _use_repo(monkeypatch, _stub_repo(tmp_path))
    _raising_session(monkeypatch)
    token = _active.set(None)  # hermetic: guarantee the no-context precondition
    try:
        assert try_get_context() is None
        with pytest.raises(RuntimeError, match="boom"):
            run_tests([_ALPHA], output_dir=tmp_path)
        assert try_get_context() is None
    finally:
        _active.reset(token)


def test_run_tests_restores_prior_output_dir_on_exception(tmp_path, monkeypatch):
    """Active-context-with-no-output_dir branch: an exception mid-session still
    rolls back the session-scoped output_dir assignment to its prior value."""
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    _use_repo(monkeypatch, _stub_repo(tmp_path))
    _raising_session(monkeypatch)
    ctx = OttoContext(lab=Lab(name="test"))
    assert ctx.output_dir is None
    token = set_context(ctx)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            run_tests([_ALPHA], output_dir=tmp_path)
        assert ctx.output_dir is None
    finally:
        reset_context(token)


# ── run_tests: cov_dir empty/overwrite guard ─────────────────────────────────


def test_run_tests_nonempty_cov_dir_without_overwrite_raises(tmp_path, monkeypatch):
    """A non-empty ``cov_dir`` without ``overwrite_cov_dir`` raises before any
    host I/O — the same guard the CLI's ``--cov-dir``/``--overwrite-cov-dir``
    pair already enforces, applied to a library caller that hands
    ``RunOptions.cov_dir`` straight to ``run_tests``."""
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    cov_dir = tmp_path / "cov_dir"
    cov_dir.mkdir()
    (cov_dir / "stale.txt").write_text("stale")

    _use_repo(monkeypatch, _stub_repo(tmp_path))
    monkeypatch.setattr("pytest.main", pytest_main_returning())
    clean_mock = AsyncMock()
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", clean_mock)
    _stub_instrumented_lab(monkeypatch)

    with pytest.raises(DestinationError, match="cov_dir target"):
        run_tests([_ALPHA], run_options=RunOptions(cov=True, cov_dir=cov_dir), output_dir=log_dir)
    # Failed before the pre-run remote clean ever ran, and the stale contents
    # were never touched.
    clean_mock.assert_not_awaited()
    assert (cov_dir / "stale.txt").exists()


def test_run_tests_refuses_a_bad_report_dir_before_the_instrumentation_scan(tmp_path, monkeypatch):
    """``prepare_run`` runs ahead of ``resolve_coverage`` in ``run_tests``: a bad
    ``cov_report_dir`` must fail before the instrumentation scan, not after it.

    The repo double is given a real ``[coverage]`` table so ``resolve_coverage``
    would genuinely reach ``detect_for_lab`` next (rather than refusing earlier
    on its own "no [coverage] table" guard) — otherwise the scan mock would be
    unreachable for a reason that has nothing to do with preflight ordering,
    and ``scan.assert_not_called()`` could never fail even if the order regressed.
    """
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    (report_dir / "stale.html").write_text("stale")
    _use_repo(
        monkeypatch,
        _stub_repo(
            tmp_path,
            settings={"coverage": {"tiers": {"system": {"kind": "e2e", "precedence": 1}}}},
        ),
    )
    scan = MagicMock(side_effect=AssertionError("the scan must not run"))
    monkeypatch.setattr("otto.coverage.instrumentation.detect_for_lab", scan)
    with pytest.raises(DestinationError, match="cov_report_dir target"):
        run_tests(["test_x"], run_options=RunOptions(cov_report_dir=report_dir), output_dir=log_dir)
    scan.assert_not_called()


def test_run_tests_overwrite_cov_dir_true_clears_and_proceeds(tmp_path, monkeypatch):
    """``overwrite_cov_dir=True`` clears a non-empty ``cov_dir`` and the run proceeds."""
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    cov_dir = tmp_path / "cov_dir"
    cov_dir.mkdir()
    (cov_dir / "stale.txt").write_text("stale")

    _use_repo(monkeypatch, _stub_repo(tmp_path))
    monkeypatch.setattr("pytest.main", pytest_main_returning())
    monkeypatch.setattr("otto.coverage.collect.clean_remote_gcda", AsyncMock())
    _stub_instrumented_lab(monkeypatch)
    monkeypatch.setattr("otto.coverage.collect.collect_coverage", AsyncMock())

    result = run_tests(
        [_ALPHA],
        run_options=RunOptions(cov=True, cov_dir=cov_dir, cov_clean=False, overwrite_cov_dir=True),
        output_dir=log_dir,
    )
    assert result.passed
    assert not (cov_dir / "stale.txt").exists()


def test_run_tests_abandons_hosts_left_on_the_inner_sessions_closed_loops(tmp_path, monkeypatch):
    """A host that connected on one of pytest's loops, which then closed unswept,
    holds state no loop can drive. After the session run_tests drops it
    (``abandon_closed_loops``) and never attempts a cross-loop close."""
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context, try_get_context

    _use_repo(monkeypatch, _stub_repo(tmp_path))
    events: "list[str]" = []

    class _SuiteHost:
        id = "bed1"

        def __init__(self) -> None:
            self._owner_loop: "asyncio.AbstractEventLoop | None" = None

        def _drop_dead_connections(self) -> None:
            events.append("drop")

        async def close(self) -> None:
            events.append("close")

    host = _SuiteHost()

    def fake_pytest_main(args, plugins=(), **_kw):
        collected(plugins)
        # A test connects the host on pytest's loop, which closes with nobody sweeping it.
        active = try_get_context()
        assert active is not None
        loop = asyncio.new_event_loop()
        host._owner_loop = loop
        active.scope_for(loop).register(host)
        loop.close()
        return pytest.ExitCode.OK

    monkeypatch.setattr("pytest.main", fake_pytest_main)

    token = set_context(OttoContext(lab=Lab(name="test")))
    try:
        run_tests([_ALPHA], output_dir=tmp_path)
    finally:
        reset_context(token)

    assert events == ["drop"], "a host on a closed loop is abandoned, never closed cross-loop"
    assert host._owner_loop is None


# ── One session-wide event loop (spec §6.6) ──────────────────────────────────
#
# These run REAL inner sessions through run_tests, i.e. through
# _run_pytest_session's base_args — the only thing that proves the loop-scope
# defaults `otto test` sets are the ones in effect. Each probe writes the id of
# the running loop, tagged, into a file the outer test reads back.

_LOOP_PROBE_HEADER = '''"""Loop-identity probe (real otto test args)."""

import asyncio
import pathlib

import pytest
import pytest_asyncio

LOOPS = pathlib.Path({loops!r})


def _record(tag: str) -> None:
    with LOOPS.open("a") as f:
        f.write(f"{{tag}} {{id(asyncio.get_running_loop())}}\\n")
'''

_ONE_LOOP_SUITE = (
    _LOOP_PROBE_HEADER
    + """

class TestLoopProbe:
    @pytest_asyncio.fixture(scope="class", autouse=True)
    @classmethod
    async def suite_loop(cls):
        _record("class-setup")
        yield
        _record("class-teardown")

    @pytest_asyncio.fixture
    async def per_test(self):
        _record("function-fixture")

    async def test_one(self, per_test) -> None:
        _record("test_one")

    async def test_two(self) -> None:
        _record("test_two")
"""
)

_ESCAPE_HATCH_SUITE = (
    _LOOP_PROBE_HEADER
    + """

@pytest.mark.asyncio(loop_scope="function")
class TestLoopProbe:
    @pytest_asyncio.fixture(scope="class", autouse=True)
    @classmethod
    async def suite_loop(cls):
        _record("class-setup")
        yield
        _record("class-teardown")

    async def test_one(self) -> None:
        _record("test_one")

    async def test_two(self) -> None:
        _record("test_two")
"""
)

_PINNED_SESSION_SUITE = (
    _LOOP_PROBE_HEADER
    + """

@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pinned_session():
    yield "ok"


class TestLoopProbe:
    async def test_uses_it(self, pinned_session) -> None:
        assert pinned_session == "ok"
"""
)

_UNPINNED_SESSION_SUITE = (
    _LOOP_PROBE_HEADER
    + """

@pytest_asyncio.fixture(scope="session")
async def unpinned_session():
    _record("session-fixture")
    yield "ok"


class TestLoopProbe:
    async def test_uses_it(self, unpinned_session) -> None:
        _record("test")
        assert unpinned_session == "ok"
"""
)


def _loop_ids(loops_file: Path) -> dict[str, set[int]]:
    """``{tag: {loop ids}}`` from a probe's LOOPS file."""
    out: dict[str, set[int]] = {}
    for line in loops_file.read_text().splitlines():
        tag, loop_id = line.split()
        out.setdefault(tag, set()).add(int(loop_id))
    return out


def _run_loop_probe(sut_repo, tmp_path: Path, src: str):
    """Run *src* (as tests/test_loop_probe.py) through run_tests; return (result, loops, out)."""
    out = tmp_path / "out"
    out.mkdir()
    loops = tmp_path / "loops.txt"
    sut_repo(files={"tests/test_loop_probe.py": src.format(loops=str(loops))})
    return run_tests(["TestLoopProbe"], output_dir=out), loops, out


def test_run_tests_shares_one_loop_across_class_fixture_tests_and_function_fixture(
    sut_repo, tmp_path
):
    """Spec §6.6: class fixture (setup AND teardown), both tests and an unpinned
    function fixture report ONE loop id, the session's. Red if either
    ASYNCIO_LOOP_ARGS entry is dropped from _run_pytest_session's base args."""
    result, loops, _ = _run_loop_probe(sut_repo, tmp_path, _ONE_LOOP_SUITE)
    assert result.passed, f"exit_code={result.exit_code}"
    ids = _loop_ids(loops)
    expected_tags = {"class-setup", "class-teardown", "function-fixture", "test_one", "test_two"}
    assert set(ids) == expected_tags, ids
    assert len(set().union(*ids.values())) == 1, f"more than one loop: {ids}"


def test_run_tests_escape_hatch_gives_each_test_its_own_loop(sut_repo, tmp_path):
    """Spec §3.4: `@pytest.mark.asyncio(loop_scope="function")` on the class opts out.

    Comparing id(test_one's loop) to id(test_two's loop) directly is unsound:
    their loops don't overlap in time, and CPython can hand the second one the
    first one's now-freed address, so the test could go red on correct
    behaviour. The class's own autouse class fixture is unpinned, so under
    ASYNCIO_LOOP_ARGS it still runs on the SESSION loop, alive for the whole
    run — a live anchor whose id neither test's (closed before the next
    opens) function loop can ever recycle. Each test's loop must differ from
    that live session loop.
    """
    result, loops, _ = _run_loop_probe(sut_repo, tmp_path, _ESCAPE_HATCH_SUITE)
    assert result.passed, f"exit_code={result.exit_code}"
    ids = _loop_ids(loops)
    class_loop = ids["class-setup"]
    assert ids["test_one"].isdisjoint(class_loop), f"expected distinct loops: {ids}"
    assert ids["test_two"].isdisjoint(class_loop), f"expected distinct loops: {ids}"


def test_run_tests_pinned_session_fixture_is_usable_from_a_class_test(sut_repo, tmp_path):
    """A session-scoped async fixture that pins loop_scope="session" still works."""
    result, _, _ = _run_loop_probe(sut_repo, tmp_path, _PINNED_SESSION_SUITE)
    assert result.passed, f"exit_code={result.exit_code}"


def test_run_tests_unpinned_session_fixture_is_usable_from_a_class_test(sut_repo, tmp_path):
    """Spec §6.6: a session-scoped async fixture needs no loop_scope: it runs on the session loop.

    Under the old class-loop default this errored at setup with a ScopeMismatch
    on pytest-asyncio's ``_class_scoped_runner``."""
    result, loops, out = _run_loop_probe(sut_repo, tmp_path, _UNPINNED_SESSION_SUITE)
    assert result.passed, f"exit_code={result.exit_code}"
    assert "ScopeMismatch" not in (out / "junit.xml").read_text()
    ids = _loop_ids(loops)
    assert set(ids) == {"session-fixture", "test"}, ids
    assert ids["session-fixture"] == ids["test"], f"fixture and test on different loops: {ids}"


# ── ctx.cov: the resolved coverage decision is visible to the tests ─────────
#
# A test or fixture reads `ctx.cov` (or `get_context().cov`) to change
# behavior under coverage — e.g. keep .gcda files on a remote for the
# post-run fetch. It must carry the RESOLVED decision (auto mode included),
# hold for the whole session, and never leak past the run.


def _record_ctx_cov_during_session(monkeypatch, *, decision: bool) -> list[bool]:
    """Force resolve_coverage to *decision*; record ctx.cov as pytest.main sees it."""
    from otto.context import get_context

    seen: list[bool] = []

    def fake_main(_args, plugins=(), **_k):
        collected(plugins)
        seen.append(get_context().cov)
        return pytest.ExitCode.OK

    monkeypatch.setattr("pytest.main", fake_main)
    monkeypatch.setattr(
        "otto.suite.run.resolve_coverage",
        lambda opts, _repos, *, command: dataclasses.replace(opts, cov=decision),
    )
    monkeypatch.setattr("otto.suite.run._pre_run_cov_clean", AsyncMock())
    monkeypatch.setattr("otto.suite.run._post_run_coverage", AsyncMock())
    return seen


@pytest.mark.parametrize("decision", [True, False])
def test_run_tests_exposes_resolved_cov_on_context(tmp_path, monkeypatch, decision):
    """Auto mode (cov=None) resolved to *decision* is what the session reads."""
    from otto.context import try_get_context

    _use_repo(monkeypatch, _stub_repo(tmp_path))
    seen = _record_ctx_cov_during_session(monkeypatch, decision=decision)

    run_tests([_ALPHA], output_dir=tmp_path)
    assert seen == [decision]
    assert try_get_context() is None  # hermetic default: the library context is gone


def test_run_tests_restores_prior_cov_on_an_active_context(tmp_path, monkeypatch):
    """A caller's own context gets its cov back after the run."""
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    _use_repo(monkeypatch, _stub_repo(tmp_path))
    seen = _record_ctx_cov_during_session(monkeypatch, decision=True)

    ctx = OttoContext(lab=Lab(name="test"), output_dir=tmp_path)
    token = set_context(ctx)
    try:
        run_tests([_ALPHA], output_dir=tmp_path)
    finally:
        reset_context(token)
    assert seen == [True]
    assert ctx.cov_decision is None  # back to undecided: detection applies again
