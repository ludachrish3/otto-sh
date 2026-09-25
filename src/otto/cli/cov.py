r"""Generate coverage reports from ``otto test --cov`` output.

Merges ``.gcda`` files collected from one or more ``otto test`` runs,
processes them with ``lcov``, and renders a multi-tier HTML report.

**Usage**::

    otto cov report RUN_DIR1 [RUN_DIR2 ...] --dir ./my_report

Each *RUN_DIR* is an ``otto test`` output directory containing a ``cov/``
subdirectory with per-host ``.gcda`` files.  Multiple directories can be
specified to stitch together coverage from separate test runs.

Per-host toolchains (``gcov``, ``lcov``) are resolved automatically from
host configuration in ``lab.json`` or by inspecting ``.gcno`` files.
See the :doc:`/cli/cov/index` and :doc:`/cli/host/index` documentation.

**Options**

``--dir PATH``
    Where to place the generated coverage report (default: ``./cov_report``).

``--project-name STR``
    Title shown in the HTML report header.

``--tickets-json PATH``
    Also write a machine-readable per-ticket coverage summary (``format: 1``,
    versioned independently of the internal ``store.json``) to this path.
    Every file path in it is repo-relative posix (never an absolute,
    machine-specific path), so it diffs cleanly across checkouts. Requires
    ``[coverage.tickets]`` to have attributed at least one ticket; fails
    loud otherwise (exit 1) rather than writing an empty file.

``--tier NAME[=PATH]``
    Repeatable.  Add a coverage tier to the report.  ``NAME`` is a
    free-form label (e.g. ``unit``, ``manual``, ``integration``); ``PATH``
    is the lcov ``.info`` file feeding that tier.  The bare form
    ``--tier system`` (no path) refers to the implicit system tier
    produced by merging the supplied ``.gcda`` directories.

    The order of ``--tier`` flags is the precedence order: the first flag
    is the highest-precedence tier and wins the row coloring on the
    annotated source view.  If no ``--tier`` flags are given, defaults
    to ``--tier system``.

    Example::

        otto cov report runs/ \\
            --tier unit=u.info \\
            --tier system \\
            --tier integration=i.info \\
            --tier manual=m.info

``otto cov get`` fetches ``.gcda`` counters straight from the lab's
instrumented products (mirroring ``otto test --cov``'s collection step) and
produces a ``capture.json`` per product, anchored to ``base_commit``, in its
output directory. It is the single retrieval command for both automated
(e2e-kind tier) and manual-session (manual-kind tier) capture production::

    otto cov get --tier manual --ticket JIRA-123

**Options**

``--output PATH / -o PATH``
    Where to write fetched coverage and per-product captures (default: the
    standard per-invocation output directory under the xdir, same as every
    other lab-touching command).

``--tier NAME``
    Coverage tier to annotate onto each capture. Defaults to the lab's sole
    e2e-kind tier; ambiguous or unknown names list the configured tiers.

``--ticket STR``
    Ticket reference annotated onto every tier's captures. Required when ``--tier``
    resolves to a manual-kind tier.

``--note STR``
    Free-text note annotated onto every tier's captures.

``--tester-name STR`` / ``--tester-email STR``
    Tester identity annotated onto each capture (manual-kind tiers only).
    Default to ``getpass.getuser()`` and ``git config user.email``
    respectively; an unset email is omitted rather than annotated empty.

``--clean``
    Zero the fetched hosts' remote ``.gcda`` counters after a successful
    retrieval — for use before starting a manual session.

``otto cov clean`` zeroes each instrumented product's ``.gcda`` counters on
the lab's **fetchable** coverage hosts — the same host selection ``get``
fetches from — without first fetching anything. Useful ahead of a manual
session when the previous capture has already been retrieved::

    otto cov clean

Embedded coverage hosts are out of scope for this phase (counter reset
requires a product-side ``cov_reset`` LLEXT function mirroring
``cov_dump``, a later phase); when the lab has any, the command logs a
note and exits 0 rather than failing.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape as escape_markup

from ..coverage.errors import CoverageDataMismatchError, CoverageToolVersionError
from ..coverage.reporter import TierSpec, run_coverage_report
from ..coverage.store.model import TIER_SYSTEM
from ..host.errors import CoverageToolMissingError

if TYPE_CHECKING:
    # Type-only: never executed, so it carries no runtime import cost and
    # doesn't touch the `cov` import-budget surface (measured by `otto cov
    # --help`, which never runs get()/clean()'s bodies). Real coverage-
    # machinery imports stay function-local per the same budget.
    import re
    from typing import Any

    from ..config.repo import Repo
    from ..coverage.exclusions.rules import ExclusionRule
    from ..coverage.overrides import OverrideConfig
    from ..coverage.store.model import Thresholds
    from ..coverage.tickets import TicketSpec
    from ..coverage.tiers import TierConfig
    from ..host.remote_host import RemoteHost

    # A named alias so _resolve_cov_settings's return annotation is a single
    # string literal — ty rejects an implicitly-concatenated string type
    # expression (a bare quoted tuple this wide would need one to fit under
    # the line-length limit).
    _CovSettings = tuple[
        Path | None,
        list[TierConfig] | None,
        list[ExclusionRule],
        Thresholds | None,
        TicketSpec | None,
        OverrideConfig | None,
    ]

logger = logging.getLogger(__name__)

cov_app = typer.Typer(
    name="cov",
    no_args_is_help=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
    help="Generate coverage reports from otto test --cov output.",
)


@cov_app.callback()
def cov_callback(ctx: typer.Context) -> None:
    """Generate coverage reports and fetch/clean lab coverage counters.

    ``cov report`` is purely local — it reads coverage artifacts and writes an
    HTML report. ``cov get`` and ``cov clean`` reach the lab's coverage hosts
    (fetching or zeroing remote ``.gcda`` counters). Only ``cov get`` creates
    a per-invocation output directory (it is where its captures land by
    default); ``report`` and ``clean`` opt out via their leaf markers.
    """
    if ctx.resilient_parsing:
        return


def _parse_tier_specs(raw_tiers: list[str]) -> list[TierSpec]:
    """Parse repeated ``--tier NAME[=PATH]`` values into ordered tier specs.

    Order is preserved (= precedence order).  ``--tier system`` without a
    path is allowed and represents the implicit lcov-merged system tier.
    Any other tier without a path is rejected.
    """
    specs: list[TierSpec] = []
    seen: set[str] = set()
    for raw in raw_tiers:
        if "=" in raw:
            name, _, path_str = raw.partition("=")
            name = name.strip()
            if not name:
                raise typer.BadParameter(f"--tier value missing name: {raw!r}")
            if not path_str:
                raise typer.BadParameter(f"--tier value missing path: {raw!r}")
            path: Path | None = Path(path_str)
        else:
            name = raw.strip()
            if not name:
                raise typer.BadParameter("--tier value cannot be empty")
            if name != TIER_SYSTEM:
                raise typer.BadParameter(
                    f"Tier {name!r} requires a path: --tier {name}=PATH "
                    f"(only the {TIER_SYSTEM!r} tier may omit a path)"
                )
            path = None

        if name in seen:
            raise typer.BadParameter(f"Duplicate --tier name: {name!r}")
        seen.add(name)
        specs.append((name, path))

    return specs


def _resolve_cov_settings() -> "_CovSettings":
    """Resolve settings for ``report``.

    Returns ``(repo_root, tier_configs, exclusion_rules, thresholds,
    ticket_spec, overrides)``.

    Uses the same first-repo-with-``[coverage]`` selection as ``get`` and
    ``clean`` (via :func:`otto.config.coverage_settings.get_cov_repo`).  Returns
    ``(None, None, [], None, None, None)`` when no coverage section is
    configured — the git-less fallback that keeps ``otto cov report``
    working exactly as before on a tree with no ``[coverage]`` settings.

    ``exclusion_rules`` comes from ``[coverage.exclusions].rules`` — the
    compiled rules (:func:`otto.coverage.exclusions.rules.load_exclusion_rules`)
    applied by the reporter's filter stage, which DELETES the lines and
    branches they name from the merged store. An empty list is not
    feature-absent: the built-in ``LCOV_EXCL_*`` families always apply on top
    of whatever is configured here. Raises
    :class:`~otto.config.coverage_settings.CoverageConfigError` (a :class:`ValueError`)
    on a malformed rule — caught by ``report``'s existing ``except ValueError``
    handler, same as ``overrides`` below.

    ``thresholds`` comes from ``[coverage.report]`` — render thresholds
    forwarded to the reporter/renderer (:func:`otto.coverage.report_config.load_report_thresholds`).

    ``ticket_spec`` comes from ``[coverage.tickets]`` — the compiled
    commit-message ticket pattern (:func:`otto.coverage.tickets.load_ticket_spec`).
    ``None`` when the table is absent is the feature-absent signal: the
    reporter runs no git log walk and the report is unchanged.

    ``overrides`` comes from ``.otto/coverage-overrides.toml`` (or the path
    named by ``[coverage.overrides].file``) via
    :func:`otto.coverage.overrides.load_override_config`. ``None`` is the
    feature-absent signal: no asserted entries fold in and no
    reattribution reaches ticket attribution.  Raises
    :class:`~otto.coverage.overrides.OverrideConfigError` (a
    :class:`ValueError`) on a malformed file — caught by ``report``'s
    existing ``except ValueError`` handler.
    """
    from ..config import get_repos
    from ..config.coverage_settings import get_cov_config, get_cov_repo
    from ..coverage.exclusions.rules import load_exclusion_rules
    from ..coverage.overrides import load_override_config
    from ..coverage.report_config import load_report_thresholds
    from ..coverage.tickets import load_ticket_spec
    from ..coverage.tiers import load_tiers

    repos = get_repos()
    cov_repo = get_cov_repo(repos)
    if cov_repo is None:
        return None, None, [], None, None, None
    cov_config = get_cov_config(repos)
    exclusion_rules = load_exclusion_rules(cov_config)
    tier_cfgs = load_tiers(cov_config)
    return (
        cov_repo.sut_dir,
        tier_cfgs,
        exclusion_rules,
        load_report_thresholds(cov_config),
        load_ticket_spec(cov_config),
        load_override_config(cov_config, cov_repo.sut_dir, tier_cfgs),
    )


@cov_app.command()
def report(
    output_dirs: Annotated[
        list[Path] | None,
        typer.Argument(
            help=(
                "otto test output directories containing cov/ subdirectories. "
                "Optional: with none given the report is built from the "
                "committed manual-capture store alone."
            ),
        ),
    ] = None,
    report_dir: Annotated[
        Path,
        typer.Option(
            "--dir",
            "-d",
            help="Where to place the generated coverage report.",
        ),
    ] = Path("./cov_report"),
    project_name: Annotated[
        str,
        typer.Option(
            "--project-name",
            help="Title shown in the HTML report header.",
        ),
    ] = "Coverage Report",
    tickets_json: Annotated[
        Path | None,
        typer.Option(
            "--tickets-json",
            help=(
                "Also write a machine-readable per-ticket coverage summary to this "
                "path. Requires [coverage.tickets]."
            ),
        ),
    ] = None,
    prefix: Annotated[
        Path | None,
        typer.Option(
            "--prefix",
            help=(
                "Strip this leading directory from file paths shown in "
                "the report (display only, like genhtml --prefix). Files "
                "outside the prefix display unchanged."
            ),
        ),
    ] = None,
    tier: Annotated[
        list[str] | None,
        typer.Option(
            "--tier",
            help=(
                "Add a coverage tier as NAME[=PATH]. Repeatable. "
                "Order is precedence order (first = highest). "
                'Use "--tier system" alone to position the implicit '
                "lcov-merged system tier. When given, --tier flags take "
                "precedence over settings tiers and select the git-less "
                'legacy path. Defaults to the configured tiers (or "system").'
            ),
        ),
    ] = None,
) -> None:
    """Generate a coverage report from otto test --cov output directories."""
    output_dirs = output_dirs or []
    # Validate output directories
    for d in output_dirs:
        if not d.is_dir():
            logger.error("Output directory does not exist: %s", d)
            raise typer.Exit(1)

    # Precedence rule: explicit --tier flags are a git-less escape hatch and
    # take precedence over settings tiers — route them through the legacy
    # path unchanged (no repo_root / tier_configs resolution, exactly as
    # before). With no --tier flags, resolve the collection-model inputs
    # (repo_root + declared tiers) from settings below, inside the try
    # block; a tree with no [coverage] section falls back to (None, None),
    # i.e. the legacy behavior.
    repo_root: Path | None = None
    tier_configs: "list[TierConfig] | None" = None
    exclusion_rules: "list[ExclusionRule]" = []
    thresholds: "Thresholds | None" = None
    ticket_spec: "TicketSpec | None" = None
    overrides: "OverrideConfig | None" = None
    if tier:
        try:
            tier_specs: list[TierSpec] = _parse_tier_specs(tier)
        except typer.BadParameter as e:
            # A --tier usage error (missing path, duplicate name): the message
            # already names the offending value and the fix — print it clean,
            # like every other user-facing error path in this command.
            logger.error(escape_markup(str(e)))  # noqa: TRY400 — deliberately no traceback: user typo, message names the fix
            raise typer.Exit(1) from e
        # --tier never resolves settings (see precedence rule above), so
        # thresholds/ticket_spec/overrides stay None here — run_coverage_report
        # defaults to Thresholds()'s 80.0/70.0 and runs no ticket
        # attribution (it has no git repo_root to walk).
    else:
        tier_specs = [(TIER_SYSTEM, None)]

    cov_dirs = [d / "cov" for d in output_dirs]
    report_dir = report_dir.resolve()

    from ..coverage.capture.gitio import GitUnavailableError, NotAGitRepoError
    from ..lifecycle import run_command

    try:
        if not tier:
            # Resolved inside the try (not above, alongside tier_specs): a
            # malformed override file raises OverrideConfigError (a
            # ValueError) from load_override_config, and must hit the same
            # clean-message `except ValueError` handler below as every
            # other settings/data ValueError, not propagate as a traceback.
            repo_root, tier_configs, exclusion_rules, thresholds, ticket_spec, overrides = (
                _resolve_cov_settings()
            )
        store = run_command(
            run_coverage_report(
                cov_dirs,
                report_dir,
                project_name=project_name,
                tier_specs=tier_specs,
                repo_root=repo_root,
                tier_configs=tier_configs,
                exclusion_rules=exclusion_rules,
                thresholds=thresholds,
                ticket_spec=ticket_spec,
                overrides=overrides,
                prefix=prefix,
            )
        )
    except (CoverageDataMismatchError, CoverageToolVersionError, CoverageToolMissingError) as e:
        # Typed capture errors — polluted tree (product rebuilt after the
        # test run), a gcov tool that cannot read the build's format (e.g.
        # clang build captured with GNU gcov), or a gcov the data's own
        # stamp names that is not installed: the message already names the
        # cause and remedy — print it clean, never as a traceback. Escaped:
        # the console handler renders log messages as Rich markup, and this
        # message is arbitrary (lcov/geninfo) text that may itself contain a
        # literal bracket.
        logger.error(escape_markup(str(e)))  # noqa: TRY400 — deliberately no traceback: user-facing cause + remedy
        raise typer.Exit(1) from e
    except NotAGitRepoError as e:
        # A [coverage] section resolved a repo_root that is not a git repo:
        # the base_commit-anchored capture features can't run. This hardcoded
        # message used to also cover a missing git and an ordinary git
        # failure, and told both the wrong story; the type now says which it
        # is, so the wording is finally true by construction.
        logger.error(  # noqa: TRY400 — deliberately no traceback: user-facing cause + remedy
            "not a git repository — base_commit-anchored capture features unavailable; "
            "use --tier NAME=PATH"
        )
        raise typer.Exit(1) from e
    except GitUnavailableError as e:
        # git missing, or a git command that failed inside a real repo: echo
        # what git said rather than mislabelling it as "not a git repository".
        logger.error(escape_markup(str(e)))  # noqa: TRY400 — deliberately no traceback: git's own message is the cause
        raise typer.Exit(1) from e
    except ValueError as e:
        # A malformed committed manual capture (load_manual_captures wraps the
        # parse error with the offending file name), or the no-[coverage]-
        # config / no-.gcda ValueErrors raised by collect_coverage. Print the
        # cause clean, escaped for the Rich-markup console handler (these
        # messages may contain a literal bracket, e.g. "[coverage]").
        logger.error(escape_markup(str(e)))  # noqa: TRY400 — deliberately no traceback: user-facing cause (names the bad file)
        raise typer.Exit(1) from e
    except RuntimeError as e:
        logger.error("Coverage merge failed: %s", escape_markup(str(e)))  # noqa: TRY400 — deliberately no traceback: lcov output is the diagnostic
        raise typer.Exit(1) from e
    if store is None:
        # run_coverage_report logged the specific warning (missing meta or
        # no host dirs); for the standalone command treat that as an error.
        # store is None only on the legacy path — the collection-model path
        # always returns a store (manual store / declared tiers still yield
        # a report even with no output dirs).
        where = ", ".join(str(d) for d in output_dirs) if output_dirs else "the given inputs"
        logger.error("Coverage report not generated — no valid coverage data in: %s", where)
        raise typer.Exit(1)

    if store.file_count() == 0:
        # A store with no files is a vacuous success — restore the old loud
        # CI-friendly fail. Name every input searched (run cov dirs plus, when
        # a [coverage] repo resolved, its committed manual-capture store).
        searched = [str(d) for d in cov_dirs]
        if repo_root is not None:
            searched.append(str(repo_root / ".otto" / "coverage" / "manual"))
        where = ", ".join(searched) if searched else "the given inputs"
        logger.error("no coverage data found in: %s", where)
        raise typer.Exit(1)

    logger.info(
        "Coverage: %.1f%% overall (%d files)",
        store.overall_pct(),
        store.file_count(),
    )
    logger.info("Report: %s", report_dir / "index.html")

    if tickets_json is not None:
        # Lazy: otto.version and otto.coverage.ticket_export are cheap, but
        # kept function-local to match every other coverage import in this
        # command (see the `report`/`get` docstrings' import-budget notes).
        from ..coverage.ticket_export import make_generated_stamp, write_ticket_export
        from ..version import get_version

        if repo_root is None:
            # The --tier legacy path and the no-[coverage]-section fallback
            # both leave repo_root unset here — and both also leave
            # ticket_spec unset, so no attribution ever ran and
            # store.tickets is always empty in this branch too. Same
            # message as the empty-store ValueError below, raised directly
            # rather than handing a None repo_root to write_ticket_export
            # (which requires one to emit repo-relative paths).
            logger.error(
                "no ticket data in this report — [coverage.tickets] must be configured "
                "for --tickets-json"
            )
            raise typer.Exit(1)

        try:
            write_ticket_export(
                store,
                tickets_json,
                repo_root=repo_root,
                project=project_name,
                otto_version=get_version(),
                generated=make_generated_stamp(),
            )
        except ValueError as e:
            # --tickets-json was explicitly requested — an empty/absent
            # ticket table is a loud, CI-visible failure here (unlike
            # `otto test`'s never-fail-a-successful-run policy for its
            # optional post-run tail).
            logger.error(escape_markup(str(e)))  # noqa: TRY400 — deliberately no traceback: clean cause line
            raise typer.Exit(1) from e
        logger.info("Ticket export: %s", tickets_json)


# `report` is purely local and must never create a per-invocation output dir
# (reporting on yesterday's run leaves no trace of its own — e2e captures are
# anchored to base_commit). The
# leaf-invoke preamble reads this marker; `get` (which produces artifacts)
# keeps the group's standard output-dir handling.
report.__cli_output_dir__ = False  # ty: ignore[unresolved-attribute]


# ---------------------------------------------------------------------------
# get — single retrieval command (fetch + produce_captures)
# ---------------------------------------------------------------------------


class _CovError(Exception):
    """Base for clean, single-line-message ``otto cov`` command failures.

    Raised directly by :func:`_connect_cov_hosts` for the one failure mode
    shared by every command that discovers coverage hosts (no ``[coverage]``
    section configured); command-specific failures raise a subclass
    (:class:`_GetError`, :class:`_CleanError`). Each command's sync wrapper
    catches this base type and prints ``str(e)`` without a traceback,
    mirroring ``report``'s ``CoverageDataMismatchError`` handling.
    """


class _GetError(_CovError):
    """Internal signal for a clean, single-line ``cov get`` failure.

    Raised by :func:`_do_get` for every ``get``-specific failure mode; the
    sync ``get`` command catches the shared :class:`_CovError` base (which
    also covers :func:`_connect_cov_hosts`'s "no config" failure).
    """


def _resolve_tester(name: str | None, email: str | None, sut_dir: Path) -> dict[str, str]:
    """Resolve tester identity for a manual capture (spec decision 15).

    ``name`` defaults to :func:`getpass.getuser`; ``email`` defaults to
    ``git config user.email`` read *in the SUT repo* (so the identity comes
    from the repo being tested, not whatever repo the process CWD happens to
    be in) and is omitted entirely (not annotated empty) when the key is
    unset or ``git config`` itself fails. CLI-supplied values always win over
    both defaults.

    Note ``config_value`` runs WITHOUT ``--local``, so a *sut_dir* that is not
    a repo still reads ``~/.gitconfig`` and answers rc 0 — the
    NotAGitRepoError arm below is defensive, not a path a non-repo takes.
    Reading the ambient identity there is the intended behaviour (it is the
    human running the capture), so this does not want narrowing.

    A MISSING git propagates instead: "otto cannot run git" is an
    environment error, not evidence that the tester has no email, and
    swallowing it here is what let it be reported as the latter. In the
    ``cov get`` flow it is unreachable anyway — the ``head_commit`` preflight
    has already proven git runs — so the top-level CLI handler is the right
    place for the case where it is not.
    """
    import getpass

    from ..coverage.capture.gitio import GitCommandFailedError, NotAGitRepoError, config_value

    resolved_name = name or getpass.getuser()
    resolved_email = email
    if not resolved_email:
        try:
            resolved_email = config_value(sut_dir, "user.email")
        except (NotAGitRepoError, GitCommandFailedError):
            resolved_email = None

    tester: dict[str, str] = {"name": resolved_name}
    if resolved_email:
        tester["email"] = resolved_email
    return tester


def _capture_annotations(
    kind: str,
    ticket: str | None,
    note: str | None,
    tester_name: str | None,
    tester_email: str | None,
    sut_dir: Path,
) -> tuple[dict[str, str] | None, str | None, str | None]:
    """Resolve the (tester, ticket, note) annotations for a capture run.

    Ticket and note annotate every tier kind (run-contexts spec §4);
    tester attribution stays manual-only — an automated run has no human
    session to attribute. *sut_dir* scopes the tester's git-identity default.
    """
    tester = _resolve_tester(tester_name, tester_email, sut_dir) if kind == "manual" else None
    return tester, ticket, note


async def _connect_cov_hosts() -> tuple[
    "list[Repo]",
    "Repo",
    "dict[str, Any]",
    "re.Pattern[str] | None",
    "list[RemoteHost]",
    "list[RemoteHost]",
]:
    """Bootstrap, locate ``[coverage]`` config, and discover matching lab hosts.

    Shared setup for both ``get``'s fetch flow and ``clean``: loads the
    active lab's repos (:func:`~otto.config.get_repos`), locates the
    repo with a ``[coverage]`` section, compiles its ``hosts`` pattern, and
    enumerates every lab host that pattern matches — mirroring
    :func:`otto.coverage.collect.collect_coverage`'s fetch stage. Deliberately stops
    short of constructing a
    :class:`~otto.coverage.fetcher.remote.GcdaFetcher`: ``get`` and
    ``clean`` disagree on both the fetcher's staging root (a real output
    dir vs. an unused placeholder) and its ``pattern`` scope (``get``
    fetches with no pattern, preserving its existing tested behavior;
    ``clean`` scopes to the already-computed ``fetch_hosts`` list, not the
    raw ``[coverage].hosts`` pattern, so it can never re-match an embedded
    host), so each command builds its own fetcher from the pieces returned
    here.

    Container hosts are included in the walk: a product can live in a
    container, and the fetch reaches it through its Unix parent.

    Raises :class:`_CovError` when no ``[coverage]`` section is configured
    at all — the one failure mode every caller treats identically.

    Returns:
        ``(repos, cov_repo, cov_config, cov_pattern, cov_hosts, fetch_hosts)``,
        where ``fetch_hosts`` are the matched hosts with a filesystem to fetch
        over the network — every host but the runner itself and the embedded
        boards, which dump over their console instead.
    """
    from ..config import all_hosts, get_repos
    from ..config.coverage_settings import (
        CoverageConfigError,
        get_cov_config,
        get_cov_repo,
        load_hosts_pattern,
    )
    from ..config.scope import EmptySelectionError
    from ..host.embedded_host import EmbeddedHost
    from ..host.local_host import LocalHost

    repos = get_repos()
    cov_config = get_cov_config(repos)
    cov_repo = get_cov_repo(repos)
    if not cov_config or cov_repo is None:
        raise _CovError("No [coverage] section found in .otto/settings.toml")

    # Same repo-declared selector collect_coverage uses to keep infrastructure
    # hosts (e.g. an SSH hop) out of the coverage set; a malformed value is
    # refused by name in the shared loader, re-framed as this file's error the
    # same way EmptySelectionError is below.
    try:
        cov_pattern = load_hosts_pattern(cov_config)
    except CoverageConfigError as e:
        raise _CovError(str(e)) from e

    # Re-framed as a `_CovError`, which is what every caller's sync wrapper
    # already prints without a traceback. The message is passed through
    # verbatim — it explains fullmatch semantics and what to type instead, and
    # this site knows nothing the reader needs that it does not. Wrapping the
    # `list(...)`, not the call: `all_hosts` is a generator, so the refusal
    # arrives at the first `next()`.
    try:
        cov_hosts = list(all_hosts(pattern=cov_pattern, include_containers=True))
    except EmptySelectionError as e:
        raise _CovError(str(e)) from e
    fetch_hosts = [h for h in cov_hosts if not isinstance(h, (LocalHost, EmbeddedHost))]

    return repos, cov_repo, cov_config, cov_pattern, cov_hosts, fetch_hosts


def _unix_only_pattern(fetch_hosts: "list[RemoteHost]") -> "re.Pattern[str]":
    """Anchored regex matching exactly the given fetchable hosts' ids.

    :meth:`~otto.coverage.fetcher.remote.GcdaFetcher.clean_remote` re-derives
    its own host set from its ``pattern`` via ``do_for_all_hosts()`` /
    ``all_hosts()`` — a path with **no** ``EmbeddedHost`` guard. Passing the
    raw ``[coverage].hosts`` pattern would therefore let ``clean_remote`` send
    an embedded board a bogus ``find ... -delete`` on a mixed lab. Scoping to
    the already-computed fetchable hosts closes that. Matching is
    ``pattern.fullmatch(host.id)`` (see :meth:`OttoContext.all_hosts`); the
    ``^``/``$`` anchors are therefore redundant and kept only because they say
    out loud that a host id like ``"zephyr37-fat"`` must not also select a sibling
    ``"zephyr37-fat2"``.
    """
    import re

    host_ids = "|".join(re.escape(h.id) for h in fetch_hosts)
    return re.compile(f"^(?:{host_ids})$")


async def _do_get(
    output_dir: Path | None,
    tier_name: str | None,
    ticket: str | None,
    note: str | None,
    tester_name: str | None,
    tester_email: str | None,
    clean: bool,
) -> list[Path]:
    """Fetch coverage from the lab and produce per-board captures.

    Owns the ``get``-specific validation (tier resolution, the manual-tier
    ``--ticket`` guard, the git preflight, and output-dir resolution) and then
    delegates the whole fetch → metadata → capture pipeline to the single
    canonical :func:`otto.coverage.collect.collect_coverage` (with
    ``clean_after_fetch=False`` — ``get`` owns its own scoped post-fetch clean
    via ``--clean``). The already-resolved ``TierConfig`` is passed straight
    through (not just its name), so ``collect_coverage`` never re-resolves it
    — one ``resolve_get_tier`` call for the whole invocation, done here.
    Manual-kind tiers additionally copy each produced capture into the repo's
    committed manual-capture store (``.otto/coverage/manual/``).

    Every failure mode raises :class:`_GetError` (or, via
    :func:`_connect_cov_hosts`, the shared :class:`_CovError`) with a
    single-line, user-facing message; the sync ``get`` command is the only
    place that turns either into ``typer.Exit(1)``.
    """
    from ..context import get_context
    from ..coverage.capture.gitio import GitUnavailableError, head_commit
    from ..coverage.capture.model import Capture
    from ..coverage.capture.store_dir import write_manual_capture
    from ..coverage.collect import collect_coverage
    from ..coverage.errors import CoverageDataMismatchError, CoverageToolVersionError
    from ..coverage.fetcher.remote import GcdaFetcher
    from ..coverage.tiers import load_tiers, resolve_get_tier
    from ..host.errors import CoverageToolMissingError

    # collect_coverage re-derives the host set (cov_pattern/cov_hosts) itself, so
    # _do_get only needs cov_config (tier resolution), cov_repo (git preflight +
    # manual store), cov_hosts (display names + the instrumentation scan), and
    # fetch_hosts (the scoped --clean). The one thing _connect_cov_hosts owns
    # that collect_coverage does not is the no-[coverage]-config _CovError,
    # raised before any fetch — the message the "no config" get/clean tests
    # assert, and the reason it stays ahead of the scan below.
    (
        repos,
        cov_repo,
        cov_config,
        _cov_pattern,
        cov_hosts,
        fetch_hosts,
    ) = await _connect_cov_hosts()

    tiers = load_tiers(cov_config)
    try:
        resolved_tier = resolve_get_tier(tiers, tier_name)
    except ValueError as e:
        raise _GetError(str(e)) from e

    if resolved_tier.kind == "manual" and not ticket:
        raise _GetError(f"tier {resolved_tier.name!r} is a manual-kind tier; requires --ticket")

    # `otto cov get` is retrieval on purpose — the forced-on mode of the same
    # decision `otto test --cov` takes. Detection is local (each product reads
    # its own artifact), so a lab with nothing instrumented is refused here,
    # with the per-product verdicts, before a single host is touched. Imported
    # inside the command body: the instrumentation module pulls rich.table, and
    # this module sits on an import-budget surface.
    from ..coverage.errors import CoverageNotInstrumentedError
    from ..coverage.instrumentation import decide_coverage, detect

    try:
        decide_coverage(True, detect(cov_hosts), has_cov_config=True, command="otto cov get")
    except CoverageNotInstrumentedError as e:
        # The verdicts go to the console as the rounded table, and _GetError
        # then carries only the headline: the rest of `str(e)` is the SAME
        # verdicts in plain text, which belongs in the run log, not printed a
        # second time under the table.
        from .invoke import render_instrumentation_refusal

        raise _GetError(render_instrumentation_refusal(e)) from e

    # Git preflight: capture production anchors to HEAD (base_commit), so a
    # non-git sut can never yield a capture. Fail fast here — before the fleet pull — rather
    # than wasting a fetch and only discovering it in produce_captures. The
    # message is identical to the post-fetch GitUnavailableError path below.
    try:
        head_commit(cov_repo.sut_dir)
    except GitUnavailableError as e:
        raise _GetError(str(e)) from e

    # Resolve the destination only now — after validation — so config/tier
    # errors surface first. The CLI preamble records the standard
    # per-invocation output dir on the context; --output overrides it; a
    # bare programmatic call has neither and must say so.
    if output_dir is None:
        output_dir = get_context().output_dir
        if output_dir is None:
            raise _GetError("no output directory available: pass --output/-o")
    output_dir = output_dir.resolve()

    cov_dir = output_dir / "cov"

    tester, produce_ticket, produce_note = _capture_annotations(
        resolved_tier.kind, ticket, note, tester_name, tester_email, cov_repo.sut_dir
    )

    # One canonical collection call (fetch → metadata sidecar → per-board
    # capture). clean_after_fetch=False: `get` owns its own post-fetch clean
    # below, scoped to the Unix host ids so a mixed lab's embedded board is never
    # zeroed — so collect_coverage must not fire its own unscoped clean. The
    # raised family maps to _GetError preserving today's exact message shapes:
    # collect_coverage raises NoCoverageDataError (a ValueError) for the "no
    # .gcda counters retrieved from any host (searched: ...)" fail-loud;
    # GitUnavailableError and the typed capture errors are RuntimeError
    # subclasses and must be caught before the bare-RuntimeError "Coverage
    # merge failed" arm.
    try:
        result = await collect_coverage(
            cov_dir,
            repos=repos,
            tier=resolved_tier,
            ticket=produce_ticket,
            note=produce_note,
            tester=tester,
            display_names={h.id: h.name for h in cov_hosts},
            clean_after_fetch=False,
        )
    except GitUnavailableError as e:
        raise _GetError(str(e)) from e
    except (CoverageDataMismatchError, CoverageToolVersionError, CoverageToolMissingError) as e:
        raise _GetError(str(e)) from e
    except ValueError as e:
        raise _GetError(str(e)) from e
    except RuntimeError as e:
        raise _GetError(f"Coverage merge failed: {e}") from e

    written = result.captures_written
    if not written:
        # collect_coverage fetched .gcda from some product but produce_captures
        # made no capture. The test-run tail swallows this; a retrieval command
        # must not. The message names every host:product it searched.
        searched = ", ".join(f"{h}:{p}" for h, p in sorted(result.product_dirs))
        where = f"searched: {searched}" if searched else "no products produced captures"
        raise _GetError(f"no .gcda counters retrieved from any product ({where})")

    if resolved_tier.kind == "manual":
        for capture_path in written:
            capture = Capture.load(capture_path)
            write_manual_capture(capture, cov_repo.sut_dir)

    # `--clean` (post-retrieval remote zero, for the start of a manual session).
    # collect_coverage skipped its internal clean, so `get` does it here — but
    # clean_remote() re-derives its own host set from the fetcher's pattern with
    # no EmbeddedHost guard, so scope a second fetcher to just the fetchable
    # host ids that actually contributed a product. Guard on those ids (not the
    # raw [coverage].hosts) so a mixed lab's embedded board can never be zeroed.
    fetched_ids = {host_id for (host_id, _p) in result.product_dirs}
    fetched_fetch_hosts = [h for h in fetch_hosts if h.id in fetched_ids]
    if clean and fetched_fetch_hosts:
        clean_fetcher = GcdaFetcher(cov_dir, pattern=_unix_only_pattern(fetched_fetch_hosts))
        await clean_fetcher.clean_remote()

    logger.info("Coverage captured: %d product(s) -> %s", len(written), cov_dir)
    return written


@cov_app.command()
def get(
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help=(
                "Directory to write fetched coverage and per-board captures into. "
                "Defaults to the command's standard per-invocation output directory."
            ),
        ),
    ] = None,
    tier: Annotated[
        str | None,
        typer.Option(
            "--tier",
            help="Coverage tier to annotate onto each capture. Defaults to the sole e2e-kind tier.",
        ),
    ] = None,
    ticket: Annotated[
        str | None,
        typer.Option(
            "--ticket",
            help="Ticket reference to annotate onto each capture. Required for manual-kind tiers.",
        ),
    ] = None,
    note: Annotated[
        str | None,
        typer.Option("--note", help="Free-text note to annotate onto each capture."),
    ] = None,
    tester_name: Annotated[
        str | None,
        typer.Option(
            "--tester-name",
            help="Tester name to annotate onto each capture. Defaults to the current user.",
        ),
    ] = None,
    tester_email: Annotated[
        str | None,
        typer.Option(
            "--tester-email",
            help="Tester email to annotate onto each capture. Defaults to `git config user.email`.",
        ),
    ] = None,
    clean: Annotated[
        bool,
        typer.Option(
            "--clean",
            help=(
                "Zero the fetched hosts' remote .gcda counters after a successful "
                "retrieval — for use before starting a manual session."
            ),
        ),
    ] = False,
) -> None:
    """Fetch .gcda from the lab's instrumented products; produce per-product captures."""
    from ..lifecycle import run_command

    try:
        run_command(
            _do_get(
                output_dir,
                tier,
                ticket,
                note,
                tester_name,
                tester_email,
                clean,
            )
        )
    except _CovError as e:
        # Escaped: this is the direct log-emit site for the no-[coverage]-
        # config / no-.gcda _CovError family (a literal bracket must survive
        # the Rich-markup console handler).
        logger.error(escape_markup(str(e)))  # noqa: TRY400 — deliberately no traceback: clean cause line
        raise typer.Exit(1) from e


