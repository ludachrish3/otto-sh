r"""Generate coverage reports from ``otto test --cov`` output.

Loads the per-product captures collected by one or more ``otto test`` runs
and renders a multi-tier HTML report.

**Usage**::

    otto cov report RUN_DIR1 [RUN_DIR2 ...] --dir ./my_report

Each *RUN_DIR* is an ``otto test`` output directory containing a ``cov/``
subdirectory with one ``<host>/<product>/`` directory per product. A product
that captured holds a ``capture.json``; one that failed holds a
``capture.failed`` and is named once in a warning; one with neither is
captured now, with the tier and commit its collection recorded. Multiple
directories can be specified to stitch together coverage from separate test
runs.

Per-host toolchains (``gcov``, ``lcov``) are resolved automatically from
host configuration in ``lab.json`` or by inspecting ``.gcno`` files.
See the :doc:`/cli/cov/index` and :doc:`/cli/host/index` documentation.

**Options**

``--dir PATH``
    Where to place the generated coverage report (default: ``cov_report``
    under this invocation's output directory).

``--overwrite-dir``
    Allow ``--dir`` to clear an existing non-empty directory.

``--report-title STR``
    Title shown in the HTML report header.

``--tickets-json PATH``
    Also write a machine-readable per-ticket coverage summary (its own
    ``format`` version, independent of the internal ``store.json``) to this path.
    Every file path in it is project-root-relative posix (never an absolute,
    machine-specific path), so it diffs cleanly across checkouts. Requires
    ``[coverage.tickets]`` to have attributed at least one ticket; fails
    loud otherwise (exit 1) rather than writing an empty file.

``--tier NAME=PATH``
    Repeatable.  Report an lcov tracefile as a coverage tier.  ``NAME`` is
    a free-form label (e.g. ``unit``, ``manual``, ``integration``); ``PATH``
    is the lcov ``.info`` file feeding that tier, whose run records the tier
    only.  Every ``--tier`` needs its ``=PATH``.

    The order of ``--tier`` flags is the precedence order: the first flag
    is the highest-precedence tier and wins the row coloring on the
    annotated source view.  ``--tier`` never reads settings, so it reports
    tracefiles alone and cannot be combined with run directories.

    Example::

        otto cov report \\
            --tier unit=u.info \\
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
    Zero the counters of every host that contributed a capture, after a
    successful retrieval — for use before starting a manual session. Each
    reset is printed; a failed one exits 1 (the captures are still written).

``otto cov clean`` zeroes each instrumented product's coverage counters on
every coverage host ``[coverage].hosts`` selects — Unix hosts, containers and
embedded boards alike, each through its product's own reset — without first
fetching anything. Useful ahead of a manual session when the previous capture
has already been retrieved::

    otto cov clean

It prints one line per host and product and exits 1 when any reset failed;
under ``--dry-run`` each reset is printed as not run.

Each verb is a thin leaf over one library call —
:func:`~otto.coverage.get.get_coverage`,
``otto.coverage.collect.clean_coverage`` and
:func:`~otto.coverage.reporter.run_coverage_report` — which owns every rule;
the leaf renders the result and spells a refused input in its flags.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape as escape_markup

if TYPE_CHECKING:
    # Type-only: never executed, so it carries no runtime import cost and
    # doesn't touch the `cov` import-budget surface (measured by `otto cov
    # --help`, which never runs get()/clean()'s bodies). Real coverage-
    # machinery imports stay function-local per the same budget.
    from ..coverage.reporter import TierSpec
    from ..coverage.reports import CleanReport, GetReport

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
    (fetching or zeroing remote ``.gcda`` counters). ``cov get`` and ``cov
    report`` both create a per-invocation output directory (the report's
    default destination, absent ``--dir``, is ``cov_report`` under it);
    ``clean`` opts out via its leaf marker.
    """
    if ctx.resilient_parsing:
        return


def _parse_tier_specs(raw_tiers: list[str]) -> "list[TierSpec]":
    """Split repeated ``--tier NAME=PATH`` values into ordered ``(name, path)`` pairs.

    Order is preserved (= precedence order). Only the syntax is checked here
    — a name, and a path after any ``=``; that every tier needs its path and
    that no name repeats are :func:`~otto.coverage.reporter.run_coverage_report`'s
    rules.
    """
    specs: "list[TierSpec]" = []
    for raw in raw_tiers:
        name, sep, path_str = raw.partition("=")
        name = name.strip()
        if not name:
            raise typer.BadParameter(f"missing tier name: {raw!r}", param_hint="--tier")
        if sep and not path_str:
            raise typer.BadParameter(f"missing path after '=': {raw!r}", param_hint="--tier")
        specs.append((name, Path(path_str) if sep else None))
    return specs


