"""Run a registered OttoSuite test suite, or a suite-less test selection.

Each ``Test*``-named ``OttoSuite`` subclass auto-registers as a subcommand of
``otto test``.  Suite-specific options (declared in the suite's inner ``Options``
dataclass) are automatically registered as Typer parameters with full type
enforcement and ``--help`` documentation.

When no suite subcommand is given, ``--tests`` and/or ``--markers`` run a
suite-less selection instead: exact test names (optionally ``Class::name``
qualified) and/or a marker expression are resolved against every repo's
collected tests, and pytest runs once per repo whose selection matched.
Plain pytest functions (not just ``OttoSuite`` classes) are runnable this
way.

**Markers**

``integration``
    Requires live Vagrant VMs.  Skip with ``--markers "not integration"``.

``timeout(seconds)``
    Fail the test if it runs longer than *seconds*.

``retry(n)``
    Retry a failing test up to *n* times before reporting failure.

**Listing tests**

``--list-suites``   List test suites with run syntax and exit.

``--list-tests``    List the selected tests (optionally narrowed by a suite name
                    and/or ``--markers``) and exit.

``--list-markers``  List the markers available to ``--markers`` and exit.

**Options on ``otto test`` (before the suite name)**

``--markers / -m EXPRESSION``
    pytest ``-m`` marker expression applied after collection.  With no suite
    subcommand, runs the marker selection across every repo (one pytest
    session per repo) instead of requiring a suite name.

``--tests NAME[,NAME...]``
    Run specific tests by exact name — no suite subcommand needed.
    Comma-separated; bare names match every collected test with that
    function name (all parametrizations, across suites and repos);
    ``TestClass::name`` disambiguates.  Combine with ``--markers`` to narrow.
    Unknown names raise a loud error with did-you-mean suggestions.

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

``--monitor``
    Enable host performance monitoring for the duration of the run.  Samples
    every host (or those matched by ``--monitor-hosts``) on a fixed interval
    and emits per-test start/end events automatically.  At the end of the run
    a JSON snapshot of all metrics and events is written to
    ``<output_dir>/monitor.json``.

``--monitor-interval SECONDS``
    Sampling interval for ``--monitor`` (default: 5).

``--monitor-output PATH``
    Override the destination for the captured monitor data.  Format inferred
    from the suffix: ``.json`` (default) writes a self-contained snapshot,
    ``.db`` writes a SQLite database loadable via ``otto monitor --file``.

``--monitor-hosts REGEX``
    Restrict ``--monitor`` to host IDs matching this regex (``re.search``).

**Examples**::

    otto test --list-suites
    otto test --list-markers
    otto test --list-tests
    otto test --list-tests --markers slow
    otto test --list-tests TestMyDevice
    otto test --tests test_login
    otto test --tests TestB::test_login,test_plain
    otto test -m slow
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

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from rich.panel import Panel

import typer
from rich import print as rprint
from rich.table import Table

from ..config import get_repos
from ..context import get_context
from ..suite.register import SUITES

# The suite-run engine lives in ``otto.suite.run`` (library-extraction Phase A):
# ``run_suite`` is called there directly by the suite runner (see
# ``otto.suite.register``). ``otto.cli.test`` keeps only the callback that
# stores ``RunOptions`` in ``ctx.meta`` and the thin ``run_selection`` adapter,
# so it imports just those names — the run-options record/key, the library
# selection entrypoint, and the two selection exception types it translates.
from ..suite.run import RUN_OPTIONS_KEY, NoTestsMatchedError, RunOptions
from ..suite.run import run_selection as _run_selection_lib
from ..suite.selection import UnknownSelectionError
from .invoke import make_registry_group

# ---------------------------------------------------------------------------
# Helpers shared with register.py runner functions
# ---------------------------------------------------------------------------


def _tests_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--tests``: static floor + pytest-collected names.

    The floor is the always-available ``ast`` scan
    (:func:`~otto.config.completion_cache.collect_test_names`, preferring
    the cache snapshot) — every statically written ``def test_*`` / ``Test*``
    method, no test code run.

    On top of it, the *pytest-collected* set adds dynamically generated tests.
    When that set is cold (never collected, or a test file changed), a single
    bounded background collection warms it — so the first ``--tests`` TAB may be
    slow, but subsequent ones (and every one after any real ``otto test`` run)
    are fast and complete. Comma-separated, so only the in-progress segment is
    completed. ``--tests`` matches by base name, so parametrizations collapse to
    their base (``test_x`` runs every ``test_x[...]``).
    """
    from ..config import get_completion_names, get_repos
    from ..config.completion_cache import (
        collect_test_names,
        maybe_warm_collected_tests,
        read_collected_tests,
    )
    from ..utils import complete_comma_list

    repos = get_repos()
    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("tests"), list):
        names = set(cached["tests"])
    else:
        names = set(collect_test_names(repos))

    collected = read_collected_tests(repos)
    if collected is None:
        collected = maybe_warm_collected_tests(repos)
    if collected:
        names.update(collected)

    return complete_comma_list(sorted(names), incomplete)


