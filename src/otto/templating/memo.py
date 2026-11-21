"""One evaluation per operation: the render memo, and the operation's cancellation route.

:class:`RenderMemo` holds each key's outcome for the memo's life, so an
operation (a run, a ``--list-labs``, a doctor pass, one completion rebuild)
evaluates each entry file once, however many readers ask. There is no
revalidation: an outcome, once settled, is pinned. What a reader derives
from the renders (a source's validated read) is kept beside them
(:meth:`RenderMemo.get_or_derive`), so it is computed once per operation too.

Completion runs its enumeration on a worker thread and gives up on it at a
deadline. :meth:`RenderMemo.cancel` is how the main thread gives up: it
settles the scope's in-progress renders as timed out and refuses the scope
any later render or publication, without waiting for the worker. One
:class:`threading.Lock` guards the memo's state, held only for O(1) state
changes, never across a render, a discovery, a file write or an ``fsync``.

The operation's cancellation reaches every step that may start work through
one operation-local route, :func:`cancellation` and
:func:`current_cancellation`, so no discovery or inventory signature carries
a token. The token is never stored on an inventory, which several projects'
views may share. Only a worker that sets one (inside a fresh
:class:`contextvars.Context`) ever sees it set; every other operation sees
never-cancelled.
"""

import contextvars
import threading
from collections.abc import Callable, Generator, Hashable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from typing_extensions import final

from .errors import RenderCancelled, TemplatingError

if TYPE_CHECKING:
    from ..inventory.config import ProtectedPaths
    from .render import RenderResult


_T = TypeVar("_T")


def _never_cancelled() -> bool:
    """Answer no: the token outside any :func:`cancellation` block."""
    return False


_CANCELLATION: "contextvars.ContextVar[Callable[[], bool]]" = contextvars.ContextVar(
    "otto_render_cancellation", default=_never_cancelled
)


@contextmanager
def cancellation(token: Callable[[], bool]) -> Generator[None, None, None]:
    """Make *token* part of the operation's cancellation while the block runs.

    An enclosing token still counts: inside the block the operation is
    cancelled when either is set. The previous token is restored on any exit.
    """
    outer = _CANCELLATION.get()
    reset = _CANCELLATION.set(lambda: outer() or token())
    try:
        yield
    finally:
        _CANCELLATION.reset(reset)


def current_cancellation() -> bool:
    """Return whether the current operation is cancelled; every step that may start work asks."""
    return _CANCELLATION.get()()


@final
@dataclass(frozen=True, kw_only=True)
class RenderKey:
    """Everything one render's outcome depends on, so equal keys share one evaluation."""

    scope: Path
    """The declaring project's root, the unit :meth:`RenderMemo.cancel` works on."""
    entry: Path
    """The entry file, resolved."""
    import_roots: tuple[Path, ...]
    """The import roots, in search order."""
    inventories: tuple[str, ...]
    """The inventories the render reads."""
    project: str
    """The project name the render is given."""
    protected: "ProtectedPaths"
    """The resolved protected-file catalog the render ran under (the value, never its provider)."""
    library_digest: str
    """The packaged ``otto.libsonnet``'s sha256, :func:`~otto.templating.imports.library_digest`."""


@final
@dataclass(frozen=True, kw_only=True)
class RenderOutcome:
    """How one key's render ended: a result, an error's text, or a timeout."""

    result: "RenderResult | None" = None
    """The rendered result; ``None`` unless the render succeeded."""
    error: "str | None" = None
    """The failure's text, or the timeout's reason; ``None`` for a result."""
    timed_out: bool = False
    """Whether the render was cancelled (its scope's deadline passed) instead of finishing."""


