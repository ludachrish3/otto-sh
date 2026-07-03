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

import re
import shutil
from dataclasses import dataclass as _dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

if TYPE_CHECKING:
    from rich.panel import Panel

    from ..suite.plugin import StabilityCollector

import typer
from rich import print as rprint
from rich.table import Table

from ..configmodule import get_repos
from ..configmodule.repo import CollectedTest, Repo
from ..context import get_context
from ..logger import get_logger
from ..suite.register import SUITES
from .invoke import make_registry_group

logger = get_logger()

RUN_OPTIONS_KEY = "otto_test_run_options"


@_dataclass(frozen=True)
class TestRunOptions:
    """Shared ``otto test`` run options, set by the suite_app callback and read by ``run_suite``.

    Stored in Typer ``ctx.meta`` (shared across the whole
    context chain by click's design) rather than ``ctx.obj`` (whose
    parent->subcommand propagation broke under click 8.3).
    """

    markers: str = ""
    tests: str = ""
    iterations: int = 0
    duration: int = 0
    threshold: float = 100.0
    results: str = ""
    cov: bool = False
    cov_dir: Path | None = None
    cov_clean: bool = True  # matches the --cov-clean CLI default
    cov_report: bool = False
    cov_report_dir: Path | None = None
    overwrite_cov_report_dir: bool = False
    project_name: str = "Coverage Report"
    monitor: bool = False
    monitor_interval: float = 5.0
    monitor_output: Path | None = None
    monitor_hosts: str | None = None


# ---------------------------------------------------------------------------
# Helpers shared with register.py runner functions
# ---------------------------------------------------------------------------


