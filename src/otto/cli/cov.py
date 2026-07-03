r"""Generate coverage reports from ``otto test --cov`` output.

Merges ``.gcda`` files collected from one or more ``otto test`` runs,
processes them with ``lcov``, and renders a multi-tier HTML report.

**Usage**::

    otto cov report RUN_DIR1 [RUN_DIR2 ...] --report ./my_report

Each *RUN_DIR* is an ``otto test`` output directory containing a ``cov/``
subdirectory with per-host ``.gcda`` files.  Multiple directories can be
specified to stitch together coverage from separate test runs.

Per-host toolchains (``gcov``, ``lcov``) are resolved automatically from
host configuration in ``hosts.json`` or by inspecting ``.gcno`` files.
See the :doc:`/guide/coverage` and :doc:`/guide/host/index` documentation.

**Options**

``--report PATH``
    Where to place the generated HTML report (default: ``./cov_report``).

``--project-name STR``
    Title shown in the HTML report header.

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

``otto cov get`` fetches ``.gcda`` counters straight from the lab (mirroring
``otto test --cov``'s collection step) and produces a pinned
``capture.json`` per board in its output directory. It is the single
retrieval command for both automated (e2e-kind tier) and manual-session
(manual-kind tier) capture production::

    otto cov get --tier manual --ticket JIRA-123

**Options**

``--output PATH / -o PATH``
    Where to write fetched coverage and per-board captures (default: the
    standard per-invocation output directory under the xdir, same as every
    other lab-touching command).

``--tier NAME``
    Coverage tier to stamp onto each capture. Defaults to the lab's sole
    e2e-kind tier; ambiguous or unknown names list the configured tiers.

``--ticket STR``
    Ticket reference stamped onto each capture. Required when ``--tier``
    resolves to a manual-kind tier.

``--note STR``
    Free-text note stamped onto each capture (manual-kind tiers only).

``--tester-name STR`` / ``--tester-email STR``
    Tester identity stamped onto each capture (manual-kind tiers only).
    Default to ``getpass.getuser()`` and ``git config user.email``
    respectively; an unset email is omitted rather than stamped empty.

``--clean``
    Zero the fetched Unix hosts' remote ``.gcda`` counters after a
    successful retrieval — for use before starting a manual session.

``otto cov clean`` zeroes ``.gcda`` counters on the lab's **Unix** coverage
hosts — the same host selection ``get`` fetches from — without first
fetching anything. Useful ahead of a manual session when the previous
capture has already been retrieved::

    otto cov clean

Embedded coverage hosts are out of scope for this phase (counter reset
requires a product-side ``cov_reset`` LLEXT function mirroring
``cov_dump``, a later phase); when the lab has any, the command logs a
note and exits 0 rather than failing.
"""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from ..coverage.errors import CoverageDataMismatchError, CoverageToolVersionError
from ..coverage.reporter import TierSpec, run_coverage_report
from ..coverage.store.model import TIER_SYSTEM
from ..logger import get_logger

if TYPE_CHECKING:
    # Type-only: never executed, so it carries no runtime import cost and
    # doesn't touch the `cov` import-budget surface (measured by `otto cov
    # --help`, which never runs get()/clean()'s bodies). Real coverage-
    # machinery imports stay function-local per the same budget.
    import re
    from typing import Any

    from ..configmodule.repo import Repo
    from ..coverage.tiers import TierConfig
    from ..host.remote_host import RemoteHost
    from ..host.unix_host import UnixHost

logger = get_logger()

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


