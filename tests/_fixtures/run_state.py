"""Put back the run state ``set_context`` installs: the context, its policy and its resolver.

``set_context`` installs three ContextVars: the context's own ``_active``, and
the ``otto.invocation`` leaf's ``_POLICY`` and ``_RESOLVER``. A fixture that
undoes its install by restoring ``_active`` alone leaves the other two behind
for every later test on the worker, which then sees a policy and a peer-host
resolver it never installed. :func:`preserved_run_state` saves all three and
restores all three; :func:`cleared_run_state` also clears all three for the
block, for a test whose premise is that no context is installed; and
:func:`installed_run_state` reads all three, for a test asserting what an
entry point left behind.

It restores with ``ContextVar.set``, never ``reset_context(binding)``: a
binding resets only in the execution context that made it, and the code that
undoes an install (an async fixture's teardown, a test that installed inside a
copied context) need not run in that one.
"""

import contextlib
from collections.abc import Iterator


def installed_run_state() -> list:
    """The installed context, run policy and peer-host resolver, in that order."""
    from otto import context, invocation

    return [
        context.try_get_context(),
        invocation.installed_policy(),
        invocation.installed_resolver(),
    ]


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


@contextlib.contextmanager
def cleared_run_state() -> Iterator[None]:
    """Run the block with no context, no run policy and no resolver; restore all three after.

    "No context" means all three. Clearing ``_active`` alone leaves a policy and
    a peer-host resolver some earlier test or fixture installed, and a host
    with no lab back-reference resolves its hop through that resolver: so a
    test asserting that such a host *cannot* resolve its hop passes or fails
    on whatever ran before it.
    """
    from otto import context, invocation

    with preserved_run_state():
        context._active.set(None)
        invocation._POLICY.set(None)
        invocation._RESOLVER.set(None)
        yield