def run_selection(ctx: typer.Context) -> None:
    """Run a suite-less selection (--tests and/or -m) — one session per repo.

    Thin CLI adapter over :func:`otto.suite.run.run_selection`: lifts run
    options out of ``ctx.meta``, calls the library engine, and maps its
    library exceptions onto Typer's conventions. An unknown ``--tests`` name
    (library ``UnknownSelectionError``, carrying the did-you-mean message)
    becomes ``typer.BadParameter`` with the same message and ``param_hint``.
    The "nothing matched" case (library ``NoTestsMatchedError``) becomes the
    historical red ``rprint`` + exit 1. Both subclass ``ValueError``; catching
    them by their specific types (``UnknownSelectionError`` first, then
    ``NoTestsMatchedError``) keeps an unrelated pipeline ``ValueError`` from
    being misreported as a no-match — it propagates untouched.
    """
    stored = ctx.meta.get(RUN_OPTIONS_KEY)
    opts = stored if isinstance(stored, RunOptions) else RunOptions()

    _log_dir = get_context().output_dir
    if _log_dir is None:
        raise RuntimeError("output_dir is not set; command_preamble must run before run_selection")

    try:
        result = _run_selection_lib(run_options=opts, output_dir=_log_dir)
    except UnknownSelectionError as e:
        raise typer.BadParameter(str(e), param_hint=e.param_hint) from None
    except NoTestsMatchedError:
        rprint("[red]No tests matched the selection.[/red]")
        raise typer.Exit(code=1) from None

    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


# ---------------------------------------------------------------------------
# Listing helpers (shared between callback eager options)
# ---------------------------------------------------------------------------


def _render_panels(panels: "list[Panel]") -> None:
    table = Table(show_header=False, show_footer=False, box=None, expand=True, padding=(0, 1, 1, 1))
    for _ in panels:
        table.add_column(ratio=1)
    table.add_row(*panels)
    rprint(table)


def list_suites_callback(value: bool) -> None:
    """Print all available test suites (one panel per repo) and exit when the flag is set."""
    if not value:
        return
    panels = [repo.get_test_suites_panel() for repo in get_repos()]
    _render_panels(panels)
    raise typer.Exit


def list_markers_callback(value: bool) -> None:
    """Print the markers available to --markers (one panel per repo) and exit."""
    if not value:
        return
    panels = [repo.get_markers_panel() for repo in get_repos()]
    _render_panels(panels)
    raise typer.Exit


# ---------------------------------------------------------------------------
# suite_app — multi-subcommand Typer app (mirrors run_app structure)
# ---------------------------------------------------------------------------

