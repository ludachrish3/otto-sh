"""Pure maintenance layer for otto's per-workspace caches: walk, clear, prune.

``otto_home()`` shards by :func:`otto.config.home.workspace_key`, and every
key directory holds up to two REBUILDABLE cache files
(:data:`otto.config.completion_cache.CACHE_FILENAME`,
:data:`otto.config.remote_completion_cache.REMOTE_CACHE_FILENAME`) and
possibly an ``env/`` directory that is a REAL virtualenv from ``otto env
create``. That venv must survive any maintenance pass by construction, not by
a case that happens to notice it -- so this module never removes a directory
except with :func:`os.rmdir <pathlib.Path.rmdir>` (via ``Path.rmdir``), which
*refuses* to touch a non-empty directory. ``shutil.rmtree`` never appears
here; if that ever changes, an env-bearing workspace could vanish trunk and
venv together, and the boundary test in this module's test suite catches
exactly that mutation.

The second half of the safety argument is the matcher: :data:`_KEY_RE` is the
one place that decides whether a directory under ``home`` is a workspace this
module may touch at all. It is intentionally narrow (an 8-hex-char hash, a
dash, then a slug -- the live format from :func:`workspace_key
<otto.config.home.workspace_key>`) rather than "everything that isn't
special-cased", so a stray directory under ``~/.otto`` (a future cache, an
operator's own notes) is never a candidate just because nothing rules it out
first. Candidates come from :func:`Path.iterdir` with an explicit
``is_symlink()`` guard -- a symlinked workspace name is never followed, so a
prune can't be tricked into deleting cache files somewhere else on disk.

The third leg is that every filesystem call that can fail for reasons outside
this module's control -- another process's permissions, a directory sitting
where a cache file's name is expected -- is caught locally and never aborts
the walk. `prune` and `clear_workspace` only ever report a file as removed
*after* the removal actually succeeded; `iter_workspaces` omits a workspace it
can't read rather than raising out of what would otherwise be a clean listing
of every other workspace. One locked-down or malformed workspace must never
hide every workspace after it just because directory iteration order is
arbitrary.

Every public function here takes ``home: Path`` (or a workspace ``Path``)
explicitly and never calls :func:`otto.config.home.otto_home`. That keeps
this module testable against a fake ``tmp_path`` home with no monkeypatching,
and the one caller that must resolve the *live* home --
``otto cache`` (the CLI layer, ``otto.cli.cache``) -- stays the only place that does.
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_KEY_RE = re.compile(r"[0-9a-f]{8}-.+")
"""Matches a live `workspace_key` directory name: 8 hex chars, dash, slug.

The boundary of what this module will ever touch -- see the module
docstring. Kept narrow on purpose: widening it to something like `.+` is
exactly the mutation `test_prune_ignores_non_matching_names` exists to catch.
"""

CACHE_FILE_NAMES = ["completion_cache.json", "remote_completion_cache.json"]
"""The two prune-target filenames, one per workspace directory.

Written as literals (not `[CACHE_FILENAME, REMOTE_CACHE_FILENAME]`) so this
list reads standalone. Deliberately NOT pinned by a module-level `assert`:
that would be elided entirely under `python -O`, silently disarming the one
check that would catch this list drifting from its two source constants.
`test_cache_file_names_matches_the_source_constants` in this module's test
suite is the pin instead -- it runs every time, `-O` or not.
"""

_ENV_DIRNAME = "env"

DEFAULT_MAX_AGE_DAYS = 60.0
"""`prune`'s own default cutoff, and the single source for the CLI's `--age`
default and its `info` "over threshold" reporting -- a second literal `60.0`
in the CLI would drift from this one silently the next time either changes."""


@dataclass(frozen=True)
class WorkspaceInfo:
    """A snapshot of one workspace directory's cache footprint."""

    path: Path
    """Absolute path to the workspace directory."""

    key: str
    """The `workspace_key` directory name (``path.name``)."""

    cache_bytes: int
    """Total size of the cache files present, in bytes."""

    newest_cache_mtime: "float | None"
    """The newer of the two cache files' mtimes, or None if neither exists."""

    oldest_cache_mtime: "float | None"
    """The OLDER of the two cache files' mtimes, or None if neither exists.

    This is the number a maintenance pass actually acts on: `prune` decides
    per FILE, not per workspace, so a workspace with one fresh cache file and
    one stale one still loses the stale one even though `newest_cache_mtime`
    alone would read as "recently used". `otto cache info` reports
    age and "would prune" counts against THIS field for exactly that reason.
    """

    has_env: bool
    """Whether an `env/` virtualenv directory is present."""

    extra_entries: int
    """Count of entries in the workspace that are neither a cache file nor `env/`."""


