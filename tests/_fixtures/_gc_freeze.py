"""Run every in-process pytest session with the outer heap frozen, thawed when it ends.

**The cost.** Every full (oldest-generation) collection walks every container
object the process holds. After pytest's collection phase an xdist worker
holds about a million: the collected item tree, every imported module,
every fixture definition. A test that runs pytest in-process (``pytester``,
``pytest.main``, ``otto test`` driven in-process) pays that walk again and
again inside the nested session: pytest's own unraisable-exception cleanup
runs a full ``gc.collect()`` when each nested session ends, and the nested
run allocates enough to trigger more. Measured on a gate-sized heap,
``tests/unit/suite/test_run_sessions.py`` spent 60 s of a 72 s run in full
collections.

**The fix: a window, not a session-long freeze.** :func:`frozen_outer_heap`
calls :func:`gc.freeze` as a nested session starts, which moves every object
the process holds into the permanent generation (the nested session's full
collections then skip them), and as it ends calls :func:`gc.unfreeze`, which
puts every one of them back, then runs ONE full collection. :func:`install`
wraps ``pytest.main`` and the ``main`` pytester calls, so every nested session
runs inside one window, and puts the originals back when the session that
installed them ends (its config's cleanup): a later top-level session in the
same process runs as unfrozen as the first. A nested session's config finds
the entry points already wrapped and neither wraps them again nor restores
them. A window inside a window (a nested session that starts another) does
nothing: the outermost owns the freeze. Measured on the same heap, the file
above spends 44 s instead of 72 s, of which the closing collections are
about 30 s.

**How a window knows one is open.** The open window is recorded on the
collector itself (:data:`WINDOW_OPEN`, an attribute present on the :mod:`gc`
module only while a window is open; :func:`window_open` reads it), not in this
module, because a nested session may import a fresh copy of this module and
every copy shares the one :mod:`gc` module. Nested sessions normally call the
outer module's wrappers anyway (:func:`install` leaves an already-wrapped entry
point alone), but a direct :func:`frozen_outer_heap` call from a fresh copy
inside an open window is safe too: it sees the flag and does nothing, where a
second freeze and thaw would unfreeze the outer window's heap halfway through
its session. The freeze count cannot serve as the guard: CPython 3.12 freezes
its own startup heap (``gc.get_freeze_count()`` is about 375 in a bare
interpreter, 0 on 3.10, 3.11, 3.13 and 3.14), and a guard relative to a
startup count captured at import would be captured afresh, mid-window, by a
fresh copy of this module. The permanent generation is all-or-nothing, so on
3.12 a window's :func:`gc.unfreeze` also thaws those startup objects; that is
harmless (they are ordinary long-lived objects the collector walks on every
other interpreter) and is accepted, not a bug to fix. So "unfrozen" means no
window is open and the freeze count is no greater than it was before any
window opened, never a freeze count of 0.

**What a window delays, and why it hides nothing.** While the heap is frozen,
the cyclic collector does not look at anything that existed when the window
opened. So an object that existed then and became cyclic garbage during the
nested session is not reclaimed during it, and neither is anything only that
garbage refers to, including objects the nested session created (a cycle that
runs through a frozen container). Their finalizers (``__del__``, weakref
callbacks, the ``ResourceWarning`` of an unclosed file or socket) are delayed
to the window's closing collection, never dropped and never left for later:
they run before ``pytest.main`` returns, inside the outer test that started
the nested session, where the outer session's unraisable and leak guards see
them and name that test. A session-long freeze would have dropped them for
good, and a thaw without the closing collection would have left them for
whichever later collection came first, naming the wrong test or none, which
is why the freeze is a window that ends in a collection. Everything the
nested session creates and drops on its own is collected inside it exactly as
before, so its own unraisable and leak reporting is unchanged.

Kept as an importable helper, not inlined in ``conftest.py``, so
``tests/unit/test_gc_freeze.py`` can drive it with a recording double.
"""

import contextlib
import gc
from collections.abc import Callable, Iterator
from typing import Any, Protocol

import pytest

WINDOW_OPEN = "otto_test_frozen_window_open"
"""The attribute a window sets on its collector while it is open, and removes when it closes."""


class GcControl(Protocol):
    """The three :mod:`gc` functions a window uses; the :mod:`gc` module itself satisfies it."""

    def freeze(self) -> None:
        """Move every tracked object into the permanent generation."""
        ...

    def unfreeze(self) -> None:
        """Move the permanent generation back into the oldest one."""
        ...

    def collect(self) -> int:
        """Run a full collection."""
        ...


def window_open(collector: GcControl = gc) -> bool:
    """Whether a window (from any copy of this module) has *collector*'s heap frozen."""
    return bool(getattr(collector, WINDOW_OPEN, False))


@contextlib.contextmanager
def frozen_outer_heap(collector: GcControl = gc) -> Iterator[bool]:
    """Freeze the heap for the block, then thaw and collect, unless a window is already open.

    Yields whether this call froze (and so will thaw). The thaw and the
    collection run however the block ends. When the block raised, its
    exception is the one that propagates: a failing closing collection
    cannot replace it.
    """
    if window_open(collector):
        yield False
        return
    try:
        collector.freeze()
        setattr(collector, WINDOW_OPEN, True)
        yield True
    except BaseException:
        _thaw(collector)
        with contextlib.suppress(Exception):
            collector.collect()
        raise
    _thaw(collector)
    collector.collect()


def _thaw(collector: GcControl) -> None:
    """Unfreeze *collector*'s heap and remove the open mark, even if the unfreeze fails.

    Safe when the freeze never happened or the mark was never set (an
    interrupt between them): an unfreeze of an empty permanent generation
    does nothing, and an absent mark stays absent.
    """
    try:
        collector.unfreeze()
    finally:
        with contextlib.suppress(AttributeError):
            delattr(collector, WINDOW_OPEN)


class FrozenWindow:
    """A pytest entry point (``pytest.main``) that runs inside :func:`frozen_outer_heap`."""

    freezes_the_outer_heap = True
    """Marks an entry point already wrapped, across fresh imports of this module."""

    def __init__(self, run: "Callable[..., Any]") -> None:
        self.run = run

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Run the wrapped entry point with the outer heap frozen."""
        with frozen_outer_heap():
            return self.run(*args, **kwargs)


def install(config: pytest.Config) -> None:
    """Wrap ``pytest.main`` and the ``main`` pytester calls in a window for *config*'s session.

    Each entry point not already wrapped is wrapped, and *config*'s cleanup
    puts back exactly what this call wrapped (if nothing replaced it since).
    An entry point already wrapped (an outer session's) is left alone.
    """
    import _pytest.pytester

    for owner in (pytest, _pytest.pytester):
        current = owner.main
        if getattr(current, "freezes_the_outer_heap", False):
            continue
        window = FrozenWindow(current)
        owner.main = window
        config.add_cleanup(_restorer(owner, window))


def _restorer(owner: Any, window: FrozenWindow) -> "Callable[[], None]":
    """A cleanup that puts *window*'s wrapped entry point back on *owner*."""

    def restore() -> None:
        if owner.main is window:
            owner.main = window.run

    return restore