_REPORT_FLAGS = {
    "cov_dirs": "OUTPUT_DIRS",
    "tier_specs": "--tier",
    "output_dir": "--dir",
    "overwrite": "--overwrite-dir",
}


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
        Path | None,
        typer.Option(
            "--dir",
            "-d",
            help=(
                "Where to place the generated coverage report "
                "(default: cov_report under this invocation's output directory)."
            ),
        ),
    ] = None,
    report_title: Annotated[
        str,
        typer.Option(
            "--report-title",
            help="Title shown in the HTML report header.",
        ),
    ] = "Coverage Report",
    tickets_json: Annotated[
        Path | None,
        typer.Option(
            "--tickets-json",
            help=(
                "Also write a machine-readable per-ticket coverage summary to this "
                "path. Requires \\[coverage.tickets]."
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
                "Report an lcov tracefile as a coverage tier: NAME=PATH. "
                "Repeatable. Order is precedence order (first = highest). "
                "When given, --tier flags replace the settings tiers and "
                "report tracefiles alone (no run directories)."
            ),
        ),
    ] = None,
    overwrite_dir: Annotated[
        bool,
        typer.Option(
            "--overwrite-dir",
            help="Allow --dir to clear an existing non-empty directory.",
        ),
    ] = False,
) -> None:
    """Generate a coverage report from otto test --cov output directories."""
    from ..coverage.errors import (
        CaptureCommitError,
        CoverageDataMismatchError,
        CoverageToolVersionError,
    )
    from ..host.errors import CoverageToolMissingError

    output_dirs = output_dirs or []

    # Precedence rule: explicit --tier flags are a git-less escape hatch and
    # take precedence over settings tiers — they pass the empty inputs (no
    # project_root / tier_configs resolution), so they report tracefiles
    # alone. With no --tier flags, resolve_report_inputs (below, inside the
    # try block) reads the collection-model inputs (project_root + declared
    # tiers) from settings; a tree where no project's [coverage] declares a
    # target gets ReportInputs(), which reports no run directory.
    from ..coverage.report_inputs import ReportInputs, resolve_report_inputs

    inputs = ReportInputs()
    # --tier never resolves settings (see precedence rule above), so inputs
    # stays ReportInputs() there — run_coverage_report defaults to
    # Thresholds()'s 80.0/70.0 and runs no ticket attribution (it has no git
    # project_root to walk).
    tier_specs: "list[TierSpec] | None" = _parse_tier_specs(tier) if tier else None

    cov_dirs = [d / "cov" for d in output_dirs]

    if report_dir is None:
        from ..run_context import get_context

        base = get_context().output_dir
        if base is None:
            raise typer.BadParameter(
                "no output directory available: pass --dir/-d", param_hint="--dir"
            )
        report_dir = base / "cov_report"
    report_dir = report_dir.resolve()

    from ..coverage.capture.gitio import GitUnavailableError, NotAGitRepoError
    from ..coverage.config import DestinationError
    from ..coverage.errors import CoverageInputError
    from ..coverage.reporter import run_coverage_report
    from ..lifecycle import run_command
    from .invoke import usage_error_from

    try:
        if not tier:
            # Resolved inside the try (not above, alongside tier_specs): a
            # malformed override file raises OverrideConfigError (a
            # ValueError) from load_override_config, and must hit the same
            # clean-message `except ValueError` handler below as every
            # other settings/data ValueError, not propagate as a traceback.
            # The composition root: importing it installs the initiator that composes
            # the projects this reads (spec 2026-10-11 placement-and-phases §3.12).
            from .. import bootstrap

            del bootstrap
            from ..declared import get_projects

            inputs = resolve_report_inputs(get_projects())
        store = run_command(
            run_coverage_report(
                cov_dirs,
                report_dir,
                inputs,
                report_title=report_title,
                tier_specs=tier_specs,
                prefix=prefix,
                overwrite=overwrite_dir,
            )
        )
    except (
        CaptureCommitError,
        CoverageDataMismatchError,
        CoverageToolVersionError,
        CoverageToolMissingError,
    ) as e:
        # Typed capture errors — the tree moved to another commit since the
        # counters were collected (check out that commit, or re-run at this
        # one), a polluted tree (product rebuilt after the test run), a gcov
        # tool that cannot read the build's format (e.g. clang build captured
        # with GNU gcov), or a gcov the data's own stamp names that is not
        # installed: the message already names the cause and remedy — print
        # it clean, never as a traceback.
        logger.error(str(e))  # noqa: TRY400 — deliberately no traceback: user-facing cause + remedy
        raise typer.Exit(1) from e
    except NotAGitRepoError as e:
        # A [coverage] section resolved a project_root that is not a git repository:
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
        # git missing, or a git command that failed inside a real git repository: echo
        # what git said rather than mislabelling it as "not a git repository".
        logger.error(str(e))  # noqa: TRY400 — deliberately no traceback: git's own message is the cause
        raise typer.Exit(1) from e
    except (DestinationError, CoverageInputError) as e:
        # A refused input — a run directory that does not exist or that no
        # [coverage] project root can anchor, a tier without a path or named
        # twice, a --dir that is not empty without --overwrite-dir — is a
        # usage error (exit 2): the fix is an argument, not a rerun. Both are
        # ValueError subclasses, so this must be caught before the generic
        # handler below.
        raise usage_error_from(e, flags=_REPORT_FLAGS) from e
    except ValueError as e:
        # A malformed committed manual capture (load_manual_captures wraps the
        # parse error with the offending file name), or the no-[coverage]-
        # config / no-.gcda ValueErrors raised by collect_coverage. Print the
        # cause clean.
        logger.error(str(e))  # noqa: TRY400 — deliberately no traceback: user-facing cause (names the bad file)
        raise typer.Exit(1) from e
    except RuntimeError as e:
        logger.error("Coverage merge failed: %s", e)  # noqa: TRY400 — deliberately no traceback: lcov output is the diagnostic
        raise typer.Exit(1) from e
    if store.file_count() == 0:
        # A store with no files is a vacuous success — restore the old loud
        # CI-friendly fail. Name every input searched (run cov dirs plus, when
        # a [coverage] project root resolved, its committed manual-capture store).
        searched = [str(d) for d in cov_dirs]
        if inputs.project_root is not None:
            searched.append(str(inputs.project_root / ".otto" / "coverage" / "manual"))
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

        if inputs.project_root is None:
            # The --tier tracefile path and the no-[coverage]-section fallback
            # both leave project_root unset here — and both also leave
            # ticket_spec unset, so no attribution ever ran and
            # store.tickets is always empty in this branch too. Same
            # message as the empty-store ValueError below, raised directly
            # rather than handing a None project_root to write_ticket_export
            # (which requires one to emit project-root-relative paths).
            logger.error(
                "no ticket data in this report — [coverage.tickets] must be configured "
                "for --tickets-json"
            )
            raise typer.Exit(1)

        try:
            write_ticket_export(
                store,
                tickets_json,
                project_root=inputs.project_root,
                report_title=report_title,
                otto_version=get_version(),
                generated=make_generated_stamp(),
            )
        except ValueError as e:
            # --tickets-json was explicitly requested — an empty/absent
            # ticket table is a loud, CI-visible failure here (unlike
            # `otto test`'s never-fail-a-successful-run policy for its
            # optional post-run tail).
            logger.error(str(e))  # noqa: TRY400 — deliberately no traceback: clean cause line
            raise typer.Exit(1) from e
        logger.info("Ticket export: %s", tickets_json)


