"""Run a registered OttoSuite test suite.

Each test suite decorated with ``@register_suite()`` appears as a subcommand of
``otto test``.  Suite-specific options (declared in the suite's inner ``Options``
dataclass) are automatically registered as Typer parameters with full type
enforcement and ``--help`` documentation.

**Markers**

``integration``
    Requires live Vagrant VMs.  Skip with ``--markers "not integration"``.

``timeout(seconds)``
    Fail the test if it runs longer than *seconds*.

``retry(n)``
    Retry a failing test up to *n* times before reporting failure.

**Listing tests**

``--list-suites``  List test suites with run syntax and exit.

**Options on ``otto test`` (before the suite name)**

``--markers / -m EXPRESSION``
    pytest ``-m`` marker expression applied after collection.

``--iterations / -i N``
    Repeat each test N times within a single setup/teardown cycle (0 = disabled).

``--duration / -d SECONDS``
    Repeat tests for N seconds within a single setup/teardown cycle (0 = disabled).

``--threshold FLOAT``
    Minimum per-test pass rate percentage required in stability mode (0-100,
    default: 100).

``--results PATH``
    Write test results (JUnit XML) to PATH (default: auto-written to the log directory).

When both ``--iterations`` and ``--duration`` are specified, testing stops when
either limit is reached first.

``--cov``
    Fetch ``.gcda`` files from remote hosts after the suite finishes and
    place them in a ``cov/`` directory in the suite's output directory.

``--cov-dir PATH``
    Write coverage data to ``PATH`` instead of the default
    ``<output_dir>/cov``.  Implies ``--cov``.  The directory is created
    if missing; if it already exists and is non-empty, the command
    aborts unless ``--overwrite-cov-dir`` is also given.

``--overwrite-cov-dir``
    Clear the contents of the ``--cov-dir`` destination before the run
    so stale data from a previous invocation cannot be mixed with the
    new results.

``--cov-clean / --no-cov-clean``
    Delete ``.gcda`` files on remote hosts before the test run.
    Enabled by default; use ``--no-cov-clean`` to keep stale data.

``--cov-report / -r``
    After coverage collection, render an HTML report.  Implies ``--cov``.
    Default location: ``<output_dir>/cov_report``.

``--cov-report-dir PATH``
    Write the HTML report to ``PATH`` instead of the default.  Implies
    ``--cov-report`` (and therefore ``--cov``).  Empty/overwrite rules
    match ``--cov-dir``: created if missing, aborts if non-empty unless
    ``--overwrite-cov-report-dir`` is also given.

``--overwrite-cov-report-dir``
    Clear the contents of ``--cov-report-dir`` before the report is rendered.

``--project-name STR``
    Title shown in the HTML report header (only used with ``--cov-report``).

**Examples**::

    otto test --list-suites
    otto test TestMyDevice --help
    otto test TestMyDevice --device-type switch --firmware 2.1
    otto test --iterations 50 --threshold 95 TestMyDevice
    otto test --duration 300 --threshold 90 TestMyDevice
    otto test --iterations 100 --duration 60 TestMyDevice
    otto test --cov TestMyDevice
    otto test --cov-dir /tmp/myrun TestMyDevice
    otto test --cov-dir /tmp/myrun --overwrite-cov-dir TestMyDevice
    otto test --cov --no-cov-clean TestMyDevice
    otto test --cov --cov-report TestMyDevice
    otto test -r --cov-report-dir /tmp/myreport TestMyDevice
"""

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional, cast

if TYPE_CHECKING:
    from ..suite.plugin import StabilityCollector

import pytest
import typer
from rich import print as rprint
from rich.table import Table

from ..configmodule import getRepos
from ..configmodule.repo import Repo
from ..logger import getOttoLogger
from ..suite.plugin import OttoPlugin
from ..suite.register import _SUITE_REGISTRY, OttoOptionsPlugin

logger = getOttoLogger()


# ---------------------------------------------------------------------------
# Helpers shared with register.py runner functions
# ---------------------------------------------------------------------------

