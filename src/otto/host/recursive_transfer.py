"""Recursive ``put``/``get``: a directory tree reduced to per-file transfers already handled.

The listing half lives here: the POSIX ``sh`` function every remote POSIX
family runs to enumerate a tree, and the parser for what it prints. No
``find`` — it is not a measured BusyBox applet, and the host's own shell is
the one tool every POSIX family is known to have (``glob`` makes the same
bet). Directory symlinks are reported neither as directories nor as files,
so a walk never descends them; a file symlink is a file (its target's
bytes are what a transfer reads).

The stream is line-oriented, which a name containing a newline would split
in two. The script therefore counts what it printed and the parser refuses
a tree whose line count disagrees with that count — exact, and without
NUL-separated output through an exec channel that may not carry it.

A directory the walker cannot read (permission denied) is still reported as a
directory — it exists — but is NOT descended, and is marked with a separate
``e`` line so the caller learns the listing is incomplete rather than reading
"zero children" as "empty".
"""

import asyncio
import os
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import OttoError
from ..result import Result
from ..utils import Status
from .transfer.base import aggregate_transfer, parse_file_mode


class ListingError(OttoError, ValueError):
    """A remote tree could not be enumerated: the listing failed, or a name cannot ride it."""


@dataclass
class RemoteListing:
    """What one path on the host is, and — for a directory — everything under it."""

    kind: str
    """``"d"`` a directory (walked), ``"f"`` exists but is not a directory, ``"-"`` missing."""
    dirs: list[Path]
    """Every directory under the root, root excluded, as the host printed it."""
    files: list[Path]
    """Every regular file under the root (file symlinks included), as the host printed it."""


_WALK_FN = (
    'w() { for p in "$1"/* "$1"/.[!.]* "$1"/..?*; do '
    '[ -e "$p" ] || [ -L "$p" ] || continue; '
    'if [ -d "$p" ] && [ ! -L "$p" ]; then printf \'d %s\\n\' "$p"; c=$((c+1)); '
    'if [ -r "$p" ] && [ -x "$p" ]; then w "$p"; '
    "else printf 'e %s\\n' \"$p\"; c=$((c+1)); fi; "
    'elif [ -f "$p" ]; then printf \'f %s\\n\' "$p"; c=$((c+1)); fi; '
    "done; }; c=0; "
)


def walk_script(quoted_root: str) -> str:
    """List the tree at *quoted_root* in one exec of the host's own POSIX ``sh``.

    *quoted_root* is already shell-quoted by the caller (``PosixFileOps._q``).
    Prints ``t d``/``t f``/``t -`` first, one ``d <path>``/``f <path>``/``e
    <path>`` line per entry, and ``n <count>`` last. ``[ -d ]`` follows a
    symlink, so a top-level link to a directory is walked as one; inside the
    walk a directory symlink is skipped by the ``! -L`` guard, and a directory
    that fails ``[ -r ] && [ -x ]`` is reported (``e``) but not descended. The
    root itself gets the same probe before it is walked — without it, an
    unreadable root's glob expands to nothing and reads as an empty tree
    instead of a listing this host could not take.
    """
    return (
        _WALK_FN
        + f"if [ -d {quoted_root} ]; then printf 't d\\n'; "
        + f"if [ -r {quoted_root} ] && [ -x {quoted_root} ]; then w {quoted_root}; "
        + f"else printf 'e %s\\n' {quoted_root}; c=$((c+1)); fi; "
        + f"elif [ -e {quoted_root} ] || [ -L {quoted_root} ]; then printf 't f\\n'; "
        + "else printf 't -\\n'; fi; "
        + "printf 'n %s\\n' \"$c\""
    )


_MIN_FRAMED_LINES = 2
"""A framed listing carries at least the ``t`` line and the ``n`` line."""


def parse_listing(text: str, root: Path) -> RemoteListing:
    """Parse :func:`walk_script`'s output for *root*.

    Raises:
        ListingError: the stream is not framed by ``t`` and ``n`` lines, the
            entry count disagrees with the ``n`` line — the signature of a
            name containing a newline — or an ``e`` entry marks a directory
            this host could not read.
    """
    lines = text.splitlines()
    if (
        len(lines) < _MIN_FRAMED_LINES
        or not lines[0].startswith("t ")
        or not lines[-1].startswith("n ")
    ):
        raise ListingError(f"{root}: malformed tree listing ({len(lines)} lines)")
    kind = lines[0][2:]
    try:
        declared = int(lines[-1][2:])
    except ValueError:
        raise ListingError(f"{root}: malformed tree listing (count line {lines[-1]!r})") from None
    entries = lines[1:-1]
    if len(entries) != declared:
        raise ListingError(
            f"{root}: a name in this tree contains a newline ({declared} entries listed, "
            f"{len(entries)} lines read); rename it and retry"
        )
    dirs: list[Path] = []
    files: list[Path] = []
    unreadable: Path | None = None
    for line in entries:
        tag, _, path = line.partition(" ")
        if tag == "d":
            dirs.append(Path(path))
        elif tag == "f":
            files.append(Path(path))
        elif tag == "e":
            if unreadable is None:
                unreadable = Path(path)
        else:
            raise ListingError(f"{root}: malformed tree listing (entry {line!r})")
    if unreadable is not None:
        raise ListingError(f"{root}: cannot read {unreadable}")
    return RemoteListing(kind=kind, dirs=dirs, files=files)


