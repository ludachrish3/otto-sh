"""Suite-run engine as a library call — ``run_suite`` without a Typer context.

This module holds the pytest-driving core that ``otto test`` used to carry
inline in ``otto.cli.test``: the run-options record, the inner pytest session,
the stability report, and the coverage pre/post hooks. Extracting it here lets a
suite run as a plain library call — ``run_suite(MySuite, output_dir=...)`` —
returning a :class:`SuiteRunResult` instead of raising ``typer.Exit``.

The CLI (``otto.cli.test``) keeps its context-driven ``run_suite`` /
``run_selection`` wrappers and imports the moved helpers from here, so
``otto test`` behavior is unchanged.

Import-weight note: this module never imports ``typer`` (nor ``pytest``) at
module load — the typer/pytest-touching pieces (the inner session's plugins, the
coverage helpers) are imported lazily inside the functions that need them, so
``import otto.suite.run`` stays cheap for library callers.
"""

import contextlib
import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.repo import Repo
    from .plugin import StabilityCollector

import logging

logger = logging.getLogger(__name__)

RUN_OPTIONS_KEY = "otto_test_run_options"


class NoTestsMatchedError(ValueError):
    """A ``--tests`` / ``-m`` selection resolved to nothing to run.

    Raised by :func:`run_selection` when the selection matched no repo — no
    repos configured, no repo carrying the marker, or (via
    :func:`otto.suite.selection.resolve_selection`) no test universe to search
    at all. It is distinct from
    :class:`otto.suite.selection.UnknownSelectionError` (a genuinely unknown
    name against a real universe, carrying did-you-mean suggestions).

    Subclasses ``ValueError`` so it stays catchable as one, but the CLI adapter
    catches *this* specifically — a broad ``except ValueError`` would misreport
    an unrelated pipeline ``ValueError`` as "No tests matched the selection."
    """


@dataclasses.dataclass(frozen=True)
class RunOptions:
    """Shared suite-run options: markers/iterations/stability/coverage/monitor.

    The ``otto test`` callback constructs one of these from its CLI flags and
    stores it in Typer ``ctx.meta[RUN_OPTIONS_KEY]``; library callers pass one
    directly to :func:`run_suite`. Field defaults mirror the ``otto test`` CLI
    defaults exactly.
    """

    markers: str = ""
    tests: str = ""
    iterations: int = 0
    duration: int = 0
    threshold: float = 100.0
    results: str = ""
    cov: bool = False
    cov_dir: Path | None = None
    overwrite_cov_dir: bool = False
    cov_clean: bool = True  # matches the --cov-clean CLI default
    cov_report: bool = False
    cov_report_dir: Path | None = None
    overwrite_cov_report_dir: bool = False
    project_name: str = "Coverage Report"
    monitor: bool = False
    monitor_interval: float = 5.0
    monitor_output: Path | None = None
    monitor_hosts: str | None = None


# Shared default for run_suite(run_options=...). RunOptions is frozen (immutable),
# so a module-level singleton is safe to share across calls — and it keeps the
# call out of the parameter default (avoids the B008 mutable-default footgun).
_DEFAULT_RUN_OPTIONS = RunOptions()


@dataclasses.dataclass(frozen=True)
class SuiteRunResult:
    """Outcome of a :func:`run_suite` invocation.

    ``exit_code`` is the ssh-like final code (pytest rc, with a stability
    threshold violation folded in). ``junit_paths`` are the JUnit XML files the
    session wrote. ``stability_report`` is the ``stability_report.txt`` path when
    a stability run produced one (else ``None``); ``stability_unstable`` is True
    when any test fell below its pass-rate threshold.
    """

    exit_code: int
    junit_paths: list[Path]
    stability_report: Path | None
    stability_unstable: bool
    output_dir: Path

    @property
    def passed(self) -> bool:
        """True when the invocation succeeded (exit code 0)."""
        return self.exit_code == 0


@dataclasses.dataclass(frozen=True)
class _SessionOutcome:
    """Result of one inner ``_run_pytest_session``: rc + stability verdict + report path."""

    rc: int
    unstable: bool
    report: Path | None


def _repo_confcutdir(suite_file: str, repos: "list[Repo]") -> Path:
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