def resolve_suite(suite: str, repos: list[Repo]) -> str:
    """Expand a sutDir-relative suite path to an absolute path for pytest."""
    file_part, _, suffix = suite.partition('::')
    p = Path(file_part)
    if p.is_absolute():
        return suite
    for repo in repos:
        candidate = (repo.sutDir / p).resolve()
        if candidate.exists():
            return f'{candidate}::{suffix}' if suffix else str(candidate)
    return suite


def run_suite(
    suite_class: type,
    suite_file: str,
    opts_instance: object | None,
) -> None:
    """Execute a registered suite via pytest.main().

    Runner options (``--markers``, ``--iterations``, ``--duration``,
    ``--threshold``, ``--results``) and coverage options (``--cov`` /
    ``--cov-clean``) are read from the parent Typer context set by the
    ``otto test`` callback.
    """
    import asyncio

    # Read runner/coverage flags from the parent Typer context.
    # Walk up the context chain (child → parent) to find the dict set by the
    # ``otto test`` callback; ``find_root()`` would overshoot to the top-level
    # ``otto`` app which does not carry these flags.
    import click
    parent_opts: dict[str, object] = {}
    try:
        ctx = click.get_current_context(silent=True)
        while ctx is not None:
            if isinstance(ctx.obj, dict) and 'cov' in ctx.obj:
                parent_opts = ctx.obj
                break
            ctx = ctx.parent
    except RuntimeError:
        pass
    markers    = str(parent_opts.get('markers', ''))
    iterations = int(cast(int, parent_opts.get('iterations', 0)))
    duration   = int(cast(int, parent_opts.get('duration', 0)))
    threshold  = float(cast(float, parent_opts.get('threshold', 100.0)))
    results    = str(parent_opts.get('results', ''))
    cov: bool = bool(parent_opts.get('cov', False))
    cov_dir_override: Optional[Path] = cast(Optional[Path], parent_opts.get('cov_dir'))
    cov_clean: bool = bool(parent_opts.get('cov_clean', False))
    cov_report: bool = bool(parent_opts.get('cov_report', False))
    cov_report_dir: Optional[Path] = cast(Optional[Path],
                                          parent_opts.get('cov_report_dir'))
    overwrite_cov_report_dir: bool = bool(
        parent_opts.get('overwrite_cov_report_dir', False))
    project_name: str = str(parent_opts.get('project_name', 'Coverage Report'))

    repos = getRepos()
    sut_test_dirs = [path for repo in repos for path in repo.tests]
    log_dir = logger.output_dir
    results_path = results or str(log_dir / 'junit.xml')

    # Pre-run cleanup of .gcda files on remotes
    if cov and cov_clean:
        asyncio.run(_cov_clean_remotes(repos))
        # Rebuild host connections so pytest gets fresh ones on its own loop.
        from ..configmodule import all_hosts
        for host in all_hosts():
            host.rebuild_connections()

    base_args: list[str] = [
        suite_file,
        '-k', suite_class.__name__,
        '-s',
        '-o', 'asyncio_mode=auto',
        '--no-cov',
        '--no-header',
        '--override-ini', 'log_cli=false',
        '--override-ini', 'addopts=',
        # pytest-asyncio registers anyio for assertion rewriting, but anyio is
        # already imported by the time pytest.main() is called from within otto.
        # The warning is harmless (anyio's internals don't affect test results)
        # so suppress it here rather than polluting suite output.
        '--override-ini', 'filterwarnings=ignore::pytest.PytestAssertRewriteWarning',
    ]
    if markers:
        base_args += ['-m', markers]

    is_stability = iterations > 0 or duration > 0
    otto_plugin = OttoPlugin(
        sut_test_dirs=sut_test_dirs, cov=cov,
        iterations=iterations, duration=duration,
    )
    options_plugin = OttoOptionsPlugin(opts_instance)

    collector: Optional['StabilityCollector'] = None
    if is_stability:
        from ..suite.plugin import StabilityCollector as _StabilityCollector
        collector = _StabilityCollector()
        otto_plugin._stability_collector = collector

    pytest.main(
        base_args + [f'--junitxml={results_path}'],
        plugins=[otto_plugin, options_plugin],
    )

    if is_stability and collector is not None:
        _print_stability_report(suite_class.__name__, collector,
                                iterations, duration, threshold, log_dir)

    # Post-test coverage collection
    if cov:
        asyncio.run(_run_coverage(repos, log_dir, cov_dir_override))

    if cov_report:
        from ..coverage.reporter import run_coverage_report
        cov_dir = cov_dir_override if cov_dir_override else log_dir / 'cov'
        report_dir = (cov_report_dir if cov_report_dir is not None
                      else log_dir / 'cov_report')
        # Default path lives inside freshly-created log_dir → always empty;
        # explicit path was validated in the callback. Safe to call either way.
        _prepare_empty_dir(report_dir, overwrite=overwrite_cov_report_dir,
                           flag_name='--cov-report-dir')
        store = asyncio.run(run_coverage_report(
            [cov_dir], report_dir, project_name=project_name,
        ))
        if store is not None:
            logger.info('Coverage: %.1f%% overall (%d files)',
                        store.overall_pct(), store.file_count())
            logger.info('Report: %s', report_dir / 'index.html')


