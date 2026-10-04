"""Test-run engine as a library call — ``run_tests`` without a Typer context.

This module holds the pytest-driving core behind ``otto test``: the
run-options record, the inner pytest session, the stability report, and the
:func:`prepare_run` preflight plus the coverage post-run hook. :func:`run_tests`
runs tests by name and/or marker as a plain library call —
``run_tests(["TestDevice"], output_dir=...)`` — returning a
:class:`SuiteRunResult` instead of raising ``typer.Exit``.

The CLI (``otto.cli.test``) keeps a thin adapter over :func:`run_tests` and
imports it at call time.

Import-weight note: this module never imports ``typer`` (nor ``pytest``) at
module load — the typer/pytest-touching pieces (the inner session's plugins, the
coverage helpers) are imported lazily inside the functions that need them, so
``import otto.suite.run`` stays cheap for library callers.
"""

import contextlib
import dataclasses
import os
import secrets
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import OttoError

if TYPE_CHECKING:
    from ..config.collected_tests import Classification, FileRecord, RepoTable
    from ..config.repo import Repo
    from ..context import OttoContext
    from ..coverage.reports import CleanReport
    from ..registry import RegistrationRefused
    from .layout import ArtifactLayout
    from .plugin import SelectedTest, StabilityCollector

import logging

logger = logging.getLogger(__name__)

RUN_OPTIONS_KEY = "otto_test_run_options"


class NoTestsMatchedError(OttoError, ValueError):
    """A test-name / ``-m`` selection resolved to nothing to run.

    Raised by :func:`run_tests` when no repo's session had a test to run — no
    repos configured, no repo carrying the marker, named tests the marker
    expression excludes, or no test universe to search at all. It is
    distinct from
    :class:`otto.suite.selection.UnknownSelectionError` (a genuinely unknown
    name against a real universe, carrying did-you-mean suggestions).

    Subclasses ``ValueError`` so it stays catchable as one, but the CLI adapter
    catches *this* specifically — a broad ``except ValueError`` would misreport
    an unrelated pipeline ``ValueError`` as "No tests matched the selection."
    """


def _lexical_path(path: Path) -> Path:
    """*path* made absolute and normalised without touching the filesystem."""
    # absolute(), not resolve(): resolve() reads symlinks; normpath folds ".."
    # without them.
    return Path(os.path.normpath(Path(path).expanduser().absolute()))


@dataclasses.dataclass(frozen=True)
class RunOptions:
    """Shared test-run options: markers/iterations/stability/coverage/monitor.

    The ``otto test`` callback constructs one of these from its CLI flags and
    stores it in Typer ``ctx.meta[RUN_OPTIONS_KEY]``; library callers pass one
    directly to :func:`run_tests`. This class is the ``otto test`` options: the
    CLI constructs it from parsed flags, and its rules (``__post_init__``) hold
    for every constructor. Which tests run is not a run option: :func:`run_tests`
    takes the names itself.
    """

    markers: str = ""
    random_order: bool = True
    """Shuffle test order (pytest-randomly). ``False`` unregisters the plugin
    for the session — ``--no-random`` — so order follows the source file.
    ``False`` with a ``seed`` is refused at construction."""
    seed: int | None = None
    """Fixed pytest-randomly seed (``--seed N``); ``None`` draws one. A seed
    with ``random_order=False`` is refused at construction: a seed with
    nothing to seed."""
    iterations: int = 0
    duration: int = 0
    threshold: float = 100.0
    results: str = ""
    cov: bool | None = None
    """Collect coverage after the run. ``None`` = auto: on when an instrumented
    product is detected and ``[coverage]`` is configured; see
    :func:`resolve_coverage`, which turns this into a plain bool before
    anything executes. Any coverage destination (``cov_dir``, ``cov_report``,
    ``cov_report_dir``, ``cov_tickets_json``) forces ``True``; ``False`` with
    one is refused at construction."""
    cov_dir: Path | None = None
    """Implies ``cov``."""
    overwrite_cov_dir: bool = False
    cov_clean: bool = True  # matches the --cov-clean CLI default
    cov_report: bool = False
    """Implies ``cov``."""
    cov_report_dir: Path | None = None
    """Implies ``cov_report``. Must not be or contain ``cov_dir``: clearing
    the report directory would clear the coverage data it reports on."""
    overwrite_cov_report_dir: bool = False
    project_name: str = "Coverage Report"
    cov_tickets_json: Path | None = None
    """Implies ``cov_report``."""
    monitor: bool = False
    """Forced ``True`` by ``monitor_output`` or ``monitor_hosts``."""
    monitor_interval: float = 5.0
    monitor_output: Path | None = None
    """Implies ``monitor``."""
    monitor_hosts: str | None = None
    """Implies ``monitor``."""

    def __post_init__(self) -> None:
        """Apply the implications and refuse the contradictions, for every constructor.

        Pure: no I/O, no repo config. The CLI, a script and a test all
        construct the same class, so what a destination implies for one it
        implies for the other. ``dataclasses.replace`` re-runs this, so a
        copy cannot lose an invariant.

        Raises:
            otto.params.OptionsValidationError: ``cov=False`` with a
                coverage destination, a ``cov_report_dir`` that is or
                contains ``cov_dir``, a ``seed`` with ``random_order=False``,
                a ``monitor_interval`` below the monitor's floor, or a
                ``monitor_hosts`` that is not a valid regex. The error class
                is imported inline on each raise branch, so the happy path
                (no contradiction) never imports it.
        """
        if self.cov_report_dir is not None or self.cov_tickets_json is not None:
            object.__setattr__(self, "cov_report", True)
        destination = self.cov_dir is not None or self.cov_report
        if destination:
            if self.cov is False:
                from ..params import OptionsValidationError

                raise OptionsValidationError(
                    "cov=False cannot be combined with cov_dir, cov_report, cov_report_dir "
                    "or cov_tickets_json, which all imply coverage"
                )
            object.__setattr__(self, "cov", True)
        if self.cov_dir is not None and self.cov_report_dir is not None:
            # Lexical, not Path.resolve(): construction reads no filesystem
            # content, so a symlink alias goes unnoticed (the CLI resolves its
            # paths first), but every spelling of one path (x/../x, ~/x, an
            # absolute x) is caught. A path that cannot be made absolute (an
            # unknown ~user, a deleted cwd) skips the rule: prepare_run's
            # destination checks report it.
            try:
                cov_dir = _lexical_path(self.cov_dir)
                report = _lexical_path(self.cov_report_dir)
            except (RuntimeError, OSError):
                cov_dir = report = None
            if cov_dir is not None and report is not None and cov_dir.is_relative_to(report):
                from ..params import OptionsValidationError

                # No paths in the message: the CLI spells field names as flags
                # word by word, so a path segment named like a field would be
                # rewritten, and the user has just typed both paths.
                raise OptionsValidationError(
                    "cov_report_dir cannot be or contain cov_dir: clearing the report "
                    "would clear the coverage data it reports on"
                )
        if self.monitor_output is not None or self.monitor_hosts is not None:
            object.__setattr__(self, "monitor", True)
        from ..utils import validate_interval

        try:
            validate_interval(self.monitor_interval)
        except ValueError as exc:
            from ..params import OptionsValidationError

            raise OptionsValidationError(f"monitor_interval: {exc}") from exc
        if self.monitor_hosts:
            from ..utils import compile_host_pattern

            try:
                compile_host_pattern(self.monitor_hosts)
            except ValueError as exc:
                from ..params import OptionsValidationError

                raise OptionsValidationError(f"monitor_hosts: {exc}") from exc
        if self.seed is not None and not self.random_order:
            from ..params import OptionsValidationError

            raise OptionsValidationError(
                "seed cannot be combined with random_order=False: a seed with nothing to seed"
            )


# Shared default for run_tests(run_options=...). RunOptions is frozen (immutable),
# so a module-level singleton is safe to share across calls — and it keeps the
# call out of the parameter default (avoids the B008 mutable-default footgun).
_DEFAULT_RUN_OPTIONS = RunOptions()