@dataclass
class MaintenanceReport:
    """What a maintenance pass (`clear_workspace` or `prune`) did or would do.

    `retained_young` and `retained_nonempty` are DISJOINT -- a workspace
    never appears in both, and summing their lengths never double-counts a
    directory. `retained_young` holds every workspace keeping at least one
    cache file for being under the age cutoff, full stop. `retained_nonempty`
    holds only workspaces NOT already in `retained_young` whose directory
    survived an attempted removal (an `env/` virtualenv, most often, but
    also a file this process failed to unlink). A workspace `prune` never
    had any reason to touch at all -- no cache files present, nothing to
    remove or retain -- appears in NEITHER list.
    """

    files_removed: "list[Path]" = field(default_factory=list)
    """Cache files removed (or, under `dry_run`, that WOULD be removed)."""

    dirs_removed: "list[Path]" = field(default_factory=list)
    """Workspace directories removed because emptying them left nothing behind."""

    bytes_freed: int = 0
    """Total size of `files_removed`, in bytes."""

    retained_young: "list[Path]" = field(default_factory=list)
    """Workspaces with at least one cache file kept because it's under the age cutoff."""

    retained_nonempty: "list[Path]" = field(default_factory=list)
    """Workspaces (not already in `retained_young`) whose directory survived
    an attempted removal -- an `env/` virtualenv, most often, but also a
    file this process failed to unlink (permissions, or a directory sitting
    where a cache file's name was expected)."""


def _dedupe(paths: "list[Path]") -> "list[Path]":
    """Return *paths* with duplicates removed, keeping first-seen order."""
    seen: "set[Path]" = set()
    out: "list[Path]" = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _candidates(home: Path) -> "list[Path]":
    """Workspace directories directly under *home* that match `_KEY_RE`.

    A missing *home* is an empty result, not an error -- otto's home is
    lazily created and a maintenance pass run before anything ever wrote to
    it has nothing to do. Symlinked entries are never candidates: following
    one would let a prune reach files outside `home` entirely.
    """
    try:
        entries = list(home.iterdir())
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if entry.is_dir() and not entry.is_symlink() and _KEY_RE.fullmatch(entry.name)
    ]


def clear_workspace(workspace: Path) -> MaintenanceReport:
    """Unconditionally remove both cache files in *workspace* -- one directory only.

    No age check, and no `rmdir`: this is the engine behind clearing the
    CURRENT workspace's cache (`otto cache clear`), which must keep
    existing regardless of what else lives in it. A file this process can't
    actually remove (permissions, or a directory sitting where a cache
    file's name is expected) is left exactly as it was and never appears in
    `files_removed` -- the report only ever claims what really happened.
    """
    report = MaintenanceReport()
    for name in CACHE_FILE_NAMES:
        f = workspace / name
        try:
            st = f.lstat()
        except OSError:
            continue
        try:
            f.unlink(missing_ok=True)
        except OSError:
            continue
        report.files_removed.append(f)
        report.bytes_freed += st.st_size
    return report