def _resolve_cov_settings() -> "tuple[Path | None, list[TierConfig] | None, list[str]]":
    """Resolve ``(repo_root, tier_configs, extra_markers)`` from settings for ``report``.

    Uses the same first-repo-with-``[coverage]`` selection as ``get`` and
    ``clean`` (via :func:`otto.cli.test._get_cov_repo`).  Returns
    ``(None, None, [])`` when no coverage section is configured — the
    git-less fallback that keeps ``otto cov report`` working exactly as
    before on a tree with no ``[coverage]`` settings.

    ``extra_markers`` comes from ``[coverage.exclusions].markers`` — extra
    exclusion-marker strings (spec §8) forwarded to the renderer's per-file
    source scan alongside the built-in ``LCOV_EXCL_*`` markers.
    """
    from ..configmodule import get_repos
    from ..coverage.tiers import load_tiers
    from .test import _get_cov_config, _get_cov_repo

    repos = get_repos()
    cov_repo = _get_cov_repo(repos)
    if cov_repo is None:
        return None, None, []
    cov_config = _get_cov_config(repos)
    extra_markers = list(cov_config.get("exclusions", {}).get("markers") or [])
    return cov_repo.sut_dir, load_tiers(cov_config, cov_repo.sut_dir), extra_markers


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
            "--report",
            "-r",
            help="Where to place the generated HTML report.",
        ),
    ] = Path("./cov_report"),
    project_name: Annotated[
        str,
        typer.Option(
            "--project-name",
            help="Title shown in the HTML report header.",
        ),
    ] = "Coverage Report",
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
    # (repo_root + declared tiers) from settings; a tree with no [coverage]
    # section falls back to (None, None), i.e. the legacy behavior.
    repo_root: Path | None = None
    tier_configs: "list[TierConfig] | None" = None
    extra_markers: list[str] = []
    if tier:
        try:
            tier_specs: list[TierSpec] = _parse_tier_specs(tier)
        except typer.BadParameter as e:
            logger.exception("Bad tier parameter")
            raise typer.Exit(1) from e
    else:
        tier_specs = [(TIER_SYSTEM, None)]
        repo_root, tier_configs, extra_markers = _resolve_cov_settings()

    cov_dirs = [d / "cov" for d in output_dirs]
    report_dir = report_dir.resolve()

    from ..coverage.capture.gitio import GitUnavailableError

    try:
        store = asyncio.run(
            run_coverage_report(
                cov_dirs,
                report_dir,
                project_name=project_name,
                tier_specs=tier_specs,
                repo_root=repo_root,
                tier_configs=tier_configs,
                extra_markers=extra_markers,
                prefix=prefix,
            )
        )
    except (CoverageDataMismatchError, CoverageToolVersionError) as e:
        # Typed capture errors — polluted tree (product rebuilt after the
        # test run) or a gcov tool that cannot read the build's format (e.g.
        # clang build captured with GNU gcov): the message already names the
        # cause and remedy — print it clean, never as a traceback.
        logger.error(str(e))  # noqa: TRY400 — deliberately no traceback: user-facing cause + remedy
        raise typer.Exit(1) from e
    except GitUnavailableError as e:
        # A [coverage] section resolved a repo_root, but it is not a git repo
        # (or git is missing): the pinned-capture features can't run. Name the
        # cause and the git-less escape hatch — clean line, no traceback.
        logger.error(  # noqa: TRY400 — deliberately no traceback: user-facing cause + remedy
            "not a git repository — pinned-capture features unavailable; use --tier NAME=PATH"
        )
        raise typer.Exit(1) from e
    except ValueError as e:
        # A malformed committed manual capture (load_manual_captures wraps the
        # parse error with the offending file name). Print the cause clean.
        logger.error(str(e))  # noqa: TRY400 — deliberately no traceback: user-facing cause (names the bad file)
        raise typer.Exit(1) from e
    except RuntimeError as e:
        logger.error("Coverage merge failed: %s", e)  # noqa: TRY400 — deliberately no traceback: lcov output is the diagnostic
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


# `report` is purely local and must never create a per-invocation output dir
# (reporting on yesterday's run leaves no trace of its own — e2e-pinned). The
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


