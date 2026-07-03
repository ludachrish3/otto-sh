"""
Unit tests for the refactored ``otto test`` subcommand.

Tests verify:
  - ``otto test --help`` shows available subcommands and parent-level runner
    options (markers / iterations / duration / threshold / results)
  - ``otto test <Suite> --help`` shows suite-specific options only
  - The callback sets the logger's log directory for the invoked suite
  - ``run_suite`` calls ``pytest.main`` with the correct arguments
  - Type enforcement: Typer rejects invalid values before pytest runs
  - Defaults are applied when suite options are omitted
  - Parent-callback options (``--iterations`` etc.) thread through ``ctx.meta``
    into ``run_suite``
"""

from dataclasses import dataclass
from typing import Annotated
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from otto.cli.test import run_suite, suite_app
from otto.context import get_context
from otto.suite.register import SUITES, register_suite_class

runner = CliRunner()


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
            patch("otto.cli.test.run_suite"),
        ):
            runner.invoke(app, ["--lab", "x", "test", "_CallbackSuite"])

        p_create.assert_called_once_with("test", "_CallbackSuite")


# ── run_suite internals ───────────────────────────────────────────────────────


class TestRunSuiteInternals:
    """Test run_suite() directly to verify pytest.main args.

    Runner options (markers/iterations/...) and coverage flags are read from
    the parent Typer context in production. These tests bypass the CLI and
    supply a fake click context so the function can be exercised in isolation.
    """

    @staticmethod
    def _fake_parent_ctx(parent_opts: dict):
        """Build a fake Typer context whose ``.meta`` carries a
        ``TestRunOptions`` from ``parent_opts``; pass it to ``run_suite``
        (which reads its options from ``ctx.meta``).
        """
        from otto.cli.test import RUN_OPTIONS_KEY, TestRunOptions

        fake_ctx = MagicMock()
        fake_ctx.meta = {RUN_OPTIONS_KEY: TestRunOptions(**parent_opts)}
        return fake_ctx

    def test_pytest_main_called_with_suite_file(self, tmp_path):
        fake_file = str(tmp_path / "test_fake.py")

        class _FakeSuite:
            __name__ = "_FakeSuite"

        get_context().output_dir = tmp_path
        with (
            patch("otto.cli.test.get_repos", return_value=[]),
            patch("pytest.main", return_value=pytest.ExitCode.OK) as mock_main,
        ):
            run_suite(_FakeSuite, fake_file, None, self._fake_parent_ctx({}))

        mock_main.assert_called_once()
        args_list = mock_main.call_args[0][0]
        assert fake_file in args_list
        assert "-k" in args_list
        assert "_FakeSuite" in args_list

    def test_results_auto_path_used_when_empty(self, tmp_path):
        class _FakeSuite3:
            __name__ = "_FakeSuite3"

        get_context().output_dir = tmp_path
        with (
            patch("otto.cli.test.get_repos", return_value=[]),
            patch("pytest.main", return_value=pytest.ExitCode.OK) as mock_main,
        ):
            run_suite(_FakeSuite3, "fake.py", None, self._fake_parent_ctx({}))

        args_list = mock_main.call_args[0][0]
        junit_arg = next((a for a in args_list if "--junitxml" in a), None)  # pytest's own flag
        assert junit_arg is not None
        assert str(tmp_path) in junit_arg

    def test_markers_arg_passed(self, tmp_path):
        class _FakeSuite4:
            __name__ = "_FakeSuite4"

        get_context().output_dir = tmp_path
        with (
            patch("otto.cli.test.get_repos", return_value=[]),
            patch("pytest.main", return_value=pytest.ExitCode.OK) as mock_main,
        ):
            run_suite(
                _FakeSuite4, "fake.py", None, self._fake_parent_ctx({"markers": "not integration"})
            )

        args_list = mock_main.call_args[0][0]
        assert "-m" in args_list
        m_index = args_list.index("-m")
        assert args_list[m_index + 1] == "not integration"

    def test_monitor_flags_reach_otto_plugin(self, tmp_path):
        """run_suite must hand --monitor settings to OttoPlugin and default the
        output path to ``<output_dir>/monitor.json`` when the user didn't
        supply ``--monitor-output``.
        """
        captured: dict = {}

        class _CapturingPlugin:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class _FakeMonSuite:
            __name__ = "_FakeMonSuite"

        get_context().output_dir = tmp_path
        with (
            patch("otto.cli.test.get_repos", return_value=[]),
            patch("pytest.main", return_value=pytest.ExitCode.OK),
            patch("otto.suite.plugin.OttoPlugin", _CapturingPlugin),
        ):
            run_suite(
                _FakeMonSuite,
                "fake.py",
                None,
                self._fake_parent_ctx(
                    {
                        "monitor": True,
                        "monitor_interval": 2.0,
                        "monitor_output": None,
                        "monitor_hosts": "router",
                    }
                ),
            )

        assert captured.get("monitor") is True
        assert captured.get("monitor_interval") == 2.0
        assert captured.get("monitor_hosts") == "router"
        assert captured.get("monitor_output") == tmp_path / "monitor.json"

    def test_monitor_output_override_passes_through(self, tmp_path):
        captured: dict = {}

        class _CapturingPlugin:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class _FakeMonSuite2:
            __name__ = "_FakeMonSuite2"

        get_context().output_dir = tmp_path
        out = tmp_path / "somewhere.db"
        with (
            patch("otto.cli.test.get_repos", return_value=[]),
            patch("pytest.main", return_value=pytest.ExitCode.OK),
            patch("otto.suite.plugin.OttoPlugin", _CapturingPlugin),
        ):
            run_suite(
                _FakeMonSuite2,
                "fake.py",
                None,
                self._fake_parent_ctx(
                    {
                        "monitor": True,
                        "monitor_output": out,
                    }
                ),
            )

        assert captured.get("monitor_output") == out

    def test_exit_code_propagated_on_tests_failed(self, tmp_path):
        """run_suite must raise typer.Exit(1) when pytest reports TESTS_FAILED."""

        class _FailSuite:
            __name__ = "_FailSuite"

        get_context().output_dir = tmp_path
        with (
            patch("otto.cli.test.get_repos", return_value=[]),
            patch("pytest.main", return_value=pytest.ExitCode.TESTS_FAILED),
            pytest.raises(typer.Exit) as exc_info,
        ):
            run_suite(_FailSuite, "fake.py", None, self._fake_parent_ctx({}))
        assert exc_info.value.exit_code == 1

    def test_no_exit_on_ok(self, tmp_path):
        """run_suite must NOT raise typer.Exit when pytest reports OK (all passed)."""

        class _OkSuite:
            __name__ = "_OkSuite"

        get_context().output_dir = tmp_path
        with (
            patch("otto.cli.test.get_repos", return_value=[]),
            patch("pytest.main", return_value=pytest.ExitCode.OK),
        ):
            # Should complete without raising.
            run_suite(_OkSuite, "fake.py", None, self._fake_parent_ctx({}))

    def test_exit_code_propagated_on_no_tests_collected(self, tmp_path):
        """run_suite must raise typer.Exit(5) when pytest collects nothing.

        A named suite that collects zero tests is treated as an error — the
        caller explicitly requested a suite but nothing ran.
        """

        class _NoTestsSuite:
            __name__ = "_NoTestsSuite"

        get_context().output_dir = tmp_path
        with (
            patch("otto.cli.test.get_repos", return_value=[]),
            patch("pytest.main", return_value=pytest.ExitCode.NO_TESTS_COLLECTED),
            pytest.raises(typer.Exit) as exc_info,
        ):
            run_suite(_NoTestsSuite, "fake.py", None, self._fake_parent_ctx({}))
        assert exc_info.value.exit_code == 5

    def test_exit_code_propagated_on_internal_error(self, tmp_path):
        """run_suite must raise typer.Exit(3) when pytest hits INTERNAL_ERROR."""

        class _InternalErrSuite:
            __name__ = "_InternalErrSuite"

        get_context().output_dir = tmp_path
        with (
            patch("otto.cli.test.get_repos", return_value=[]),
            patch("pytest.main", return_value=pytest.ExitCode.INTERNAL_ERROR),
            pytest.raises(typer.Exit) as exc_info,
        ):
            run_suite(_InternalErrSuite, "fake.py", None, self._fake_parent_ctx({}))
        assert exc_info.value.exit_code == 3


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

        mock_logger = MagicMock()
        with patch("otto.cli.test.run_suite"), patch("otto.cli.test.logger", mock_logger):
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

        def fake_run_suite(suite_class, suite_file, opts_instance, ctx):
            captured["opts"] = opts_instance

        with patch("otto.cli.test.run_suite", fake_run_suite):
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

        from otto.cli.test import RUN_OPTIONS_KEY

        captured: dict = {}

        def fake_run_suite(*_args, **_kwargs):
            ctx = _args[3] if len(_args) > 3 else None  # run_suite is called positionally
            if ctx is not None:
                opts = ctx.meta.get(RUN_OPTIONS_KEY)
                if opts is not None:
                    captured.update(dataclasses.asdict(opts))

        mock_logger = MagicMock()
        with (
            patch("otto.cli.test.run_suite", fake_run_suite),
            patch("otto.cli.test.logger", mock_logger),
        ):
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

    from otto.cli.test import RUN_OPTIONS_KEY

    captured: dict = {}

    def fake_run_suite(*_args, **_kwargs):
        ctx = _args[3] if len(_args) > 3 else None  # run_suite is called positionally
        if ctx is not None:
            opts = ctx.meta.get(RUN_OPTIONS_KEY)
            if opts is not None:
                captured.update(dataclasses.asdict(opts))

    mock_logger = MagicMock()
    with (
        patch("otto.cli.test.run_suite", fake_run_suite),
        patch("otto.cli.test.logger", mock_logger),
    ):
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