@dataclasses.dataclass(frozen=True)
class SuiteRunResult:
    """Outcome of a :func:`run_tests` invocation.

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
class _Selection:
    """What one session collects and selects: see :class:`otto.suite.plugin.OttoPlugin`.

    *table* is the repo's table as the run read it, and *classification*
    what :func:`~otto.config.collected_tests.classify` made of it: the
    session trusts its stats for the files it does not read, and what it
    reads is merged into *table*. ``candidates`` ``None`` collects the whole
    tree; otherwise those files, and :attr:`candidate_dirs` in full. A name
    in *must_match* the session cannot find stops it before any test.
    *markers* is pytest's ``-m``: it narrows what the names select, and the
    table still records everything collected.
    """

    table: "RepoTable | None"
    classification: "Classification"
    candidates: list[Path] | None = None
    names: list[str] = dataclasses.field(default_factory=list)
    must_match: list[str] = dataclasses.field(default_factory=list)
    markers: str = ""

    @property
    def candidate_dirs(self) -> list[Path]:
        """The directories a pruned session takes in full: they gained or lost an entry."""
        return [] if self.candidates is None else list(self.classification.candidate_dirs)


@dataclasses.dataclass(frozen=True)
class _SessionOutcome:
    """Result of one inner ``_run_pytest_session``.

    The rc, the stability verdict and report path, and the repo's table as
    the session left it. Every field after those is what the session's
    ``OttoPlugin`` learned: see :class:`otto.suite.plugin.OttoPlugin` for
    each of them.
    """

    rc: int
    unstable: bool
    report: Path | None
    table: "RepoTable | None" = None
    """The table with what the session read merged in; ``None`` when its
    collection did not finish (it vouches for nothing)."""
    collection_finished: bool = False
    unmatched_names: list[str] = dataclasses.field(default_factory=list)
    selected: "list[SelectedTest]" = dataclasses.field(default_factory=list)
    records: "dict[str, FileRecord]" = dataclasses.field(default_factory=dict)
    dirs: dict[str, list[int] | None] = dataclasses.field(default_factory=dict)
    dep_stats: dict[str, list[int] | None] = dataclasses.field(default_factory=dict)
    registered_markers: list[str] = dataclasses.field(default_factory=list)
    refusals: "list[RegistrationRefused]" = dataclasses.field(default_factory=list)
    stop_reason: str | None = None


def resolve_output_dir(output_dir: Path | None) -> Path:
    """Explicit param → context output_dir → CWD (xdir-defaults-to-CWD philosophy)."""
    if output_dir is not None:
        return output_dir
    from ..context import try_get_context

    ctx = try_get_context()
    if ctx is not None and ctx.output_dir is not None:
        return ctx.output_dir
    return Path.cwd()


ASYNCIO_LOOP_ARGS: list[str] = [
    "-o",
    "asyncio_default_test_loop_scope=session",
    "-o",
    "asyncio_default_fixture_loop_scope=session",
]
"""pytest-asyncio loop-scope defaults every ``otto test`` session runs under.

Every test and every async fixture runs on one session-wide event loop unless
it pins a narrower one. The consequences:

- A host is shared across the whole run. It connects on first use, every
  later test reuses that connection (and so its shell state), and the
  session loop's sweep closes it once, when the session ends.
- A fixture needs no ``loop_scope`` for the common case: a class-, module-
  or session-scoped async fixture runs on the session loop as it is.
- A test or class pinned with ``@pytest.mark.asyncio(loop_scope="class")``
  (or ``module``, or ``function``) gets its own loop. The hosts that loop
  opens belong to it and are closed when it ends.
- A host the session loop already owns fails fast from a narrower loop with
  a :class:`~otto.host.loop_owner.HostLoopError`. ``get_host`` returns one
  shared instance per host, so a host any unpinned test has used stays owned
  by the session loop until the run ends, and a narrower-pinned test can use
  it only if nothing unpinned has touched it yet. With random test order
  that depends on order, so narrower pins suit hosts that only the pinned
  code uses.
- An async fixture used by class-pinned tests should pin
  ``loop_scope="class"`` too: an unpinned one runs on the session loop, and
  the hosts it connects belong there. A module- or session-scoped fixture
  pinned to ``loop_scope="class"`` fails at setup with pytest-asyncio's
  ``ScopeMismatch``.

