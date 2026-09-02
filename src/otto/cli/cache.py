"""``otto cache`` — inspect, clear, and prune otto's per-workspace caches.

Thin by design: every filesystem decision lives in
:mod:`otto.config.cache_maintenance` (walk, clear, prune -- see that module's
docstring for the safety argument), and this module is the Typer surface plus
presentation over it.

- ``otto cache info`` -- one table, one row per workspace under
  :func:`~otto.config.home.otto_home`, oldest cache first. Age and the
  "over threshold" count are read off each workspace's OLDEST cache file
  (:attr:`~otto.config.cache_maintenance.WorkspaceInfo.oldest_cache_mtime`),
  not the newest -- ``prune`` decides per FILE, so a workspace with one stale
  file and one fresh one is still something a prune would touch, and
  reporting the newest file's age alone would hide that.
- ``otto cache clear`` -- clears THIS workspace's cache files only (the engine
  is :func:`~otto.config.cache_maintenance.clear_workspace` on
  :func:`~otto.config.home.workspace_home`); ``--all`` clears every
  workspace's cache files regardless of age, via
  :func:`~otto.config.cache_maintenance.prune` with ``age_blind=True``. Either
  way, a workspace directory is removed only once emptying it of cache files
  leaves nothing behind -- an ``env/`` virtualenv always survives.
- ``otto cache prune`` -- removes cache files older than ``--age`` days
  (default :data:`~otto.config.cache_maintenance.DEFAULT_MAX_AGE_DAYS`,
  matching :func:`~otto.config.cache_maintenance.prune`'s own default) across
  every workspace; ``--dry-run`` reports what would happen without touching
  the filesystem.

IMPORT DISCIPLINE: everything from ``otto.config.cache_maintenance`` and
``otto.config.home`` beyond the one small constant below is imported INSIDE
the verb that uses it, matching ``otto.cli.inventory``'s convention -- this
module is registered with a lazy ``"otto.cli.cache:cache_app"`` loader (see
``otto.cli.builtin_commands``), so none of it is paid until ``otto cache ...``
actually dispatches. ``DEFAULT_MAX_AGE_DAYS`` is the one exception: it has to
be resolvable at THIS module's def-time to serve as ``prune``'s ``--age``
default, and it costs nothing to import early -- ``cache_maintenance`` pulls
in stdlib only (see that module's own docstring).
"""

import time
from typing import TYPE_CHECKING, Annotated

import typer
from rich import box
from rich import print as rprint
from rich.markup import escape
from rich.table import Table

from ..config.cache_maintenance import DEFAULT_MAX_AGE_DAYS

if TYPE_CHECKING:
    from pathlib import Path

    from ..config.cache_maintenance import MaintenanceReport

cache_app = typer.Typer(
    name="cache",
    no_args_is_help=True,
    help="Inspect, clear, and prune otto's per-workspace caches.",
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)

_BYTE_UNITS = ((1024**3, "GB"), (1024**2, "MB"), (1024, "KB"))


def _fmt_bytes(n: int) -> str:
    for div, unit in _BYTE_UNITS:
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n}B"


_AGE_UNITS = ((86400, "d"), (3600, "h"), (60, "m"))


def _fmt_age(seconds: float) -> str:
    whole = int(seconds)
    for div, unit in _AGE_UNITS:
        if whole >= div:
            return f"{whole // div}{unit}"
    return f"{whole}s"


def _print_removed(paths: "list[Path]", *, verb: str = "removed") -> None:
    """Print `clear`'s per-file report: one line per removed file, or "nothing to remove"."""
    if not paths:
        typer.echo("nothing to remove")
        return
    for f in paths:
        typer.echo(f"{verb} {f}")


def _print_prune_report(report: "MaintenanceReport", *, dry_run: bool) -> None:
    """Print the full maintenance-report summary: a line per file, then totals.

    Shared by `prune` and `clear --all` -- both run the same
    `cache_maintenance.prune` engine underneath, so both owe the caller the
    same signal: what was (or, under `dry_run`, would be) removed, whether
    any directory emptied out and went with it, and how many workspaces were
    kept and why. `clear`'s BARE form does not use this: `clear_workspace`
    never touches a directory or an age cutoff, so `dirs_removed` and both
    `retained_*` lists are always empty for it -- a per-file report
    (`_print_removed`) says everything there is to say.

    `dry_run` tenses EVERY clause that describes an action, not just the verb
    printed per file -- a bytes-freed total is exactly as hypothetical as the
    removal it was computed from, so it reads "would free", not "freed".
    """
    verb = "would remove" if dry_run else "removed"
    _print_removed(report.files_removed, verb=verb)

    freed = _fmt_bytes(report.bytes_freed)
    bytes_phrase = f"would free {freed}" if dry_run else f"{freed} freed"
    bits = [f"{len(report.files_removed)} file(s)", bytes_phrase]
    if report.dirs_removed:
        dir_verb = "would be removed" if dry_run else "removed"
        bits.append(f"{len(report.dirs_removed)} dir(s) {dir_verb}")
    # retained_young and retained_nonempty are DISJOINT (see MaintenanceReport's
    # docstring) -- a workspace is counted for exactly ONE cause below, never
    # both, which is what makes this why-split safe to render straight off the
    # two list lengths. The finer split the spec defers is INSIDE
    # retained_nonempty (env/ virtualenv vs. a file this process could not
    # delete) -- that one genuinely needs a report change, since
    # MaintenanceReport doesn't distinguish the two causes today.
    young_n = len(report.retained_young)
    nonempty_n = len(report.retained_nonempty)
    kept = young_n + nonempty_n
    if kept:
        why = []
        if young_n:
            why.append(f"{young_n} young")
        if nonempty_n:
            why.append(f"{nonempty_n} non-empty")
        bits.append(f"{kept} workspace(s) kept: {', '.join(why)}")
    typer.echo(", ".join(bits))