@dataclass
class _Level:
    """One directory of a local tree: where it sits under the root, and its regular files."""

    rel: Path
    files: list[Path]


@dataclass
class _LocalTree:
    """A walked local tree, or the reason it could not be walked."""

    levels: list[_Level]
    error: str | None = None


def _walk_local(root: Path) -> _LocalTree:
    """``os.walk`` under the listing rules.

    No descent into a directory symlink; a file symlink is read by content.
    """
    problems: list[str] = []

    def note(exc: OSError) -> None:
        problems.append(f"{exc.filename}: {exc.strerror}")

    levels: list[_Level] = []
    for dirpath, _dirnames, filenames in os.walk(root, onerror=note, followlinks=False):
        here = Path(dirpath)
        files = [here / n for n in filenames if (here / n).is_file()]
        levels.append(_Level(rel=here.relative_to(root), files=files))
    if problems:
        return _LocalTree(levels=[], error=f"{root}: could not read {'; '.join(problems)}")
    return _LocalTree(levels=levels)


async def _run_levels(
    coros: "list[Coroutine[Any, Any, Result]]", *, concurrent: bool
) -> "list[Result]":
    """Run one ``host.put``/``host.get`` per directory level.

    Together under ``asyncio.gather`` when *concurrent*: every level's files
    then compete for the SAME transfer object's semaphore, so the whole tree
    stays within the protocol's cap while a deep tree of small files fills it
    instead of draining it level by level. In order otherwise. In both modes,
    a raise cancels and drains the siblings before propagating; nothing is
    left running. The drain waits for each sibling level to honour that
    cancellation, so a per-file coroutine that swallows ``CancelledError``
    holds the drain for as long as it takes -- bounded in practice by each
    backend's own cancellation cleanup timeouts.
    """
    if concurrent:
        tasks = [asyncio.ensure_future(coro) for coro in coros]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            # A raise from one level must not leave its siblings' tasks
            # running detached — cancel them, then drain (`return_exceptions`)
            # so their own CancelledError is collected here rather than
            # surfacing later as an unretrieved task exception.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    landed: list[Result] = []
    try:
        for coro in coros:
            # Not a list comprehension: a mid-sequence raise must leave
            # `landed` holding exactly what completed, so the except branch
            # below knows which coroutines were never started.
            landed.append(await coro)  # noqa: PERF401
    except BaseException:
        # A raise mid-sequence must not leave the remaining coroutine
        # objects un-awaited and un-closed (a ResourceWarning, and a leaked
        # generator) — the one that raised is already spent.
        for coro in coros[len(landed) + 1 :]:
            coro.close()
        raise
    return landed


async def _put_one_tree(
    host: Any,
    root: Path,
    dest_root: Path,
    *,
    mode: "int | str | None",
    user: "str | None",
    show_progress: bool,
    concurrent: bool,
) -> Result:
    from .host import is_dry_run

    tree = _walk_local(root)
    if tree.error is not None:
        return Result(Status.Error, msg=tree.error, value={})
    if not is_dry_run():
        made = await host._mkdir_all(  # noqa: SLF001 — the host's declared batch-mkdir hook
            [dest_root / level.rel for level in tree.levels], user=user
        )
        if not made.is_ok:
            return Result(
                Status.Error,
                msg=f"{root}: could not create the destination tree under {dest_root}: {made.msg}",
                value={},
            )
    elif not any(level.files for level in tree.levels):
        return Result(
            Status.NotRun,
            value={},
            msg=f"[DRY RUN] PUT {root} -> {dest_root}: directories only",
        )
    per_file: dict[Path, Result] = {}
    coros = [
        host.put(
            level.files,
            dest_root / level.rel,
            mode=mode,
            user=user,
            show_progress=show_progress,
            concurrent=concurrent,
        )
        for level in tree.levels
        if level.files
    ]
    for landed in await _run_levels(coros, concurrent=concurrent):
        for src, outcome in (landed.value or {}).items():
            per_file[src.relative_to(root)] = outcome
    return aggregate_transfer(per_file)