suite_app = typer.Typer(
    name="test",
    invoke_without_command=True,
    cls=make_registry_group(SUITES),
    # Explicit user-facing help: without it, typer falls back to the group
    # callback's docstring — an internal note about ctx.meta plumbing that
    # `otto test --help` (and the docs' captured terminal blocks) would show.
    help=(
        "Run a registered test suite by name, or select tests directly with"
        " --tests / -m — no suite required."
    ),
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


@suite_app.callback()
def main(  # noqa: PLR0913 — CLI command params
    ctx: typer.Context,
    list_suites: Annotated[  # noqa: ARG001 — required by Typer eager callback option signature
        bool,
        typer.Option(
            "--list-suites",
            callback=list_suites_callback,
            is_eager=True,
            help="List test suites with run syntax and exit.",
        ),
    ] = False,
    list_markers: Annotated[  # noqa: ARG001 — required by Typer eager callback option signature
        bool,
        typer.Option(
            "--list-markers",
            callback=list_markers_callback,
            is_eager=True,
            help="List the markers available to --markers and exit.",
        ),
    ] = False,
    list_tests: Annotated[
        bool,
        typer.Option(
            "--list-tests",
            help="List the selected tests (optionally narrowed by a suite name / --markers) and exit.",  # noqa: E501
        ),
    ] = False,
    markers: Annotated[
        str,
        typer.Option(
            "--markers",
            "-m",
            metavar="EXPRESSION",
            help=(
                "pytest -m marker expression applied after collection. With no "
                "suite name, runs a suite-less selection across all repos."
            ),
        ),
    ] = "",
    tests: Annotated[
        str,
        typer.Option(
            "--tests",
            metavar="NAME[,NAME...]",
            autocompletion=_tests_completer,
            help=(
                "Run specific tests by exact name, across all suites and repos — "
                "no suite subcommand needed. Comma-separated; TestClass::name "
                "disambiguates. Combine with --markers to narrow."
            ),
        ),
    ] = "",
    iterations: Annotated[
        int,
        typer.Option(
            "--iterations",
            "-i",
            help="Repeat each test N times within a single setup/teardown cycle (0 = disabled).",
        ),
    ] = 0,
    duration: Annotated[
        int,
        typer.Option(
            "--duration",
            "-d",
            help="Repeat tests for N seconds within a single setup/teardown cycle (0 = disabled).",
        ),
    ] = 0,
    threshold: Annotated[
        float,
        typer.Option(
            "--threshold",
            help="Minimum per-test pass rate percentage required in stability mode (0-100).",
        ),
    ] = 100.0,
    results: Annotated[
        str,
        typer.Option(
            "--results",
            metavar="PATH",
            help="Write test results (JUnit XML) to PATH (default: auto in log dir).",
        ),
    ] = "",
    cov: Annotated[
        bool,
        typer.Option(
            "--cov",
            help="Collect gcov coverage from remote hosts after the suite finishes.",
        ),
    ] = False,
    cov_dir: Annotated[
        Path | None,
        typer.Option(
            "--cov-dir",
            help=(
                "Directory to write coverage data to. Implies --cov. "
                "Default when --cov is used alone: <output_dir>/cov."
            ),
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    overwrite_cov_dir: Annotated[
        bool,
        typer.Option(
            "--overwrite-cov-dir",
            help=(
                "Allow --cov-dir to target an existing non-empty directory "
                "(its contents will be cleared before the run)."
            ),
        ),
    ] = False,
    cov_clean: Annotated[
        bool,
        typer.Option(
            help="Delete .gcda files on remote hosts before the test run.",
        ),
    ] = True,
    cov_report: Annotated[
        bool,
        typer.Option(
            "--cov-report",
            "-r",
            help=(
                "Generate an HTML coverage report after the suite finishes. "
                "Implies --cov. Default location: <output_dir>/cov_report."
            ),
        ),
    ] = False,
    cov_report_dir: Annotated[
        Path | None,
        typer.Option(
            "--cov-report-dir",
            help=(
                "Directory to write the HTML coverage report to. "
                "Implies --cov-report (and --cov). "
                "Default when --cov-report is used alone: <output_dir>/cov_report."
            ),
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    overwrite_cov_report_dir: Annotated[
        bool,
        typer.Option(
            "--overwrite-cov-report-dir",
            help=(
                "Allow --cov-report-dir to target an existing non-empty directory "
                "(its contents will be cleared before the report is rendered)."
            ),
        ),
    ] = False,
    project_name: Annotated[
        str,
        typer.Option(
            "--project-name",
            help="Title shown in the HTML report header (only used with --cov-report).",
        ),
    ] = "Coverage Report",
    monitor: Annotated[
        bool,
        typer.Option(
            help="Collect host performance metrics for the entire test run.",
        ),
    ] = False,
    monitor_interval: Annotated[
        float,
        typer.Option(
            "--monitor-interval",
            metavar="SECONDS",
            help="Sampling interval for --monitor.",
            min=1.0,
        ),
    ] = 5.0,
    monitor_output: Annotated[
        Path | None,
        typer.Option(
            "--monitor-output",
            metavar="PATH",
            help=(
                "Override the destination for monitor data. Format inferred from "
                "suffix (.json or .db). Default: <output_dir>/monitor.json."
            ),
        ),
    ] = None,
    monitor_hosts: Annotated[
        str | None,
        typer.Option(
            "--monitor-hosts",
            metavar="REGEX",
            help="Regex matched against host IDs to restrict --monitor (re.search).",
        ),
    ] = None,
) -> None:
    """Collect ``otto test`` run options and store them in ``ctx.meta`` for suite runners.

    Validates coverage and report directories and resolves implied flags (e.g.
    ``--cov-dir`` implies ``--cov``). Output-directory creation and the
    reservation gate happen later, in the shared leaf-invoke command preamble.
    """
    if ctx.resilient_parsing:
        return

    if ctx.invoked_subcommand is not None and tests:
        raise typer.BadParameter(
            "--tests cannot be combined with a suite subcommand; run `otto test --tests ...` "
            "without naming a suite, or narrow within the suite using -m",
            param_hint="--tests",
        )

    if list_tests:
        suite = ctx.invoked_subcommand
        repos = get_repos()
        per_repo = [
            (repo, repo.collect_tests(markers=markers or None, suite=suite)) for repo in repos
        ]
        _render_panels([repo.get_tests_panel(items) for repo, items in per_repo])
        if not markers and suite is None:
            # An unfiltered full collection just ran — warm the --tests
            # completion cache for free (never from a marker/suite-narrowed
            # list, which would cache an incomplete set).
            import contextlib

            from ..config.completion_cache import record_collected_tests_from_items

            with contextlib.suppress(Exception):
                record_collected_tests_from_items(
                    repos, [item for _, items in per_repo for item in items]
                )
        raise typer.Exit

    if cov_dir is not None:
        # Lazy: pulling otto.coverage runs its package __init__ (the whole
        # collection stack), so a plain `otto test` without --cov-dir/-report-dir
        # never loads it. The gate raises ValueError; the CLI surfaces the
        # identical BadParameter (message + param_hint) users saw before the move.
        from ..coverage.config import prepare_empty_dir

        try:
            prepare_empty_dir(cov_dir, overwrite=overwrite_cov_dir, flag_name="--cov-dir")
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--cov-dir") from e

    cov_report_effective = cov_report or cov_report_dir is not None
    if cov_report_dir is not None:
        # Re-import is free when --cov-dir already ran the block above: Python
        # caches the module in sys.modules, so this never re-pays the
        # otto.coverage package __init__ cost noted there.
        from ..coverage.config import prepare_empty_dir

        try:
            prepare_empty_dir(
                cov_report_dir, overwrite=overwrite_cov_report_dir, flag_name="--cov-report-dir"
            )
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--cov-report-dir") from e

    monitor_effective = monitor or monitor_output is not None or monitor_hosts is not None

    ctx.meta[RUN_OPTIONS_KEY] = RunOptions(
        markers=markers,
        tests=tests,
        iterations=iterations,
        duration=duration,
        threshold=threshold,
        results=results,
        cov=cov or cov_dir is not None or cov_report_effective,
        cov_dir=cov_dir,
        cov_clean=cov_clean,
        cov_report=cov_report_effective,
        cov_report_dir=cov_report_dir,
        overwrite_cov_report_dir=overwrite_cov_report_dir,
        project_name=project_name,
        monitor=monitor_effective,
        monitor_interval=monitor_interval,
        monitor_output=monitor_output,
        monitor_hosts=monitor_hosts,
    )
    # Output-dir creation and the reservation gate moved to the shared
    # leaf-invoke command_preamble (see otto.cli.invoke), so a subcommand
    # `--help` (which exits before invoke) can never create a spurious dir.
    if ctx.invoked_subcommand is None:
        if tests or markers:
            # The group callback is not a wrapped leaf, so the leaf-invoke
            # preamble (session/lab/output-dir/gate) has not run — stamp the
            # `test` spec and run it here before executing the selection.
            from .invoke import command_preamble
            from .registry import CLI_COMMANDS

            ctx.meta.setdefault("_otto_command_spec", CLI_COMMANDS.get("test"))
            command_preamble(ctx)
            run_selection(ctx)
            raise typer.Exit
        rprint(ctx.get_help())
        raise typer.Exit
