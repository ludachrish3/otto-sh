"""Read the context a CLI invocation installed, at the moment Click closes it.

A CLI invocation resets what it installed when Click closes its root context
(``call_on_close``), so once ``CliRunner.invoke`` or ``entry()`` returns,
``get_context()`` answers whatever was installed BEFORE the run. A test that
asserts on the context the invocation built reads it here instead: the
recorder wraps :func:`otto.context.reset_context` and appends the context that
is still installed as each reset runs, then lets the reset proceed.

It sees every ``reset_context`` call made while it is installed --
``open_context``'s and an instruction run's too -- so install it after any
library setup the test does before the invocation it is about.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

    from otto.context import ContextBinding, OttoContext


def record_contexts_at_close(monkeypatch: "pytest.MonkeyPatch") -> "list[OttoContext]":
    """Wrap ``otto.context.reset_context``; return the list the wrapper fills.

    The CLI imports ``reset_context`` from ``otto.context`` inside the function
    that registers the reset, so a patch of the defining module made before the
    invocation is the function Click calls at close.
    """
    import otto.context as context_mod

    real_reset = context_mod.reset_context
    seen: "list[OttoContext]" = []

    def _recording_reset(token: "ContextBinding") -> None:
        installed = context_mod.try_get_context()
        if installed is not None:
            seen.append(installed)
        real_reset(token)

    monkeypatch.setattr("otto.context.reset_context", _recording_reset)
    return seen