def _resolve_tester(name: str | None, email: str | None) -> dict[str, str]:
    """Resolve tester identity for a manual capture (spec decision 15).

    ``name`` defaults to :func:`getpass.getuser`; ``email`` defaults to
    ``git config user.email`` and is omitted entirely (not stamped empty)
    when unset. CLI-supplied values always win over both defaults.
    """
    import getpass
    import subprocess

    resolved_name = name or getpass.getuser()
    resolved_email = email
    if not resolved_email:
        proc = subprocess.run(
            ["git", "config", "user.email"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            resolved_email = proc.stdout.strip()

    tester: dict[str, str] = {"name": resolved_name}
    if resolved_email:
        tester["email"] = resolved_email
    return tester


async def _connect_cov_hosts() -> tuple[
    "list[Repo]",
    "Repo",
    "dict[str, Any]",
    "re.Pattern[str] | None",
    "list[RemoteHost]",
    "list[UnixHost]",
    str,
]:
    """Bootstrap, locate ``[coverage]`` config, and discover matching lab hosts.

    Shared setup for both ``get``'s fetch flow and ``clean``: loads the
    active lab's repos (:func:`~otto.configmodule.get_repos`), locates the
    repo with a ``[coverage]`` section, compiles its ``hosts`` pattern, and
    enumerates every lab host that pattern matches — mirroring
    :func:`otto.cli.test._run_coverage`'s fetch stage. Deliberately stops
    short of constructing a
    :class:`~otto.coverage.fetcher.remote.GcdaFetcher`: ``get`` and
    ``clean`` disagree on both the fetcher's staging root (a real output
    dir vs. an unused placeholder) and its ``pattern`` scope (``get``
    fetches with no pattern, preserving its existing tested behavior;
    ``clean`` scopes to the already-computed ``unix_hosts`` list, not the
    raw ``[coverage].hosts`` pattern, so it can never re-match an embedded
    host), so each command builds its own fetcher from the pieces returned
    here.

    Raises :class:`_CovError` when no ``[coverage]`` section is configured
    at all — the one failure mode every caller treats identically.

    Returns:
        ``(repos, cov_repo, cov_config, cov_pattern, cov_hosts, unix_hosts,
        gcda_remote_dir)``.
    """
    import re

    from ..configmodule import all_hosts, get_repos
    from ..host import UnixHost
    from .test import _get_cov_config, _get_cov_repo

    repos = get_repos()
    cov_config = _get_cov_config(repos)
    cov_repo = _get_cov_repo(repos)
    if not cov_config or cov_repo is None:
        raise _CovError("No [coverage] section found in .otto/settings.toml")

    # Same repo-declared selector _run_coverage uses to keep infrastructure
    # hosts (e.g. an SSH hop) out of the coverage set.
    hosts_pattern = cov_config.get("hosts")
    cov_pattern = re.compile(hosts_pattern) if hosts_pattern else None

    cov_hosts = list(all_hosts(pattern=cov_pattern))
    unix_hosts = [h for h in cov_hosts if isinstance(h, UnixHost)]
    gcda_remote_dir = cov_config.get("gcda_remote_dir", "")

    return repos, cov_repo, cov_config, cov_pattern, cov_hosts, unix_hosts, gcda_remote_dir


def _unix_only_pattern(unix_hosts: "list[UnixHost]") -> "re.Pattern[str]":
    """Anchored regex matching exactly the given Unix hosts' ids.

    :meth:`~otto.coverage.fetcher.remote.GcdaFetcher.clean_remote` re-derives
    its own host set from its ``pattern`` via ``do_for_all_hosts()`` /
    ``all_hosts()`` — a path with **no** ``EmbeddedHost`` guard. Passing the
    raw ``[coverage].hosts`` pattern would therefore let ``clean_remote`` send
    an embedded board a bogus ``find ... -delete`` on a mixed lab. Scoping to
    the already-computed ``unix_hosts`` list closes that. Matching is
    ``pattern.search(host.id)`` (see :meth:`OttoContext.all_hosts`), so each
    alternative is fullmatch-anchored to keep a host id like ``"sprout"`` from
    also matching a sibling ``"sprout2"``.
    """
    import re

    unix_ids = "|".join(re.escape(h.id) for h in unix_hosts)
    return re.compile(f"^(?:{unix_ids})$")


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

    Mirrors :func:`otto.cli.test._run_coverage`'s fetch (Unix ``.gcda`` over
    the network + embedded console dump) and metadata sidecar, then hands
    the collected ``cov_dir`` to
    :func:`~otto.coverage.capture.produce.produce_captures`. Manual-kind
    tiers additionally copy each produced capture into the repo's committed
    manual-capture store (``.otto/coverage/manual/``).

    Every failure mode raises :class:`_GetError` (or, via
    :func:`_connect_cov_hosts`, the shared :class:`_CovError`) with a
    single-line, user-facing message; the sync ``get`` command is the only
    place that turns either into ``typer.Exit(1)``.
    """
    from ..context import get_context
    from ..coverage.capture.gitio import GitUnavailableError, head_commit
    from ..coverage.capture.model import Capture
    from ..coverage.capture.produce import produce_captures
    from ..coverage.capture.store_dir import write_manual_capture
    from ..coverage.errors import CoverageDataMismatchError, CoverageToolVersionError
    from ..coverage.fetcher.embedded import collect_embedded_coverage
    from ..coverage.fetcher.remote import GcdaFetcher
    from ..coverage.tiers import load_tiers, resolve_get_tier
    from .test import _write_cov_metadata

    (
        repos,
        cov_repo,
        cov_config,
        cov_pattern,
        cov_hosts,
        unix_hosts,
        gcda_remote_dir,
    ) = await _connect_cov_hosts()

    tiers = load_tiers(cov_config)
    try:
        resolved_tier = resolve_get_tier(tiers, tier_name)
    except ValueError as e:
        raise _GetError(str(e)) from e

    if resolved_tier.kind == "manual" and not ticket:
        raise _GetError(f"tier {resolved_tier.name!r} is a manual-kind tier; requires --ticket")

    # Git preflight: capture production pins to HEAD, so a non-git sut can
    # never yield a capture. Fail fast here — before the fleet pull — rather
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
    host_dirs: dict[str, Path] = {}

    unix_dirs: dict[str, Path] = {}
    fetcher: GcdaFetcher | None = None
    if gcda_remote_dir and unix_hosts:
        for host in unix_hosts:
            host.rebuild_connections()
        fetcher = GcdaFetcher(cov_dir)
        unix_dirs = await fetcher.fetch_all(gcda_remote_dir)
        host_dirs.update(unix_dirs)

    embedded_dirs = await collect_embedded_coverage(cov_config, cov_dir, pattern=cov_pattern)
    host_dirs.update(embedded_dirs)

    if not host_dirs:
        searched = ", ".join(sorted(h.id for h in cov_hosts))
        where = f"searched: {searched}" if searched else "no hosts matched [coverage].hosts"
        raise _GetError(f"no .gcda counters retrieved from any host ({where})")

    await _write_cov_metadata(
        repos=repos,
        cov_config=cov_config,
        unix_hosts=unix_hosts,
        unix_dirs=unix_dirs,
        cov_hosts=cov_hosts,
        embedded_dirs=embedded_dirs,
        cov_dir=cov_dir,
    )

    # Tester/ticket/note are only meaningful for a manual-kind tier — an
    # automated e2e-kind pull has no human "tester" to attribute.
    tester: dict[str, str] | None = None
    produce_ticket: str | None = None
    produce_note: str | None = None
    if resolved_tier.kind == "manual":
        tester = _resolve_tester(tester_name, tester_email)
        produce_ticket = ticket
        produce_note = note

    try:
        written = await produce_captures(
            cov_dir,
            tier=resolved_tier.name,
            repo_root=cov_repo.sut_dir,
            labs=[cov_repo.name],
            tester=tester,
            ticket=produce_ticket,
            note=produce_note,
        )
    except GitUnavailableError as e:
        raise _GetError(str(e)) from e
    except (CoverageDataMismatchError, CoverageToolVersionError) as e:
        raise _GetError(str(e)) from e
    except RuntimeError as e:
        raise _GetError(f"Coverage merge failed: {e}") from e

    if not written:
        searched = ", ".join(sorted(host_dirs))
        where = f"searched: {searched}" if searched else "no boards produced captures"
        raise _GetError(f"no .gcda counters retrieved from any board ({where})")

    if resolved_tier.kind == "manual":
        for capture_path in written:
            capture = Capture.load(capture_path)
            write_manual_capture(capture, cov_repo.sut_dir)

    if clean and unix_dirs:
        # The fetch fetcher carries no pattern, so its clean_remote() would
        # re-derive the raw lab host set and could zero an embedded board on a
        # mixed lab (same bug fixed for `cov clean`). Build a second, scoped
        # fetcher just for the clean call; the fetch above is left untouched.
        clean_fetcher = GcdaFetcher(cov_dir, pattern=_unix_only_pattern(unix_hosts))
        await clean_fetcher.clean_remote(gcda_remote_dir)

    logger.info("Coverage captured: %d board(s) -> %s", len(written), cov_dir)
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
            help="Coverage tier to stamp onto each capture. Defaults to the sole e2e-kind tier.",
        ),
    ] = None,
    ticket: Annotated[
        str | None,
        typer.Option(
            "--ticket",
            help="Ticket reference to stamp onto each capture. Required for manual-kind tiers.",
        ),
    ] = None,
    note: Annotated[
        str | None,
        typer.Option("--note", help="Free-text note to stamp onto each capture."),
    ] = None,
    tester_name: Annotated[
        str | None,
        typer.Option(
            "--tester-name",
            help="Tester name to stamp onto each capture. Defaults to the current user.",
        ),
    ] = None,
    tester_email: Annotated[
        str | None,
        typer.Option(
            "--tester-email",
            help="Tester email to stamp onto each capture. Defaults to `git config user.email`.",
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
    """Fetch .gcda coverage from the lab and produce pinned per-board captures."""
    try:
        asyncio.run(
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
        logger.error(str(e))  # noqa: TRY400 — deliberately no traceback: clean cause line
        raise typer.Exit(1) from e


# ---------------------------------------------------------------------------
# clean — zero remote .gcda counters (no fetch)
# ---------------------------------------------------------------------------


class _CleanError(_CovError):
    """Internal signal for a clean, single-line ``cov clean`` failure.

    Raised by :func:`_do_clean` for every ``clean``-specific failure mode
    (no ``gcda_remote_dir`` configured, no matching Unix hosts); the sync
    ``clean`` command catches the shared :class:`_CovError` base (which also
    covers :func:`_connect_cov_hosts`'s "no config" failure).
    """


async def _do_clean() -> None:
    """Zero remote ``.gcda`` counters on the lab's Unix coverage hosts.

    Uses :func:`_connect_cov_hosts` for the identical host discovery
    ``get`` uses (same ``[coverage].hosts`` pattern, same Unix/embedded
    split), then hands the matched hosts to the existing
    :meth:`~otto.coverage.fetcher.remote.GcdaFetcher.clean_remote`. That
    method already logs one line per host (success or failure) via its own
    module logger, so no extra per-host logging is added here — only a
    completion summary.

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
        unix_hosts,
        gcda_remote_dir,
    ) = await _connect_cov_hosts()

    if not gcda_remote_dir:
        raise _CleanError("No coverage.gcda_remote_dir configured in .otto/settings.toml")

    has_embedded = any(isinstance(h, EmbeddedHost) for h in cov_hosts)

    if not unix_hosts:
        if has_embedded:
            logger.info(
                "embedded boards not cleaned (requires product-side counter reset — later phase)"
            )
            return
        raise _CleanError("No coverage hosts matched [coverage].hosts — nothing to clean")

    for host in unix_hosts:
        host.rebuild_connections()
    # staging_root is unused by clean_remote() (no files are downloaded); the
    # scoped pattern keeps clean_remote()'s own host re-derivation off embedded
    # boards on a mixed lab (see _unix_only_pattern).
    fetcher = GcdaFetcher(Path("/tmp"), pattern=_unix_only_pattern(unix_hosts))  # noqa: S108 — deliberate staging path, never written to
    await fetcher.clean_remote(gcda_remote_dir)
    logger.info("Coverage counters cleared on %d host(s)", len(unix_hosts))

    if has_embedded:
        logger.info(
            "embedded boards not cleaned (requires product-side counter reset — later phase)"
        )


@cov_app.command()
def clean() -> None:
    """Zero .gcda counters on the lab's Unix coverage hosts."""
    try:
        asyncio.run(_do_clean())
    except _CovError as e:
        logger.error(str(e))  # noqa: TRY400 — deliberately no traceback: clean cause line
        raise typer.Exit(1) from e


# `clean` zeroes remote counters and writes nothing locally — no output dir.
clean.__cli_output_dir__ = False  # ty: ignore[unresolved-attribute]