async def put_tree(
    host: Any,
    src_files: list[Path],
    dest_dir: Path,
    *,
    mode: "int | str | None",
    user: "str | None",
    show_progress: bool,
    concurrent: bool,
) -> Result:
    """Upload *src_files*, walking each directory among them; plain files ride one ordinary put.

    *host* is any family whose non-recursive ``put`` and ``_mkdir_all`` this
    reduces to; the family's own preamble (user validation, destination
    resolution) has already run. Returns the nested shape: top level keyed
    by each source as passed; a directory entry's ``value`` keyed by path
    relative to that directory.

    *mode* is validated FIRST, before any listing or ``_mkdir_all`` — a bad
    mode must refuse the whole tree up front rather than creating the
    destination skeleton and only then failing per level.

    *user* reaches ``_mkdir_all`` as well as the per-level puts: the
    skeleton must be built by the SAME identity that lands the files, or
    every directory is login-owned with ``mkdir``'s default bits and the
    per-file put — authenticated as *user* — is refused by the destination
    it was just given. What that costs per family is the family's own rule:
    a unix host whose term carries no SSH exec channel refuses
    ``exec(user=...)`` by name, so the tree is refused before a byte moves
    rather than half-landed; a container host ignores it and creates the
    directories as ``root`` (the identity ``docker cp`` writes as anyway);
    ``LocalHost`` refuses a non-``None`` *user* upstream, before any of
    this runs.

    *concurrent* fans the levels out together under the backend's own cap.
    """
    check = parse_file_mode(mode)
    if not check.is_ok:
        return aggregate_transfer(
            {src: Result(check.status, msg=check.msg, value={}) for src in src_files}
        )
    plain = [p for p in src_files if not p.is_dir()]
    per_source: dict[Path, Result] = {}
    if plain:
        # One call carrying ALL plain sources: `put_tree` must not change what a
        # plain, non-recursive put does, so every plain source is attempted
        # and this single call exists only so the batch shares one dispatch
        # under the cap, exactly as it would without recursion.
        flat = await host.put(
            plain,
            dest_dir,
            mode=mode,
            user=user,
            show_progress=show_progress,
            concurrent=concurrent,
        )
        per_source.update(flat.value or {})
    for src in src_files:
        if src.is_dir():
            per_source[src] = await _put_one_tree(
                host,
                src,
                dest_dir / src.name,
                mode=mode,
                user=user,
                show_progress=show_progress,
                concurrent=concurrent,
            )
    return aggregate_transfer({src: per_source[src] for src in src_files})


async def _get_one_tree(
    host: Any,
    root: Path,
    listing: RemoteListing,
    dest_root: Path,
    *,
    user: "str | None",
    show_progress: bool,
    concurrent: bool,
) -> Result:
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        for d in listing.dirs:
            (dest_root / d.relative_to(root)).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Result(
            Status.Error,
            msg=f"{root}: could not create the local tree under {dest_root}: "
            f"{exc.filename}: {exc.strerror}",
            value={},
        )
    by_level: dict[Path, list[Path]] = {}
    for f in listing.files:
        by_level.setdefault(f.parent.relative_to(root), []).append(f)
    per_file: dict[Path, Result] = {}
    coros = [
        host.get(
            files, dest_root / rel, user=user, show_progress=show_progress, concurrent=concurrent
        )
        for rel, files in by_level.items()
    ]
    for landed in await _run_levels(coros, concurrent=concurrent):
        for src, outcome in (landed.value or {}).items():
            per_file[src.relative_to(root)] = outcome
    return aggregate_transfer(per_file)


async def get_tree(
    host: Any,
    src_files: list[Path],
    dest_dir: Path,
    *,
    user: "str | None",
    show_progress: bool,
    concurrent: bool,
) -> Result:
    """Download *src_files*, walking each that the host reports as a directory.

    One listing exec per source classifies it — directory, file, or missing
    — so a file source rides one ordinary get with the other files. Under a
    dry run nothing is asked of the host: every source is a single
    ``NotRun`` entry previewing its destination, because a fabricated
    listing would read as "nothing to transfer".

    *concurrent* fans the levels out together under the backend's own cap.
    """
    from .host import is_dry_run

    if is_dry_run():
        return aggregate_transfer(
            {
                src: Result(
                    Status.NotRun,
                    value=dest_dir / src.name,
                    msg=f"[DRY RUN] GET {src} -> {dest_dir / src.name}: tree not enumerated",
                )
                for src in src_files
            }
        )
    per_source: dict[Path, Result] = {}
    plain: list[Path] = []
    for src in src_files:
        try:
            listing = await host._walk_remote(src)  # noqa: SLF001 — the host's declared walk hook
        except ListingError as exc:
            per_source[src] = Result(Status.Error, msg=str(exc), value={})
            continue
        if listing.kind == "-":
            per_source[src] = Result(
                Status.Error, msg=f"{src}: no such file or directory", value={}
            )
        elif listing.kind == "f":
            plain.append(src)
        else:
            per_source[src] = await _get_one_tree(
                host,
                src,
                listing,
                dest_dir / src.name,
                user=user,
                show_progress=show_progress,
                concurrent=concurrent,
            )
    if plain:
        flat = await host.get(
            plain, dest_dir, user=user, show_progress=show_progress, concurrent=concurrent
        )
        per_source.update(flat.value or {})
    return aggregate_transfer({src: per_source[src] for src in src_files})