# ---------------------------------------------------------------------------
# get — retrieve the lab's coverage now (get_coverage)
# ---------------------------------------------------------------------------

_GET_FLAGS = {"tier": "--tier", "ticket": "--ticket", "output_dir": "--output"}


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
                "Zero the counters of every product that captured, after retrieval — "
                "for use before starting a manual session. A product that failed to "
                "capture keeps its counters."
            ),
        ),
    ] = False,
) -> None:
    """Fetch .gcda from the lab's instrumented products; produce per-product captures."""
    # Function-local: this module sits on the `cov` import-budget surface, and
    # the coverage library pulls the collector, the fetcher and rich.table.
    from ..coverage.errors import (
        CoverageCaptureError,
        CoverageInputError,
        CoverageNotInstrumentedError,
    )
    from ..coverage.get import get_coverage
    from ..lifecycle import run_command
    from .invoke import render_instrumentation_refusal, usage_error_from

    try:
        got = run_command(
            get_coverage(
                output_dir,
                tier=tier,
                ticket=ticket,
                note=note,
                tester_name=tester_name,
                tester_email=tester_email,
                clean=clean,
            )
        )
    except CoverageInputError as e:
        raise usage_error_from(e, flags=_GET_FLAGS) from e
    except CoverageNotInstrumentedError as e:
        # The verdicts go to the console as the rounded table; only the
        # headline is logged beside it (the rest of the message is the same
        # verdicts in plain text).
        logger.error(render_instrumentation_refusal(e))  # noqa: TRY400 — deliberately no traceback: clean cause line
        raise typer.Exit(1) from e
    except CoverageCaptureError as e:
        # The products that captured were kept; with --clean their clean is
        # reported too, a failed reset as loudly as on success.
        logger.error(str(e))  # noqa: TRY400 — deliberately no traceback: clean cause line
        if e.clean is not None:
            _render_clean_report(e.clean)
        raise typer.Exit(1) from e
    except (ValueError, RuntimeError) as e:
        # Every other refusal (no project's [coverage] declaring a target, an
        # empty host selection, no repository, no data, a typed capture error)
        # names its own cause and remedy.
        logger.error(_failure_line(e))  # noqa: TRY400 — deliberately no traceback: clean cause line
        raise typer.Exit(1) from e
    _render_get_report(got)