# ---------------------------------------------------------------------------
# clean — zero remote .gcda counters (no fetch)
# ---------------------------------------------------------------------------


class _CleanError(_CovError):
    """Internal signal for a clean, single-line ``cov clean`` failure.

    Raised by :func:`_do_clean` for every ``clean``-specific failure mode
    (no matching fetchable hosts); the sync ``clean`` command catches the
    shared :class:`_CovError` base (which also covers
    :func:`_connect_cov_hosts`'s "no config" failure).
    """


async def _do_clean() -> None:
    """Zero remote ``.gcda`` counters on the lab's fetchable coverage hosts.

    Uses :func:`_connect_cov_hosts` for the identical host discovery
    ``get`` uses (same ``[coverage].hosts`` pattern, same fetchable/embedded
    split), then hands the matched hosts to the existing
    :meth:`~otto.coverage.fetcher.remote.GcdaFetcher.clean_remote`, which
    walks each host's instrumented products and deletes the counters under
    each product's own ``cov_dir``. That method already logs one line per
    host (success or failure) via its own module logger, so no extra
    per-host logging is added here — only a completion summary.

    Embedded coverage hosts are out of scope for this phase (counter reset
    needs a product-side ``cov_reset`` LLEXT function mirroring
    ``cov_dump``): when the matched hosts include any, this logs a note but
    does not fail. A lab with *only* embedded coverage hosts (no Unix hosts
    matched) is likewise not an error — there is simply nothing this phase
    can clean yet.

    Every failure mode raises :class:`_CleanError`; the sync ``clean``
    command is the only place that turns the shared :class:`_CovError` base
    into ``typer.Exit(1)``.
    """
    from ..coverage.fetcher.remote import GcdaFetcher
    from ..host.embedded_host import EmbeddedHost

    (
        _repos,
        _cov_repo,
        _cov_config,
        _cov_pattern,
        cov_hosts,
        fetch_hosts,
    ) = await _connect_cov_hosts()

    has_embedded = any(isinstance(h, EmbeddedHost) for h in cov_hosts)

    if not fetch_hosts:
        if has_embedded:
            logger.info(
                "embedded boards not cleaned (requires product-side counter reset — later phase)"
            )
            return
        # Not "nothing matched": the runner itself can match [coverage].hosts
        # and is then dropped as unfetchable, so say which families were
        # excluded rather than sending the reader back to the selector.
        raise _CleanError(
            "No fetchable coverage host matched [coverage].hosts — nothing to clean "
            "(the otto runner itself and embedded boards are excluded: neither has "
            "remote counters this command can zero)"
        )

    for host in fetch_hosts:
        rebuild = getattr(host, "rebuild_connections", None)
        if rebuild is not None:
            rebuild()
    # staging_root is unused by clean_remote() (no files are downloaded); the
    # scoped pattern keeps clean_remote()'s own host re-derivation off embedded
    # boards on a mixed lab (see _unix_only_pattern).
    fetcher = GcdaFetcher(Path("/tmp"), pattern=_unix_only_pattern(fetch_hosts))  # noqa: S108 — deliberate staging path, never written to
    await fetcher.clean_remote()
    logger.info("Coverage counters cleared on %d host(s)", len(fetch_hosts))

    if has_embedded:
        logger.info(
            "embedded boards not cleaned (requires product-side counter reset — later phase)"
        )


