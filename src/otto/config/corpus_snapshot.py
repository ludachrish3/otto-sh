"""One stat per path, for the length of a completion-cache rebuild.

A rebuild asks about the same files from two places: the ``names`` section's
digest and the shim's stored stat triples. Each used to stat every key path
itself, which is invisible on a local disk and a round trip per path on a
network filesystem.

Inside :func:`corpus_snapshot`, :func:`stat` answers each question once and
replays the answer, so every consumer keeps its own logic and only the I/O is
shared. Outside a scope it is the plain call it replaces, so a caller that
never opens one (a warm TAB, a library user) behaves exactly as before.

The scope is a context variable, not module state: it ends with the ``with``
block, so nothing it cached can outlive the rebuild that asked. Within a scope
the answers are a consistent snapshot. A file that changes mid-rebuild is seen
as it was at first sight, and the next run's digest sees the change, which is
the safe direction (a miss, never a stale hit).

What a path's stat says comes in three classes (:func:`classify`): a
``stat_result``; ``None``, absent (``ENOENT`` or ``ENOTDIR``); or
:class:`Unreadable`, any other ``OSError`` (``ELOOP``, ``EACCES``, ...). The
one rule for "absent", shared by the settings loader, the cache writer's
digest and stored stat triples, and mirrored by the console-script shim's
stat pass: a key that is still unreadable is unchanged, not missing.

The name is historical: the scope once also memoized the walk of the test
corpus a rebuild made. A rebuild no longer reads the corpus, and what is
left is a ``stat`` memo.
"""

import contextlib
import contextvars
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

ABSENT_ERRORS = (FileNotFoundError, NotADirectoryError)
"""The stat errors that mean the path is absent; every other ``OSError`` is :class:`Unreadable`."""


@dataclass(frozen=True)
class Unreadable:
    """A path whose stat failed for a reason other than absence (``ELOOP``, ``EACCES``, ...)."""

    error: OSError
    """The error the stat raised."""


def classify(path: "Path | str") -> "os.stat_result | Unreadable | None":
    """Stat *path* once: its ``stat_result``, ``None`` when absent, or :class:`Unreadable`.

    Not memoized; :func:`observe` is the memoized form.
    """
    try:
        # `os.stat`, not `Path.stat()`: a monkeypatch of `os.stat` (the test
        # harness's way to count calls, since there is no stat audit event)
        # must actually be observed here, and on 3.10 `pathlib` binds its
        # accessor at import time, so a `Path.stat()` call would not see it.
        return os.stat(path)  # noqa: PTH116
    except ABSENT_ERRORS:
        return None
    except OSError as e:
        return Unreadable(e)


@dataclass
class CorpusSnapshot:
    """What one rebuild has already learned about the filesystem."""

    stats: "dict[str, os.stat_result | Unreadable | None]" = field(default_factory=dict)


_ACTIVE: "contextvars.ContextVar[CorpusSnapshot | None]" = contextvars.ContextVar(
    "otto_corpus_snapshot", default=None
)


@contextlib.contextmanager
def corpus_snapshot() -> Iterator[CorpusSnapshot]:
    """Share stats until the block ends; a nested scope reuses the outer one."""
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


def observe(path: Path) -> "os.stat_result | Unreadable | None":
    """:func:`classify` *path*, memoized inside a scope."""
    snap = _ACTIVE.get()
    key = str(path)
    if snap is not None and key in snap.stats:
        return snap.stats[key]
    result = classify(path)
    if snap is not None:
        snap.stats[key] = result
    return result


def stat(path: Path) -> os.stat_result | None:
    """``os.stat(path)``, or ``None`` when it does not stat (absent or unreadable); memoized."""
    result = observe(path)
    return None if isinstance(result, Unreadable) else result
