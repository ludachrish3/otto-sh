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
    from ..suite.plugin import StabilityCollector

import pytest
import typer
from rich import print as rprint
from rich.table import Table

from ..configmodule import get_repos
from ..configmodule.repo import Repo
from ..context import get_context
from ..logger import get_otto_logger, management
from ..suite.plugin import OttoPlugin
from ..suite.register import _SUITE_REGISTRY, OttoOptionsPlugin

logger = get_otto_logger()

RUN_OPTIONS_KEY = "otto_test_run_options"


@_dataclass(frozen=True)
class TestRunOptions:
    """Shared ``otto test`` run options, set by the suite_app callback and read by ``run_suite``.

    Stored in Typer ``ctx.meta`` (shared across the whole
    context chain by click's design) rather than ``ctx.obj`` (whose
    parent->subcommand propagation broke under click 8.3).
    """

    markers: str = ""
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

    markers = opts.markers
    iterations = opts.iterations
    duration = opts.duration
    threshold = opts.threshold
    results = opts.results
    cov = opts.cov
    cov_dir_override = opts.cov_dir
    cov_clean = opts.cov_clean
    cov_report = opts.cov_report
    cov_report_dir = opts.cov_report_dir
    overwrite_cov_report_dir = opts.overwrite_cov_report_dir
    project_name = opts.project_name
    monitor = opts.monitor
    monitor_interval = opts.monitor_interval
    monitor_output = opts.monitor_output
    monitor_hosts = opts.monitor_hosts

    repos = get_repos()
    sut_test_dirs = [path for repo in repos for path in repo.tests]
    _log_dir = get_context().output_dir
    if _log_dir is None:
        raise RuntimeError("output_dir is not set; create_output_dir must run before run_suite")
    log_dir: Path = _log_dir
    results_path = results or str(log_dir / "junit.xml")

    # Pre-run cleanup of .gcda files on remotes
    if cov and cov_clean:
        asyncio.run(_cov_clean_remotes(repos))
        # Rebuild host connections so pytest gets fresh ones on its own loop.
        # rebuild_connections() only exists on UnixHost; embedded targets
        # don't carry the same connection lifecycle so skip them.
        from ..configmodule import all_hosts
        from ..host import UnixHost

        for host in all_hosts():
            if isinstance(host, UnixHost):
                host.rebuild_connections()

    base_args: list[str] = [
        suite_file,
        "-k",
        suite_class.__name__,
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
        # Restrict conftest loading to the suite file's directory tree so that
        # otto's own tests/conftest.py (which resets logging management state)
        # is not picked up by the inner session when the suite lives inside the
        # otto project tree.
        f"--confcutdir={Path(suite_file).resolve().parent}",
        # pytest-asyncio registers anyio for assertion rewriting, but anyio is
        # already imported by the time pytest.main() is called from within otto.
        # The warning is harmless (anyio's internals don't affect test results)
        # so suppress it here rather than polluting suite output.
        "--override-ini",
        "filterwarnings=ignore::pytest.PytestAssertRewriteWarning",
    ]
    if markers:
        base_args += ["-m", markers]

    is_stability = iterations > 0 or duration > 0
    if monitor and monitor_output is None:
        monitor_output = log_dir / "monitor.json"
    otto_plugin = OttoPlugin(
        sut_test_dirs=sut_test_dirs,
        cov=cov,
        iterations=iterations,
        duration=duration,
        monitor=monitor,
        monitor_interval=monitor_interval,
        monitor_output=monitor_output,
        monitor_hosts=monitor_hosts,
    )
    options_plugin = OttoOptionsPlugin(opts_instance)

    collector: "StabilityCollector | None" = None
    if is_stability:
        from ..suite.plugin import StabilityCollector as _StabilityCollector

        collector = _StabilityCollector()
        otto_plugin._stability_collector = collector  # noqa: SLF001 — intra-package write to stability plugin's collector slot

    pytest.main(
        [*base_args, f"--junitxml={results_path}"],
        plugins=[otto_plugin, options_plugin],
    )

    if is_stability and collector is not None:
        _print_stability_report(
            suite_class.__name__, collector, iterations, duration, threshold, log_dir
        )

    # Post-test coverage collection
    if cov:
        asyncio.run(_run_coverage(repos, log_dir, cov_dir_override))

    if cov_report:
        from ..coverage.reporter import run_coverage_report

        cov_dir = cov_dir_override or log_dir / "cov"
        report_dir = cov_report_dir if cov_report_dir is not None else log_dir / "cov_report"
        # Default path lives inside freshly-created log_dir → always empty;
        # explicit path was validated in the callback. Safe to call either way.
        _prepare_empty_dir(
            report_dir, overwrite=overwrite_cov_report_dir, flag_name="--cov-report-dir"
        )
        store = asyncio.run(
            run_coverage_report(
                [cov_dir],
                report_dir,
                project_name=project_name,
            )
        )
        if store is not None:
            logger.info(
                "Coverage: %.1f%% overall (%d files)", store.overall_pct(), store.file_count()
            )
            logger.info("Report: %s", report_dir / "index.html")


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


def _list_tests_display(panel_method: str) -> None:
    panels = [getattr(repo, panel_method)(repo.collect_tests()) for repo in get_repos()]
    table = Table(show_header=False, show_footer=False, box=None, expand=True, padding=(0, 1, 1, 1))
    for _ in panels:
        table.add_column(ratio=1)
    table.add_row(*panels)
    rprint(table)


def list_suites_callback(value: bool) -> None:
    if not value:
        return
    _list_tests_display("get_test_suites_panel")
    raise typer.Exit


# ---------------------------------------------------------------------------
# suite_app — multi-subcommand Typer app (mirrors run_app structure)
# ---------------------------------------------------------------------------

suite_app = typer.Typer(
    name="test",
    no_args_is_help=True,
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
    markers: Annotated[
        str,
        typer.Option(
            "--markers",
            "-m",
            metavar="EXPRESSION",
            help="pytest -m marker expression applied after collection.",
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
    if ctx.resilient_parsing:
        return

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
    if ctx.invoked_subcommand is not None:
        get_context().output_dir = management.create_output_dir("test", ctx.invoked_subcommand)
        from ..reservations import gate

        gate(ctx)


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


def _get_cov_repo(repos: list["Repo"]) -> "Repo | None":
    """Return the first repo with a ``[coverage]`` section in its settings."""
    for repo in repos:
        if repo.settings.get("coverage"):
            return repo
    return None


def _get_cov_config(repos: list["Repo"]) -> dict[str, Any]:
    """Extract the ``[coverage]`` config from the first repo that has one."""
    repo = _get_cov_repo(repos)
    return repo.settings["coverage"] if repo else {}