class RenderMemo:
    """The per-operation render memo; see the module docstring.

    A key two threads ask for at once may be rendered by both: the first to
    settle is kept, and the other's outcome is discarded.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outcomes: dict[RenderKey, RenderOutcome] = {}
        self._in_progress: set[RenderKey] = set()
        self._cancelled: dict[Path, str] = {}  # scope -> the reason it was cancelled
        self._derived: dict[Hashable, Any] = {}

    def get_or_derive(self, key: Hashable, derive: "Callable[[], _T]") -> "_T":
        """Return what *derive* computes from this operation's renders, computed once per *key*.

        For a value every reader of the operation would otherwise recompute
        from the same renders: a ``jsonnet`` lab source's validated read,
        keyed by its renders' keys, so its listing, every load, its host
        summaries and the doctor share one parse and one duplicate check.
        *key* must hold everything the value depends on. *derive* runs
        outside the lock; a value it raises is not kept, so the next reader
        derives again (each render it reads is still the memo's one
        evaluation). Of two threads deriving one key at once, the first to
        finish is kept. A kept value is shared: its readers must not mutate
        it.
        """
        with self._lock:
            if key in self._derived:
                return self._derived[key]
        value = derive()
        with self._lock:
            return self._derived.setdefault(key, value)

    def get_or_render(self, key: RenderKey, run: "Callable[[], RenderResult]") -> RenderOutcome:
        """Return *key*'s outcome, calling *run* (outside the lock) when it has none.

        In a cancelled scope it returns a timed-out outcome without calling
        *run*. A :class:`~otto.templating.errors.RenderCancelled` from *run*
        settles as the timed-out outcome, and any other
        :class:`~otto.templating.errors.TemplatingError` as the key's error;
        anything else propagates and leaves the key unsettled.
        """
        with self._lock:
            settled = self._outcomes.get(key)
            if settled is not None:
                return settled
            reason = self._cancelled.get(key.scope)
            if reason is not None:
                return RenderOutcome(error=reason, timed_out=True)
            self._in_progress.add(key)
        try:
            outcome = RenderOutcome(result=run())
        except RenderCancelled as e:
            outcome = RenderOutcome(error=str(e), timed_out=True)
        except TemplatingError as e:
            outcome = RenderOutcome(error=str(e))
        except BaseException:
            with self._lock:
                self._in_progress.discard(key)
            raise
        return self._settle(key, outcome)

    def _settle(self, key: RenderKey, outcome: RenderOutcome) -> RenderOutcome:
        """Pin *outcome* for *key* unless one is already pinned (a timeout, or a faster render)."""
        with self._lock:
            self._in_progress.discard(key)
            return self._outcomes.setdefault(key, outcome)

    def cancel(self, *, scope: Path, reason: str) -> None:
        """Settle *scope*'s in-progress renders as timed out with *reason*, and cancel the scope.

        Nonblocking: it never waits for a worker, so the main thread can call
        it right after giving up on one. A publication the scope accepted
        before this call still completes on the thread that runs it.
        """
        with self._lock:
            timed_out = RenderOutcome(error=reason, timed_out=True)
            for key in [k for k in self._in_progress if k.scope == scope]:
                self._outcomes.setdefault(key, timed_out)
                self._in_progress.discard(key)
            self._cancelled.setdefault(scope, reason)

    def cancelled(self, scope: Path) -> Callable[[], bool]:
        """Return *scope*'s cancellation token, set once :meth:`cancel` has run for it."""

        def token() -> bool:
            with self._lock:
                return scope in self._cancelled

        return token

    def publish(self, *, scope: Path, action: Callable[[], None]) -> bool:
        """Accept a publication for *scope*, then run *action* outside the lock.

        Returns ``False``, running nothing, when the scope is already
        cancelled. Otherwise the publication is accepted (the scope was not
        cancelled while the lock was held), and *action* (the slow
        persistence: a file write and its ``fsync``) runs on the calling
        thread after the lock is released, even if the scope is cancelled
        meanwhile; :meth:`cancel` never waits for it.
        """
        with self._lock:
            if scope in self._cancelled:
                return False
        action()
        return True
