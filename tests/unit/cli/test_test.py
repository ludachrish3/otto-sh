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
  - Parent-callback options (``--iterations`` etc.) thread through ``ctx.obj``
    into ``run_suite``
"""

from dataclasses import dataclass
from typing import Annotated
from unittest.mock import AsyncMock, MagicMock, patch

import typer
from typer.testing import CliRunner

from otto.cli.test import run_suite, suite_app
from otto.suite.register import _SUITE_REGISTRY, register_suite

runner = CliRunner()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_isolated_app(suite_class: type) -> typer.Typer:
    """Build a fresh Typer app containing only the given suite as a subcommand."""
    for name, sub_app in reversed(_SUITE_REGISTRY):
        if name == suite_class.__name__:
            app = typer.Typer(no_args_is_help=True)
            app.add_typer(sub_app)
            return app
    raise LookupError(f'{suite_class.__name__} not found in _SUITE_REGISTRY')


# ── Help behaviour ────────────────────────────────────────────────────────────

class TestTestHelp:
    def test_help_flag(self):
        result = runner.invoke(suite_app, ['--help'])
        assert result.exit_code == 0

    def test_short_help_flag(self):
        result = runner.invoke(suite_app, ['-h'])
        assert result.exit_code == 0

    def test_suite_help_shows_options(self):
        """otto test <SuiteName> --help must list suite-specific options."""
        @register_suite()
        class _HelpSuite:
            @dataclass
            class Options:
                firmware: Annotated[str, typer.Option()] = "latest"

        app = _make_isolated_app(_HelpSuite)
        result = runner.invoke(app, ['_HelpSuite', '--help'])
        assert result.exit_code == 0
        assert '--firmware' in result.output

    def test_parent_help_shows_runner_options(self):
        """Runner options live on ``otto test --help``, not the suite subcommand."""
        result = runner.invoke(suite_app, ['--help'])
        assert result.exit_code == 0
        for flag in ('--iterations', '--duration', '--threshold',
                     '--results', '--markers'):
            assert flag in result.output

    def test_suite_help_omits_runner_options(self):
        """Runner options must NOT appear in the per-suite ``--help`` output."""
        @register_suite()
        class _SuiteNoRunnerOpts:
            pass

        app = _make_isolated_app(_SuiteNoRunnerOpts)
        result = runner.invoke(app, ['_SuiteNoRunnerOpts', '--help'])
        assert result.exit_code == 0
        for flag in ('--iterations', '--duration', '--threshold',
                     '--results', '--markers'):
            assert flag not in result.output


# ── Callback / logger setup ───────────────────────────────────────────────────

class TestTestCallback:
    def test_logger_output_dir_called_for_suite(self):
        """The suite_app callback must call create_output_dir('test', suite_name)."""
        import otto.cli.test as test_module

        @register_suite()
        class _CallbackSuite:
            pass

        # Attach the suite directly to suite_app so the callback fires
        for name, sub_app in reversed(_SUITE_REGISTRY):
            if name == '_CallbackSuite':
                suite_app.add_typer(sub_app)
                break

        mock_logger = MagicMock()
        with patch.object(test_module, 'logger', mock_logger), \
             patch('otto.cli.test.run_suite'):
            runner.invoke(suite_app, ['_CallbackSuite'])

        mock_logger.create_output_dir.assert_called_once_with('test', '_CallbackSuite')


# ── run_suite internals ───────────────────────────────────────────────────────

class TestRunSuiteInternals:
    """Test run_suite() directly to verify pytest.main args.

    Runner options (markers/iterations/...) and coverage flags are read from
    the parent Typer context in production. These tests bypass the CLI and
    supply a fake click context so the function can be exercised in isolation.
    """

    @staticmethod
    def _patch_parent_ctx(parent_opts: dict):
        """Return a context manager that makes click.get_current_context()
        yield a fake ctx whose ``.obj`` is ``parent_opts`` (with a 'cov' key
        so run_suite's lookup loop recognises it).
        """
        fake_ctx = MagicMock()
        fake_ctx.obj = {'cov': False, 'cov_clean': False, **parent_opts}
        fake_ctx.parent = None
        return patch('click.get_current_context', return_value=fake_ctx)

    def test_pytest_main_called_with_suite_file(self, tmp_path):
        import otto.cli.test as test_module

        fake_file = str(tmp_path / 'test_fake.py')
        mock_logger = MagicMock()
        mock_logger.output_dir = tmp_path

        class _FakeSuite:
            __name__ = '_FakeSuite'

        with patch('otto.cli.test.getRepos', return_value=[]), \
             patch.object(test_module, 'logger', mock_logger), \
             patch('otto.cli.test.pytest') as mock_pytest, \
             self._patch_parent_ctx({}):
            run_suite(_FakeSuite, fake_file, None)

        mock_pytest.main.assert_called_once()
        args_list = mock_pytest.main.call_args[0][0]
        assert fake_file in args_list
        assert '-k' in args_list
        assert '_FakeSuite' in args_list

    def test_results_auto_path_used_when_empty(self, tmp_path):
        import otto.cli.test as test_module

        mock_logger = MagicMock()
        mock_logger.output_dir = tmp_path

        class _FakeSuite3:
            __name__ = '_FakeSuite3'

        with patch('otto.cli.test.getRepos', return_value=[]), \
             patch.object(test_module, 'logger', mock_logger), \
             patch('otto.cli.test.pytest') as mock_pytest, \
             self._patch_parent_ctx({}):
            run_suite(_FakeSuite3, 'fake.py', None)

        args_list = mock_pytest.main.call_args[0][0]
        junit_arg = next((a for a in args_list if '--junitxml' in a), None)  # pytest's own flag
        assert junit_arg is not None
        assert str(tmp_path) in junit_arg

    def test_markers_arg_passed(self, tmp_path):
        import otto.cli.test as test_module

        mock_logger = MagicMock()
        mock_logger.output_dir = tmp_path

        class _FakeSuite4:
            __name__ = '_FakeSuite4'

        with patch('otto.cli.test.getRepos', return_value=[]), \
             patch.object(test_module, 'logger', mock_logger), \
             patch('otto.cli.test.pytest') as mock_pytest, \
             self._patch_parent_ctx({'markers': 'not integration'}):
            run_suite(_FakeSuite4, 'fake.py', None)

        args_list = mock_pytest.main.call_args[0][0]
        assert '-m' in args_list
        m_index = args_list.index('-m')
        assert args_list[m_index + 1] == 'not integration'


# ── Type enforcement ──────────────────────────────────────────────────────────

class TestTypeEnforcement:

    def test_invalid_int_rejected_by_typer(self):
        """Passing a non-integer to an int option must fail at CLI level."""
        @register_suite()
        class _TypeSuite:
            @dataclass
            class Options:
                count: Annotated[int, typer.Option()] = 1

        app = _make_isolated_app(_TypeSuite)
        result = runner.invoke(app, ['_TypeSuite', '--count', 'not-a-number'])
        assert result.exit_code != 0

    def test_invalid_iterations_rejected(self):
        """--iterations lives on the parent; bad values must still reject."""
        @register_suite()
        class _IterSuite:
            pass

        for name, sub_app in reversed(_SUITE_REGISTRY):
            if name == '_IterSuite':
                suite_app.add_typer(sub_app)
                break

        mock_logger = MagicMock()
        with patch('otto.cli.test.run_suite'), \
             patch('otto.cli.test.logger', mock_logger):
            result = runner.invoke(suite_app,
                                   ['--iterations', 'oops', '_IterSuite'])
        assert result.exit_code != 0

    def test_defaults_applied_when_omitted(self):
        @register_suite()
        class _DefaultSuite:
            @dataclass
            class Options:
                max_retries: Annotated[int, typer.Option()] = 9

        app = _make_isolated_app(_DefaultSuite)
        captured: dict[str, object] = {}

        def fake_run_suite(suite_class, suite_file, opts_instance):
            captured['opts'] = opts_instance

        with patch('otto.cli.test.run_suite', fake_run_suite):
            result = runner.invoke(app, ['_DefaultSuite'])

        assert result.exit_code == 0
        opts = captured.get('opts')
        assert opts is not None
        assert opts.max_retries == 9  # type: ignore[union-attr]


# ── Help content (integration-level) ─────────────────────────────────────────

class TestHelpContent:
    """Verify that typer.Option help text appears in rendered --help output."""

    def test_annotated_help_in_cli_output(self):
        """A field annotated with typer.Option(help=...) shows that text in --help."""
        @register_suite()
        class _AnnotatedHelpSuite:
            @dataclass
            class Options:
                device_type: Annotated[str, typer.Option(
                    help="Kind of device under test.",
                )] = "router"

        app = _make_isolated_app(_AnnotatedHelpSuite)
        result = runner.invoke(app, ['_AnnotatedHelpSuite', '--help'])
        assert result.exit_code == 0
        assert '--device-type' in result.output
        assert 'Kind of device under test.' in result.output

    def test_no_help_when_option_has_none(self):
        """A bare typer.Option() with no help= produces no help text in --help."""
        @register_suite()
        class _BareHelpSuite:
            @dataclass
            class Options:
                firmware: Annotated[str, typer.Option()] = "latest"

        app = _make_isolated_app(_BareHelpSuite)
        result = runner.invoke(app, ['_BareHelpSuite', '--help'])
        assert result.exit_code == 0
        assert '--firmware' in result.output

    def test_inherited_annotated_field_help_in_cli_output(self):
        """Parent class annotated fields appear with their help text in child --help."""
        @dataclass
        class _InheritedParentOpts:
            device_type: Annotated[str, typer.Option(
                help="Inherited device help.",
            )] = "router"

        @register_suite()
        class _InheritedHelpSuite:
            @dataclass
            class Options(_InheritedParentOpts):
                firmware: Annotated[str, typer.Option(
                    help="Suite firmware help.",
                )] = "latest"

        app = _make_isolated_app(_InheritedHelpSuite)
        result = runner.invoke(app, ['_InheritedHelpSuite', '--help'])
        assert result.exit_code == 0
        assert 'Inherited device help.' in result.output
        assert 'Suite firmware help.' in result.output

    def test_parent_runner_option_help_present(self):
        """Parent-callback runner options retain their help text.

        Rich wraps long help strings across multiple columns, so we assert
        on short fragments rather than the full sentence.
        """
        result = runner.invoke(suite_app, ['--help'])
        assert result.exit_code == 0
        output_lower = result.output.lower()
        assert '--markers' in result.output
        assert 'marker' in output_lower
        assert 'iterations' in output_lower


# ── Parent-callback runner options thread through ctx.obj ────────────────────

class TestParentRunnerOptionsCtx:
    """Verify ``--markers``, ``--iterations`` etc. reach run_suite via ctx.obj.

    These options live on ``suite_app``'s callback, so the full CLI path is
    exercised to check wiring: CLI → callback sets ctx.obj → runner closure →
    run_suite reads parent context.
    """

    def _capture_ctx(self, cli_args: list[str], suite_name: str) -> dict:
        import click as _click

        captured: dict = {}

        def fake_run_suite(*_args, **_kwargs):
            ctx = _click.get_current_context(silent=True)
            while ctx is not None:
                if isinstance(ctx.obj, dict) and 'cov' in ctx.obj:
                    captured.update(ctx.obj)
                    return
                ctx = ctx.parent

        mock_logger = MagicMock()
        with patch('otto.cli.test.run_suite', fake_run_suite), \
             patch('otto.cli.test.logger', mock_logger):
            runner.invoke(suite_app, [*cli_args, suite_name])
        return captured

    def test_iterations_forwarded_via_ctx(self):
        @register_suite()
        class _CtxIterSuite:
            pass

        for name, sub_app in reversed(_SUITE_REGISTRY):
            if name == '_CtxIterSuite':
                suite_app.add_typer(sub_app)
                break

        ctx_obj = self._capture_ctx(['--iterations', '5'], '_CtxIterSuite')
        assert ctx_obj.get('iterations') == 5

    def test_markers_forwarded_via_ctx(self):
        @register_suite()
        class _CtxMarkSuite:
            pass

        for name, sub_app in reversed(_SUITE_REGISTRY):
            if name == '_CtxMarkSuite':
                suite_app.add_typer(sub_app)
                break

        ctx_obj = self._capture_ctx(
            ['--markers', 'not integration'], '_CtxMarkSuite',
        )
        assert ctx_obj.get('markers') == 'not integration'

    def test_defaults_when_omitted(self):
        @register_suite()
        class _CtxDefSuite:
            pass

        for name, sub_app in reversed(_SUITE_REGISTRY):
            if name == '_CtxDefSuite':
                suite_app.add_typer(sub_app)
                break

        ctx_obj = self._capture_ctx([], '_CtxDefSuite')
        assert ctx_obj.get('markers') == ''
        assert ctx_obj.get('iterations') == 0
        assert ctx_obj.get('duration') == 0
        assert ctx_obj.get('threshold') == 100.0
        assert ctx_obj.get('results') == ''


# ── --cov-dir option (destination override + validation) ─────────────────────

# Register a single suite once at module import; every --cov-dir test
# reuses it, varying only the CLI args it's invoked with.
@register_suite()
class _CovCtxSuite:  # noqa: D401
    """Fixture suite used for exercising the cov/cov-dir callback plumbing."""


def _capture_cov_ctx(cli_args: list[str]) -> tuple[int, dict, str]:
    """Invoke ``otto test <cli_args> _CovCtxSuite`` against the real suite_app.

    Callback options like ``--cov`` / ``--cov-dir`` are declared on
    ``suite_app`` itself, so we invoke through it (not an isolated app) to
    exercise the actual option wiring.

    Returns ``(exit_code, ctx_obj, output)``. ``ctx_obj`` is ``{}`` when the
    command aborts before the subcommand is reached (e.g. during option
    validation).
    """
    import click as _click

    # Attach the suite's sub-app to the real ``suite_app`` the first time
    # this helper runs. ``suite_app.registered_groups`` is initialised at
    # module load from the registry snapshot; later @register_suite() calls
    # (like ours at module scope) aren't automatically propagated.
    if not getattr(_CovCtxSuite, '_otto_attached', False):
        for name, sub_app in reversed(_SUITE_REGISTRY):
            if name == '_CovCtxSuite':
                suite_app.add_typer(sub_app)
                _CovCtxSuite._otto_attached = True  # type: ignore[attr-defined]
                break

    captured: dict = {}

    def fake_run_suite(*_args, **_kwargs):
        ctx = _click.get_current_context(silent=True)
        while ctx is not None:
            if isinstance(ctx.obj, dict) and 'cov' in ctx.obj:
                captured.update(ctx.obj)
                return
            ctx = ctx.parent

    mock_logger = MagicMock()
    with patch('otto.cli.test.run_suite', fake_run_suite), \
         patch('otto.cli.test.logger', mock_logger):
        result = runner.invoke(suite_app, [*cli_args, '_CovCtxSuite'])

    return result.exit_code, captured, result.output


class TestCovDirOption:
    def test_no_flags_disables_coverage(self):
        exit_code, ctx_obj, output = _capture_cov_ctx([])
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['cov'] is False
        assert ctx_obj['cov_dir'] is None

    def test_cov_flag_only_uses_default_dir(self):
        exit_code, ctx_obj, output = _capture_cov_ctx(['--cov'])
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['cov'] is True
        assert ctx_obj['cov_dir'] is None

    def test_cov_dir_implies_cov_and_records_path(self, tmp_path):
        target = tmp_path / 'custom'
        exit_code, ctx_obj, output = _capture_cov_ctx(['--cov-dir', str(target)])
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['cov'] is True
        assert ctx_obj['cov_dir'] == target.resolve()
        # Validation creates the directory eagerly.
        assert target.is_dir()

    def test_cov_with_cov_dir_records_path(self, tmp_path):
        target = tmp_path / 'both'
        exit_code, ctx_obj, output = _capture_cov_ctx(
            ['--cov', '--cov-dir', str(target)],
        )
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['cov'] is True
        assert ctx_obj['cov_dir'] == target.resolve()

    def test_cov_dir_nonempty_without_overwrite_aborts(self, tmp_path):
        target = tmp_path / 'existing'
        target.mkdir()
        (target / 'leftover.txt').write_text('stale')

        exit_code, ctx_obj, output = _capture_cov_ctx(['--cov-dir', str(target)])
        assert exit_code != 0
        assert ctx_obj == {}
        assert 'not empty' in output or '--overwrite-cov-dir' in output
        # Stale file preserved when we refuse to proceed.
        assert (target / 'leftover.txt').exists()

    def test_overwrite_cov_dir_clears_contents(self, tmp_path):
        target = tmp_path / 'to_clear'
        target.mkdir()
        (target / 'leftover.txt').write_text('stale')
        (target / 'sub').mkdir()
        (target / 'sub' / 'nested.txt').write_text('more stale')

        exit_code, ctx_obj, output = _capture_cov_ctx(
            ['--cov-dir', str(target), '--overwrite-cov-dir'],
        )
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['cov'] is True
        assert ctx_obj['cov_dir'] == target.resolve()
        assert target.is_dir()
        assert list(target.iterdir()) == []

    def test_cov_dir_pointing_at_file_fails(self, tmp_path):
        target = tmp_path / 'not_a_dir'
        target.write_text('i am a file')

        exit_code, _, output = _capture_cov_ctx(['--cov-dir', str(target)])
        assert exit_code != 0
        assert '--cov-dir' in output and (
            'is a file' in output or 'not a directory' in output
        )


# ── _run_coverage destination resolution ─────────────────────────────────────

class TestRunCoverageDestination:
    """Verify ``_run_coverage`` honours the override vs. default destination."""

    def _invoke(self, *, log_dir, override):
        import asyncio
        from otto.cli.test import _run_coverage

        repo = MagicMock()
        repo.sutDir = log_dir
        repo.name = 'repo'

        fetcher_instance = MagicMock()
        fetcher_instance.fetch_all = AsyncMock(return_value={})
        fetcher_instance.clean_remote = AsyncMock(return_value=None)

        with patch('otto.cli.test._get_cov_config',
                   return_value={'gcda_remote_dir': '/remote'}), \
             patch('otto.configmodule.all_hosts', return_value=[MagicMock()]), \
             patch('otto.coverage.fetcher.remote.GcdaFetcher',
                   return_value=fetcher_instance) as fetcher_cls:
            asyncio.run(_run_coverage([repo], log_dir, override))
        return fetcher_cls

    def test_override_used_when_provided(self, tmp_path):
        override = tmp_path / 'override'
        log_dir = tmp_path / 'log'
        override.mkdir()
        log_dir.mkdir()
        fetcher_cls = self._invoke(log_dir=log_dir, override=override)
        fetcher_cls.assert_called_once_with(override)

    def test_default_used_when_no_override(self, tmp_path):
        log_dir = tmp_path / 'log'
        log_dir.mkdir()
        fetcher_cls = self._invoke(log_dir=log_dir, override=None)
        fetcher_cls.assert_called_once_with(log_dir / 'cov')


# ── --cov-report option (report generation alongside collection) ─────────────

class TestCovReportOption:
    def test_no_flags_disables_report(self):
        exit_code, ctx_obj, output = _capture_cov_ctx([])
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['cov_report'] is False
        assert ctx_obj['cov_report_dir'] is None

    def test_cov_report_flag_enables_report_and_implies_cov(self):
        exit_code, ctx_obj, output = _capture_cov_ctx(['--cov-report'])
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['cov_report'] is True
        assert ctx_obj['cov'] is True
        assert ctx_obj['cov_report_dir'] is None

    def test_short_r_flag_enables_report(self):
        exit_code, ctx_obj, output = _capture_cov_ctx(['-r'])
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['cov_report'] is True
        assert ctx_obj['cov'] is True

    def test_cov_report_dir_implies_cov_report_and_cov(self, tmp_path):
        target = tmp_path / 'report'
        exit_code, ctx_obj, output = _capture_cov_ctx(
            ['--cov-report-dir', str(target)],
        )
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['cov_report'] is True
        assert ctx_obj['cov'] is True
        assert ctx_obj['cov_report_dir'] == target.resolve()
        # Validation creates the directory eagerly.
        assert target.is_dir()

    def test_cov_report_dir_nonempty_without_overwrite_aborts(self, tmp_path):
        target = tmp_path / 'existing'
        target.mkdir()
        (target / 'stale.html').write_text('stale')
        exit_code, ctx_obj, output = _capture_cov_ctx(
            ['--cov-report-dir', str(target)],
        )
        assert exit_code != 0
        assert ctx_obj == {}
        assert 'not empty' in output or '--overwrite-cov-report-dir' in output
        assert (target / 'stale.html').exists()

    def test_overwrite_cov_report_dir_clears_contents(self, tmp_path):
        target = tmp_path / 'to_clear'
        target.mkdir()
        (target / 'stale.html').write_text('stale')
        (target / 'sub').mkdir()
        (target / 'sub' / 'nested.html').write_text('nested')

        exit_code, ctx_obj, output = _capture_cov_ctx(
            ['--cov-report-dir', str(target), '--overwrite-cov-report-dir'],
        )
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['cov_report'] is True
        assert ctx_obj['cov_report_dir'] == target.resolve()
        assert list(target.iterdir()) == []

    def test_project_name_recorded(self):
        exit_code, ctx_obj, output = _capture_cov_ctx(
            ['--cov-report', '--project-name', 'My App'],
        )
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['project_name'] == 'My App'

    def test_project_name_default(self):
        exit_code, ctx_obj, output = _capture_cov_ctx([])
        assert exit_code == 0, f'output={output!r}'
        assert ctx_obj['project_name'] == 'Coverage Report'

    def test_cov_report_dir_pointing_at_file_fails(self, tmp_path):
        target = tmp_path / 'not_a_dir'
        target.write_text('i am a file')
        exit_code, _, output = _capture_cov_ctx(
            ['--cov-report-dir', str(target)],
        )
        assert exit_code != 0
        assert '--cov-report-dir' in output and (
            'is a file' in output or 'not a directory' in output
        )


# ── run_suite post-test report generation ────────────────────────────────────

class TestRunSuiteReport:
    """Verify run_suite wires --cov-report to run_coverage_report correctly.

    When ``--cov-report`` is enabled, ``run_suite`` should forward the right
    cov_dir, report_dir, and project_name to the shared reporter helper.
    """

    def _invoke(self, *, parent_opts, log_dir):
        from otto.cli.test import run_suite

        repo = MagicMock()
        repo.tests = [log_dir]
        repo.sutDir = log_dir
        repo.name = 'repo'

        import click as _click

        mock_store = MagicMock()
        mock_store.overall_pct.return_value = 50.0
        mock_store.file_count.return_value = 1
        mock_run_report = AsyncMock(return_value=mock_store)

        class _FakeCtx:
            obj = parent_opts
            parent = None

        mock_logger = MagicMock()
        mock_logger.output_dir = log_dir

        with patch('otto.cli.test.getRepos', return_value=[repo]), \
             patch('otto.cli.test.logger', mock_logger), \
             patch('otto.cli.test.pytest.main'), \
             patch('otto.cli.test._run_coverage', new=AsyncMock()), \
             patch('otto.cli.test._cov_clean_remotes', new=AsyncMock()), \
             patch('otto.coverage.reporter.run_coverage_report',
                   new=mock_run_report), \
             patch.object(_click, 'get_current_context',
                          return_value=_FakeCtx()):
            class _FakeSuite:
                pass
            run_suite(_FakeSuite, str(log_dir / 'fake.py'), None)

        return mock_run_report

    def test_no_cov_report_means_no_call(self, tmp_path):
        log_dir = tmp_path / 'log'
        log_dir.mkdir()
        mock = self._invoke(
            parent_opts={'cov': True, 'cov_dir': None, 'cov_clean': False,
                         'cov_report': False, 'cov_report_dir': None,
                         'overwrite_cov_report_dir': False,
                         'project_name': 'Coverage Report'},
            log_dir=log_dir,
        )
        mock.assert_not_called()

    def test_default_report_dir_under_log_dir(self, tmp_path):
        log_dir = tmp_path / 'log'
        log_dir.mkdir()
        mock = self._invoke(
            parent_opts={'cov': True, 'cov_dir': None, 'cov_clean': False,
                         'cov_report': True, 'cov_report_dir': None,
                         'overwrite_cov_report_dir': False,
                         'project_name': 'Coverage Report'},
            log_dir=log_dir,
        )
        mock.assert_called_once()
        args = mock.call_args.args
        assert args[0] == [log_dir / 'cov']
        assert args[1] == log_dir / 'cov_report'
        assert (log_dir / 'cov_report').is_dir()

    def test_explicit_report_dir_and_project_name(self, tmp_path):
        log_dir = tmp_path / 'log'
        log_dir.mkdir()
        report_dir = tmp_path / 'my_report'
        report_dir.mkdir()
        mock = self._invoke(
            parent_opts={'cov': True, 'cov_dir': None, 'cov_clean': False,
                         'cov_report': True, 'cov_report_dir': report_dir,
                         'overwrite_cov_report_dir': False,
                         'project_name': 'My App'},
            log_dir=log_dir,
        )
        mock.assert_called_once()
        args = mock.call_args.args
        assert args[1] == report_dir
        assert mock.call_args.kwargs['project_name'] == 'My App'

    def test_cov_dir_override_used_as_source(self, tmp_path):
        log_dir = tmp_path / 'log'
        log_dir.mkdir()
        cov_dir = tmp_path / 'custom_cov'
        cov_dir.mkdir()
        mock = self._invoke(
            parent_opts={'cov': True, 'cov_dir': cov_dir, 'cov_clean': False,
                         'cov_report': True, 'cov_report_dir': None,
                         'overwrite_cov_report_dir': False,
                         'project_name': 'Coverage Report'},
            log_dir=log_dir,
        )
        mock.assert_called_once()
        args = mock.call_args.args
        assert args[0] == [cov_dir]