def _tests_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--tests``: static floor + pytest-collected names.

    The floor is the always-available ``ast`` scan
    (:func:`~otto.configmodule.completion_cache.collect_test_names`, preferring
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
    from ..configmodule import get_completion_names, get_repos
    from ..configmodule.completion_cache import (
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


def resolve_suite(suite: str, repos: list[Repo]) -> str:
    """Expand a sut_dir-relative suite path to an absolute path for pytest."""
    file_part, _, suffix = suite.partition("::")
    p = Path(file_part)
    if p.is_absolute():
        return suite
    for repo in repos:
        candidate = (repo.sut_dir / p).resolve()
        if candidate.exists():
            return f"{candidate}::{suffix}" if suffix else str(candidate)
    return suite


def _repo_confcutdir(suite_file: str, repos: list[Repo]) -> Path:
    """Root for pytest's --confcutdir: the suite file's owning repo.

    Cutting at the SUT repo root (the directory holding ``.otto/``) loads the
    user repo's FULL conftest hierarchy — root, ``tests/``, per-subdir — while
    still excluding otto's own ``tests/conftest.py`` for the in-tree example
    repos (it sits above ``tests/repoN/``). Fallback for a file outside every
    repo: the file's parent (the historical behavior).
    """
    resolved = Path(suite_file).resolve()
    for repo in repos:
        if resolved.is_relative_to(repo.sut_dir):
            return repo.sut_dir
    return resolved.parent


def _base_test_name(name: str) -> str:
    """``test_param[a-b]`` → ``test_param`` (parametrization-insensitive match)."""
    return name.partition("[")[0]


def _absolute_nodeid(item: CollectedTest) -> str:
    """Rebuild a collected test's nodeid with an absolute file path.

    ``CollectedTest.nodeid`` (from pytest's own ``item.nodeid``) is relative
    to the collection rootdir chosen by :meth:`Repo.collect_tests` — not
    otto's own process cwd — so it cannot be handed to a later, independent
    ``pytest.main()`` call. ``CollectedTest.path`` is always absolute, so
    rebuild the ``path::Class::name`` (or ``path::name``) suffix from it.
    """
    suffix = item.nodeid.split("::", 1)[1] if "::" in item.nodeid else ""
    return f"{item.path}::{suffix}" if suffix else str(item.path)


def _resolve_selection(
    repos: list[Repo], names: list[str], markers: str
) -> list[tuple[Repo, list[str]]]:
    """Resolve --tests names to exact nodeids, one entry per matching repo.

    A bare name matches every collected test with that function name (all
    parametrizations); ``Class::name`` restricts to one suite. Unknown names
    raise ``typer.BadParameter`` with did-you-mean suggestions — never a
    silent empty run.
    """
    import difflib

    per_repo: list[tuple[Repo, list[str]]] = []
    matched: set[str] = set()
    seen_names: set[str] = set()
    for repo in repos:
        items = repo.collect_tests(markers=markers or None)
        nodeids: list[str] = []
        for item in items:
            base = _base_test_name(item.name)
            seen_names.add(base)
            if item.cls_name:
                seen_names.add(f"{item.cls_name}::{base}")
            for wanted in names:
                cls_part, _, name_part = wanted.rpartition("::")
                if base == name_part and (not cls_part or item.cls_name == cls_part):
                    nodeids.append(_absolute_nodeid(item))
                    matched.add(wanted)
                    break
        if nodeids:
            per_repo.append((repo, nodeids))

    unknown = [n for n in names if n not in matched]
    if unknown:
        hints = []
        for n in unknown:
            close = difflib.get_close_matches(n, sorted(seen_names), n=3)
            hint = f" (did you mean: {', '.join(close)}?)" if close else ""
            hints.append(f"{n!r}{hint}")
        raise typer.BadParameter(
            f"no collected test matches: {'; '.join(hints)}", param_hint="--tests"
        )
    return per_repo


def _repos_with_marker_matches(repos: list[Repo], markers: str) -> list[Repo]:
    """Filter to repos whose collection has >=1 item matching ``markers``.

    Used by the ``-m``-alone branch of ``run_selection`` so a repo whose
    suites don't carry the given marker never gets a pytest session of its
    own — such a session would collect nothing and exit 5
    (NO_TESTS_COLLECTED), which previously failed the whole multi-repo run
    via ``worst = max(worst, rc)`` even when every other repo matched fine.
    """
    return [repo for repo in repos if repo.collect_tests(markers=markers or None)]


async def _pre_run_cov_clean(repos: list[Repo], opts: TestRunOptions) -> None:
    """Pre-run cleanup of .gcda files on remotes, when --cov and --cov-clean.

    Extracted from ``run_suite`` so ``run_selection`` shares the exact same
    once-per-invocation cleanup (never once-per-repo inside the session loop).
    """
    if not (opts.cov and opts.cov_clean):
        return
    await _cov_clean_remotes(repos)
    # Rebuild host connections so pytest gets fresh ones on its own loop.
    # rebuild_connections() only exists on UnixHost; embedded targets
    # don't carry the same connection lifecycle so skip them.
    from ..configmodule import all_hosts
    from ..host import UnixHost

    for host in all_hosts():
        if isinstance(host, UnixHost):
            host.rebuild_connections()


async def _post_run_coverage(repos: list[Repo], log_dir: Path, opts: TestRunOptions) -> None:
    """Post-run coverage collection and optional HTML report, shared by both run paths."""
    if opts.cov:
        await _run_coverage(repos, log_dir, opts.cov_dir)

    if opts.cov_report:
        from ..coverage.reporter import run_coverage_report

        cov_dir = opts.cov_dir or log_dir / "cov"
        report_dir = (
            opts.cov_report_dir if opts.cov_report_dir is not None else log_dir / "cov_report"
        )
        # Default path lives inside freshly-created log_dir → always empty;
        # explicit path was validated in the callback. Safe to call either way.
        _prepare_empty_dir(
            report_dir, overwrite=opts.overwrite_cov_report_dir, flag_name="--cov-report-dir"
        )
        store = await run_coverage_report(
            [cov_dir],
            report_dir,
            project_name=opts.project_name,
        )
        if store is not None:
            logger.info(
                "Coverage: %.1f%% overall (%d files)", store.overall_pct(), store.file_count()
            )
            logger.info("Report: %s", report_dir / "index.html")


def _run_pytest_session(
    targets: list[str],
    keyword: str | None,
    confcutdir: Path,
    opts: TestRunOptions,
    opts_instance: object | None,
    results_path: str,
    sut_test_dirs: list[Path],
    log_dir: Path,
    label: str,
) -> int:
    """One inner pytest session: base args + plugins + stability report. Returns rc."""
    from ..suite.plugin import OttoPlugin
    from ..suite.pytest_plugin import OttoOptionsPlugin

    base_args: list[str] = [
        *targets,
        "-s",
        "-o",
        "asyncio_mode=auto",
        # pytest-timeout honors @pytest.mark.timeout(N) on tests/classes. No
        # global default is imposed here — timeouts in user suites stay opt-in,
        # as they were before — but signal method ensures a fired timeout
        # interrupts blocking calls and the session still reaches sessionfinish.
        "-o",
        "timeout_method=signal",
        "--no-cov",
        "--no-header",
        "--override-ini",
        "log_cli=false",
        "--override-ini",
        "addopts=",
        # Cut conftest loading at the suite's repo root: the user repo's whole
        # conftest hierarchy loads; otto's own tests/conftest.py (which resets
        # logging management state) stays excluded for in-tree example repos
        # because it lives above their sut_dir.
        f"--confcutdir={confcutdir}",
        # pytest-asyncio registers anyio for assertion rewriting, but anyio is
        # already imported by the time pytest.main() is called from within otto.
        # The warning is harmless (anyio's internals don't affect test results)
        # so suppress it here rather than polluting suite output.
        "--override-ini",
        "filterwarnings=ignore::pytest.PytestAssertRewriteWarning",
    ]
    if keyword:
        base_args += ["-k", keyword]
    if opts.markers:
        # Re-apply -m at run time: _resolve_selection() filtered by markers during
        # collection, and we apply again here so name resolution and live session can
        # never diverge. Belt-and-suspenders marker filtering ensures consistency.
        base_args += ["-m", opts.markers]

    is_stability = opts.iterations > 0 or opts.duration > 0
    monitor_output = opts.monitor_output
    if opts.monitor and monitor_output is None:
        monitor_output = log_dir / "monitor.json"
    otto_plugin = OttoPlugin(
        sut_test_dirs=sut_test_dirs,
        cov=opts.cov,
        iterations=opts.iterations,
        duration=opts.duration,
        monitor=opts.monitor,
        monitor_interval=opts.monitor_interval,
        monitor_output=monitor_output,
        monitor_hosts=opts.monitor_hosts,
    )
    options_plugin = OttoOptionsPlugin(opts_instance)

    collector: "StabilityCollector | None" = None
    if is_stability:
        from ..suite.plugin import StabilityCollector as _StabilityCollector

        collector = _StabilityCollector()
        otto_plugin._stability_collector = collector  # noqa: SLF001 — intra-package write to stability plugin's collector slot

    import pytest

    # Capture the exit code so we can propagate it after post-run steps.
    rc = pytest.main(
        [*base_args, f"--junitxml={results_path}"],
        plugins=[otto_plugin, options_plugin],
    )

    if is_stability and collector is not None:
        _print_stability_report(
            label, collector, opts.iterations, opts.duration, opts.threshold, log_dir
        )

    return int(rc)


def run_suite(
    suite_class: type,
    suite_file: str,
    opts_instance: object | None,
    ctx: typer.Context,
) -> None:
    """Execute a registered suite via pytest.main().

    Runner options (``--markers``, ``--iterations``, ``--duration``,
    ``--threshold``, ``--results``) and coverage options (``--cov`` /
    ``--cov-clean``) are read from the ``TestRunOptions`` the ``otto test``
    callback stored in ``ctx.meta[RUN_OPTIONS_KEY]``. The context is passed in
    by the suite runner (Typer injects it), so this function never reaches into
    a global context stack.
    """
    import asyncio

    stored = ctx.meta.get(RUN_OPTIONS_KEY)
    opts = stored if isinstance(stored, TestRunOptions) else TestRunOptions()

    repos = get_repos()
    sut_test_dirs = [path for repo in repos for path in repo.tests]
    _log_dir = get_context().output_dir
    if _log_dir is None:
        raise RuntimeError("output_dir is not set; create_output_dir must run before run_suite")
    log_dir: Path = _log_dir
    results_path = opts.results or str(log_dir / "junit.xml")

    asyncio.run(_pre_run_cov_clean(repos, opts))

    rc = _run_pytest_session(
        [suite_file],
        suite_class.__name__,
        _repo_confcutdir(suite_file, repos),
        opts,
        opts_instance,
        results_path,
        sut_test_dirs,
        log_dir,
        suite_class.__name__,
    )

    asyncio.run(_post_run_coverage(repos, log_dir, opts))

    # Propagate a non-zero pytest exit code so callers and CI scripts see
    # failure.  rc=5 (NO_TESTS_COLLECTED) is also treated as an error: a
    # named suite that collects nothing almost certainly indicates a
    # misconfiguration or stale suite name.  The stability threshold violation
    # path (SystemExit(1) from _print_stability_report) already exits before
    # reaching this point, so there is no double-exit risk.
    if rc != 0:
        raise typer.Exit(code=int(rc))


def run_selection(ctx: typer.Context) -> None:
    """Run a suite-less selection (--tests and/or -m) — one session per repo."""
    import asyncio

    stored = ctx.meta.get(RUN_OPTIONS_KEY)
    opts = stored if isinstance(stored, TestRunOptions) else TestRunOptions()

    repos = get_repos()
    names = [n.strip() for n in opts.tests.split(",") if n.strip()]
    if names:
        per_repo = _resolve_selection(repos, names, opts.markers)
    else:  # -m alone: marker expression over each repo's test dirs
        matching_repos = _repos_with_marker_matches(repos, opts.markers)
        per_repo = [(r, [str(d) for d in r.tests if d.exists()]) for r in matching_repos]
        per_repo = [(r, t) for r, t in per_repo if t]

    if not per_repo:
        rprint("[red]No tests matched the selection.[/red]")
        raise typer.Exit(code=1)

    _log_dir = get_context().output_dir
    if _log_dir is None:
        raise RuntimeError("output_dir is not set; command_preamble must run before run_selection")
    log_dir: Path = _log_dir
    asyncio.run(_pre_run_cov_clean(repos, opts))

    worst = 0
    multi = len(per_repo) > 1
    for repo, targets in per_repo:
        default_junit = log_dir / (f"junit_{repo.name}.xml" if multi else "junit.xml")
        if opts.results and multi:
            # A single explicit --results path would otherwise have every
            # participating repo's session overwrite the last one's junit
            # output. Fan out from the same stem instead: PATH -> PATH_repo.
            results_source = Path(opts.results)
            results_path = str(results_source.with_stem(f"{results_source.stem}_{repo.name}"))
        else:
            results_path = opts.results or str(default_junit)
        sut_test_dirs = [p for r in repos for p in r.tests]
        rc = _run_pytest_session(
            targets,
            None,
            repo.sut_dir,
            opts,
            None,  # no per-suite Options instance: Task 3's fixture default-constructs
            results_path,
            sut_test_dirs,
            log_dir,
            label=f"selection:{repo.name}",
        )
        worst = max(worst, int(rc))

    asyncio.run(_post_run_coverage(repos, log_dir, opts))
    if worst != 0:
        raise typer.Exit(code=worst)


def _print_stability_report(
    suite_name: str,
    collector: "StabilityCollector",
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
        mode_parts.append(f"{iterations} iterations")
    if duration > 0:
        mode_parts.append(f"{duration}s duration")
    mode = ", ".join(mode_parts) or "stability"

    lines: list[str] = [
        f"Stability Results for {suite_name} ({mode}, threshold {threshold:.0f}%):",
    ]
    any_unstable = False
    for test_name, (passed, total) in collector.results.items():
        rate_pct = (passed / total * 100) if total else 0.0
        status = "STABLE" if rate_pct >= threshold else "UNSTABLE"
        if status == "UNSTABLE":
            any_unstable = True
        lines.append(f"  {test_name:<40} {passed}/{total} ({rate_pct:.0f}%)  {status}")
    lines.append(f"Overall: {'FAIL' if any_unstable else 'PASS'}")

    report = "\n".join(lines)
    logger.info(report)
    report_path = log_dir / "stability_report.txt"
    report_path.write_text(report)

    if any_unstable:
        raise SystemExit(1)


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

            from ..configmodule.completion_cache import record_collected_tests_from_items

            with contextlib.suppress(Exception):
                record_collected_tests_from_items(
                    repos, [item for _, items in per_repo for item in items]
                )
        raise typer.Exit

    if cov_dir is not None:
        _prepare_empty_dir(cov_dir, overwrite=overwrite_cov_dir, flag_name="--cov-dir")

    cov_report_effective = cov_report or cov_report_dir is not None
    if cov_report_dir is not None:
        _prepare_empty_dir(
            cov_report_dir, overwrite=overwrite_cov_report_dir, flag_name="--cov-report-dir"
        )

    monitor_effective = monitor or monitor_output is not None or monitor_hosts is not None

    ctx.meta[RUN_OPTIONS_KEY] = TestRunOptions(
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
        overwrite_flag = f"--overwrite-{flag_name.lstrip('-')}"
        raise typer.BadParameter(
            f"{flag_name} target {path} is not empty; pass {overwrite_flag} to clear it.",
            param_hint=flag_name,
        )
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


async def _cov_clean_remotes(repos: list["Repo"]) -> None:
    """Delete .gcda files on all configured remote hosts before a test run."""
    from ..configmodule import all_hosts
    from ..coverage.fetcher.remote import GcdaFetcher

    cov_config = _get_cov_config(repos)
    if not cov_config:
        return

    gcda_remote_dir = cov_config.get("gcda_remote_dir", "")
    if not gcda_remote_dir:
        logger.warning("coverage.gcda_remote_dir not configured — skipping pre-run cleanup")
        return

    if not any(all_hosts()):
        return

    fetcher = GcdaFetcher(Path("/tmp"))  # noqa: S108 — deliberate staging path
    await fetcher.clean_remote(gcda_remote_dir)


async def _write_cov_metadata(
    repos: list["Repo"],
    cov_config: dict[str, Any],
    unix_hosts: list[Any],
    unix_dirs: dict[str, Path],
    cov_hosts: list[Any],
    embedded_dirs: dict[str, Path],
    cov_dir: Path,
) -> None:
    """Write ``.otto_cov_meta.json`` so ``otto cov report`` can find source roots and toolchains.

    Extracted from ``_run_coverage`` to keep that function's cyclomatic complexity
    below the project limit.  Behavior is identical to the original tail.
    """
    import json

    cov_repo = _get_cov_repo(repos)
    if not cov_repo:
        return

    toolchains: dict[str, dict[str, str]] = {}
    for host in unix_hosts:
        # Only hosts that actually produced coverage — skip infrastructure hosts
        # (e.g. an SSH hop) that are in the lab solely for connectivity.
        if host.id not in unix_dirs:
            continue
        tc = host.toolchain
        toolchains[host.id] = {
            "sysroot": str(tc.sysroot),
            "lcov": str(tc.lcov),
            "gcov": str(tc.gcov),
        }

    sut_dir = str(cov_repo.sut_dir.resolve())

    # Embedded hosts now carry a per-host Toolchain (lab-data ``toolchain``),
    # exactly like Unix hosts: the bed declares the cross-gcov for binaries it
    # runs. Use it per host; fall back to scanning the build's .gcno only for a
    # host left at the default (unconfigured) toolchain.
    #
    # The build dir is the report's source root when there are no Unix hosts
    # (standalone-embedded). Multi-Zephyr-version labs declare per-version build
    # dirs under [coverage.embedded.builds.<version>]; each host's os_version
    # selects its own root, recorded in ``source_roots`` so the reporter can
    # resolve the correct .gcno tree per host. The single ``build_dir`` remains
    # supported as a legacy/fallback for single-version labs.
    embedded_cfg = cov_config.get("embedded") or {}
    embedded_build_dir = embedded_cfg.get("build_dir")  # single legacy/fallback
    embedded_builds = embedded_cfg.get("builds") or {}  # {"3.7": {"build_dir": ...}}

    def _resolve_build_dir(host: object) -> str | None:
        ver = getattr(host, "os_version", None)
        if ver and ver in embedded_builds:
            bd = embedded_builds[ver].get("build_dir")
            if bd:
                return bd
        return embedded_build_dir

    source_roots: dict[str, str] = {}
    if embedded_dirs and (embedded_build_dir or embedded_builds):
        from ..host import LocalHost
        from ..host.embedded_host import EmbeddedHost
        from ..host.toolchain import Toolchain
        from ..host.toolchain_discovery import discover_toolchain_from_gcno

        embedded_hosts = {h.id: h for h in cov_hosts if isinstance(h, EmbeddedHost)}
        # Cache .gcno-discovery per build dir so hosts sharing a build dir do
        # not re-trigger the (potentially slow) filesystem scan.
        discovery_cache: dict[str, Toolchain | None] = {}
        for host_id in embedded_dirs:
            host = embedded_hosts.get(host_id)
            host_build_dir = _resolve_build_dir(host) if host is not None else embedded_build_dir
            if host_build_dir:
                source_roots[host_id] = str(Path(host_build_dir).resolve())
            tc = host.toolchain if host is not None and host.toolchain != Toolchain() else None
            if tc is None:
                bd_key = host_build_dir or ""
                if bd_key not in discovery_cache:
                    if host_build_dir:
                        discovery_cache[bd_key] = await discover_toolchain_from_gcno(
                            Path(host_build_dir),
                            LocalHost(),
                            cov_dir / "_toolchain_work",
                        )
                    else:
                        discovery_cache[bd_key] = None
                tc = discovery_cache[bd_key]
            if tc is not None:
                toolchains[host_id] = {
                    "sysroot": str(tc.sysroot),
                    "lcov": str(tc.lcov),
                    "gcov": str(tc.gcov),
                }
        if not unix_dirs:
            # Use the single fallback if present; otherwise the first resolved root.
            if embedded_build_dir:
                sut_dir = str(Path(embedded_build_dir).resolve())
            elif source_roots:
                sut_dir = next(iter(source_roots.values()))

    meta: dict[str, object] = {
        "repo_name": cov_repo.name,
        "sut_dir": sut_dir,
        "toolchains": toolchains,
        "source_roots": source_roots,
    }
    (cov_dir / ".otto_cov_meta.json").write_text(json.dumps(meta, indent=2))


async def _run_coverage(
    repos: list["Repo"],
    log_dir: Path,
    cov_dir_override: Path | None = None,
) -> None:
    """Collect ``.gcda`` coverage from Unix and/or embedded hosts into the cov dir.

    Unix hosts emit ``.gcda`` to a filesystem fetched by :class:`GcdaFetcher`;
    embedded (Zephyr LLEXT) hosts have no filesystem and instead dump theirs
    over the console, decoded by :func:`collect_embedded_coverage`.  Both land
    under the same ``cov_dir`` so the merge/report step treats them identically.

    When ``cov_dir_override`` is provided, coverage is written there; otherwise
    the default ``<log_dir>/cov`` is used.
    """
    from ..configmodule import all_hosts
    from ..coverage.fetcher.embedded import collect_embedded_coverage
    from ..coverage.fetcher.remote import GcdaFetcher
    from ..host import UnixHost

    cov_config = _get_cov_config(repos)
    if not cov_config:
        logger.warning("--cov was specified but no [coverage] section found in .otto/settings.toml")
        return

    cov_dir = cov_dir_override or log_dir / "cov"
    host_dirs: dict[str, Path] = {}

    # The set of hosts to collect coverage from is repo-declared: an optional
    # ``[coverage].hosts`` regex (matched against each host id) selects targets,
    # defaulting to every host in the lab. This is how a lab's SSH **hop** (e.g.
    # `basil` fronting `sprout_cov`) is kept out of the coverage set — it is
    # excluded by the pattern, not inferred from the fact that it emits no .gcda.
    hosts_pattern = cov_config.get("hosts")
    cov_pattern = re.compile(hosts_pattern) if hosts_pattern else None

    # Unix hosts compile the SUT and emit .gcda to a filesystem we fetch over
    # the network. EmbeddedHost/DockerContainerHost are skipped by the fetcher.
    cov_hosts = list(all_hosts(pattern=cov_pattern))
    unix_hosts = [h for h in cov_hosts if isinstance(h, UnixHost)]
    gcda_remote_dir = cov_config.get("gcda_remote_dir", "")
    # Unix hosts that actually produced .gcda (host id -> dir). Keying the meta
    # below off *collected coverage* (rather than lab membership) is a safety net
    # behind the ``[coverage].hosts`` selector above: should an infrastructure
    # host slip through the pattern, producing no .gcda keeps it from being
    # mistaken for a Unix coverage target — which would otherwise flip the
    # source-root choice (breaking embedded .gcno discovery) and write a bogus
    # toolchain entry.
    unix_dirs: dict[str, Path] = {}
    if gcda_remote_dir and unix_hosts:
        # Hosts may carry stale connections from pytest's event loop; rebuild
        # their connection state so they reconnect on the current loop.
        for host in unix_hosts:
            host.rebuild_connections()
        fetcher = GcdaFetcher(cov_dir)
        unix_dirs = await fetcher.fetch_all(gcda_remote_dir)
        host_dirs.update(unix_dirs)
        if unix_dirs:
            await fetcher.clean_remote(gcda_remote_dir)

    # Embedded (RTOS) hosts dump .gcda over the console (no filesystem).
    embedded_dirs = await collect_embedded_coverage(cov_config, cov_dir, pattern=cov_pattern)
    host_dirs.update(embedded_dirs)

    if not host_dirs:
        logger.warning("No coverage data collected from any host")
        return

    logger.info("Coverage data collected to %s (%d hosts)", cov_dir, len(host_dirs))

    await _write_cov_metadata(
        repos=repos,
        cov_config=cov_config,
        unix_hosts=unix_hosts,
        unix_dirs=unix_dirs,
        cov_hosts=cov_hosts,
        embedded_dirs=embedded_dirs,
        cov_dir=cov_dir,
    )

    # Produce a pinned capture.json per board against the lab's default
    # e2e-kind tier, so a bare `otto test --cov` run always leaves behind
    # capture artifacts (not just raw .gcda) — the same production step
    # `otto cov get` uses for a manual/on-demand pull. This tail must never
    # fail an otherwise-successful test run: a non-git sut, ambiguous/
    # misconfigured tiers (``resolve_get_tier`` raising ``ValueError``), or a
    # stamp-mismatch during merge (``CoverageDataMismatchError``, a
    # ``RuntimeError``) are all logged and swallowed, leaving the raw
    # ``.gcda`` artifacts on disk for manual recovery via ``otto cov get``.
    from ..coverage.capture.produce import produce_captures
    from ..coverage.tiers import load_tiers, resolve_get_tier

    cov_repo = _get_cov_repo(repos)
    if cov_repo is None:
        return

    try:
        tiers = load_tiers(cov_config)
        e2e_tier = resolve_get_tier(tiers, None)
        written = await produce_captures(
            cov_dir,
            tier=e2e_tier.name,
            repo_root=cov_repo.sut_dir,
            labs=[cov_repo.name],
        )
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        logger.warning(
            "Coverage capture emission failed (%s); raw .gcda artifacts remain in %s", e, cov_dir
        )
        return

    logger.info("Coverage captures produced: %d board(s)", len(written))


def _has_cov_config(cov: dict[str, Any]) -> bool:
    """Return True when the repo actually declared coverage settings."""
    return bool(
        cov.get("gcda_remote_dir") or cov.get("embedded") or cov.get("tiers") or cov.get("hosts")
    )


def _get_cov_repo(repos: list["Repo"]) -> "Repo | None":
    """Return the first repo with a ``[coverage]`` section in its settings."""
    for repo in repos:
        if _has_cov_config(repo.settings.get("coverage") or {}):
            return repo
    return None


def _get_cov_config(repos: list["Repo"]) -> dict[str, Any]:
    """Extract the ``[coverage]`` config from the first repo that has one."""
    repo = _get_cov_repo(repos)
    return repo.settings["coverage"] if repo else {}