def resolve_output_dir(output_dir: Path | None) -> Path:
    """Explicit param → context output_dir → CWD (xdir-defaults-to-CWD philosophy)."""
    if output_dir is not None:
        return output_dir
    from ..context import try_get_context

    ctx = try_get_context()
    if ctx is not None and ctx.output_dir is not None:
        return ctx.output_dir
    return Path.cwd()


def find_suite(name: str) -> type:
    """Resolve a registered ``OttoSuite`` subclass by class name via ``SUITES``."""
    from .register import SUITES

    if name not in SUITES:
        registered = ", ".join(sorted(SUITES.names())) or "<none>"
        raise LookupError(f"unknown suite {name!r}; registered: {registered}")
    return SUITES.get(name).cls


def _final_exit_code(rc: int, unstable: bool) -> int:
    """Threshold violations fail an otherwise-green run; pytest rc wins otherwise."""
    return 1 if (unstable and rc == 0) else int(rc)


@contextlib.contextmanager
def _session_context(log_dir: Path) -> Iterator[None]:
    """Guarantee an active ``OttoContext`` with an ``output_dir`` for the session(s).

    ``OttoSuite`` internals (``setup_method``/``setup_class`` per-test dirs, the
    ``ctx`` fixture) call ``get_context()``; in the CLI that context is
    installed by the command preamble, but a library caller
    (``bootstrap()`` → :func:`run_suite`) has none. Three cases:

    - **No active context**: install a minimal lab-less one
      (``OttoContext(lab=Lab(name=LIBRARY_LAB_NAME), output_dir=log_dir)``) for
      the duration of the session and always restore the prior state via the
      ``set_context``/``reset_context`` token pair. The sentinel ``Lab`` carries
      no hosts, so ``get_host()`` inside such a suite fails loud with its
      normal unknown-host error (plus an ``open_context`` breadcrumb keyed off
      ``LIBRARY_LAB_NAME`` — see :meth:`otto.context.OttoContext.get_host`) —
      correct for hostless library runs; suites that need lab hosts use
      ``open_context()`` (see the Python library guide).
    - **Active context without an output_dir**: point it at *log_dir* for the
      session (the same assignment the CLI preamble makes) and restore the
      prior value afterwards. The prior value is captured explicitly (``prior
      = active.output_dir``) rather than assumed to be the literal ``None``
      this branch's guard implies — a defensive habit that stays correct even
      if this branch's precondition ever changes.
    - **Active context with an output_dir**: leave it untouched.
    """
    from ..context import try_get_context

    active = try_get_context()
    if active is None:
        from ..config.lab import Lab
        from ..context import LIBRARY_LAB_NAME, OttoContext, reset_context, set_context

        token = set_context(OttoContext(lab=Lab(name=LIBRARY_LAB_NAME), output_dir=log_dir))
        try:
            yield
        finally:
            reset_context(token)
    elif active.output_dir is None:
        prior = active.output_dir
        active.output_dir = log_dir
        try:
            yield
        finally:
            active.output_dir = prior
    else:
        yield


def _pre_run_cov_dir_check(opts: RunOptions) -> None:
    """Ensure an explicit ``cov_dir`` is empty (or clear it) before a run.

    Mirrors the CLI's own preflight: ``otto.cli.test`` calls
    ``otto.coverage.config.prepare_empty_dir`` on ``--cov-dir`` while
    building its ``RunOptions``, so a library caller that hands
    :attr:`RunOptions.cov_dir` straight to :func:`run_suite`/
    :func:`run_selection` (bypassing that callback) now gets the identical
    empty/overwrite guard. On the CLI path this call is a no-op: the
    callback already cleared ``cov_dir`` before ``RunOptions`` was built, so
    it is already empty by the time this runs, regardless of
    ``overwrite_cov_dir``. Raises the neutral ``ValueError``
    ``prepare_empty_dir`` itself raises — never ``typer`` — matching the
    library's no-typer contract. Synchronous (no host I/O) and run before
    ``otto.coverage.collect.clean_remote_gcda``'s network + event-loop work,
    so a bad ``cov_dir`` fails before any remote is touched. Imported lazily
    so a non-coverage library run never pulls the coverage stack at load time.
    """
    if not (opts.cov and opts.cov_dir is not None):
        return
    from ..coverage.config import prepare_empty_dir

    prepare_empty_dir(opts.cov_dir, overwrite=opts.overwrite_cov_dir, flag_name="cov_dir")