def _print_stability_report(
    suite_name: str,
    collector: 'StabilityCollector',
    iterations: int,
    duration: int,
    threshold: float,
    log_dir: Path,
) -> None:
    """Print and save a per-test pass-rate stability report.

    Parameters
    ----------
    threshold :
        Minimum pass rate as a percentage (0-100).
    """
    mode_parts: list[str] = []
    if iterations > 0:
        mode_parts.append(f'{iterations} iterations')
    if duration > 0:
        mode_parts.append(f'{duration}s duration')
    mode = ', '.join(mode_parts) or 'stability'

    lines: list[str] = [
        f'Stability Results for {suite_name} ({mode}, threshold {threshold:.0f}%):',
    ]
    any_unstable = False
    for test_name, (passed, total) in collector.results.items():
        rate_pct = (passed / total * 100) if total else 0.0
        status = 'STABLE' if rate_pct >= threshold else 'UNSTABLE'
        if status == 'UNSTABLE':
            any_unstable = True
        lines.append(f'  {test_name:<40} {passed}/{total} ({rate_pct:.0f}%)  {status}')
    lines.append(f'Overall: {"FAIL" if any_unstable else "PASS"}')

    report = '\n'.join(lines)
    logger.info(report)
    report_path = log_dir / 'stability_report.txt'
    report_path.write_text(report)

    if any_unstable:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Listing helpers (shared between callback eager options)
# ---------------------------------------------------------------------------

def _list_tests_display(panel_method: str) -> None:
    panels = [getattr(repo, panel_method)(repo.collectTests()) for repo in getRepos()]
    table = Table(show_header=False, show_footer=False, box=None, expand=True, padding=(0, 1, 1, 1))
    for _ in panels:
        table.add_column(ratio=1)
    table.add_row(*panels)
    rprint(table)


def list_suites_callback(value: bool) -> None:
    if not value:
        return
    _list_tests_display('getTestSuitesPanel')
    raise typer.Exit()


# ---------------------------------------------------------------------------
# suite_app — multi-subcommand Typer app (mirrors run_app structure)
# ---------------------------------------------------------------------------

suite_app = typer.Typer(
    name='test',
    no_args_is_help=True,
    context_settings={
        'help_option_names': ['-h', '--help'],
    },
)


