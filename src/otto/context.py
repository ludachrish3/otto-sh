"""The run context API: open a run, reach the active one, read its policy and variant.

``from otto.context import open_context`` is the spelling to use, or the root
aliases (``otto.open_context``, ``otto.OttoContext``, ``otto.get_context``,
``otto.try_get_context``). Each name is implemented in another module and
documented there, once:

- :class:`~otto.run_context.OttoContext`, a run's lab, policy and projects, and
  :func:`~otto.run_context.open_context`, which composes the projects and opens
  a run on a lab;
- :func:`~otto.run_context.get_context`, :func:`~otto.run_context.try_get_context`,
  :func:`~otto.run_context.set_context` and :func:`~otto.run_context.reset_context`:
  the active run;
- :class:`~otto.run_context.ProjectContextView`, one project's view of a run;
- :func:`~otto.run_context.variant`, :func:`~otto.run_context.set_variant` and
  :func:`~otto.run_context.reset_variant`: the run's variant;
- :class:`~otto.invocation.RunPolicy`, :class:`~otto.invocation.Variant`,
  :class:`~otto.invocation.ContextBinding` and :class:`~otto.invocation.HostResolver`,
  from the invocation leaf every layer reads.

Every name is exported lazily (PEP 562): ``import otto.context`` loads none of
the modules that implement it, and a name loads its defining module on first
access. ``OttoContext`` and ``open_context`` compose the run's projects without a
run in hand, so resolving either first imports the composition root,
:mod:`otto.bootstrap`, which installs the initiator the run layer composes
through and composes nothing itself (spec 2026-10-11 placement-and-phases
§3.12). A route that reaches either name without this resolver
(``otto.run_context`` imported directly, ``typing.get_type_hints`` on a
run-context function annotated with the class, such as ``get_context``, or a
pickled class, context or ``open_context`` loaded), in a process that never
loaded the root, composes nothing: a
context's first composing read, and entering ``open_context`` before it
installs or yields a context, raise the catalog's :exc:`RuntimeError`, whose
message names both ways to compose. A context whose first composing read
refused keeps that refusal after the root loads. The resolver does not write
a resolved name back into the module dict, so that whether a name has loaded
stays observable.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .invocation import ContextBinding, HostResolver, RunPolicy, Variant
    from .run_context import (
        OttoContext,
        ProjectContextView,
        get_context,
        open_context,
        reset_context,
        reset_variant,
        set_context,
        set_variant,
        try_get_context,
        variant,
    )

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "ContextBinding": "otto.invocation",
    "HostResolver": "otto.invocation",
    "RunPolicy": "otto.invocation",
    "Variant": "otto.invocation",
    "OttoContext": "otto.run_context",
    "ProjectContextView": "otto.run_context",
    "get_context": "otto.run_context",
    "open_context": "otto.run_context",
    "reset_context": "otto.run_context",
    "reset_variant": "otto.run_context",
    "set_context": "otto.run_context",
    "set_variant": "otto.run_context",
    "try_get_context": "otto.run_context",
    "variant": "otto.run_context",
}

# The names whose use composes the run's projects with no run in hand: resolving
# one imports the composition root first.
_COMPOSING = frozenset({"OttoContext", "open_context"})


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.context's public exports."""
    import importlib

    if name in _COMPOSING:
        # Importing the root installs the initiator and composes nothing.
        from . import bootstrap

        del bootstrap
    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "ContextBinding",
    "HostResolver",
    "OttoContext",
    "ProjectContextView",
    "RunPolicy",
    "Variant",
    "get_context",
    "open_context",
    "reset_context",
    "reset_variant",
    "set_context",
    "set_variant",
    "try_get_context",
    "variant",
]