@cache_app.command("info")
def info() -> None:
    """List every workspace's cache footprint, oldest cache first."""
    from ..config.cache_maintenance import iter_workspaces
    from ..config.home import otto_home
    from ..models.settings import OttoEnvSettings

    home = otto_home()
    # Mirrors otto_home()'s OWN condition for "did $OTTO_HOME determine this"
    # exactly -- OttoEnvSettings().home is not None (env_ignore_empty=True
    # means an empty OTTO_HOME counts as unset, same as otto_home() sees it).
    # A parallel presence-in-environ check here could drift from otto_home()'s
    # the moment either changes; this stays coupled to the one source of truth.
    origin = "from $OTTO_HOME" if OttoEnvSettings().home is not None else "default"
    typer.echo(f"home: {home} ({origin})")

    workspaces = iter_workspaces(home)
    if not workspaces:
        typer.echo("no cached workspaces")
        return

    now = time.time()
    table = Table(box=box.ROUNDED)
    table.add_column("key")
    table.add_column("cache size", justify="right")
    table.add_column("cache age", justify="right")
    table.add_column("env?")
    table.add_column("extra entries", justify="right")

    total_bytes = 0
    over_threshold = 0
    for ws in workspaces:
        total_bytes += ws.cache_bytes
        # The OLDEST file, not the newest: `prune` decides per file, so a
        # workspace with one fresh cache file and one stale one is still
        # something a prune would act on. Reporting the newest file's age
        # here would under-report what "over threshold" actually means.
        if ws.oldest_cache_mtime is None:
            age_cell = "—"
        else:
            age = now - ws.oldest_cache_mtime
            age_cell = _fmt_age(age)
            if age > DEFAULT_MAX_AGE_DAYS * 86400:
                over_threshold += 1
        table.add_row(
            escape(ws.key),
            _fmt_bytes(ws.cache_bytes),
            age_cell,
            "yes" if ws.has_env else "",
            str(ws.extra_entries),
        )
    rprint(table)
    typer.echo(
        f"{len(workspaces)} workspace(s), {_fmt_bytes(total_bytes)} total, "
        f"{over_threshold} older than {DEFAULT_MAX_AGE_DAYS:g}d"
    )


@cache_app.command("clear")
def clear(
    all_workspaces: Annotated[
        bool,
        typer.Option("--all", help="Clear every workspace's cache files, not just this one's."),
    ] = False,
) -> None:
    """Clear this workspace's cache files, or every workspace's with --all.

    Bare, this never removes the workspace directory itself -- it must keep
    existing regardless of what else lives in it. ``--all`` clears every
    workspace's cache files unconditionally (no age check) and removes a
    workspace directory once emptying it leaves nothing behind; an ``env/``
    virtualenv always keeps its directory.
    """
    if all_workspaces:
        from ..config.cache_maintenance import prune as run_prune
        from ..config.home import otto_home

        report = run_prune(otto_home(), age_blind=True)
        # The full summary, not just the per-file lines: `--all` can rmdir a
        # workspace directory outright, and printing only `files_removed`
        # left that with zero signal -- a directory can vanish and the
        # operator is never told.
        _print_prune_report(report, dry_run=False)
    else:
        from ..config.cache_maintenance import clear_workspace
        from ..config.home import workspace_home

        report = clear_workspace(workspace_home())
        _print_removed(report.files_removed)


@cache_app.command("prune")
def prune(
    age: Annotated[
        float,
        typer.Option(
            "--age",
            help=f"Cache files older than this many days are pruned "
            f"(default {DEFAULT_MAX_AGE_DAYS:g}).",
        ),
    ] = DEFAULT_MAX_AGE_DAYS,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would be pruned without removing anything."),
    ] = False,
) -> None:
    """Remove cache files older than --age days across every workspace."""
    from ..config.cache_maintenance import prune as run_prune
    from ..config.home import otto_home

    report = run_prune(otto_home(), max_age_days=age, dry_run=dry_run)
    _print_prune_report(report, dry_run=dry_run)