# ── _run_coverage destination resolution ─────────────────────────────────────


class TestRunCoverageDestination:
    """Verify ``_run_coverage`` honours the override vs. default destination."""

    def _invoke(self, *, log_dir, override):
        import asyncio

        from otto.cli.test import _run_coverage

        repo = MagicMock()
        repo.sut_dir = log_dir
        repo.name = "repo"

        fetcher_instance = MagicMock()
        fetcher_instance.fetch_all = AsyncMock(return_value={})
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        from otto.host import UnixHost

        with (
            patch("otto.cli.test._get_cov_config", return_value={"gcda_remote_dir": "/remote"}),
            patch("otto.configmodule.all_hosts", return_value=[MagicMock(spec=UnixHost)]),
            patch(
                "otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance
            ) as fetcher_cls,
        ):
            asyncio.run(_run_coverage([repo], log_dir, override))
        return fetcher_cls

    def test_override_used_when_provided(self, tmp_path):
        override = tmp_path / "override"
        log_dir = tmp_path / "log"
        override.mkdir()
        log_dir.mkdir()
        fetcher_cls = self._invoke(log_dir=log_dir, override=override)
        fetcher_cls.assert_called_once_with(override)

    def test_default_used_when_no_override(self, tmp_path):
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        fetcher_cls = self._invoke(log_dir=log_dir, override=None)
        fetcher_cls.assert_called_once_with(log_dir / "cov")


