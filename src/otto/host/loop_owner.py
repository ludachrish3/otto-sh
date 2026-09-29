"""Which event loop owns a host's connection, and the error for using it from another.

A host's transports and shell sessions are bound to the event loop that opened
them. ``BaseHost._claim_loop`` records that loop as the
host's owner on first use; a later call from a different, still-running loop
raises :class:`HostLoopError` rather than hanging on futures the owning loop
will never drive. A connection whose owning loop has closed is not an error: it
is dropped and the host reconnects on the loop that uses it next.
"""

import asyncio
import weakref

from ..errors import OttoError

LOOP_LABELS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, str]" = (
    weakref.WeakKeyDictionary()
)
"""Human names for event loops, set by whoever creates a loop worth naming.

A name such as ``TestRouter's loop`` or ``the session's loop`` lets a
:class:`HostLoopError` say which scope opened the host; a loop with no entry is
named by its id. Weakly keyed: a label never keeps a closed loop alive.
"""

SESSION_LOOP_LABEL = "the session's loop"
"""The name of a pytest session's own loop, the loop ``otto test`` runs unpinned tests on.

:class:`HostLoopError` recognises it: a host that loop owns stays owned for
the whole run, so the error says so to a narrower-pinned caller.
"""


def loop_label(loop: asyncio.AbstractEventLoop) -> str:
    """Return a loop's registered name, or its id when nobody named it."""
    return LOOP_LABELS.get(loop) or f"event loop {id(loop):#x}"


class HostLoopError(OttoError, RuntimeError):
    """A host was used from an event loop other than the live one that owns its connection.

    Raised by the host's first connection-touching step, before any I/O, in
    place of asyncio's own "attached to a different loop" error or a silent
    wait on a loop that is not running. Closing the host on its owning loop
    (``await host.close()``) releases it for any loop.
    """

    def __init__(
        self,
        host_id: str,
        owner: asyncio.AbstractEventLoop,
        caller: asyncio.AbstractEventLoop,
    ) -> None:
        # The hint is for a narrower pytest loop only. pytest-asyncio's class,
        # module, package and function loops are always labelled (by
        # otto.suite.loops.runner_label); an unlabelled caller, such as a sync
        # test's own asyncio.run() loop, or a nested session's loop is not one.
        caller_label = LOOP_LABELS.get(caller)
        session_hint = (
            "A host that unpinned tests use stays on the session's loop for the whole run; "
            "tests pinned to a narrower loop need hosts only they use. "
            if LOOP_LABELS.get(owner) == SESSION_LOOP_LABEL
            and caller_label not in (None, SESSION_LOOP_LABEL)
            else ""
        )
        super().__init__(
            f"host {host_id!r} is connected on {loop_label(owner)} but was used from "
            f"{loop_label(caller)}. A connection belongs to the event loop that opened it. "
            f"{session_hint}"
            "Pin this test or fixture to the loop that opened the host, or close the host at "
            'the end of the scope that opened it. See "Sharing host connections across tests" '
            "(docs/cookbook/host-scopes.md)."
        )


__all__ = ["LOOP_LABELS", "HostLoopError", "loop_label"]