def _failure_line(error: BaseException) -> str:
    """Say what failed: a bare RuntimeError is the merge; every typed error speaks for itself.

    An empty host selection, direct or wrapped, speaks in its CLI wording: no
    coverage command takes the membership flags its library message names.
    """
    from ..config.scope import selection_message

    if type(error) is RuntimeError:
        return f"Coverage merge failed: {error}"
    return selection_message(error, walker="coverage") or str(error)


def _render_get_report(got: "GetReport") -> None:
    """Print one line per capture and the summary; a failed ``--clean`` exits 1."""
    from rich import print as rprint

    for path in got.captures:
        rprint(escape_markup(str(path)))
    rprint(escape_markup(f"Coverage captured: {len(got.captures)} product(s) -> {got.cov_dir}"))
    if got.clean is not None:
        _render_clean_report(got.clean)


# ---------------------------------------------------------------------------
# clean — zero every coverage host's counters (clean_coverage)
# ---------------------------------------------------------------------------


@cov_app.command()
def clean() -> None:
    """Zero each instrumented product's counters on the lab's coverage hosts."""
    from ..coverage.collect import clean_coverage
    from ..lifecycle import run_command

    try:
        cleaned = run_command(clean_coverage())
    except ValueError as e:
        # No project declaring [coverage], a malformed selector, or no host to walk:
        # each message names the cause.
        logger.error(_failure_line(e))  # noqa: TRY400 — deliberately no traceback: clean cause line
        raise typer.Exit(1) from e
    _render_clean_report(cleaned)


# `clean` zeroes remote counters and writes nothing locally — no output dir.
clean.__cli_output_dir__ = False  # ty: ignore[unresolved-attribute]


def _render_clean_report(cleaned: "CleanReport") -> None:
    """Print one line per host and product; exit 1 when any reset failed.

    A reset a dry run declined is printed as not run and is not a failure.
    """
    from rich import print as rprint

    from ..utils import Status
    from .invoke import print_error

    for host, products in cleaned.hosts.items():
        if not products:
            rprint(f"[dim]{escape_markup(f'{host}: no instrumented products')}")
        for product, result in products.items():
            if result.status is Status.NotRun:
                rprint(f"[dim]{escape_markup(f'{host}/{product}: not run (dry run)')}")
            elif result.is_ok:
                rprint(f"[green]{escape_markup(f'{host}/{product}: counters cleared')}")
    for failed in cleaned.failed:
        print_error(f"{failed.host}/{failed.product}: {failed.reason}")
    if not cleaned.ok:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# otto cov kmodcov — vendor the otto_kmodcov library and check a vendored copy
# ---------------------------------------------------------------------------


kmodcov_app = typer.Typer(
    name="kmodcov",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Vendor the otto_kmodcov kernel-module library into a project, or check a vendored copy.",
)


@kmodcov_app.callback()
def kmodcov_callback(ctx: typer.Context) -> None:
    """Vendor the otto_kmodcov library (`export`) or compare a vendored copy with it (`check`).

    The library is a kernel module a user's own build system builds against
    each of their kernels; otto never builds it. `export` writes the sources
    this otto ships, overwriting (the copy is committed, so the project's own
    diff is the review); `check` compares byte for byte, ignoring the
    `kmodcov_local.h` override, and exits 0 current / 1 differs / 2 absent.
    """
    if ctx.resilient_parsing:
        return


@kmodcov_app.command("export")
def kmodcov_export(
    directory: Annotated[
        Path, typer.Argument(help="Where the vendored copy lives (created if absent).")
    ],
) -> None:
    """Write the otto_kmodcov sources this otto ships into DIRECTORY."""
    from ..kmodcov.library import export_tree

    result = export_tree(directory)
    if result.changed:
        typer.echo(
            f"{result.directory}: {len(result.changed)} file(s) written "
            f"(otto {result.version}): {', '.join(result.changed)}"
        )
    else:
        typer.echo(f"{result.directory}: already current (otto {result.version})")