@suite_app.callback()
def main(
    ctx: typer.Context,
    list_suites: Annotated[bool,
        typer.Option('--list-suites',
            callback=list_suites_callback,
            is_eager=True,
            help='List test suites with run syntax and exit.',
        )
    ] = False,
    markers: Annotated[str, typer.Option(
        '--markers', '-m', metavar='EXPRESSION',
        help='pytest -m marker expression applied after collection.',
    )] = '',
    iterations: Annotated[int, typer.Option(
        '--iterations', '-i',
        help='Repeat each test N times within a single setup/teardown cycle (0 = disabled).',
    )] = 0,
    duration: Annotated[int, typer.Option(
        '--duration', '-d',
        help='Repeat tests for N seconds within a single setup/teardown cycle (0 = disabled).',
    )] = 0,
    threshold: Annotated[float, typer.Option(
        '--threshold',
        help='Minimum per-test pass rate percentage required in stability mode (0-100).',
    )] = 100.0,
    results: Annotated[str, typer.Option(
        '--results', metavar='PATH',
        help='Write test results (JUnit XML) to PATH (default: auto in log dir).',
    )] = '',
    cov: Annotated[bool, typer.Option(
        '--cov',
        help='Collect gcov coverage from remote hosts after the suite finishes.',
    )] = False,
    cov_dir: Annotated[Optional[Path], typer.Option(
        '--cov-dir',
        help=(
            'Directory to write coverage data to. Implies --cov. '
            'Default when --cov is used alone: <output_dir>/cov.'
        ),
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    )] = None,
    overwrite_cov_dir: Annotated[bool, typer.Option(
        '--overwrite-cov-dir',
        help=(
            'Allow --cov-dir to target an existing non-empty directory '
            '(its contents will be cleared before the run).'
        ),
    )] = False,
    cov_clean: Annotated[bool, typer.Option(
        help='Delete .gcda files on remote hosts before the test run.',
    )] = True,
    cov_report: Annotated[bool, typer.Option(
        '--cov-report', '-r',
        help=(
            'Generate an HTML coverage report after the suite finishes. '
            'Implies --cov. Default location: <output_dir>/cov_report.'
        ),
    )] = False,
    cov_report_dir: Annotated[Optional[Path], typer.Option(
        '--cov-report-dir',
        help=(
            'Directory to write the HTML coverage report to. '
            'Implies --cov-report (and --cov). '
            'Default when --cov-report is used alone: <output_dir>/cov_report.'
        ),
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    )] = None,
    overwrite_cov_report_dir: Annotated[bool, typer.Option(
        '--overwrite-cov-report-dir',
        help=(
            'Allow --cov-report-dir to target an existing non-empty directory '
            '(its contents will be cleared before the report is rendered).'
        ),
    )] = False,
    project_name: Annotated[str, typer.Option(
        '--project-name',
        help='Title shown in the HTML report header (only used with --cov-report).',
    )] = 'Coverage Report',
) -> None:
    if ctx.resilient_parsing:
        return
    ctx.ensure_object(dict)

    if cov_dir is not None:
        _prepare_empty_dir(cov_dir, overwrite=overwrite_cov_dir,
                           flag_name='--cov-dir')

    cov_report_effective = cov_report or cov_report_dir is not None
    if cov_report_dir is not None:
        _prepare_empty_dir(cov_report_dir, overwrite=overwrite_cov_report_dir,
                           flag_name='--cov-report-dir')

    ctx.obj['markers'] = markers
    ctx.obj['iterations'] = iterations
    ctx.obj['duration'] = duration
    ctx.obj['threshold'] = threshold
    ctx.obj['results'] = results
    ctx.obj['cov'] = cov or cov_dir is not None or cov_report_effective
    ctx.obj['cov_dir'] = cov_dir
    ctx.obj['cov_clean'] = cov_clean
    ctx.obj['cov_report'] = cov_report_effective
    ctx.obj['cov_report_dir'] = cov_report_dir
    ctx.obj['overwrite_cov_report_dir'] = overwrite_cov_report_dir
    ctx.obj['project_name'] = project_name
    if ctx.invoked_subcommand is not None:
        logger.create_output_dir('test', ctx.invoked_subcommand)
        from ..configmodule import tryGetConfigModule
        from ..reservations import gate
        gate(tryGetConfigModule())


# ---------------------------------------------------------------------------
# Register suites discovered during configmodule initialization
# ---------------------------------------------------------------------------

for _, _suite_sub_app in _SUITE_REGISTRY:
    suite_app.add_typer(_suite_sub_app)


# ---------------------------------------------------------------------------
# Coverage helpers
# ---------------------------------------------------------------------------

