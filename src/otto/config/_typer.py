"""The one place otto reaches into Typer's private internals.

Typer vendors click as ``typer._click`` and keeps its completion machinery in
underscore modules. Upstream treats all of them as private: "nothing in the
module ``typer/_click`` ... should be used directly by users and all of it is
subject to change in the near future", and a change there is "not considered
to be 'breaking'" (fastapi/typer#1831). Typer 0.29 proved it by deleting
``typer._click.types`` and ``typer._types``, which crashed every otto command
at import.

So every private name otto needs is imported HERE and nowhere else; the
ast-grep rule ``typer-internals-through-adapter`` refuses, anywhere else in
``src/``, ``tests/`` or ``scripts/``, a ``typer._*`` import, an underscore
name of any typer module (imported, as an attribute, or through
``getattr``), and a ``"typer._..."`` module path in a string. A Typer release
that moves one of these names then breaks this one file, and the import
error names it. Each name below says why no public spelling serves and
whether upstream has been asked for one; drop it when Typer publishes one.
A test- or script-only name lives in the twin,
``tests/_fixtures/typer_internals.py``.

What otto may use directly is Typer's public surface: ``typer``,
``typer.core`` (``TyperGroup``, ``TyperCommand``, ``TyperOption``,
``TyperArgument``), ``typer.models``, ``typer.testing``, and the
non-underscore converters of ``typer.main`` (``get_command``, ``get_group``,
``get_command_name``, ``get_install_completion_arguments``,
``solve_typer_info_help``). The ``typer.main`` converters are undocumented
but carry no underscore, so the rule leaves them alone.

Known reaches no static rule sees, each kept on purpose:

* :mod:`otto.lifecycle` swaps ``visible_prompt_func`` in the module that
  defined the prompt it is handed (``prompt.__module__``), which for
  ``typer.prompt`` is the vendored ``termui``. lifecycle sits below
  otto.config, so it cannot import this module; it names no Typer module.
* ``otto.cli.main``'s root group reads ``ctx._protected_args`` in
  ``parse_args`` (Typer stores the pending subcommand there and has no
  public accessor) and overrides click's ``_main_shell_completion`` hook.
  Both run on every invocation, where importing this module would cost
  every command its file operations.
* ``otto._shim_complete`` mirrors the vendored parser without importing it;
  the differential test (``tests/unit/shim/test_differential.py``) holds the
  two together.

Importing this module imports nothing Typer has not already loaded:
``import typer`` loads every module named below.
"""

from typing import Any

# Click's command, context and parameter base classes. Typer exposes its own
# subclasses (``typer.core``), but ``TyperCommand.context_class`` is the
# vendored base ``Context``, and ``typer.Context`` is a subclass no command
# instantiates; an annotation that must accept what Typer really hands over
# names the base. No upstream request yet (#1118).
from typer._click.core import Command, Context, Parameter

# Usage errors otto raises or catches itself. Typer re-exports only
# ``BadParameter``, ``Exit`` and ``Abort`` (discussion fastapi/typer#1832 asks
# for more).
from typer._click.exceptions import NoSuchOption, UsageError

# The running command's context, for code that is not handed one (a leaf whose
# signature is the user's). Typer has no public accessor; ``typer.main`` binds
# the name only as an incidental import of its own, not as an export. No
# upstream request yet (#1118).
from typer._click.globals import get_current_context

# The completion-class registry, which otto wraps so a completer's own output
# never reaches the shell. Typer registers its classes here and publishes no
# way to look one up or replace it. No upstream request yet (#1118).
from typer._click.shell_completion import (
    ShellComplete,
    add_completion_class,
    get_completion_class,
)

# The shells Typer completes for, one completion class each. ``typer.completion``
# binds the name only as an incidental import of its own, not as an export. No
# upstream request yet (#1118).
from typer._completion_shared import Shells

__all__ = [
    "Command",
    "Context",
    "NoSuchOption",
    "Parameter",
    "ShellComplete",
    "Shells",
    "UsageError",
    "add_completion_class",
    "autocompletion_of",
    "get_completion_class",
    "get_current_context",
]


def autocompletion_of(param: Parameter) -> Any | None:
    """Return the ``autocompletion=`` callable behind *param*, or ``None`` when it has none.

    Typer keeps it in the private ``_custom_shell_complete`` attribute, wrapped
    twice: an ``autocompletion=`` function becomes a closure over a variable
    named ``autocompletion`` (itself possibly a ``functools.wraps`` wrapper).
    A raw ``shell_complete=`` callable is returned as it is.
    """
    compat = getattr(param, "_custom_shell_complete", None)
    if compat is None:
        return None
    code = getattr(compat, "__code__", None)
    if code is None:  # a raw shell_complete= callable need not be a function
        return compat
    for name, cell in zip(code.co_freevars, compat.__closure__ or (), strict=False):
        if name == "autocompletion":
            wrapper = cell.cell_contents
            return getattr(wrapper, "__wrapped__", wrapper)
    return compat