def iter_workspaces(home: Path) -> "list[WorkspaceInfo]":
    """Return one `WorkspaceInfo` per workspace directory under *home*.

    Sorted oldest-cache-first (by `oldest_cache_mtime`, ascending), with
    `None` mtimes -- a workspace with no cache files at all -- sorted last,
    since there is no age to compare them by. Sorting on `oldest_cache_mtime`
    rather than `newest_cache_mtime` is deliberate: `prune` decides per FILE
    (see its own docstring), so the STALEST file in a workspace is what makes
    it prune-worthy, and a caller sorting/rendering by "how prune-worthy is
    this workspace" needs the two to agree -- `otto cache info`
    renders `oldest_cache_mtime` in its age column, and a workspace's rank in
    this list must match the number printed next to it, not a different one.

    A workspace this process cannot read (permissions, most often) is
    OMITTED from the result rather than raising or appearing as a degraded
    record. This is the read-only side of a maintenance pass --
    `otto cache info` walks every workspace to summarize it -- and one
    unreadable directory must never crash a listing that would otherwise
    show every other workspace fine.
    """
    infos = []
    for ws in _candidates(home):
        try:
            cache_bytes = 0
            newest_mtime: "float | None" = None
            oldest_mtime: "float | None" = None
            for name in CACHE_FILE_NAMES:
                f = ws / name
                try:
                    st = f.lstat()
                except OSError:
                    continue
                cache_bytes += st.st_size
                if newest_mtime is None or st.st_mtime > newest_mtime:
                    newest_mtime = st.st_mtime
                if oldest_mtime is None or st.st_mtime < oldest_mtime:
                    oldest_mtime = st.st_mtime
            has_env = (ws / _ENV_DIRNAME).is_dir()
            extra_entries = sum(
                1
                for entry in ws.iterdir()
                if entry.name not in CACHE_FILE_NAMES and entry.name != _ENV_DIRNAME
            )
        except OSError:
            continue
        infos.append(
            WorkspaceInfo(
                path=ws,
                key=ws.name,
                cache_bytes=cache_bytes,
                newest_cache_mtime=newest_mtime,
                oldest_cache_mtime=oldest_mtime,
                has_env=has_env,
                extra_entries=extra_entries,
            )
        )
    infos.sort(key=lambda i: (i.oldest_cache_mtime is None, i.oldest_cache_mtime or 0.0))
    return infos


def prune(
    home: Path,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    dry_run: bool = False,
    age_blind: bool = False,
) -> MaintenanceReport:
    """Remove cache files older than *max_age_days* across every workspace under *home*.

    ``age_blind=True`` drops the age check entirely -- every cache file goes
    regardless of mtime -- which is the engine behind
    ``otto cache clear --all``. ``dry_run=True`` reports exactly what would
    happen (including which directories would empty out and get `rmdir`'d)
    without touching the filesystem.

    A workspace directory is `rmdir`'d -- never `rmtree`'d -- once emptying
    it of cache files leaves nothing behind. The safety here is not a
    pre-check that predicts emptiness and only then calls `rmdir`: it is
    calling `rmdir` UNCONDITIONALLY (outside `dry_run`, and only for a
    workspace that actually had a cache file to act on) and trusting the
    OSError it raises on a non-empty directory. An `env/` virtualenv makes
    that call fail every time, so an env-bearing workspace always survives
    with its cache files gone and the directory intact. Swap that call for
    `shutil.rmtree` and this stops being true -- `rmtree` does not refuse a
    non-empty directory, it deletes it, env and all.

    A single file this process can't remove (permissions, or a directory
    sitting where a cache file's name is expected) is caught locally: it is
    never counted in `files_removed`/`bytes_freed`, and the walk moves on to
    the next workspace rather than aborting -- directory iteration order is
    arbitrary, so an error early in the walk must never silently skip every
    workspace after it.
    """
    cutoff = time.time() - max_age_days * 86400
    report = MaintenanceReport()
    for ws in _candidates(home):
        touched = False
        young_here = False
        removed_names_here: "set[str]" = set()
        for name in CACHE_FILE_NAMES:
            f = ws / name
            try:
                st = f.lstat()
            except OSError:
                continue
            touched = True
            if not (age_blind or st.st_mtime < cutoff):
                report.retained_young.append(ws)
                young_here = True
                continue
            if dry_run:
                report.files_removed.append(f)
                report.bytes_freed += st.st_size
                removed_names_here.add(name)
                continue
            try:
                f.unlink(missing_ok=True)
            except OSError:
                continue  # left in place; the rmdir attempt below will see it
            report.files_removed.append(f)
            report.bytes_freed += st.st_size
            removed_names_here.add(name)
        if not touched:
            continue  # nothing here for this pass to act on either way
        if dry_run:
            emptied = not [
                entry.name for entry in ws.iterdir() if entry.name not in removed_names_here
            ]
            if emptied:
                report.dirs_removed.append(ws)
            elif not young_here:
                report.retained_nonempty.append(ws)
            continue
        try:
            ws.rmdir()  # refuses non-empty -- an env/ dir always survives
        except OSError:
            if not young_here:
                report.retained_nonempty.append(ws)
        else:
            report.dirs_removed.append(ws)
    report.retained_young = _dedupe(report.retained_young)
    report.retained_nonempty = _dedupe(report.retained_nonempty)
    return report
