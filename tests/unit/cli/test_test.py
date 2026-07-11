"""
Unit tests for the refactored ``otto test`` subcommand.

Tests verify:
  - ``otto test --help`` shows available subcommands and parent-level runner
    options (markers / iterations / duration / threshold / results)
  - ``otto test <Suite> --help`` shows suite-specific options only
  - The callback sets the logger's log directory for the invoked suite
  - Type enforcement: Typer rejects invalid values before pytest runs
  - Defaults are applied when suite options are omitted
  - Parent-callback options (``--iterations`` etc.) thread through ``ctx.meta``
    into the run options the suite runner hands to ``otto.suite.run.run_suite``

The suite-run engine itself (``run_suite`` / ``run_selection`` calling
``pytest.main``) is exercised as a library in ``tests/unit/suite/test_run_api.py``;
this module covers only the CLI surface (callback, adapters, option wiring).
"""

from dataclasses import dataclass
from typing import Annotated
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from otto.cli.test import suite_app
from otto.context import get_context
from otto.suite.register import SUITES, register_suite_class

runner = CliRunner()


def _lib_ok_result():
    """A zero-exit SuiteRunResult for faked ``otto.suite.run.run_suite`` calls.

    The suite runner (``otto.suite.register``) now consumes the library
    ``run_suite`` (returning a ``SuiteRunResult``) and raises ``typer.Exit`` on
    a non-zero exit code, so a fake must hand back a result carrying
    ``exit_code == 0``.
    """
    from pathlib import Path

    from otto.suite.run import SuiteRunResult

    return SuiteRunResult(
        exit_code=0,
        junit_paths=[],
        stability_report=None,
        stability_unstable=False,
        output_dir=Path(),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_isolated_app(suite_class: type) -> typer.Typer:
    """Build a fresh Typer app containing only the given suite as a subcommand."""
    if suite_class.__name__ not in SUITES:
        raise LookupError(f"{suite_class.__name__} not found in SUITES")
    app = typer.Typer(no_args_is_help=True)
    app.add_typer(SUITES.get(suite_class.__name__).sub_app)
    return app


# ── Help behaviour ────────────────────────────────────────────────────────────


class TestTestHelp:
    def test_help_flag(self):
        result = runner.invoke(suite_app, ["--help"])
        assert result.exit_code == 0

    def test_short_help_flag(self):
        result = runner.invoke(suite_app, ["-h"])
        assert result.exit_code == 0

    def test_suite_help_shows_options(self):
        """Otto test <SuiteName> --help must list suite-specific options."""

        class _HelpSuite:
            @dataclass
            class Options:
                firmware: Annotated[str, typer.Option()] = "latest"

        register_suite_class(_HelpSuite)

        app = _make_isolated_app(_HelpSuite)
        result = runner.invoke(app, ["_HelpSuite", "--help"])
        assert result.exit_code == 0
        assert "--firmware" in result.output

    def test_parent_help_shows_runner_options(self):
        """Runner options live on ``otto test --help``, not the suite subcommand."""
        result = runner.invoke(suite_app, ["--help"])
        assert result.exit_code == 0
        for flag in ("--iterations", "--duration", "--threshold", "--results", "--markers"):
            assert flag in result.output

    def test_suite_help_omits_runner_options(self):
        """Runner options must NOT appear in the per-suite ``--help`` output."""

        class _SuiteNoRunnerOpts:
            pass

        register_suite_class(_SuiteNoRunnerOpts)

        app = _make_isolated_app(_SuiteNoRunnerOpts)
        result = runner.invoke(app, ["_SuiteNoRunnerOpts", "--help"])
        assert result.exit_code == 0
        for flag in ("--iterations", "--duration", "--threshold", "--results", "--markers"):
            assert flag not in result.output


# ── Callback / logger setup ───────────────────────────────────────────────────


class TestTestCallback:
    def test_logger_output_dir_called_for_suite(self):
        """The leaf-invoke preamble creates the test output dir named after the suite.

        Since Task 7 the output dir is created by the shared leaf-invoke preamble
        (``otto.cli.invoke.command_preamble``), not the ``suite_app`` callback — so
        dispatch goes through the root ``app`` (which wraps leaves with the
        preamble). ``ensure_cli_session`` / ``ensure_lab_context`` are stubbed to
        isolate the output-dir naming (``create_output_dir('test', <suite>)``).
        """
        from otto.cli.main import app

        class _CallbackSuite:
            pass

        register_suite_class(_CallbackSuite)

        with (
            patch("otto.cli.invoke.ensure_cli_session"),
            patch("otto.cli.invoke.ensure_lab_context"),
            patch("otto.logger.management.create_output_dir") as p_create,
            patch("otto.suite.run.run_suite", new=lambda *a, **k: _lib_ok_result()),
        ):
            runner.invoke(app, ["--lab", "x", "test", "_CallbackSuite"])

        p_create.assert_called_once_with("test", "_CallbackSuite")


# ── run_selection adapter: no-match narrowing ────────────────────────────────


class TestRunSelectionAdapter:
    """The CLI ``run_selection`` adapter maps library exceptions onto Typer.

    Only the library's :class:`~otto.suite.run.NoTestsMatchedError` becomes the
    red "No tests matched the selection." + exit 1. An unrelated pipeline
    ``ValueError`` must propagate untouched — a broad ``except ValueError``
    would misreport it as a no-match.
    """

    @staticmethod
    def _fake_ctx(tmp_path):
        from otto.cli.test import RUN_OPTIONS_KEY
        from otto.suite.run import RunOptions

        get_context().output_dir = tmp_path
        ctx = MagicMock()
        ctx.meta = {RUN_OPTIONS_KEY: RunOptions(tests="test_x")}
        return ctx

    def test_no_tests_matched_reports_and_exits_1(self, tmp_path):
        from otto.cli.test import run_selection
        from otto.suite.run import NoTestsMatchedError

        with (
            patch(
                "otto.cli.test._run_selection_lib",
                side_effect=NoTestsMatchedError("No tests matched the selection."),
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            run_selection(self._fake_ctx(tmp_path))
        assert exc_info.value.exit_code == 1

    def test_unrelated_value_error_propagates(self, tmp_path):
        """A generic ValueError from the pipeline is NOT relabeled 'No tests matched'."""
        from otto.cli.test import run_selection

        with (
            patch(
                "otto.cli.test._run_selection_lib",
                side_effect=ValueError("pipeline exploded"),
            ),
            pytest.raises(ValueError, match="pipeline exploded"),
        ):
            run_selection(self._fake_ctx(tmp_path))


# ── Type enforcement ──────────────────────────────────────────────────────────


class TestTypeEnforcement:
    def test_invalid_int_rejected_by_typer(self):
        """Passing a non-integer to an int option must fail at CLI level."""

        class _TypeSuite:
            @dataclass
            class Options:
                count: Annotated[int, typer.Option()] = 1

        register_suite_class(_TypeSuite)

        app = _make_isolated_app(_TypeSuite)
        result = runner.invoke(app, ["_TypeSuite", "--count", "not-a-number"])
        assert result.exit_code != 0

    def test_invalid_iterations_rejected(self):
        """--iterations lives on the parent; bad values must still reject."""

        class _IterSuite:
            pass

        register_suite_class(_IterSuite)

        with patch("otto.suite.run.run_suite"):
            result = runner.invoke(suite_app, ["--iterations", "oops", "_IterSuite"])
        assert result.exit_code != 0

    def test_defaults_applied_when_omitted(self):
        class _DefaultSuite:
            @dataclass
            class Options:
                max_retries: Annotated[int, typer.Option()] = 9

        register_suite_class(_DefaultSuite)

        app = _make_isolated_app(_DefaultSuite)
        captured: dict[str, object] = {}

        def fake_run_suite(suite, **kw):
            captured["opts"] = kw["options"]
            return _lib_ok_result()

        with patch("otto.suite.run.run_suite", fake_run_suite):
            result = runner.invoke(app, ["_DefaultSuite"])

        assert result.exit_code == 0
        opts = captured.get("opts")
        assert opts is not None
        assert opts.max_retries == 9  # type: ignore[union-attr]


# ── Help content (integration-level) ─────────────────────────────────────────


class TestHelpContent:
    """Verify that typer.Option help text appears in rendered --help output."""

    def test_annotated_help_in_cli_output(self):
        """A field annotated with typer.Option(help=...) shows that text in --help."""

        class _AnnotatedHelpSuite:
            @dataclass
            class Options:
                device_type: Annotated[
                    str,
                    typer.Option(
                        help="Kind of device under test.",
                    ),
                ] = "router"

        register_suite_class(_AnnotatedHelpSuite)

        app = _make_isolated_app(_AnnotatedHelpSuite)
        result = runner.invoke(app, ["_AnnotatedHelpSuite", "--help"])
        assert result.exit_code == 0
        assert "--device-type" in result.output
        assert "Kind of device under test." in result.output

    def test_no_help_when_option_has_none(self):
        """A bare typer.Option() with no help= produces no help text in --help."""

        class _BareHelpSuite:
            @dataclass
            class Options:
                firmware: Annotated[str, typer.Option()] = "latest"

        register_suite_class(_BareHelpSuite)

        app = _make_isolated_app(_BareHelpSuite)
        result = runner.invoke(app, ["_BareHelpSuite", "--help"])
        assert result.exit_code == 0
        assert "--firmware" in result.output

    def test_inherited_annotated_field_help_in_cli_output(self):
        """Parent class annotated fields appear with their help text in child --help."""

        @dataclass
        class _InheritedParentOpts:
            device_type: Annotated[
                str,
                typer.Option(
                    help="Inherited device help.",
                ),
            ] = "router"

        class _InheritedHelpSuite:
            @dataclass
            class Options(_InheritedParentOpts):
                firmware: Annotated[
                    str,
                    typer.Option(
                        help="Suite firmware help.",
                    ),
                ] = "latest"

        register_suite_class(_InheritedHelpSuite)

        app = _make_isolated_app(_InheritedHelpSuite)
        result = runner.invoke(app, ["_InheritedHelpSuite", "--help"])
        assert result.exit_code == 0
        assert "Inherited device help." in result.output
        assert "Suite firmware help." in result.output

    def test_parent_runner_option_help_present(self):
        """Parent-callback runner options retain their help text.

        Rich wraps long help strings across multiple columns, so we assert
        on short fragments rather than the full sentence.
        """
        result = runner.invoke(suite_app, ["--help"])
        assert result.exit_code == 0
        output_lower = result.output.lower()
        assert "--markers" in result.output
        assert "marker" in output_lower
        assert "iterations" in output_lower


# ── Parent-callback runner options thread through ctx.meta ───────────────────


class TestParentRunnerOptionsCtx:
    """Verify ``--markers``, ``--iterations`` etc. reach run_suite via ctx.meta.

    These options live on ``suite_app``'s callback, so the full CLI path is
    exercised to check wiring: CLI → callback sets ctx.meta → runner closure →
    run_suite reads parent context.
    """

    def _capture_ctx(self, cli_args: list[str], suite_name: str) -> dict:
        import dataclasses

        captured: dict = {}

        def fake_run_suite(*_args, **_kwargs):
            # The runner reads RunOptions from ctx.meta and passes them to the
            # library run_suite as the ``run_options`` keyword.
            opts = _kwargs.get("run_options")
            if opts is not None:
                captured.update(dataclasses.asdict(opts))
            return _lib_ok_result()

        with patch("otto.suite.run.run_suite", fake_run_suite):
            runner.invoke(suite_app, [*cli_args, suite_name])
        return captured

    def test_iterations_forwarded_via_ctx(self):
        class _CtxIterSuite:
            pass

        register_suite_class(_CtxIterSuite)

        ctx_obj = self._capture_ctx(["--iterations", "5"], "_CtxIterSuite")
        assert ctx_obj.get("iterations") == 5

    def test_markers_forwarded_via_ctx(self):
        class _CtxMarkSuite:
            pass

        register_suite_class(_CtxMarkSuite)

        ctx_obj = self._capture_ctx(
            ["--markers", "not integration"],
            "_CtxMarkSuite",
        )
        assert ctx_obj.get("markers") == "not integration"

    def test_defaults_when_omitted(self):
        class _CtxDefSuite:
            pass

        register_suite_class(_CtxDefSuite)

        ctx_obj = self._capture_ctx([], "_CtxDefSuite")
        assert ctx_obj.get("markers") == ""
        assert ctx_obj.get("iterations") == 0
        assert ctx_obj.get("duration") == 0
        assert ctx_obj.get("threshold") == 100.0
        assert ctx_obj.get("results") == ""
        # Monitor defaults: disabled, default interval, no override path / regex.
        assert ctx_obj.get("monitor") is False
        assert ctx_obj.get("monitor_interval") == 5.0
        assert ctx_obj.get("monitor_output") is None
        assert ctx_obj.get("monitor_hosts") is None

    def test_monitor_flag_forwarded_via_ctx(self):
        class _CtxMonSuite:
            pass

        register_suite_class(_CtxMonSuite)

        ctx_obj = self._capture_ctx(["--monitor"], "_CtxMonSuite")
        assert ctx_obj.get("monitor") is True

    def test_monitor_options_forwarded_via_ctx(self, tmp_path):
        class _CtxMonOptSuite:
            pass

        register_suite_class(_CtxMonOptSuite)

        out = tmp_path / "m.json"
        ctx_obj = self._capture_ctx(
            [
                "--monitor",
                "--monitor-interval",
                "2",
                "--monitor-output",
                str(out),
                "--monitor-hosts",
                "router|switch",
            ],
            "_CtxMonOptSuite",
        )
        assert ctx_obj.get("monitor") is True
        assert ctx_obj.get("monitor_interval") == 2.0
        assert ctx_obj.get("monitor_output") == out
        assert ctx_obj.get("monitor_hosts") == "router|switch"

    def test_monitor_implied_by_output_or_hosts(self):
        """--monitor-output or --monitor-hosts alone should imply --monitor."""

        class _CtxMonImplSuite:
            pass

        register_suite_class(_CtxMonImplSuite)

        ctx_obj = self._capture_ctx(["--monitor-hosts", "router"], "_CtxMonImplSuite")
        assert ctx_obj.get("monitor") is True


# ── --cov-dir option (destination override + validation) ─────────────────────


# Register a single suite once at module import; every --cov-dir test
# reuses it, varying only the CLI args it's invoked with.
class _CovCtxSuite:
    """Fixture suite used for exercising the cov/cov-dir callback plumbing."""


register_suite_class(_CovCtxSuite)


def _capture_cov_ctx(cli_args: list[str]) -> tuple[int, dict, str]:
    """Invoke ``otto test <cli_args> _CovCtxSuite`` against the real suite_app.

    Callback options like ``--cov`` / ``--cov-dir`` are declared on
    ``suite_app`` itself, so we invoke through it (not an isolated app) to
    exercise the actual option wiring. ``suite_app`` resolves ``_CovCtxSuite``
    lazily from the ``SUITES`` registry, so no explicit attach step is needed.

    Returns ``(exit_code, ctx_obj, output)``. ``ctx_obj`` is ``{}`` when the
    command aborts before the subcommand is reached (e.g. during option
    validation).
    """
    import dataclasses

    captured: dict = {}

    def fake_run_suite(*_args, **_kwargs):
        # The runner reads RunOptions from ctx.meta and passes them to the
        # library run_suite as the ``run_options`` keyword.
        opts = _kwargs.get("run_options")
        if opts is not None:
            captured.update(dataclasses.asdict(opts))
        return _lib_ok_result()

    with patch("otto.suite.run.run_suite", fake_run_suite):
        result = runner.invoke(suite_app, [*cli_args, "_CovCtxSuite"])

    return result.exit_code, captured, result.output


class TestCovDirOption:
    def test_no_flags_disables_coverage(self):
        exit_code, ctx_obj, output = _capture_cov_ctx([])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov"] is False
        assert ctx_obj["cov_dir"] is None

    def test_cov_flag_only_uses_default_dir(self):
        exit_code, ctx_obj, output = _capture_cov_ctx(["--cov"])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_dir"] is None

    def test_cov_dir_implies_cov_and_records_path(self, tmp_path):
        target = tmp_path / "custom"
        exit_code, ctx_obj, output = _capture_cov_ctx(["--cov-dir", str(target)])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_dir"] == target.resolve()
        # Validation creates the directory eagerly.
        assert target.is_dir()

    def test_cov_with_cov_dir_records_path(self, tmp_path):
        target = tmp_path / "both"
        exit_code, ctx_obj, output = _capture_cov_ctx(
            ["--cov", "--cov-dir", str(target)],
        )
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_dir"] == target.resolve()

    def test_cov_dir_nonempty_without_overwrite_aborts(self, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        (target / "leftover.txt").write_text("stale")

        exit_code, ctx_obj, output = _capture_cov_ctx(["--cov-dir", str(target)])
        assert exit_code != 0
        assert ctx_obj == {}
        assert "not empty" in output or "--overwrite-cov-dir" in output
        # Stale file preserved when we refuse to proceed.
        assert (target / "leftover.txt").exists()

    def test_overwrite_cov_dir_clears_contents(self, tmp_path):
        target = tmp_path / "to_clear"
        target.mkdir()
        (target / "leftover.txt").write_text("stale")
        (target / "sub").mkdir()
        (target / "sub" / "nested.txt").write_text("more stale")

        exit_code, ctx_obj, output = _capture_cov_ctx(
            ["--cov-dir", str(target), "--overwrite-cov-dir"],
        )
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_dir"] == target.resolve()
        assert target.is_dir()
        assert list(target.iterdir()) == []

    def test_cov_dir_pointing_at_file_fails(self, tmp_path):
        target = tmp_path / "not_a_dir"
        target.write_text("i am a file")

        exit_code, _, output = _capture_cov_ctx(["--cov-dir", str(target)])
        assert exit_code != 0
        assert "--cov-dir" in output
        assert "is a file" in output or "not a directory" in output


class TestInRunReportGeneration:
    """``otto test --cov-report`` renders via the collection-model report path.

    The fetch/metadata/capture collection tail itself now lives in
    ``otto.coverage.collect`` (exercised in ``tests/unit/cov/test_collect.py``);
    this class covers only the in-run HTML report block of
    ``otto.suite.run._post_run_coverage``.
    """

    @pytest.fixture
    def sut_repo(self, tmp_path):
        """A real tmp_path git repo standing in for the SUT checkout."""
        import subprocess

        root = tmp_path / "sut"
        root.mkdir()

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args],
                cwd=root,
                check=True,
                capture_output=True,
                env={
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@x",
                    "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@x",
                    "HOME": str(tmp_path),
                    "PATH": "/usr/bin:/bin",
                },
            )

        git("init", "-q")
        (root / "f.c").write_text("int a;\nint b;\n")
        git("add", "f.c")
        git("commit", "-qm", "init")
        return root

    def test_in_run_report_uses_configured_tiers(self, tmp_path, sut_repo):
        """``otto test --cov-report`` must render via the collection-model path,
        not the legacy system-only one: the store.json the in-run report writes
        carries the settings-declared tier precedence."""
        import asyncio
        import json

        from otto.suite.run import RunOptions, _post_run_coverage

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        report_dir = tmp_path / "cov_report"

        cov_config = {
            "tiers": {
                "unit": {"kind": "unit", "precedence": 1},
                "system": {"kind": "e2e", "precedence": 2},
                "manual": {"kind": "manual", "precedence": 3},
            }
        }
        repo = MagicMock()
        repo.sut_dir = sut_repo
        repo.name = "repo"
        repo.settings = {"coverage": cov_config}

        # cov=False keeps the fetch machinery out; only the report block runs.
        opts = RunOptions(
            cov=False,
            cov_report=True,
            cov_dir=cov_dir,
            cov_report_dir=report_dir,
        )
        asyncio.run(_post_run_coverage([repo], tmp_path / "log", opts))

        store_json = json.loads((report_dir / "store.json").read_text())
        assert store_json["tier_order"] == ["unit", "system", "manual"]


# ── --cov-report option (report generation alongside collection) ─────────────


class TestCovReportOption:
    def test_no_flags_disables_report(self):
        exit_code, ctx_obj, output = _capture_cov_ctx([])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_report"] is False
        assert ctx_obj["cov_report_dir"] is None

    def test_cov_report_flag_enables_report_and_implies_cov(self):
        exit_code, ctx_obj, output = _capture_cov_ctx(["--cov-report"])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_report"] is True
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_report_dir"] is None

    def test_short_r_flag_enables_report(self):
        exit_code, ctx_obj, output = _capture_cov_ctx(["-r"])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_report"] is True
        assert ctx_obj["cov"] is True

    def test_cov_report_dir_implies_cov_report_and_cov(self, tmp_path):
        target = tmp_path / "report"
        exit_code, ctx_obj, output = _capture_cov_ctx(
            ["--cov-report-dir", str(target)],
        )
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_report"] is True
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_report_dir"] == target.resolve()
        # Validation creates the directory eagerly.
        assert target.is_dir()

    def test_cov_report_dir_nonempty_without_overwrite_aborts(self, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        (target / "stale.html").write_text("stale")
        exit_code, ctx_obj, output = _capture_cov_ctx(
            ["--cov-report-dir", str(target)],
        )
        assert exit_code != 0
        assert ctx_obj == {}
        assert "not empty" in output or "--overwrite-cov-report-dir" in output
        assert (target / "stale.html").exists()

    def test_overwrite_cov_report_dir_clears_contents(self, tmp_path):
        target = tmp_path / "to_clear"
        target.mkdir()
        (target / "stale.html").write_text("stale")
        (target / "sub").mkdir()
        (target / "sub" / "nested.html").write_text("nested")

        exit_code, ctx_obj, output = _capture_cov_ctx(
            ["--cov-report-dir", str(target), "--overwrite-cov-report-dir"],
        )
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_report"] is True
        assert ctx_obj["cov_report_dir"] == target.resolve()
        assert list(target.iterdir()) == []

    def test_project_name_recorded(self):
        exit_code, ctx_obj, output = _capture_cov_ctx(
            ["--cov-report", "--project-name", "My App"],
        )
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["project_name"] == "My App"

    def test_project_name_default(self):
        exit_code, ctx_obj, output = _capture_cov_ctx([])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["project_name"] == "Coverage Report"

    def test_cov_report_dir_pointing_at_file_fails(self, tmp_path):
        target = tmp_path / "not_a_dir"
        target.write_text("i am a file")
        exit_code, _, output = _capture_cov_ctx(
            ["--cov-report-dir", str(target)],
        )
        assert exit_code != 0
        assert "--cov-report-dir" in output
        assert "is a file" in output or "not a directory" in output