def _prepare_empty_dir(path: Path, *, overwrite: bool, flag_name: str) -> None:
    """Ensure ``path`` is an empty, existing directory.

    Typer's ``click.Path(file_okay=False, dir_okay=True, resolve_path=True)``
    has already rejected non-directory targets; this helper only handles
    create-if-missing and the empty/overwrite contract.

    ``flag_name`` is the user-visible flag (e.g. ``--cov-dir``) used in the
    error message when the target is non-empty and overwrite is not set;
    the corresponding overwrite flag name is derived from it.
    """
    path.mkdir(parents=True, exist_ok=True)
    if not any(path.iterdir()):
        return
    if not overwrite:
        overwrite_flag = f'--overwrite-{flag_name.lstrip("-")}'
        raise typer.BadParameter(
            f'{flag_name} target {path} is not empty; '
            f'pass {overwrite_flag} to clear it.',
            param_hint=flag_name,
        )
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


async def _cov_clean_remotes(repos: list['Repo']) -> None:
    """Delete .gcda files on all configured remote hosts before a test run."""
    from ..configmodule import all_hosts
    from ..coverage.fetcher.remote import GcdaFetcher

    cov_config = _get_cov_config(repos)
    if not cov_config:
        return

    gcda_remote_dir = cov_config.get('gcda_remote_dir', '')
    if not gcda_remote_dir:
        logger.warning('coverage.gcda_remote_dir not configured — skipping pre-run cleanup')
        return

    if not any(all_hosts()):
        return

    fetcher = GcdaFetcher(Path('/tmp'))
    await fetcher.clean_remote(gcda_remote_dir)


async def _run_coverage(
    repos: list['Repo'],
    log_dir: Path,
    cov_dir_override: Optional[Path] = None,
) -> None:
    """Fetch .gcda files from remote hosts into the coverage destination.

    When ``cov_dir_override`` is provided, coverage data is written there;
    otherwise the default ``<log_dir>/cov`` is used.
    """
    from ..configmodule import all_hosts
    from ..coverage.fetcher.remote import GcdaFetcher

    cov_config = _get_cov_config(repos)
    if not cov_config:
        logger.warning(
            '--cov was specified but no [coverage] section found in .otto/settings.toml'
        )
        return

    gcda_remote_dir = cov_config.get('gcda_remote_dir', '')
    if not gcda_remote_dir:
        logger.error('coverage.gcda_remote_dir is required in .otto/settings.toml')
        return

    hosts = list(all_hosts())
    if not hosts:
        logger.warning('No hosts available for coverage collection')
        return

    # Hosts may carry stale connections from pytest's event loop.
    # Rebuild their connection state so they reconnect on the current loop.
    for host in hosts:
        host.rebuild_connections()

    cov_dir = cov_dir_override if cov_dir_override else log_dir / 'cov'
    fetcher = GcdaFetcher(cov_dir)
    host_dirs = await fetcher.fetch_all(gcda_remote_dir)

    if host_dirs:
        logger.info('Coverage data collected to %s (%d hosts)', cov_dir, len(host_dirs))
        await fetcher.clean_remote(gcda_remote_dir)

        # Write metadata so ``otto cov report`` can find the source root
        # and per-host toolchains without relying on the working directory.
        import json
        cov_repo = _get_cov_repo(repos)
        if cov_repo:
            meta: dict[str, object] = {
                'repo_name': cov_repo.name,
                'sut_dir': str(cov_repo.sutDir.resolve()),
            }
            # Persist per-host toolchain info for the report step
            toolchains: dict[str, dict[str, str]] = {}
            for host in hosts:
                tc = host.toolchain
                toolchains[host.id] = {
                    'sysroot': str(tc.sysroot),
                    'lcov': str(tc.lcov),
                    'gcov': str(tc.gcov),
                }
            meta['toolchains'] = toolchains
            (cov_dir / '.otto_cov_meta.json').write_text(json.dumps(meta, indent=2))
    else:
        logger.warning('No .gcda files fetched from any host')


def _get_cov_repo(repos: list['Repo']) -> 'Repo | None':
    """Return the first repo with a ``[coverage]`` section in its settings."""
    for repo in repos:
        if repo.settings.get('coverage'):
            return repo
    return None


def _get_cov_config(repos: list['Repo']) -> dict:
    """Extract the ``[coverage]`` config from the first repo that has one."""
    repo = _get_cov_repo(repos)
    return repo.settings['coverage'] if repo else {}