@cov_app.command()
def clean() -> None:
    """Zero each instrumented product's .gcda counters on the lab's fetchable hosts."""
    from ..lifecycle import run_command

    try:
        run_command(_do_clean())
    except _CovError as e:
        # Escaped: same log-emit site as `get`'s _CovError handler above —
        # a literal bracket (e.g. "[coverage]") must survive the Rich-markup
        # console handler.
        logger.error(escape_markup(str(e)))  # noqa: TRY400 — deliberately no traceback: clean cause line
        raise typer.Exit(1) from e


# `clean` zeroes remote counters and writes nothing locally — no output dir.
clean.__cli_output_dir__ = False  # ty: ignore[unresolved-attribute]


# ---------------------------------------------------------------------------
# otto cov kgcov — vendor the otto_kgcov library and check a vendored copy
# ---------------------------------------------------------------------------

kgcov_app = typer.Typer(
    name="kgcov",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Vendor the otto_kgcov kernel-module library into a repo, or check a vendored copy.",
)


@kgcov_app.callback()
def kgcov_callback(ctx: typer.Context) -> None:
    """Vendor the otto_kgcov library (`export`) or compare a vendored copy with this otto (`check`).

    The library is a kernel module a user's own build system builds against
    each of their kernels; otto never builds it. `export` writes the sources
    this otto ships, overwriting (the copy is committed, so the repo's own
    diff is the review); `check` compares byte for byte, ignoring the
    `kgcov_local.h` override, and exits 0 current / 1 differs / 2 absent.
    """
    if ctx.resilient_parsing:
        return


