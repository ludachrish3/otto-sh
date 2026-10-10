"""Put back the run state ``set_context`` installs: the context, its policy and its resolver.

``set_context`` installs three ContextVars: the context's own ``_active``, and
the ``otto.invocation`` leaf's ``_POLICY`` and ``_RESOLVER``. A fixture that
undoes its install by restoring ``_active`` alone leaves the other two behind
for every later test on the worker, which then sees a policy and a peer-host
resolver it never installed. :func:`preserved_run_state` saves all three and
restores all three.

It restores with ``ContextVar.set``, never ``reset_context(binding)``: a
binding resets only in the execution context that made it, and the code that
undoes an install (an async fixture's teardown, a test that installed inside a
copied context) need not run in that one.
"""

import contextlib
from collections.abc import Iterator


@contextlib.contextmanager
def preserved_run_state() -> Iterator[None]:
    """Restore the context, run-policy and resolver ContextVars to their values at entry."""
    from otto import context, invocation

    saved = (context._active.get(), invocation._POLICY.get(), invocation._RESOLVER.get())
    try:
        yield
    finally:
        context._active.set(saved[0])
        invocation._POLICY.set(saved[1])
        invocation._RESOLVER.set(saved[2])