async def _pre_run_cov_clean(repos: "list[Repo]", opts: RunOptions) -> None:
    """Pre-run cleanup of .gcda files on remotes, when --cov and --cov-clean.

    Shared by both the suite and suite-less run paths so cleanup happens once per
    invocation (never once-per-repo inside the session loop). The .gcda-removal
    machinery (clean + host-connection rebuild) lives in
    :func:`otto.coverage.collect.clean_remote_gcda`; it is imported lazily so
    this module never pulls the coverage stack at load time. The
    ``--cov``/``--cov-clean`` gate stays here.
    """
    if not (opts.cov and opts.cov_clean):
        return
    from ..coverage.collect import clean_remote_gcda

    await clean_remote_gcda(repos)


async def _post_run_coverage(repos: "list[Repo]", log_dir: Path, opts: RunOptions) -> None:
    """Post-run coverage collection and optional HTML report, shared by both run paths.

    Collection runs through :func:`otto.coverage.collect.collect_coverage` (the
    single canonical fetch/metadata/capture workflow), which *fails loud*: the
    never-fail-a-successful-run swallow policy lives here, in the ``try/except``
    around it. The optional HTML report reuses the config-resolution helpers and
    the neutral empty/overwrite gate (``prepare_empty_dir``) in
    ``otto.coverage.config`` — the gate raises a plain ``ValueError`` (never
    ``typer``), so a report-dir collision on a library re-run is swallowed here
    like any other report failure. Everything is imported lazily to avoid a
    load-time cost for non-coverage runs and to keep the existing patch points
    valid.
    """
    if not (opts.cov or opts.cov_report):
        # Nothing to do — and skipping the lazy import below keeps a
        # non-coverage library run_suite() call from pulling the CLI (typer).
        return

    if opts.cov:
        from rich.markup import escape as escape_markup

        from ..coverage.collect import collect_coverage

        cov_dir = opts.cov_dir or log_dir / "cov"
        # collect_coverage fails loud; a bare `otto test --cov` run must never
        # let a coverage-collection failure (no [coverage] section, no .gcda
        # retrieved, an ambiguous/misconfigured tier, a non-git sut, or a
        # merge/produce error) turn an otherwise-successful test run red. Log
        # and swallow, leaving the raw artifacts on disk for manual recovery
        # via `otto cov get`. %s-formats *e* through escape_markup: the
        # console handler renders log messages as Rich markup, and this
        # message may echo a literal bracket (e.g. "no [coverage] section").
        try:
            await collect_coverage(cov_dir, repos=repos)
        except (ValueError, RuntimeError, FileNotFoundError) as e:
            logger.warning(
                "Coverage collection failed (%s); raw coverage artifacts remain in %s",
                escape_markup(str(e)),
                cov_dir,
            )

    if opts.cov_report:
        from rich.markup import escape as escape_markup

        from ..coverage.config import get_cov_config, get_cov_repo, prepare_empty_dir
        from ..coverage.reporter import run_coverage_report
        from ..coverage.tiers import load_tiers

        cov_dir = opts.cov_dir or log_dir / "cov"
        report_dir = (
            opts.cov_report_dir if opts.cov_report_dir is not None else log_dir / "cov_report"
        )
        # Resolve the same collection-model inputs `otto cov report` uses
        # (declared tiers/colors, exclusion markers, the committed manual
        # store), from the repos already in hand — mirroring
        # cov._resolve_cov_settings but without re-fetching. A tree with no
        # [coverage] section falls back to (None, None, []), i.e. the legacy
        # gcda-only report, exactly as before.
        cov_repo = get_cov_repo(repos)
        repo_root = cov_repo.sut_dir if cov_repo is not None else None
        cov_config = get_cov_config(repos)
        tier_configs = load_tiers(cov_config, repo_root) if cov_repo is not None else None
        extra_markers = list(cov_config.get("exclusions", {}).get("markers") or [])
        # Like the capture tail, in-run report generation must never fail an
        # otherwise-successful test run: the empty/overwrite gate
        # (prepare_empty_dir, which now raises a neutral ValueError — a report-dir
        # collision on a library re-run into a reused output_dir warns and skips
        # rather than raising typer from a public library entrypoint), a non-git
        # sut, a polluted tree, or a malformed manual capture are logged and
        # swallowed, leaving the raw coverage artifacts on disk. The CLI already
        # validated an explicit --cov-report-dir up front; the default path lives
        # inside the freshly-created log_dir and is always empty.
        try:
            prepare_empty_dir(
                report_dir,
                overwrite=opts.overwrite_cov_report_dir,
                flag_name="--cov-report-dir",
            )
            store = await run_coverage_report(
                [cov_dir],
                report_dir,
                project_name=opts.project_name,
                repo_root=repo_root,
                tier_configs=tier_configs,
                extra_markers=extra_markers,
            )
        except (ValueError, RuntimeError, FileNotFoundError) as e:
            # escape_markup(*e*): the console handler renders log messages as
            # Rich markup, and this message may echo a literal bracket (e.g.
            # a no-[coverage]-section ValueError from prepare_empty_dir's
            # config resolution).
            logger.warning(
                "Coverage report generation failed (%s); raw coverage artifacts remain in %s",
                escape_markup(str(e)),
                cov_dir,
            )
            store = None
        if store is not None:
            logger.info(
                "Coverage: %.1f%% overall (%d files)", store.overall_pct(), store.file_count()
            )
            logger.info("Report: %s", report_dir / "index.html")