@kgcov_app.command("export")
def kgcov_export(
    directory: Annotated[
        Path, typer.Argument(help="Where the vendored copy lives (created if absent).")
    ],
) -> None:
    """Write the otto_kgcov sources this otto ships into DIRECTORY."""
    from ..kgcov import export_tree

    result = export_tree(directory)
    if result.changed:
        typer.echo(
            f"{result.directory}: {len(result.changed)} file(s) written "
            f"(otto {result.version}): {', '.join(result.changed)}"
        )
    else:
        typer.echo(f"{result.directory}: already current (otto {result.version})")


kgcov_export.__cli_output_dir__ = False  # ty: ignore[unresolved-attribute]
kgcov_export.__cli_lab_free__ = True  # ty: ignore[unresolved-attribute]


@kgcov_app.command("check")
def kgcov_check(
    directory: Annotated[Path, typer.Argument(help="The vendored copy to compare.")],
) -> None:
    """Compare DIRECTORY with the library this otto ships; exit 0 current, 1 differs, 2 absent."""
    from ..kgcov import check_tree

    result = check_tree(directory)
    if result.state == "absent":
        typer.echo(f"{result.directory}: no otto_kgcov there (no kgcov.h)")
    elif result.state == "current":
        origin = f", exported by otto {result.exported_by}" if result.exported_by else ""
        override = "; kgcov_local.h present" if result.local_override else ""
        typer.echo(f"{result.directory}: current{origin}{override}")
    else:
        origin = f" (exported by otto {result.exported_by})" if result.exported_by else ""
        parts = []
        if result.differing:
            parts.append(f"differs: {', '.join(result.differing)}")
        if result.missing:
            parts.append(f"missing: {', '.join(result.missing)}")
        typer.echo(
            f"{result.directory}: not this otto's library{origin} — {'; '.join(parts)}. "
            f"Re-export with `otto cov kgcov export {result.directory}` and review the diff."
        )
    if result.exit_code:
        raise typer.Exit(result.exit_code)


kgcov_check.__cli_output_dir__ = False  # ty: ignore[unresolved-attribute]
kgcov_check.__cli_lab_free__ = True  # ty: ignore[unresolved-attribute]

cov_app.add_typer(kgcov_app, name="kgcov")
