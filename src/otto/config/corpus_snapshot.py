"""One walk and one stat per path, for the length of a completion-cache rebuild.

A rebuild asks about the same test corpus from several places: the ``tests``
section's digest, the static name scan, the shim's stored stat triples, and
the collected-tests fingerprint. Each used to walk the tree and stat every file
itself: four stats per nested test file and five listings per directory. That
is invisible on a local disk and seconds on a network filesystem, where every
one of them is a round trip.

Inside :func:`corpus_snapshot`, :func:`walk`, :func:`stat` and :func:`glob`
answer each question once and replay the answer, so every consumer keeps its
own logic, filtering and ordering, and only the I/O is shared. Outside a scope
they are the plain calls they replace, so a caller that never opens one (a
warm TAB, a library user) behaves exactly as before.

The scope is a context variable, not module state: it ends with the ``with``
block, so nothing it cached can outlive the rebuild that asked. Within a scope
the answers are a consistent snapshot. A file that changes mid-rebuild is seen
as it was at first sight, and the next run's digest sees the change, which is
the safe direction (a miss, never a stale hit).
"""

import contextlib
import contextvars
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

WalkStep = tuple[str, list[str], list[str]]
"""One ``os.walk`` step: ``(root, dirnames after pruning, filenames)``."""


@dataclass
class CorpusSnapshot:
    """What one rebuild has already learned about the filesystem."""

    walks: dict[tuple[str, Callable[[str], bool]], list[WalkStep]] = field(default_factory=dict)
    stats: dict[str, os.stat_result | None] = field(default_factory=dict)
    globs: dict[tuple[str, str], list[Path]] = field(default_factory=dict)


_ACTIVE: "contextvars.ContextVar[CorpusSnapshot | None]" = contextvars.ContextVar(
    "otto_corpus_snapshot", default=None
)


@contextlib.contextmanager
def corpus_snapshot() -> Iterator[CorpusSnapshot]:
    """Share walks and stats until the block ends; a nested scope reuses the outer one."""
    current = _ACTIVE.get()
    if current is not None:
        yield current
        return
    snap = CorpusSnapshot()
    token = _ACTIVE.set(snap)
    try:
        yield snap
    finally:
        _ACTIVE.reset(token)


def stat(path: Path) -> os.stat_result | None:
    """``os.stat(path)``, or ``None`` when it does not stat; memoized inside a scope."""
    snap = _ACTIVE.get()
    key = str(path)
    if snap is not None and key in snap.stats:
        return snap.stats[key]
    try:
        # `os.stat`, not `Path.stat()`: a monkeypatch of `os.stat` (the test
        # harness's way to count calls, since there is no stat audit event)
        # must actually be observed here, and on 3.10 `pathlib` binds its
        # accessor at import time, so a `Path.stat()` call would not see it.
        result: os.stat_result | None = os.stat(path)  # noqa: PTH116
    except OSError:
        result = None
    if snap is not None:
        snap.stats[key] = result
    return result


def walk(top: Path, prune: Callable[[str], bool]) -> list[WalkStep]:
    """Walk *top*, pruned and non-symlink-following; memoized per ``(top, prune)``.

    Same shape and semantics as ``os.walk(top)`` with ``dirs[:] = [d for d in
    dirs if not prune(d)]`` applied at each step (directories named by *prune*
    are neither listed nor descended into, and a symlinked directory is never
    followed) — but built directly on :func:`os.scandir` instead. ``os.walk``
    re-derives each directory's symlink status from its path with
    ``os.path.islink``, one ``lstat`` per directory that throws away the
    ``DirEntry`` :func:`os.scandir` already read it from. Reading that
    ``DirEntry``'s type here instead is free on a filesystem that fills
    ``d_type`` (as a network or local one normally does): a second stat per
    directory, on top of the one this module's callers fold into a digest.

    Returns a fresh list of fresh ``(root, dirs, files)`` tuples — never the
    memoized lists themselves — so a caller free to mutate its result (the
    ``dirs[:] = ...`` pruning idiom this docstring itself describes) cannot
    corrupt a later call's answer inside the same scope.
    """
    snap = _ACTIVE.get()
    key = (str(top), prune)
    if snap is not None and key in snap.walks:
        return [(root, list(dirs), list(files)) for root, dirs, files in snap.walks[key]]
    steps = _walk(os.fspath(top), prune)
    if snap is not None:
        snap.walks[key] = steps
    return [(root, list(dirs), list(files)) for root, dirs, files in steps]


def _walk(top: str, prune: Callable[[str], bool]) -> list[WalkStep]:
    """Top-down, pruned, non-symlink-following walk of *top*, iterative (no recursion depth limit).

    An explicit stack rather than a recursive helper: a nested test tree is
    still bounded by the filesystem, not by Python's call-stack depth, and a
    generated or vendored corpus is exactly the kind of tree that can nest
    deeper than the default recursion limit.
    """
    steps: list[WalkStep] = []
    stack = [top]
    while stack:
        current = stack.pop()
        dirnames: list[str] = []
        filenames: list[str] = []
        descend: list[str] = []
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        is_dir = False
                    if not is_dir:
                        filenames.append(entry.name)
                        continue
                    dirnames.append(entry.name)
                    if prune(entry.name):
                        continue
                    try:
                        is_symlink = entry.is_symlink()
                    except OSError:
                        is_symlink = False
                    if not is_symlink:
                        descend.append(entry.path)
        except OSError:
            continue
        steps.append((current, [d for d in dirnames if not prune(d)], filenames))
        # Reversed: a stack pops last-in-first-out, and pushing in reverse
        # keeps sibling order (as returned by scandir) the same as a
        # recursive top-down walk would have produced.
        stack.extend(reversed(descend))
    return steps


def glob(directory: Path, pattern: str) -> list[Path]:
    """``sorted(directory.glob(pattern))``; memoized inside a scope.

    Returns a fresh list — never the memoized one — so a caller mutating its
    result cannot corrupt a later call's answer inside the same scope.
    """
    snap = _ACTIVE.get()
    key = (str(directory), pattern)
    if snap is not None and key in snap.globs:
        return list(snap.globs[key])
    result = sorted(directory.glob(pattern))
    if snap is not None:
        snap.globs[key] = result
    return list(result)