def _run_pytest_session(
    targets: list[str],
    keyword: str | None,
    confcutdir: Path,
    opts: RunOptions,
    opts_instance: object | None,
    results_path: str,
    sut_test_dirs: list[Path],
    log_dir: Path,
    label: str,
) -> _SessionOutcome:
    """One inner pytest session: base args + plugins + stability report.

    Returns a :class:`_SessionOutcome` carrying the pytest rc, whether any test
    fell below its stability threshold, and the stability-report path (when a
    stability run produced one).
    """
    from .plugin import OttoPlugin
    from .pytest_plugin import OttoOptionsPlugin

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
        # Re-apply -m at run time: resolve_selection() filtered by markers during
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
        from .plugin import StabilityCollector as _StabilityCollector

        collector = _StabilityCollector()
        otto_plugin._stability_collector = collector  # noqa: SLF001 — intra-package write to stability plugin's collector slot

    import pytest

    # Capture the exit code so we can propagate it after post-run steps.
    rc = pytest.main(
        [*base_args, f"--junitxml={results_path}"],
        plugins=[otto_plugin, options_plugin],
    )

    unstable = False
    report: Path | None = None
    if is_stability and collector is not None:
        unstable = _print_stability_report(
            label, collector, opts.iterations, opts.duration, opts.threshold, log_dir
        )
        report = log_dir / "stability_report.txt"

    return _SessionOutcome(rc=int(rc), unstable=unstable, report=report)