class TestRunCoverageEmbedded:
    """``_run_coverage`` collects embedded hosts even with no Unix gcda_remote_dir."""

    def test_collects_embedded_when_only_embedded_configured(self, tmp_path):
        import asyncio

        from otto.cli.test import _run_coverage

        repo = MagicMock()
        log_dir = tmp_path / "log"
        log_dir.mkdir()

        embedded_collect = AsyncMock(
            return_value={"sprout": tmp_path / "cov" / "sprout"},
        )
        with (
            patch(
                "otto.cli.test._get_cov_config", return_value={"embedded": {"extension": "cov_ext"}}
            ),
            patch("otto.configmodule.all_hosts", return_value=[]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.cli.test._get_cov_repo", return_value=None),
        ):
            asyncio.run(_run_coverage([repo], log_dir, None))

        embedded_collect.assert_awaited_once()

    def test_unix_hop_host_not_treated_as_coverage_target(self, tmp_path):
        """A Unix SSH hop in the lab must not pollute the embedded meta.

        An embedded coverage lab must include the SSH hop (e.g. ``basil``
        fronting ``sprout_cov``) so the hop resolves — but the hop is
        infrastructure, not a coverage target, and emits no ``.gcda``. The meta
        must therefore (a) keep ``sut_dir`` = the embedded build dir (the hop must
        not flip it to the repo dir, which breaks ``.gcno`` discovery and made
        ``geninfo`` skip the file on the real lab) and (b) carry only the embedded
        host's toolchain, not the hop's. Regression for the basil-hop report bug.
        """
        import asyncio
        import json
        from pathlib import Path

        from otto.cli.test import _run_coverage
        from otto.host import UnixHost
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build_dir = tmp_path / "build" / "cov_ext_app"
        build_dir.mkdir(parents=True)

        repo = MagicMock()
        repo.name = "repo3"
        repo.sut_dir = tmp_path / "repo3"  # NOT what sut_dir should resolve to

        hop = MagicMock(spec=UnixHost)
        hop.id = "basil_seed"  # a Unix hop, produces no coverage

        sprout_cov = ZephyrHost(
            ip="192.0.2.33",
            element="sprout_cov",
            transfer="console",
            toolchain=Toolchain(
                sysroot=Path("/opt/sdk/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )

        embedded_collect = AsyncMock(
            return_value={"sprout_cov": cov_dir / "sprout_cov"},
        )
        cov_config = {
            "embedded": {
                "extension": "cov_ext",
                "build_dir": str(build_dir),
            },
        }
        with (
            patch("otto.cli.test._get_cov_config", return_value=cov_config),
            patch("otto.configmodule.all_hosts", return_value=[hop, sprout_cov]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.cli.test._get_cov_repo", return_value=repo),
        ):
            asyncio.run(_run_coverage([repo], tmp_path / "log", cov_dir))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        assert meta["sut_dir"] == str(build_dir.resolve())
        assert set(meta["toolchains"]) == {"sprout_cov"}
        assert "basil_seed" not in meta["toolchains"]

    def test_embedded_toolchain_is_per_host(self, tmp_path):
        """Each embedded host's coverage toolchain comes from host.toolchain."""
        import asyncio
        import json
        from pathlib import Path

        from otto.cli.test import _run_coverage
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        host = ZephyrHost(
            ip="192.0.2.33",
            element="sprout_cov",
            transfer="console",
            toolchain=Toolchain(
                sysroot=Path("/home/vagrant/zephyr-sdk-0.16.8/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build_dir = tmp_path / "build"
        (build_dir / "zephyr").mkdir(parents=True)

        repo = MagicMock()
        repo.name = "repo3"
        repo.sut_dir = tmp_path / "repo3"

        embedded_collect = AsyncMock(
            return_value={"sprout_cov": cov_dir / "sprout_cov"},
        )
        cov_config = {
            "embedded": {
                "extension": "cov_ext",
                "build_dir": str(build_dir),
            },
        }
        with (
            patch("otto.cli.test._get_cov_config", return_value=cov_config),
            patch("otto.configmodule.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.cli.test._get_cov_repo", return_value=repo),
        ):
            asyncio.run(_run_coverage([repo], tmp_path / "log", cov_dir))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        entry = meta["toolchains"]["sprout_cov"]
        assert entry["gcov"] == "bin/arm-zephyr-eabi-gcov"
        assert entry["sysroot"] == "/home/vagrant/zephyr-sdk-0.16.8/arm-zephyr-eabi"
        assert entry["lcov"] == "/usr/bin/lcov"

    def test_embedded_toolchain_falls_back_to_gcno_discovery(self, tmp_path):
        """A host left at the default Toolchain() resolves via .gcno discovery."""
        import asyncio
        import json
        from pathlib import Path

        from otto.cli import test as test_mod
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        host = ZephyrHost(ip="192.0.2.33", element="sprout_cov", transfer="console")
        # No toolchain configured -> default Toolchain() -> discovery fallback.
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build_dir = tmp_path / "build"
        build_dir.mkdir()

        repo = MagicMock()
        repo.name = "repo3"
        repo.sut_dir = tmp_path / "repo3"

        embedded_collect = AsyncMock(
            return_value={"sprout_cov": cov_dir / "sprout_cov"},
        )
        cov_config = {
            "embedded": {
                "extension": "cov_ext",
                "build_dir": str(build_dir),
            },
        }

        discovered = Toolchain(
            sysroot=Path("/discovered"),
            gcov=Path("bin/x-gcov"),
            lcov=Path("/usr/bin/lcov"),
        )

        async def _fake_discover(build_dir_arg, localhost, work_dir):
            return discovered

        with (
            patch("otto.cli.test._get_cov_config", return_value=cov_config),
            patch("otto.configmodule.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.cli.test._get_cov_repo", return_value=repo),
            patch("otto.host.toolchain_discovery.discover_toolchain_from_gcno", new=_fake_discover),
        ):
            asyncio.run(test_mod._run_coverage([repo], tmp_path / "log", cov_dir))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        assert meta["toolchains"]["sprout_cov"]["gcov"] == "bin/x-gcov"
        assert meta["toolchains"]["sprout_cov"]["sysroot"] == "/discovered"

    def test_coverage_hosts_regex_passed_to_both_selectors(self, tmp_path):
        """``[coverage].hosts`` compiles to a regex handed to the Unix and
        embedded host selectors, so the collect-from set is repo-declared
        rather than inferred from which hosts happened to emit ``.gcda``.
        """
        import asyncio

        from otto.cli.test import _run_coverage

        repo = MagicMock()
        log_dir = tmp_path / "log"
        log_dir.mkdir()

        all_hosts_mock = MagicMock(return_value=[])
        embedded_collect = AsyncMock(return_value={})
        with (
            patch(
                "otto.cli.test._get_cov_config",
                return_value={"hosts": "sprout_cov", "embedded": {"extension": "cov_ext"}},
            ),
            patch("otto.configmodule.all_hosts", new=all_hosts_mock),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.cli.test._get_cov_repo", return_value=None),
        ):
            asyncio.run(_run_coverage([repo], log_dir, None))

        unix_pat = all_hosts_mock.call_args.kwargs.get("pattern")
        assert unix_pat is not None
        assert unix_pat.search("sprout_cov")
        assert not unix_pat.search("basil_seed")

        emb_pat = embedded_collect.await_args.kwargs.get("pattern")
        assert emb_pat is not None
        assert emb_pat.pattern == "sprout_cov"

    def test_unset_coverage_hosts_passes_no_pattern(self, tmp_path):
        """Unset ``[coverage].hosts`` → ``pattern=None`` (collect from all hosts)."""
        import asyncio

        from otto.cli.test import _run_coverage

        repo = MagicMock()
        log_dir = tmp_path / "log"
        log_dir.mkdir()

        all_hosts_mock = MagicMock(return_value=[])
        embedded_collect = AsyncMock(return_value={})
        with (
            patch(
                "otto.cli.test._get_cov_config", return_value={"embedded": {"extension": "cov_ext"}}
            ),
            patch("otto.configmodule.all_hosts", new=all_hosts_mock),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.cli.test._get_cov_repo", return_value=None),
        ):
            asyncio.run(_run_coverage([repo], log_dir, None))

        assert all_hosts_mock.call_args.kwargs.get("pattern") is None
        assert embedded_collect.await_args.kwargs.get("pattern") is None

    def test_per_version_source_roots_recorded(self, tmp_path):
        """Two embedded hosts of different os_version each record their own build_dir
        as a per-host source root in the meta (multi-Zephyr-version coverage).
        """
        import asyncio
        import json
        from pathlib import Path

        from otto.cli.test import _run_coverage
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build37 = tmp_path / "build" / "v3_7"
        build37.mkdir(parents=True)
        build44 = tmp_path / "build" / "v4_4"
        build44.mkdir(parents=True)

        repo = MagicMock()
        repo.name = "repo3"
        repo.sut_dir = tmp_path / "repo3"

        sprout = ZephyrHost(
            ip="192.0.2.33",
            element="sprout",
            transfer="console",
            os_version="3.7",
            toolchain=Toolchain(
                sysroot=Path("/opt/sdk37/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )
        sprout44 = ZephyrHost(
            ip="192.0.2.34",
            element="sprout44",
            transfer="console",
            os_version="4.4",
            toolchain=Toolchain(
                sysroot=Path("/opt/sdk44/gnu/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )

        embedded_collect = AsyncMock(
            return_value={
                "sprout": cov_dir / "sprout",
                "sprout44": cov_dir / "sprout44",
            }
        )
        cov_config = {
            "embedded": {
                "extension": "cov_ext",
                "builds": {
                    "3.7": {"build_dir": str(build37)},
                    "4.4": {"build_dir": str(build44)},
                },
            },
        }
        with (
            patch("otto.cli.test._get_cov_config", return_value=cov_config),
            patch("otto.configmodule.all_hosts", return_value=[sprout, sprout44]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.cli.test._get_cov_repo", return_value=repo),
        ):
            asyncio.run(_run_coverage([repo], tmp_path / "log", cov_dir))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        assert meta["source_roots"]["sprout"] == str(build37.resolve())
        assert meta["source_roots"]["sprout44"] == str(build44.resolve())


class TestRunCoverageCaptureTail:
    """The post-collection capture.json production tail (the last thing
    ``_run_coverage`` does) must never fail an otherwise-successful
    ``otto test --cov`` run — a non-git sut, ambiguous/misconfigured tiers,
    or a capture-production error are all logged and swallowed.
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

    def _run(self, repo, log_dir, cov_dir, cov_config):
        """Invoke ``_run_coverage`` with one embedded board already collected."""
        import asyncio

        from otto.cli.test import _run_coverage

        embedded_collect = AsyncMock(return_value={"board1": cov_dir / "board1"})
        with (
            patch("otto.cli.test._get_cov_config", return_value=cov_config),
            patch("otto.configmodule.all_hosts", return_value=[]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.cli.test._get_cov_repo", return_value=repo),
        ):
            asyncio.run(_run_coverage([repo], log_dir, cov_dir))

    def test_happy_path_writes_pinned_capture(self, tmp_path, sut_repo, monkeypatch):
        """A well-formed single-tier repo leaves behind a real capture.json."""
        import json

        from otto.coverage.capture import produce as produce_mod

        cov_dir = tmp_path / "cov"
        (cov_dir / "board1").mkdir(parents=True)
        (cov_dir / "board1" / "x.gcda").write_bytes(b"")

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            output.write_text(f"TN:\nSF:{sut_repo / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

        repo = MagicMock()
        repo.sut_dir = sut_repo
        repo.name = "repo"

        # Non-empty (truthy) but no explicit [coverage.tiers] -> implicit
        # single "system" e2e tier.
        self._run(repo, tmp_path / "log", cov_dir, {"tiers": {}})

        capture_path = cov_dir / "board1" / "capture.json"
        assert capture_path.is_file()
        assert json.loads(capture_path.read_text())["tier"] == "system"

    def test_ambiguous_tiers_do_not_fail_the_run(self, tmp_path, sut_repo, monkeypatch, caplog):
        """Two e2e-kind tiers make ``resolve_get_tier`` raise ``ValueError``;
        the tail must swallow it (with a warning), not crash the test run.
        """
        from otto.coverage.capture import produce as produce_mod

        cov_dir = tmp_path / "cov"
        (cov_dir / "board1").mkdir(parents=True)
        (cov_dir / "board1" / "x.gcda").write_bytes(b"")

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            output.write_text(f"TN:\nSF:{sut_repo / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

        repo = MagicMock()
        repo.sut_dir = sut_repo
        repo.name = "repo"

        cov_config = {
            "tiers": {
                "system": {"kind": "e2e", "precedence": 1},
                "nightly": {"kind": "e2e", "precedence": 2},
            }
        }

        with caplog.at_level("WARNING"):
            self._run(repo, tmp_path / "log", cov_dir, cov_config)

        assert not (cov_dir / "board1" / "capture.json").exists()
        assert any("Coverage capture emission failed" in rec.message for rec in caplog.records)

    def test_non_git_sut_does_not_fail_the_run(self, tmp_path, monkeypatch, caplog):
        """A non-git sut dir is legacy behavior and must not fail the run."""
        from otto.coverage.capture import produce as produce_mod

        cov_dir = tmp_path / "cov"
        (cov_dir / "board1").mkdir(parents=True)
        (cov_dir / "board1" / "x.gcda").write_bytes(b"")

        notgit = tmp_path / "notgit"
        notgit.mkdir()
        (notgit / "f.c").write_text("int a;\n")

        async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
            output.write_text(f"TN:\nSF:{notgit / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

        repo = MagicMock()
        repo.sut_dir = notgit
        repo.name = "repo"

        with caplog.at_level("WARNING"):
            self._run(repo, tmp_path / "log", cov_dir, {"tiers": {}})

        assert not (cov_dir / "board1" / "capture.json").exists()
        assert any("Coverage capture emission failed" in rec.message for rec in caplog.records)

    def test_in_run_report_uses_configured_tiers(self, tmp_path, sut_repo):
        """``otto test --cov-report`` must render via the collection-model path,
        not the legacy system-only one: the store.json the in-run report writes
        carries the settings-declared tier precedence."""
        import asyncio
        import json

        from otto.cli.test import TestRunOptions, _post_run_coverage

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
        opts = TestRunOptions(
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


# ── run_suite post-test report generation ────────────────────────────────────


class TestRunSuiteReport:
    """Verify run_suite wires --cov-report to run_coverage_report correctly.

    When ``--cov-report`` is enabled, ``run_suite`` should forward the right
    cov_dir, report_dir, and project_name to the shared reporter helper.
    """

    def _invoke(self, *, parent_opts, log_dir):
        from otto.cli.test import RUN_OPTIONS_KEY, TestRunOptions, run_suite

        repo = MagicMock()
        repo.tests = [log_dir]
        repo.sut_dir = log_dir
        repo.name = "repo"
        # No [coverage] section → _post_run_coverage resolves (None, None, [])
        # and the report runs on the legacy gcda-only path (what these tests
        # pin). A real dict (not a MagicMock) keeps _get_cov_repo/load_tiers
        # from iterating an auto-attribute.
        repo.settings = {}

        mock_store = MagicMock()
        mock_store.overall_pct.return_value = 50.0
        mock_store.file_count.return_value = 1
        mock_run_report = AsyncMock(return_value=mock_store)

        class _FakeCtx:
            def __init__(self):
                self.meta = {RUN_OPTIONS_KEY: TestRunOptions(**parent_opts)}

        get_context().output_dir = log_dir

        with (
            patch("otto.cli.test.get_repos", return_value=[repo]),
            patch("pytest.main", return_value=pytest.ExitCode.OK),
            patch("otto.cli.test._run_coverage", new=AsyncMock()),
            patch("otto.cli.test._cov_clean_remotes", new=AsyncMock()),
            patch("otto.coverage.reporter.run_coverage_report", new=mock_run_report),
        ):

            class _FakeSuite:
                pass

            run_suite(_FakeSuite, str(log_dir / "fake.py"), None, _FakeCtx())

        return mock_run_report

    def test_no_cov_report_means_no_call(self, tmp_path):
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        mock = self._invoke(
            parent_opts={
                "cov": True,
                "cov_dir": None,
                "cov_clean": False,
                "cov_report": False,
                "cov_report_dir": None,
                "overwrite_cov_report_dir": False,
                "project_name": "Coverage Report",
            },
            log_dir=log_dir,
        )
        mock.assert_not_called()

    def test_default_report_dir_under_log_dir(self, tmp_path):
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        mock = self._invoke(
            parent_opts={
                "cov": True,
                "cov_dir": None,
                "cov_clean": False,
                "cov_report": True,
                "cov_report_dir": None,
                "overwrite_cov_report_dir": False,
                "project_name": "Coverage Report",
            },
            log_dir=log_dir,
        )
        mock.assert_called_once()
        args = mock.call_args.args
        assert args[0] == [log_dir / "cov"]
        assert args[1] == log_dir / "cov_report"
        assert (log_dir / "cov_report").is_dir()

    def test_explicit_report_dir_and_project_name(self, tmp_path):
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        report_dir = tmp_path / "my_report"
        report_dir.mkdir()
        mock = self._invoke(
            parent_opts={
                "cov": True,
                "cov_dir": None,
                "cov_clean": False,
                "cov_report": True,
                "cov_report_dir": report_dir,
                "overwrite_cov_report_dir": False,
                "project_name": "My App",
            },
            log_dir=log_dir,
        )
        mock.assert_called_once()
        args = mock.call_args.args
        assert args[1] == report_dir
        assert mock.call_args.kwargs["project_name"] == "My App"

    def test_cov_dir_override_used_as_source(self, tmp_path):
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        cov_dir = tmp_path / "custom_cov"
        cov_dir.mkdir()
        mock = self._invoke(
            parent_opts={
                "cov": True,
                "cov_dir": cov_dir,
                "cov_clean": False,
                "cov_report": True,
                "cov_report_dir": None,
                "overwrite_cov_report_dir": False,
                "project_name": "Coverage Report",
            },
            log_dir=log_dir,
        )
        mock.assert_called_once()
        args = mock.call_args.args
        assert args[0] == [cov_dir]