kmodcov_export.__cli_output_dir__ = False  # ty: ignore[unresolved-attribute]
kmodcov_export.__cli_lab_free__ = True  # ty: ignore[unresolved-attribute]


@kmodcov_app.command("check")
def kmodcov_check(
    directory: Annotated[Path, typer.Argument(help="The vendored copy to compare.")],
) -> None:
    """Compare DIRECTORY with the library this otto ships; exit 0 current, 1 differs, 2 absent."""
    from ..kmodcov.library import check_tree

    result = check_tree(directory)
    if result.state == "absent":
        typer.echo(f"{result.directory}: no otto_kmodcov there (no kmodcov.h)")
    elif result.state == "current":
        origin = f", exported by otto {result.exported_by}" if result.exported_by else ""
        override = "; kmodcov_local.h present" if result.local_override else ""
        typer.echo(f"{result.directory}: current{origin}{override}")
    else:
        origin = f" (exported by otto {result.exported_by})" if result.exported_by else ""
        typer.echo(
            f"{result.directory}: not this otto's library{origin} — {result.differences}. "
            f"Re-export with `otto cov kmodcov export {result.directory}` and review the diff."
        )
    if result.exit_code:
        raise typer.Exit(result.exit_code)


kmodcov_check.__cli_output_dir__ = False  # ty: ignore[unresolved-attribute]
kmodcov_check.__cli_lab_free__ = True  # ty: ignore[unresolved-attribute]

cov_app.add_typer(kmodcov_app, name="kmodcov")


# ---------------------------------------------------------------------------
# otto cov embedded-gcov — write the embedded-gcov runtime's patch, check a copy
# ---------------------------------------------------------------------------

embedded_gcov_app = typer.Typer(
    name="embedded-gcov",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Write the patch for the embedded-gcov runtime (GNU compilers, the gcov format), "
        "or check a copy of it."
    ),
)


@embedded_gcov_app.callback()
def embedded_gcov_callback(ctx: typer.Context) -> None:
    """Write the embedded-gcov runtime's patch (`export`) or compare a copy with it (`check`).

    embedded-gcov is NASA's coverage runtime for targets with no filesystem; it
    relies on a GNU compiler and the gcov format. otto does not vendor it, only
    the patch that lets it build under GCC 12 through 14. `export` writes the
    patch this otto ships, overwriting; `check` compares byte for byte and
    exits 0 current / 1 differs / 2 absent.
    """
    if ctx.resilient_parsing:
        return


@embedded_gcov_app.command("export")
def embedded_gcov_export(
    directory: Annotated[
        Path, typer.Argument(help="Where to write the patch (created if absent).")
    ],
) -> None:
    """Write the embedded-gcov patch this otto ships into DIRECTORY."""
    from ..coverage.embedded_gcov.library import SHIPPED_FILES, export_patch

    changed = export_patch(directory)
    if changed:
        typer.echo(f"{directory}: {len(changed)} file(s) written: {', '.join(changed)}")
    else:
        typer.echo(f"{directory}: already current ({', '.join(SHIPPED_FILES)})")


embedded_gcov_export.__cli_output_dir__ = False  # ty: ignore[unresolved-attribute]
embedded_gcov_export.__cli_lab_free__ = True  # ty: ignore[unresolved-attribute]


@embedded_gcov_app.command("check")
def embedded_gcov_check(
    directory: Annotated[Path, typer.Argument(help="The directory holding the patch copy.")],
) -> None:
    """Compare DIRECTORY with the patch this otto ships; exit 0 current, 1 differs, 2 absent."""
    from ..coverage.embedded_gcov.library import check_patch

    result = check_patch(directory)
    if result.state == "absent":
        typer.echo(f"{result.directory}: no embedded-gcov patch there")
    elif result.state == "current":
        typer.echo(f"{result.directory}: current")
    else:
        typer.echo(
            f"{result.directory}: not this otto's patch — {result.differences}. "
            f"Re-export with `otto cov embedded-gcov export {result.directory}` "
            "and review the diff."
        )
    if result.exit_code:
        raise typer.Exit(result.exit_code)


embedded_gcov_check.__cli_output_dir__ = False  # ty: ignore[unresolved-attribute]
embedded_gcov_check.__cli_lab_free__ = True  # ty: ignore[unresolved-attribute]

cov_app.add_typer(embedded_gcov_app, name="embedded-gcov")