def _print_stability_report(
    suite_name: str,
    collector: "StabilityCollector",
    iterations: int,
    duration: int,
    threshold: float,
    log_dir: Path,
) -> bool:
    """Print and save a per-test pass-rate stability report; return the unstable verdict.

    Writes ``stability_report.txt`` under ``log_dir`` and returns ``True`` when
    any test's pass rate fell below *threshold* (a percentage, 0-100). The
    caller folds that verdict into the invocation's exit code
    (:func:`_final_exit_code`) — this function no longer exits the process.
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

    return any_unstable


def run_suite(
    suite: type,
    *,
    options: object | None = None,
    run_options: RunOptions = _DEFAULT_RUN_OPTIONS,
    output_dir: Path | None = None,
) -> SuiteRunResult:
    """Run a suite class via ``pytest.main`` and return its :class:`SuiteRunResult`.

    *suite* is an ``OttoSuite`` subclass (or any pytest-collectable class);
    *options* is its per-suite ``Options`` instance (or ``None``); *run_options*
    carries the markers/stability/coverage/monitor settings; *output_dir*
    overrides where JUnit/coverage/stability artifacts land (defaults per
    :func:`resolve_output_dir`).
    """
    import asyncio
    import inspect

    from ..config import get_repos

    repos = get_repos()
    suite_file = inspect.getfile(suite)
    log_dir = resolve_output_dir(output_dir)
    results_path = run_options.results or str(log_dir / "junit.xml")
    sut_test_dirs = [p for r in repos for p in r.tests]

    with _session_context(log_dir):
        _pre_run_cov_dir_check(run_options)
        asyncio.run(_pre_run_cov_clean(repos, run_options))
        outcome = _run_pytest_session(
            [suite_file],
            suite.__name__,
            _repo_confcutdir(suite_file, repos),
            run_options,
            options,
            results_path,
            sut_test_dirs,
            log_dir,
            suite.__name__,
        )
        asyncio.run(_post_run_coverage(repos, log_dir, run_options))

    return SuiteRunResult(
        exit_code=_final_exit_code(outcome.rc, outcome.unstable),
        junit_paths=[Path(results_path)],
        stability_report=outcome.report,
        stability_unstable=outcome.unstable,
        output_dir=log_dir,
    )


def run_selection(
    *,
    run_options: RunOptions = _DEFAULT_RUN_OPTIONS,
    output_dir: Path | None = None,
) -> SuiteRunResult:
    """Run a suite-less ``--tests`` / ``-m`` selection — one pytest session per matching repo.

    Mirrors ``otto test --tests`` / ``otto test -m`` as a plain library call:
    exact test names (optionally ``Class::name`` qualified) and/or a marker
    expression are resolved against every configured repo's collected tests
    (:func:`otto.suite.selection.resolve_selection` for ``--tests``,
    :func:`otto.suite.selection.repos_with_marker_matches` for ``-m`` alone),
    and a pytest session runs once per repo whose selection matched. Sessions
    fold into a single :class:`SuiteRunResult`: ``exit_code`` is the worst
    per-session exit code (via the same stability-aware rule ``run_suite``
    uses internally), and ``junit_paths`` lists every session's JUnit file,
    in run order.

    Raises :class:`NoTestsMatchedError` (``"No tests matched the selection."``)
    when the selection resolves to nothing to run — no repos, no matching
    marker, or (via :func:`~otto.suite.selection.resolve_selection`) no test
    universe to search at all. A genuinely unknown test name against a real,
    non-empty test universe instead raises
    :class:`~otto.suite.selection.UnknownSelectionError` (a ``ValueError``
    subclass carrying the did-you-mean message) from
    :func:`~otto.suite.selection.resolve_selection` itself. Both subclass
    ``ValueError``; catch ``UnknownSelectionError`` before
    ``NoTestsMatchedError`` to distinguish the typo case.

    Raises ``ValueError`` immediately if *run_options* carries neither ``tests``
    nor ``markers``: with both empty a selection would match every test in every
    repo, so — like the ``otto test`` callback, which only reaches this path when
    ``--tests``/``-m`` is set — the library refuses rather than silently running
    everything.
    """
    if not (run_options.tests or run_options.markers):
        raise ValueError("run_selection requires run_options.tests or run_options.markers")

    import asyncio

    from ..config import get_repos
    from .selection import SelectionMatch, repos_with_marker_matches, resolve_selection

    opts = run_options
    repos = get_repos()
    names = [n.strip() for n in opts.tests.split(",") if n.strip()]
    if names:
        per_repo = resolve_selection(repos, names, opts.markers)
    else:  # -m alone: marker expression over each repo's test dirs
        matching_repos = repos_with_marker_matches(repos, opts.markers)
        per_repo = [
            SelectionMatch(repo=r, targets=[str(d) for d in r.tests if d.exists()])
            for r in matching_repos
        ]
        per_repo = [m for m in per_repo if m.targets]

    if not per_repo:
        raise NoTestsMatchedError("No tests matched the selection.")

    log_dir = resolve_output_dir(output_dir)

    worst = 0
    multi = len(per_repo) > 1
    junit_paths: list[Path] = []
    last_report: Path | None = None
    any_unstable = False
    with _session_context(log_dir):
        _pre_run_cov_dir_check(opts)
        asyncio.run(_pre_run_cov_clean(repos, opts))
        for match in per_repo:
            repo, targets = match.repo, match.targets
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
            outcome = _run_pytest_session(
                targets,
                None,
                repo.sut_dir,
                opts,
                None,  # no per-suite Options instance: suite-less selections have none
                results_path,
                sut_test_dirs,
                log_dir,
                label=f"selection:{repo.name}",
            )
            worst = max(worst, _final_exit_code(outcome.rc, outcome.unstable))
            junit_paths.append(Path(results_path))
            any_unstable = any_unstable or outcome.unstable
            if outcome.report is not None:
                last_report = outcome.report

        asyncio.run(_post_run_coverage(repos, log_dir, opts))

    return SuiteRunResult(
        exit_code=worst,
        junit_paths=junit_paths,
        stability_report=last_report,
        stability_unstable=any_unstable,
        output_dir=log_dir,
    )
