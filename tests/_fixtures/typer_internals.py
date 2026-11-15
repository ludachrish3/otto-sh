"""Typer's private internals that only tests import: the test-side twin of ``otto.config._typer``.

Product code takes every private Typer name from :mod:`otto.config._typer`. A
few tests pin otto against Typer's vendored click more closely than any
product code does: how click probes a stream before its first echo, which
reader its prompt calls, how its completion registry is filled, which command
the test runner invokes. Those names live here (a script that needs one takes
it from here too), and the ast-grep rule ``typer-internals-through-adapter`` exempts
this file and ``otto.config._typer`` only. A test that needs a name the product
also uses imports it from ``otto.config._typer``, not from here.
"""

from contextlib import AbstractContextManager
from typing import Any
from unittest import mock

import typer.testing

# The stream probes click makes before its first echo (``_default_text_stderr``,
# ``_is_binary_writer``, ``_find_binary_writer``): the queued-stream tests check
# otto's console proxy answers them the way a real stream would.
from typer._click import _compat as click_compat

# The module whose ``visible_prompt_func`` ``typer.prompt`` reads a line with.
# The product reaches it too, without importing it: ``otto.lifecycle`` swaps
# the reader in whatever module defined the prompt it is handed.
from typer._click import termui

# Registers Typer's completion classes in click's registry, which a real
# ``otto`` invocation does at startup; the differential test builds the bash
# completer from that registry.
from typer._completion_classes import completion_init


def runner_invokes(cmd: Any) -> AbstractContextManager[Any]:
    """Make ``typer.testing.CliRunner.invoke`` run *cmd*, whatever app it is handed.

    ``CliRunner.invoke``'s only use of its ``app`` is the private
    ``typer.testing._get_command(app)``; substituting the command there keeps
    every other runner behavior Typer's own. Typer publishes no way to invoke
    a click command it did not build itself through its runner.
    """
    return mock.patch.object(typer.testing, "_get_command", return_value=cmd)


__all__ = ["click_compat", "completion_init", "runner_invokes", "termui"]