Otto's own in-process test sessions import this list rather than restating it.
"""


def _final_exit_code(rc: int, unstable: bool) -> int:
    """Threshold violations fail an otherwise-green run; pytest rc wins otherwise."""
    return 1 if (unstable and rc == 0) else int(rc)


@contextlib.contextmanager
def _session_context(log_dir: Path) -> "Iterator[OttoContext]":
    """Guarantee an active ``OttoContext`` with an ``output_dir`` for the session(s).

    Yields that context, so the caller can stamp the run's resolved coverage
    decision on it (``ctx.cov_decision``, read as ``ctx.cov``) once
    :func:`resolve_coverage` has taken it.

    Otto's own ``ctx`` fixture (and any suite code that reaches for a host)
    calls ``get_context()``; in the CLI that context is installed by the
    command preamble, but a library caller (``bootstrap()`` →
    :func:`run_tests`) has none. (``module_dir``/``test_dir`` no longer need
    this: their :class:`~otto.suite.layout.ArtifactLayout` is built from
    *log_dir* directly, below, and handed to the session's plugins.) Two
    cases:

    - **No active context**: install a minimal lab-less one
      (``OttoContext(lab=Lab(name=LIBRARY_LAB_NAME), output_dir=log_dir)``) for
      the duration of the session and always restore the prior state via the
      ``set_context``/``reset_context`` token pair. The sentinel ``Lab`` carries
      no hosts, so ``get_host()`` inside such a suite fails loud with its
      normal unknown-host error (plus an ``open_context`` breadcrumb keyed off
      ``LIBRARY_LAB_NAME`` — see :meth:`otto.context.OttoContext.get_host`) —
      correct for hostless library runs; suites that need lab hosts use
      ``open_context()`` (see the Cookbook's Python library page).
    - **Active context**: point its ``output_dir`` at *log_dir* when it has
      none (the same assignment the CLI preamble makes; one it already has is
      left alone), and restore ``output_dir``, ``cov_decision`` and the verb
      binding (:meth:`~otto.context.OttoContext.verb_binding_preserved` --
      :func:`run_tests` binds ``test``) afterwards — the run's per-session
      state never outlives the run on a context the caller owns.
    """
    from ..context import try_get_context

    active = try_get_context()
    if active is None:
        from ..config.lab import Lab
        from ..context import LIBRARY_LAB_NAME, OttoContext, reset_context, set_context

        ctx = OttoContext(lab=Lab(name=LIBRARY_LAB_NAME), output_dir=log_dir)
        token = set_context(ctx)
        try:
            yield ctx
        finally:
            reset_context(token)
        return
    prior_output_dir, prior_cov = active.output_dir, active.cov_decision
    if active.output_dir is None:
        active.output_dir = log_dir
    try:
        with active.verb_binding_preserved():
            yield active
    finally:
        active.output_dir, active.cov_decision = prior_output_dir, prior_cov


def prepare_run(opts: RunOptions, *, dry_run: bool = False) -> None:
    """Refuse a run that cannot save its files, before any host is touched.

    The I/O preflights ``otto test`` and :func:`run_tests` share: the
    explicit ``cov_dir`` and ``cov_report_dir`` destinations are prepared
    (created, or cleared under ``overwrite_*``) and proven writable, and
    ``cov_tickets_json`` needs a ``[coverage.tickets]`` table now rather
    than a warning after the run. Under *dry_run* the destinations are
    checked but nothing is created or cleared. The default destinations
    under the run's output directory need no check: creating that
    directory was the check. A script can call this before a long run.

    :func:`run_tests` calls it twice: under *dry_run* ahead of
    :func:`resolve_coverage`, so a bad destination is refused before the
    instrumentation scan, then for real once coverage is decided, so a run
    the decision refuses (for example, no ``[coverage]`` table configured)
    never clears an ``overwrite_cov_dir``/``overwrite_cov_report_dir``
    destination. Both calls come before any host is touched.

    Raises:
        otto.coverage.config.DestinationError: a destination is not a
            directory, cannot be written, or is not empty without
            ``overwrite_*``.
        otto.params.OptionsValidationError: ``cov_tickets_json`` is set
            and no ``[coverage.tickets]`` table is configured.
    """
    if opts.cov_dir is not None or opts.cov_report_dir is not None:
        # Lazy: otto.coverage's package __init__ is the whole collection stack.
        from ..coverage.config import check_destination, prepare_destination

        gate = check_destination if dry_run else prepare_destination
        if opts.cov_dir is not None:
            gate(
                opts.cov_dir,
                overwrite=opts.overwrite_cov_dir,
                field="cov_dir",
                remedy_field="overwrite_cov_dir",
            )
        if opts.cov_report_dir is not None:
            gate(
                opts.cov_report_dir,
                overwrite=opts.overwrite_cov_report_dir,
                field="cov_report_dir",
                remedy_field="overwrite_cov_report_dir",
            )
    if opts.cov_tickets_json is not None:
        # Knowable now, unlike "the git walk matched nothing", which stays a
        # post-run warning in _post_run_coverage.
        from ..config import get_repos
        from ..config.coverage_settings import get_cov_config
        from ..coverage.tickets import load_ticket_spec
        from ..params import OptionsValidationError

        if load_ticket_spec(get_cov_config(get_repos())) is None:
            raise OptionsValidationError(
                "cov_tickets_json requires [coverage.tickets] to be configured"
            )


def resolve_coverage(opts: RunOptions, repos: "list[Repo]", *, command: str) -> RunOptions:
    """Turn the tri-state ``cov`` into a decision — a copy with ``cov: bool``.

    Runs the local instrumentation scan over the coverage hosts once, before
    anything executes, so a forced-on run with nothing instrumented refuses
    here rather than after the suite. *command* names the invocation in the
    messages the decision logs.

    Forced off (``--no-cov``) short-circuits: no scan, no ``[coverage]``
    lookup, nothing at all.

    Forced on is a request that must be answerable *now*: with no ``[coverage]``
    table there is nothing to collect into, so this refuses here rather than
    running the whole suite and discarding the collection failure in
    ``_post_run_coverage``'s never-fail-a-successful-run swallow. A coverage-only
    configuration error (a ``[coverage].hosts`` selector that matches nothing,
    or a malformed one) is likewise fatal when forced on — and, in auto mode,
    only a warning: a plain ``otto test`` asked for no coverage and must not die
    of a coverage misconfiguration.

    Raises:
        otto.config.coverage_settings.CoverageConfigError: ``cov`` is forced on and
            either no ``[coverage]`` table is configured or its ``hosts``
            selector is malformed.
        otto.config.scope.EmptySelectionError: ``cov`` is forced on and the
            ``[coverage].hosts`` selector matched no host.
        otto.coverage.errors.CoverageNotInstrumentedError: ``cov`` is forced
            on and no product on any coverage host is instrumented.
    """
    if opts.cov is False:
        return dataclasses.replace(opts, cov=False)
    # Function-scope: otto.coverage.instrumentation pulls rich.table, and this
    # module sits on an import-budget surface a plain `otto test` must not
    # widen.
    from ..config.coverage_settings import CoverageConfigError, get_cov_config
    from ..config.scope import EmptySelectionError
    from ..coverage.instrumentation import decide_coverage, detect_for_lab

    forced_on = opts.cov is True
    has_cov_config = bool(get_cov_config(repos))
    if forced_on and not has_cov_config:
        raise CoverageConfigError(
            f"{command}: coverage was requested but no [coverage] table is configured — "
            "add a [coverage] table (and a tier) to .otto/settings.toml, or drop --cov."
        )

    try:
        report = detect_for_lab(repos)
    except (EmptySelectionError, CoverageConfigError) as e:
        if forced_on:
            raise
        from rich.markup import escape as escape_markup

        # Auto: the user asked for a test run, not for coverage. A broken
        # [coverage].hosts selector turns retrieval off with one warning
        # instead of killing the run. escape_markup(*e*): both of these
        # messages carry a literal bracket — "[coverage].hosts must be a
        # string", or the user's own regex (`test[123]`) quoted back by
        # EmptySelectionError — and the console handler and both log files
        # render messages as Rich markup, which would eat it.
        logger.warning("%s: coverage stays off: %s", command, escape_markup(str(e)))
        return dataclasses.replace(opts, cov=False)

    on = decide_coverage(opts.cov, report, has_cov_config=has_cov_config, command=command)
    return dataclasses.replace(opts, cov=on)


def _cov_command_label(opts: RunOptions, command: str) -> str:
    """Name the invocation for :func:`resolve_coverage`'s messages."""
    return f"{command} --cov" if opts.cov else command


async def _pre_run_cov_clean(repos: "list[Repo]", opts: RunOptions) -> None:
    """Pre-run cleanup of coverage counters on the lab's hosts, when --cov and --cov-clean.

    Called once per invocation, never once per repo inside the session loop.
    The clean itself lives in :func:`otto.coverage.collect.clean_coverage`; it
    is imported lazily so this module never pulls the coverage stack at load
    time. The ``--cov``/``--cov-clean`` gate stays here. A failed reset is
    never best-effort: it raises :class:`~otto.coverage.errors.CoverageCleanError`,
    naming the host and product, which ends the run before any test runs (the
    ``pytest.exit`` wrapper around this call, in :meth:`_Sessions._before_tests`,
    carries it out).
    """
    if not (opts.cov and opts.cov_clean):
        return
    from ..coverage.collect import clean_coverage
    from ..coverage.errors import CoverageCleanError

    report = await clean_coverage(repos)
    if not report.ok:
        raise CoverageCleanError(report)
    _log_clean_summary(report, "before the run")


def _log_clean_summary(report: "CleanReport", when: str) -> None:
    """Leave one INFO line in the run log for a clean that succeeded (*when* names which)."""
    cleared = report.cleared
    hosts = {host for host, _product in cleared}
    message = (
        f"coverage counters cleared {when} on {len(cleared)} product(s) across {len(hosts)} host(s)"
    )
    if report.not_run:
        message += f"; {len(report.not_run)} not run (dry run)"
    logger.info(message)


def _post_run_coverage_beside(
    in_flight: BaseException | None,
    repos: "list[Repo]",
    taking_part: "list[tuple[_RepoRun, _SessionOutcome]]",
    log_dir: Path,
    opts: RunOptions,
) -> None:
    """Collect the coverage of the tests that ran, never hiding the error that ended the run.

    With *in_flight* set (a later repo refused a registration, say), a
    coverage failure is logged, naming the repos whose tests ran, and the
    original error goes on propagating. With nothing in flight it raises.
    """
    from ..lifecycle import run_command

    try:
        run_command(_post_run_coverage(repos, log_dir, opts))
    except Exception:
        if in_flight is None:
            raise
        ran = ", ".join(run.repo.name for run, _ in taking_part)
        logger.exception("coverage collection for %s failed after the run already failed", ran)


async def _post_run_coverage(repos: "list[Repo]", log_dir: Path, opts: RunOptions) -> None:
    """Post-run coverage collection and optional HTML report, shared by both run paths.

    Collection runs through :func:`otto.coverage.collect.collect_coverage` (the
    single canonical fetch/metadata/capture workflow), which *fails loud*: the
    never-fail-a-successful-run swallow policy lives here, in the ``try/except``
    around it. The optional HTML report resolves its inputs via
    :func:`otto.coverage.report_inputs.resolve_report_inputs` and calls
    :func:`otto.coverage.reporter.run_coverage_report`, whose own two-mode
    destination gate (``prepare_destination``) raises a field-named
    ``DestinationError`` (never ``typer``), so a report-dir collision on a
    library re-run is swallowed here like any other report failure.
    Everything is imported lazily to avoid a load-time cost for non-coverage
    runs and to keep the existing patch points valid.
    """
    if not (opts.cov or opts.cov_report):
        # Nothing to do — and skipping the lazy import below keeps a
        # non-coverage library run_tests() call from pulling the CLI (typer).
        return

    # A failed clean after collection fails the run, but only once the report
    # below has had its turn: the captures are already written, and the
    # report is best-effort by policy, so it is never skipped for the clean.
    clean_failure: Exception | None = None
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
        result = None
        try:
            result = await collect_coverage(cov_dir, repos=repos)
        except (ValueError, RuntimeError, FileNotFoundError) as e:
            logger.warning(
                "Coverage collection failed (%s); raw coverage artifacts remain in %s",
                escape_markup(str(e)),
                cov_dir,
            )
        if result is not None and result.clean is not None and not result.clean.ok:
            from ..coverage.errors import CoverageCleanError

            # Clearing is never best-effort: stale counters would mix into the
            # next run's coverage. Raised after the report block below.
            clean_failure = CoverageCleanError(result.clean)
        elif result is not None and result.clean is not None:
            _log_clean_summary(result.clean, "after collection")

    if opts.cov_report:
        from rich.markup import escape as escape_markup

        from ..coverage.config import DestinationError
        from ..coverage.report_inputs import resolve_report_inputs
        from ..coverage.reporter import run_coverage_report

        cov_dir = opts.cov_dir or log_dir / "cov"
        report_dir = (
            opts.cov_report_dir if opts.cov_report_dir is not None else log_dir / "cov_report"
        )
        # Like the capture tail, in-run report generation must never fail an
        # otherwise-successful test run: run_coverage_report's own
        # destination gate (prepare_destination, which raises a field-named
        # DestinationError — a report-dir collision on a library re-run into
        # a reused output_dir warns and skips rather than raising typer from
        # a public library entrypoint), a non-git sut, a polluted tree, a
        # malformed manual capture, or a malformed override file are logged
        # and swallowed, leaving the raw coverage artifacts on disk.
        # resolve_report_inputs lives inside this try (not resolved ahead of
        # it) so a malformed [coverage.exclusions] rule or override file
        # raises from resolve_report_inputs inside this try — it raises
        # CoverageConfigError/OverrideConfigError (both a ValueError), caught
        # below same as every other report-generation failure, rather than
        # failing an otherwise-successful test run.
        inputs = None
        try:
            inputs = resolve_report_inputs(repos)
            store = await run_coverage_report(
                [cov_dir],
                report_dir,
                inputs,
                project_name=opts.project_name,
                overwrite=opts.overwrite_cov_report_dir,
            )
        except (ValueError, RuntimeError, FileNotFoundError) as e:
            # run_coverage_report's own gate speaks in ITS field names
            # (output_dir/overwrite) — this caller's user set RunOptions
            # fields (cov_report_dir/overwrite_cov_report_dir), so a
            # DestinationError from that gate is re-rendered in those names
            # before it is logged, the same translation
            # otto.cli.invoke.usage_error_from does for the CLI.
            from ..coverage.config import destination_message

            message = str(e)
            if isinstance(e, DestinationError) and e.field == "output_dir":
                message = destination_message(
                    e.kind,
                    e.path,
                    subject="cov_report_dir",
                    remedy="set overwrite_cov_report_dir=True",
                    reason=e.reason,
                )
            # escape_markup(*message*): the console handler renders log
            # messages as Rich markup, and this message may echo a literal
            # bracket (e.g. a malformed [coverage.exclusions] rule).
            logger.warning(
                "Coverage report generation failed (%s); raw coverage artifacts remain in %s",
                escape_markup(message),
                cov_dir,
            )
            store = None
        if store is not None and inputs is not None:
            logger.info(
                "Coverage: %.1f%% overall (%d files)", store.overall_pct(), store.file_count()
            )
            logger.info("Report: %s", report_dir / "index.html")

            if opts.cov_tickets_json is not None:
                # Scoped separately from the report-generation try/except
                # above: a missing-ticket-data ValueError here must not be
                # misreported as "Coverage report generation failed" (the
                # HTML report already succeeded) — same never-fail-a-
                # successful-run policy, distinct message. The CLI
                # (cli/test.py's main() callback) fails fast on the
                # *knowable* misconfiguration (no [coverage.tickets] at all)
                # before the test run even starts; this stays the catch-all
                # for the genuinely unknowable case (config present, but the
                # git walk matched nothing) plus any library caller that
                # skipped that preflight (e.g. no repo_root at all here).
                if inputs.repo_root is None:
                    logger.warning(
                        "Ticket export skipped (no ticket data in this report — "
                        "[coverage.tickets] must be configured for --cov-tickets-json); "
                        "coverage report still written to %s",
                        report_dir,
                    )
                else:
                    from ..coverage.ticket_export import make_generated_stamp, write_ticket_export
                    from ..version import get_version

                    try:
                        write_ticket_export(
                            store,
                            opts.cov_tickets_json,
                            repo_root=inputs.repo_root,
                            project=opts.project_name,
                            otto_version=get_version(),
                            generated=make_generated_stamp(),
                        )
                    except ValueError as e:
                        logger.warning(
                            "Ticket export failed (%s); coverage report still written to %s",
                            escape_markup(str(e)),
                            report_dir,
                        )
                    else:
                        logger.info("Ticket export: %s", opts.cov_tickets_json)

    if clean_failure is not None:
        raise clean_failure


def _guarded_pytest_session(
    *args: Any, **kwargs: Any
) -> "tuple[_SessionOutcome | None, int | None]":
    """Run one in-process pytest session under the sync-phase interrupt policy.

    Returns ``(outcome, interrupted_signum)``. The first SIGINT/SIGTERM
    reaches pytest as its normal graceful KeyboardInterrupt teardown (fixtures
    unwind, suite fixtures release connections) with the teardown deadline
    armed; a second signal or deadline expiry force-exits ``128 + signum``
    (see :func:`otto.lifecycle.sync_phase`). ``outcome`` is ``None`` only if
    KeyboardInterrupt escaped pytest itself (hard abort); a graceful
    in-session interrupt still returns pytest's outcome, with the signal
    reported alongside so callers exit ``128 + signum``.

    Off the main thread (``run_tests`` as a plain library call from a worker
    thread) the session runs unguarded — signal handlers
    are a main-thread-only facility, and the async policy degrades the same
    way (``_CommandRun._main`` swallows ``add_signal_handler``'s refusal).
    """
    from otto.lifecycle import SyncPhaseInterrupt, sync_phase

    guard = None
    try:
        with sync_phase(
            what="pytest session",
            install_handlers=threading.current_thread() is threading.main_thread(),
        ) as g:
            guard = g
            outcome = _run_pytest_session(*args, **kwargs)
            return outcome, g.interrupted_signum
    except KeyboardInterrupt as exc:
        # The guard's raise can escape sync_phase itself (its entry/exit
        # windows), not just the phase body; SyncPhaseInterrupt carries the
        # signal so 128+signum holds there too. A foreign KeyboardInterrupt
        # (user code, no guard signal) keeps its pre-guard behavior.
        if isinstance(exc, SyncPhaseInterrupt):
            return None, exc.signum
        if guard is None or guard.interrupted_signum is None:
            raise  # not ours — a bare KeyboardInterrupt from user code
        return None, guard.interrupted_signum


_NOTHING_RAN = (4, 5)
"""pytest's exit codes for a session that ran no test: a usage error, no test collected.

A usage error is also how a session stops for an unknown name, a refused
registration or a failed coverage pre-clean (``pytest.ExitCode.USAGE_ERROR``,
``NO_TESTS_COLLECTED``; plain ints, so this module need not import pytest).
"""


class _SessionStdout:
    """``sys.stdout`` for one inner pytest session.

    pytest's terminal reporter binds ``sys.stdout`` when the session is
    configured, so everything it prints comes through here. A run writes its
    JUnit file under a temporary name (:func:`_run_pytest_session`): the
    "generated xml file" line names the path the file is moved to, or is
    dropped when the file is not kept (*kept* is asked then, at the end of
    the session). A *silent* stream drops everything: a collection prints
    nothing.
    """

    def __init__(
        self, real: Any, *, partial: str, final: str, kept: Callable[[], bool], silent: bool
    ) -> None:
        self._real = real
        self._partial = partial
        self._final = final
        self._kept = kept
        self._silent = silent
        self._dropped_line = False

    def write(self, text: str) -> int:
        """Pass *text* on, naming the JUnit file where it ends up; or drop it."""
        dropped_line, self._dropped_line = self._dropped_line, False
        if self._silent or (dropped_line and text == "\n"):
            return len(text)
        if self._partial in text:
            if not self._kept():
                self._dropped_line = True
                return len(text)
            text = text.replace(self._partial, self._final)
        return self._real.write(text) or len(text)

    def flush(self) -> None:
        """Flush the real stream, unless this one is silent."""
        if not self._silent:
            self._real.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@contextlib.contextmanager
def _outside_the_repo(*, collect_only: bool, home: Path | None = None) -> Iterator[list[str]]:
    """Keep one session's bytecode and pytest's cache out of the repo; yield its pytest args.

    While the session runs, ``sys.pycache_prefix`` points at the workspace's
    ``pycache`` directory under ``OTTO_HOME`` (:data:`otto.config.home.PYCACHE_DIRNAME`),
    so what the session imports, assertion-rewritten test files included,
    writes its bytecode there instead of into a ``__pycache__`` beside it. A
    prefix the user set (``PYTHONPYCACHEPREFIX``) is left as it is. The
    prefix before the session is restored afterwards, however it ends: it is
    process-wide, and sessions in one process run one after another.

    The args give a run pytest's cache in the workspace's ``pytest-cache``
    directory; a collection has none at all. pytest otherwise writes
    ``.pytest_cache/`` into its rootdir, which for a repo with no pytest
    config of its own is a test directory.

    So a session adds no entry to any directory of the repo, and a test
    directory's stat moves only when a file in it is added, removed or
    renamed. *home* is the workspace home when the caller already has it
    (:func:`otto.config.home.workspace_home`, which resolves every SUT dir).
    """
    from ..config.home import PYCACHE_DIRNAME, PYTEST_CACHE_DIRNAME, workspace_home

    home = home or workspace_home()
    if collect_only:
        args = ["-p", "no:cacheprovider"]
    else:
        args = ["-o", f"cache_dir={(home / PYTEST_CACHE_DIRNAME).absolute()}"]
    prior = sys.pycache_prefix
    sys.pycache_prefix = prior or str((home / PYCACHE_DIRNAME).absolute())
    try:
        yield args
    finally:
        sys.pycache_prefix = prior


def _run_pytest_session(
    repo: "Repo", selection: _Selection, run: "_RunSetup | None" = None, *, home: Path | None = None
) -> _SessionOutcome:
    """Collect *repo*'s tests in one inner pytest session, and write what it read into its table.

    The one way anything learns what a repo's test files hold: the seed of a
    cold table, a refresh, a run and a listing all come through here. The
    session covers *repo*'s test directories as *selection* says
    (:class:`_Selection`), and *run* says how it runs the tests the names
    select. *home* is the workspace home, when the caller has it
    (:func:`_outside_the_repo`: no session writes into the repo).

    With *run* ``None`` the session is ``--collect-only``, in collection
    order: it runs nothing, prints nothing and writes no JUnit. A repo with
    no test directory starts no session at all: pytest given no path
    collects its cwd.

    A run writes its JUnit file beside its ``results_path`` under a temporary
    name and moves it into place only when the session did not end in a
    usage error or with nothing collected (``_NOTHING_RAN``): a session
    stopped for an unknown name, or one that matched nothing, leaves no file
    and never replaces an earlier one.

    What a finished collection read is merged into ``selection.table``
    (:func:`~otto.config.collected_tests.updated_table`; a whole-tree one
    also replaces the registered markers and dates the table), and written
    when that changed it. Returns a :class:`_SessionOutcome`: the pytest rc,
    whether any test fell below its stability threshold, the
    stability-report path (when a stability run produced one), the table,
    and what the plugin collected.

    Raises:
        otto.registry.RegistrationRefused: a test file or conftest registered
            something while loading; nothing is written.
    """
    import pytest

    from ..config.collected_tests import updated_table, write_tables

    targets = [str(d) for d in repo.tests if d.exists()]
    if targets:
        outcome = _pytest_session(repo, targets, selection, run, home)
    else:
        outcome = _SessionOutcome(
            rc=pytest.ExitCode.NO_TESTS_COLLECTED,
            unstable=False,
            report=None,
            collection_finished=True,
        )
    if outcome.refusals:
        raise outcome.refusals[0]
    if not outcome.collection_finished:
        return outcome
    whole_tree = selection.candidates is None
    table = updated_table(
        repo,
        selection.table,
        selection.classification,
        outcome.records,
        registered_markers=outcome.registered_markers if whole_tree else None,
        whole_tree=whole_tree,
        dirs=outcome.dirs,
        dep_stats=outcome.dep_stats,
    )
    if table != selection.table:
        write_tables([table], home=home)
    return dataclasses.replace(outcome, table=table)


_COLLECTING = RunOptions(random_order=False)
"""The options of a ``--collect-only`` session: nothing runs, so only the order matters."""


def _pytest_session(
    repo: "Repo",
    targets: list[str],
    selection: _Selection,
    run: "_RunSetup | None",
    home: Path | None,
) -> _SessionOutcome:
    """Run the pytest session of :func:`_run_pytest_session` over *targets*; return its outcome."""
    from .layout import ArtifactLayout
    from .plugin import OttoPlugin
    from .pytest_plugin import OttoFixturesPlugin

    collect_only = run is None
    opts = _COLLECTING if run is None else run.opts
    base_args: list[str] = [
        *targets,
        "-s",
        # The runtime-dependency plugins the arguments below belong to, named
        # by their entry points: pytest's plugin autoload is the user's to
        # switch off (PYTEST_DISABLE_PLUGIN_AUTOLOAD), and without these an
        # argument is a usage error or an ini option silently ignored.
        "-p",
        "asyncio",
        "-p",
        "timeout",
        "-o",
        "asyncio_mode=auto",
        *ASYNCIO_LOOP_ARGS,
        # pytest-timeout honors @pytest.mark.timeout(N) on tests/classes. No
        # global default is imposed here — timeouts in user suites stay opt-in,
        # as they were before — but signal method ensures a fired timeout
        # interrupts blocking calls and the session still reaches sessionfinish.
        "-o",
        "timeout_method=signal",
        # pytest-cov is a development dependency, so the inner session blocks
        # it with pytest's own `-p no:` (which parses with or without the
        # plugin) rather than pytest-cov's `--no-cov` (which does not, #593).
        "-p",
        "no:pytest_cov",
        "--no-header",
        "--override-ini",
        "log_cli=false",
        "--override-ini",
        "addopts=",
        # `addopts=` above drops the dev tree's `-p no:tach` guard along with
        # everything else, so re-assert it directly: tach's pytest plugin
        # installs a C-level SIGINT handler at import and panics on the second
        # in-process pytest session (issue #193) — `otto test` runs one
        # session per invocation of this function.
        "-p",
        "no:tach",
        # Cut conftest loading at the repo root: the user repo's whole
        # conftest hierarchy loads; otto's own tests/conftest.py (which resets
        # logging management state) stays excluded for in-tree example repos
        # because it lives above their sut_dir.
        f"--confcutdir={repo.sut_dir}",
        # pytest-asyncio registers anyio for assertion rewriting, but anyio is
        # already imported by the time pytest.main() is called from within otto.
        # The warning is harmless (anyio's internals don't affect test results)
        # so suppress it here rather than polluting suite output.
        "--override-ini",
        "filterwarnings=ignore::pytest.PytestAssertRewriteWarning",
    ]
    # pytest-randomly is a runtime dependency, named like the plugins above:
    # random order is the default. `--no-header` above hides the plugin's own
    # "Using --randomly-seed=N" line — OttoPlugin logs the seed instead.
    if not opts.random_order:
        base_args += ["-p", "no:randomly"]
    else:
        base_args += ["-p", "randomly"]
        if opts.seed is not None:
            base_args.append(f"--randomly-seed={opts.seed}")
    if selection.markers:
        # -m deselects after OttoPlugin matched the names, so a name the
        # expression excludes is still a known name (it matches nothing).
        base_args += ["-m", selection.markers]
    # A file that fails to collect doesn't stop the tests that were selected,
    # by name or by marker, or the rest of a collection (pytest then exits 1).
    base_args.append("--continue-on-collection-errors")

    final_junit = partial_junit = None
    if run is None:
        base_args.append("--collect-only")
    else:
        final_junit = run.results_path
        partial_junit = final_junit.with_name(f".{final_junit.name}.{os.getpid()}.partial")
        base_args.append(f"--junitxml={partial_junit}")

    is_stability = not collect_only and (opts.iterations > 0 or opts.duration > 0)
    monitor_output = opts.monitor_output
    if opts.monitor and monitor_output is None and run is not None:
        monitor_output = run.log_dir / "monitor.json"
    collector: "StabilityCollector | None" = None
    if is_stability:
        from .plugin import StabilityCollector as _StabilityCollector

        collector = _StabilityCollector()
    otto_plugin = OttoPlugin(
        sut_test_dirs=list(repo.tests) if run is None else run.sut_test_dirs,
        iterations=opts.iterations,
        duration=opts.duration,
        monitor=opts.monitor,
        monitor_interval=opts.monitor_interval,
        monitor_output=monitor_output,
        monitor_hosts=opts.monitor_hosts,
        candidates=selection.candidates,
        candidate_dirs=selection.candidate_dirs,
        known_stats=selection.classification.stats,
        known_dirs=selection.classification.dirs,
        known_deps=selection.classification.deps,
        names=selection.names,
        must_match=selection.must_match,
        before_tests=None if run is None else run.before_tests,
        # run_tests draws the run's one seed and says it once.
        announce_seed=False,
        stability_collector=collector,
    )
    fixtures_plugin = OttoFixturesPlugin(
        layout=ArtifactLayout(root=repo.sut_dir, test_roots=repo.tests)
        if run is None
        else run.layout
    )
    stream = _SessionStdout(
        sys.stdout,
        partial=str(partial_junit),
        final=str(final_junit),
        kept=lambda: otto_plugin.exitstatus not in _NOTHING_RAN,
        silent=collect_only,
    )

    import pytest

    from ..registry import loading_test_files

    # Capture the exit code so we can propagate it after post-run steps. The
    # session imports the repo's test files and conftests, and those may not
    # register anything: extensions belong in an init module.
    # What a session that raised out of pytest.main() is taken to have
    # returned: its JUnit file, if any, is kept as evidence.
    rc: int = pytest.ExitCode.INTERNAL_ERROR
    try:
        with (
            _outside_the_repo(collect_only=collect_only, home=home) as cache_args,
            loading_test_files(),
            contextlib.redirect_stdout(stream),  # type: ignore[type-var]
        ):
            rc = pytest.main([*base_args, *cache_args], plugins=[otto_plugin, fixtures_plugin])
    finally:
        # The next session in this process imports the files as they are
        # then: pytest would serve these modules from the last import.
        for name in otto_plugin.imported_modules():
            sys.modules.pop(name, None)
        if partial_junit is not None and final_junit is not None:
            if rc not in _NOTHING_RAN and partial_junit.exists():
                partial_junit.replace(final_junit)
            else:
                partial_junit.unlink(missing_ok=True)

    unstable = False
    report: Path | None = None
    if run is not None and collector is not None and rc not in _NOTHING_RAN:
        unstable = _print_stability_report(
            f"selection:{repo.name}",
            collector,
            opts.iterations,
            opts.duration,
            opts.threshold,
            run.log_dir,
        )
        report = run.log_dir / "stability_report.txt"

    return _SessionOutcome(
        rc=int(rc),
        unstable=unstable,
        report=report,
        collection_finished=otto_plugin.collection_finished,
        unmatched_names=list(otto_plugin.unmatched_names),
        selected=list(otto_plugin.selected),
        records=dict(otto_plugin.records),
        dirs=dict(otto_plugin.dirs),
        dep_stats=dict(otto_plugin.dep_stats),
        registered_markers=list(otto_plugin.registered_markers),
        refusals=list(otto_plugin.refusals),
        stop_reason=otto_plugin.stop_reason,
    )


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


# ── which files hold the names: decided before any session ──────────────────


@dataclasses.dataclass
class _Known:
    """What a run knows of one repo's tests before its session: its table, and how far to trust it.

    A record is trusted to say what its file holds and lacks when
    :func:`~otto.config.collected_tests.classify` found it fresh, or when a
    collection earlier in this run read the file (*read*): that record is
    what pytest just saw, even while a dependency seen for the first time
    keeps it changed.
    """

    table: "RepoTable | None"
    classification: "Classification"
    read: set[str] = dataclasses.field(default_factory=set)
    """Files a collection earlier in this run read."""
    searched: bool = False
    """A collection earlier in this run read every file the table could not vouch for."""

    @property
    def stale(self) -> bool:
        """Whether the table is cold, or a file or a directory's entries changed since it."""
        c = self.classification
        return c.whole_tree or bool(c.changed or c.new or c.candidate_dirs)

    @property
    def uncertain(self) -> bool:
        """Whether a name no trusted record holds may still be here: stale, and not yet searched."""
        return self.stale and not self.searched

    def holding(self, name: str) -> list[Path]:
        """Return the files whose trusted record holds a test *name* selects."""
        from .selection import matches_name

        if self.table is None or self.classification.whole_tree:
            return []
        trusted = {str(p) for p in self.classification.fresh} | self.read
        return sorted(
            Path(key)
            for key, record in self.table.files.items()
            if key in trusted
            and any(matches_name(name, test.classes, test.name) for test in record.tests)
        )


@dataclasses.dataclass(frozen=True)
class _Decision:
    """What a run does before its first session; see :func:`_decide`."""

    refresh: list[int] = dataclasses.field(default_factory=list)
    """Repos to collect (``--collect-only``) before anything else is decided."""
    unknown: list[str] = dataclasses.field(default_factory=list)
    """Names no repo can hold: refused before any session starts."""
    sessions: dict[int, _Selection] = dataclasses.field(default_factory=dict)
    """Each run session, by its repo's position in the run's repo list, in the order they run."""
    searching: int | None = None
    """The one uncertain repo whose run session must find the names no table places."""


def _decide(names: list[str], repos: list[_Known], markers: str = "") -> _Decision:
    """Decide which repo collects which files for *names*, from the tables alone.

    The rule a run's sessions follow. A name is placed when a trusted record
    in some repo holds it (:meth:`_Known.holding`). A name placed nowhere
    may only be in an uncertain repo (:attr:`_Known.uncertain`):

    - with none, it is unknown, before any session;
    - with exactly one, that repo's run session is its collection too: it
      runs first and must find the name, so a name it lacks, or a
      collection that cannot finish, stops the run before any test in any
      repo;
    - with more, each is collected first (``--collect-only``), and the
      caller decides again over what those collections read, which leaves
      none uncertain (a collection that cannot finish ends the run: see
      :class:`_Sessions`).

    Every repo that holds a name or has a file the table cannot vouch for
    gets one run session over those files, in the configured order: so a
    name runs wherever it is, and a file that changed is read and recorded.
    A file a collection earlier in this run already read (:attr:`_Known.read`)
    is not read again unless it holds a name.
    Each must find the names only it is known to hold (should a record be
    wrong, the session says so before its tests run), and *markers* narrows
    what it selects.
    """
    holding = [{name: known.holding(name) for name in names} for known in repos]
    holders = {name: [i for i, found in enumerate(holding) if found[name]] for name in names}
    unplaced = [name for name in names if not holders[name]]
    uncertain = [i for i, known in enumerate(repos) if known.uncertain]
    if unplaced and not uncertain:
        return _Decision(unknown=unplaced)
    if unplaced and len(uncertain) > 1:
        return _Decision(refresh=uncertain)
    searching = uncertain[0] if unplaced else None
    sessions: dict[int, _Selection] = {}
    for i in sorted(range(len(repos)), key=lambda i: i != searching):
        known, c = repos[i], repos[i].classification
        held = {path for paths in holding[i].values() for path in paths}
        unread = {*c.changed, *c.new} - {Path(key) for key in known.read}
        if not (held or unread or c.candidate_dirs or c.whole_tree):
            continue
        sessions[i] = _Selection(
            table=known.table,
            classification=c,
            candidates=None if c.whole_tree else sorted({*held, *unread}),
            names=names,
            must_match=[
                n for n in names if holders[n] == [i] or (i == searching and not holders[n])
            ],
            markers=markers,
        )
    return _Decision(sessions=sessions, searching=searching)


# ── the run's sessions ──────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class _RunSetup:
    """What a session that runs tests needs beyond what it collects: ``run_tests``' part."""

    opts: RunOptions
    results_path: Path
    layout: "ArtifactLayout"
    log_dir: Path
    sut_test_dirs: list[Path]
    """Every repo's test directories: a run's sessions share one lab."""
    before_tests: "Callable[[], None] | None" = None


@dataclasses.dataclass
class _RepoRun:
    """One repo's part in a run: what the run knows of it, and how its session runs tests."""

    repo: "Repo"
    known: _Known
    setup: _RunSetup | None = None
    """Where its session writes and how it runs the tests; ``None``: it only collects."""
    matched: set[str] = dataclasses.field(default_factory=set)
    """The requested names this repo's run session found."""
    outcome: _SessionOutcome | None = None
    """The session that ran this repo's tests, or whose collection did not finish."""


class _Sessions:
    """The pytest sessions of one run: at most one run session per repo.

    With names, the sessions :func:`_decide` plans, which says which repo
    collects what, and when a name is unknown before any test runs. With no
    names, each repo gets one whole-tree session. Each session writes what it
    collected back to the table. A repo with no :attr:`_RepoRun.setup` has
    its sessions collect only (a listing, a dry run): the same sessions, and
    nothing runs.

    An interrupt stops the run; so does a session searching for a name no
    table places (a ``--collect-only`` one, or the one uncertain repo's run
    session) whose collection cannot finish (a conftest that fails to load,
    a ``pytest.exit``): it is logged, naming the repo and why, and the run
    fails with its exit code (:attr:`failed`) before any test. Any other
    run session that cannot finish fails its repo's part, the run goes on,
    and no name it was to find is called unknown; a collection that cannot
    finish is logged and fails the run just the same.
    """

    def __init__(
        self,
        repos: "list[Repo]",
        runs: list[_RepoRun],
        names: list[str],
        opts: RunOptions,
        home: Path,
    ) -> None:
        self.repos = repos
        self.runs = runs
        self.names = names
        self.opts = opts
        self.home = home
        self.interrupted: int | None = None
        self.failed: int | None = None
        """The exit code of the collection that could not finish, which ended the run."""
        self._cleaned = False
        self._clean_error: BaseException | None = None

    def run(self) -> None:
        """Run every repo's session; stop at an interrupt or an unfinished search."""
        markers = self.opts.markers
        if not self.names:
            for run in self.runs:
                selection = _Selection(run.known.table, run.known.classification, markers=markers)
                if not self._session(run, selection):
                    return
            return
        decision = _decide(self.names, [run.known for run in self.runs], markers)
        if decision.refresh:
            for i in decision.refresh:
                if not self.refresh(self.runs[i]):
                    return
            decision = _decide(self.names, [run.known for run in self.runs], markers)
        if decision.unknown:
            raise self._unknown(decision.unknown)
        for i, selection in decision.sessions.items():
            if not self._session(self.runs[i], selection, searching=i == decision.searching):
                return
        unfinished = any(r.outcome is not None and r.outcome.table is None for r in self.runs)
        unmatched = [n for n in self.names if not any(n in run.matched for run in self.runs)]
        if unmatched and not unfinished:
            # Every record said a name was there, and no session found it (an
            # edit its stat did not show): tests may have run, but it is said.
            raise self._unknown(unmatched)

    def refresh(self, run: _RepoRun) -> bool:
        """Collect what *run*'s table cannot vouch for, running nothing; ``False`` to stop.

        One ``--collect-only`` session over the changed and new files and the
        candidate directories, or the whole tree when the table is cold. A
        table whose only news is deleted files, in directories whose stat did
        not move, drops their records with no session.
        """
        from ..config.collected_tests import classify, updated_table, write_tables

        c = run.known.classification
        candidates = None if c.whole_tree else sorted({*c.changed, *c.new})
        if candidates == [] and not c.candidate_dirs and run.known.table is not None:
            table = updated_table(run.repo, run.known.table, c, {})
            if table != run.known.table:
                write_tables([table], home=self.home)
            run.known = _Known(table=table, classification=classify(run.repo, table), searched=True)
            return True
        selection = _Selection(run.known.table, c, candidates)
        outcome = self._collected(run, selection, None, searching=True)
        if outcome is None or outcome.table is None:
            return False
        run.known = _Known(
            table=outcome.table,
            classification=classify(run.repo, outcome.table),
            read=set(outcome.records),
            searched=True,
        )
        return True

    def _session(self, run: _RepoRun, selection: _Selection, *, searching: bool = False) -> bool:
        """Run *run*'s one session over *selection*; ``False`` to stop.

        *searching*: the session must find names no table places
        (:attr:`_Decision.searching`), so one that cannot finish ends the run.

        Raises:
            otto.suite.selection.UnknownSelectionError: the session stopped
                because a name it had to find is not there.
        """
        setup = run.setup
        if setup is not None:
            setup = dataclasses.replace(setup, before_tests=self._before_tests)
        outcome = self._collected(run, selection, setup, searching=searching)
        if outcome is None or (outcome.table is None and searching):
            return False
        if outcome.table is not None:
            run.known = dataclasses.replace(run.known, table=outcome.table)
            run.matched |= {n for n in self.names if n not in outcome.unmatched_names}
            missing = [n for n in selection.must_match if n in outcome.unmatched_names]
            if missing:
                raise self._unknown(missing)
        run.outcome = outcome
        return True

    def _collected(
        self, run: _RepoRun, selection: _Selection, setup: _RunSetup | None, *, searching: bool
    ) -> _SessionOutcome | None:
        """Run one session of *run*'s repo under the interrupt policy; ``None`` once interrupted.

        One that could not finish is logged, naming the repo and why. When it
        was *searching* for names no table places, or only collects, it also
        sets :attr:`failed`: a run session's own exit code already fails the run.
        """
        outcome, self.interrupted = _guarded_pytest_session(
            run.repo, selection, setup, home=self.home
        )
        if self._clean_error is not None:
            raise self._clean_error
        if outcome is None or self.interrupted is not None:
            return None
        if outcome.table is None:
            logger.error(
                "Could not collect the tests of repo %r (pytest exit code %d): %s",
                run.repo.name,
                outcome.rc,
                outcome.stop_reason or "pytest gave no reason",
            )
            if searching or setup is None:
                # A collection that stopped with 0 (pytest.exit(returncode=0))
                # still left the run unanswered.
                self.failed = outcome.rc or 1
        else:
            self._log_broken(run, outcome)
        return outcome

    def _before_tests(self) -> None:
        """Clean the remote coverage counters, once per run, before the first test runs.

        Called from inside the first session committed to running tests, so
        a run that ends in an unknown name, or matches nothing, never touches
        a host. A failure ends that session cleanly (``pytest.exit``, not an
        internal error) and is raised again once it has returned.
        """
        import pytest

        from ..lifecycle import run_command

        if self._cleaned:
            return
        self._cleaned = True
        try:
            run_command(_pre_run_cov_clean(self.repos, self.opts))
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — re-raised by _session
            self._clean_error = exc
            pytest.exit(f"coverage pre-clean failed: {exc}", returncode=pytest.ExitCode.USAGE_ERROR)

    def _unknown(self, unknown: list[str]) -> Exception:
        """Return the error for names no repo answers to: did-you-mean, and what broke.

        :class:`NoTestsMatchedError` when no table knows a single name and no
        file failed to collect (no test universe to suggest from).
        """
        from .selection import UnknownSelectionError, unknown_names_message

        known: set[str] = set()
        broken: list[str] = []
        for run in self.runs:
            table = run.known.table
            if table is None:
                continue
            known.update(table.names)
            for key, record in table.files.items():
                if record.error is not None:
                    broken.append(f"{_shown(run.repo, key)} did not collect: {record.error}")
        if not (known or broken):
            return NoTestsMatchedError("No tests matched the selection.")
        return UnknownSelectionError(unknown_names_message(unknown, known, broken=broken))

    def _log_broken(self, run: _RepoRun, outcome: _SessionOutcome) -> None:
        """Log each file *outcome*'s session could not collect."""
        for key, record in outcome.records.items():
            if record.error is not None:
                logger.error(
                    "Test collection failed for %s in repo %r: %s",
                    _shown(run.repo, key),
                    run.repo.name,
                    record.error,
                )


def _shown(repo: "Repo", key: str) -> Path:
    """Return *key* (an absolute path) as a user reads it: relative to *repo* when inside it."""
    path = Path(key)
    return path.relative_to(repo.sut_dir) if path.is_relative_to(repo.sut_dir) else path


def _read_known(repos: "list[Repo]", home: Path) -> list[_Known]:
    """Return what the stored tables say of each of *repos*, in order (one cache-file read)."""
    from ..config.collected_tests import classify, read_tables

    tables = read_tables(repos, home=home)
    known: list[_Known] = []
    for repo in repos:
        table = tables.get(str(repo.sut_dir))
        known.append(_Known(table=table, classification=classify(repo, table)))
    return known


def run_tests(
    names: list[str] | None = None,
    *,
    run_options: RunOptions = _DEFAULT_RUN_OPTIONS,
    options: list[object] | None = None,
    output_dir: Path | None = None,
) -> SuiteRunResult:
    """Run the named tests, and/or those a marker expression selects, in every repo.

    *names* select by :func:`otto.suite.selection.matches_name`'s rule:
    ``test_reboot`` selects every test of that name, ``TestDevice`` every
    test in a class of that name, and ``TestDevice::test_reboot`` that test
    in every such class. ``run_options.markers`` narrows the named tests, or,
    with no names, selects on its own.

    Each repo runs in one pytest session over its test directories, and
    that session both finds the names and runs what they select, in
    collection order. Before any session starts, the test-names cache
    of every repo say which files hold the names; a session then collects
    only those files and the files that changed since the table saw them
    (the whole tree when the table is cold: none yet, another pytest
    config or installation, or past its TTL). A file's record is trusted to
    say a name is NOT there only when pytest wrote it and neither the file
    nor any file its tests come from (a base class in another module or a
    library) changed since; tests generated from a data file are the one
    case no stat follows. Where a name no record holds is looked for, and
    in what order the repos run, is decided from the tables alone (the rule
    is ``_decide``'s, in this module): a name found nowhere is unknown, and
    no test runs in any repo.

    A file that fails to collect is logged and doesn't stop the tests that
    were asked for, but the run then exits 1, as pytest does. A collection
    that cannot finish (a conftest that fails to load) fails the run with
    its exit code; one that was to search its repo for a name no table
    holds ends the run before any test. Every session writes what it
    collected back to the table.

    Sessions fold into one :class:`SuiteRunResult`: ``exit_code`` is the
    worst exit code (stability-aware) of the repos with a test to run, and
    ``junit_paths`` lists their JUnit files in repo order. A repo whose
    session had nothing to run takes no part in the result. When more than
    one repo has a test directory, each repo's artifacts go under
    ``<output_dir>/<repo name>/`` and its JUnit file is ``junit_<repo>.xml``.

    *options* are instances of classes registered for the ``test`` verb
    (:func:`otto.params.register_options`); they are bound on the context, so
    tests read them with ``ctx.options(Cls)`` and the ``ensure`` marker's
    converge builds each install body's options from them. A registered class
    that is not passed is built from its defaults, as the command line would
    build it, and a required field raises.

    A repo's libraries are imported once per process: after editing one,
    start a new process to see the change.

    Raises:
        ValueError: neither *names* nor ``run_options.markers`` was given —
            with both empty a run would match every test in every repo.
        otto.params.OptionsRegistrationError: an instance in *options* is of a
            class not registered for ``test``.
        otto.suite.selection.UnknownSelectionError: a name matches no test
            in any repo although there were collected tests to search; the
            message carries did-you-mean suggestions and names each file
            that did not collect. No test has run, except when a table's
            record said a name was there and an edit its file's stat did
            not show took it away: then the error comes after the run.
        NoTestsMatchedError: no repo had a test to run — no repos, no repo
            carrying the marker, named tests the marker expression excludes,
            or no test universe to search at all. Both this and
            ``UnknownSelectionError`` subclass ``ValueError``; catch
            ``UnknownSelectionError`` first to tell the typo case apart.
        otto.registry.RegistrationRefused: a test file or conftest
            registered something while loading.
        otto.coverage.config.DestinationError: :func:`prepare_run`'s preflight found a
            ``cov_dir`` or ``cov_report_dir`` destination that is not a directory,
            cannot be written, or is not empty without ``overwrite_*``.
        otto.params.OptionsValidationError: :func:`prepare_run` found ``cov_tickets_json``
            set with no ``[coverage.tickets]`` table configured.
    """
    names = [n.strip() for n in names or [] if n.strip()]
    if not (names or run_options.markers):
        raise ValueError("run_tests needs at least one test name or run_options.markers")

    import pytest

    from ..config import get_repos
    from ..config.home import workspace_home
    from ..params import flatten_option_instances
    from .layout import ArtifactLayout

    flat = flatten_option_instances(list(options or []), verb="test")
    opts = run_options
    repos = get_repos()
    log_dir = resolve_output_dir(output_dir)
    searched = [r for r in repos if any(d.exists() for d in r.tests)]
    multi = len(searched) > 1

    with _session_context(log_dir) as session_ctx:
        # Bound first: every test (and the `ensure` converge) reads the
        # options off this context, and a bad value fails before any
        # collection or host work.
        session_ctx.bind_verb_options("test", flat)
        if not searched:
            raise NoTestsMatchedError("No tests matched the selection.")

        # Destination and tickets preflight: local checks only, so they fail
        # before the instrumentation scan. Check-only here: an overwrite
        # clear waits for the coverage decision, so a refused run never
        # empties a destination. The remote pre-clean waits for the first
        # session that is committed to running tests (_Sessions._before_tests).
        prepare_run(opts, dry_run=True)
        opts = resolve_coverage(opts, repos, command=_cov_command_label(opts, "otto test"))
        prepare_run(opts)
        session_ctx.cov_decision = bool(opts.cov)
        if opts.random_order:
            # One seed for every session of the run, said once: it is what
            # `--seed N` reproduces.
            if opts.seed is None:
                opts = dataclasses.replace(opts, seed=secrets.randbits(32))
            logger.info(
                "random test order, seed %s (reproduce with --seed %s)", opts.seed, opts.seed
            )

        # One workspace home for the table's read and write and every
        # session's bytecode and cache: each lookup resolves every SUT dir.
        home = workspace_home()
        sut_test_dirs = [p for r in repos for p in r.tests]
        runs: list[_RepoRun] = []
        for repo, known in zip(searched, _read_known(searched, home), strict=True):
            default_junit = log_dir / (f"junit_{repo.name}.xml" if multi else "junit.xml")
            if opts.results and multi:
                # A single explicit --results path would otherwise have every
                # repo's session overwrite the last one's junit output. Fan
                # out from the same stem instead: PATH -> PATH_repo.
                results_source = Path(opts.results)
                results_path = results_source.with_stem(f"{results_source.stem}_{repo.name}")
            else:
                results_path = Path(opts.results) if opts.results else default_junit
            setup = _RunSetup(
                opts=opts,
                results_path=results_path,
                # The repo layer whenever more than one repo is searched, so
                # where a run's files go never depends on the cache. Each
                # session's own repo.tests: it only collects there.
                layout=ArtifactLayout(
                    root=log_dir / repo.name if multi else log_dir, test_roots=repo.tests
                ),
                log_dir=log_dir,
                sut_test_dirs=sut_test_dirs,
            )
            runs.append(_RepoRun(repo=repo, known=known, setup=setup))
        sessions = _Sessions(repos, runs, names, opts, home)
        taking_part: list[tuple[_RepoRun, _SessionOutcome]] = []
        in_flight: BaseException | None = None
        try:
            sessions.run()
        except BaseException as exc:
            in_flight = exc
            raise
        finally:
            # The repos whose session had a test to run; pytest's "no tests
            # collected" is no match in that repo, not a failure of the run.
            taking_part = [
                (run, outcome)
                for run in runs
                if (outcome := run.outcome) is not None
                and outcome.rc != pytest.ExitCode.NO_TESTS_COLLECTED
            ]
            # The backstop. Every pytest-asyncio loop closed the hosts it
            # owned before it closed (OttoPlugin.pytest_fixture_setup), so
            # this should find nothing; anything left holds state no loop can
            # drive, and is dropped so the post-run phase reconnects it.
            session_ctx.abandon_closed_loops()
            # Tests that ran have their coverage collected even when a later
            # repo's session then raises (a refused registration); an
            # interrupt means STOP: no further repos, no post-coverage.
            if sessions.interrupted is None and taking_part:
                _post_run_coverage_beside(in_flight, repos, taking_part, log_dir, opts)
        if sessions.interrupted is not None:
            raise SystemExit(128 + sessions.interrupted)
        if not taking_part and sessions.failed is None:
            raise NoTestsMatchedError("No tests matched the selection.")

    outcomes = [outcome for _, outcome in taking_part]
    reports = [o.report for o in outcomes if o.report is not None]
    failed = [] if sessions.failed is None else [sessions.failed]
    return SuiteRunResult(
        exit_code=max([_final_exit_code(o.rc, o.unstable) for o in outcomes] + failed),
        junit_paths=[run.setup.results_path for run, _ in taking_part if run.setup is not None],
        stability_report=reports[-1] if reports else None,
        stability_unstable=any(o.unstable for o in outcomes),
        output_dir=log_dir,
    )


def _refresh_tables(repos: "list[Repo]", *, cold_only: bool = False) -> "list[RepoTable | None]":
    """Bring each of *repos*' tables in the test-names cache up to date, running nothing.

    Each repo is refreshed as a run refreshes one it must search
    (:meth:`_Sessions.refresh`): a cold table (none, another pytest config
    or installation, past its TTL) is seeded by one whole-tree
    ``--collect-only`` session, and a warm one re-reads only what moved. A
    table that is current is left as it is; with *cold_only*, so is every
    warm one. Every table written is written as a run writes it.

    Returns the tables in the order of *repos*. ``None`` is a repo whose
    table this could not bring up to date: its collection did not finish
    (logged, naming the repo and why), or a directory it set out to list is
    still to be listed (a conftest at or under it failed to load), so it
    stays a candidate for the next collection.

    Raises:
        SystemExit: ``128 + signum`` when a session was interrupted.
        otto.registry.RegistrationRefused: a test file or conftest
            registered something while loading.
    """
    from ..config.home import workspace_home

    home = workspace_home()
    runs = [_RepoRun(repo=r, known=k) for r, k in zip(repos, _read_known(repos, home), strict=True)]
    sessions = _Sessions(repos, runs, [], _COLLECTING, home)
    tables: list[RepoTable | None] = []
    for run in runs:
        before = run.known.classification
        if before.is_current or (cold_only and not before.whole_tree):
            tables.append(run.known.table)
            continue
        refreshed = sessions.refresh(run)
        if sessions.interrupted is not None:
            raise SystemExit(128 + sessions.interrupted)
        unlisted = set(before.candidate_dirs) & set(run.known.classification.candidate_dirs)
        tables.append(run.known.table if refreshed and not unlisted else None)
    return tables


@dataclasses.dataclass(frozen=True)
class Listing:
    """What :func:`selected_tests` collected: each repo's selected tests, and how it went."""

    tests: "dict[str, list[SelectedTest]]"
    """Each repo's selected tests, by repo name, in collection order; a repo no
    session collected (or whose collection did not finish) has none."""
    exit_code: int
    """``0``, or the exit code of a collection that could not finish (it was logged)."""


def selected_tests(names: list[str], *, markers: str = "") -> Listing:
    """Collect what *names* and *markers* select in every repo, as :func:`run_tests` would.

    The sessions a run would start, each ``--collect-only``: the same
    decision over the test-names cache, the same pruned or whole-tree
    collections, and every table written back. Nothing runs: pytest imports
    the test files and conftests it collects, and no test, fixture or host
    is touched. With neither names nor *markers*, each repo's whole tree is
    collected, so every file is read again.

    A repo's libraries are imported once per process: after editing one,
    start a new process to see the change.

    Raises:
        otto.suite.selection.UnknownSelectionError: a name no repo holds,
            with did-you-mean suggestions.
        NoTestsMatchedError: a name, and no collected test to suggest from.
        otto.registry.RegistrationRefused: a test file or conftest
            registered something while loading.
    """
    from ..config import get_repos
    from ..config.home import workspace_home

    names = [n.strip() for n in names if n.strip()]
    repos = get_repos()
    searched = [r for r in repos if any(d.exists() for d in r.tests)]
    home = workspace_home()
    known = _read_known(searched, home)
    runs = [_RepoRun(repo=r, known=k) for r, k in zip(searched, known, strict=True)]
    sessions = _Sessions(repos, runs, names, RunOptions(markers=markers), home)
    sessions.run()
    if sessions.interrupted is not None:
        raise SystemExit(128 + sessions.interrupted)
    return Listing(
        tests={run.repo.name: run.outcome.selected for run in runs if run.outcome is not None},
        exit_code=sessions.failed or 0,
    )
