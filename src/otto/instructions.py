"""Run an ``otto run`` instruction from Python.

:func:`run_instruction` runs a registered instruction on a context as
``otto run <name>`` would, and :func:`bind_handler_kwargs` is the one rule that
turns parsed values into a standalone handler's arguments, shared with the
CLI's leaf wrapper. :func:`instruction`, the decorator that registers an
instruction, is served here at its public path. It and the instruction tables
live in :mod:`otto.instruction_catalog`, which reads nothing above the
foundation layer; running an instruction reaches the context, the options and
the project layer, so it lives here, above them.
"""

import dataclasses
import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from .instruction_catalog import INSTRUCTIONS, InstructionEntry, instruction, options_parameter

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

    from .run_context import OttoContext

__all__ = ["instruction", "run_instruction"]


def _handler_of(entry: InstructionEntry) -> "Callable[..., Awaitable[Any]]":
    """Return *entry*'s standalone handler.

    Raises:
        TypeError: *entry* is a project instruction, which has bodies, not a handler.
    """
    if entry.handler is None:
        raise TypeError(f"instruction {entry.name!r} is a project instruction; it has no handler")
    return entry.handler


def bind_handler_kwargs(
    ctx: "OttoContext",
    entry: InstructionEntry,
    kwargs: dict[str, Any],
    *,
    own_instance: object | None = None,
) -> dict[str, Any]:
    """Turn flat values into a standalone handler's keyword arguments, by the one rule.

    Splits the own class's fields out of *kwargs* and builds the instance
    (or takes *own_instance*); binds every class registered for ``run`` on
    *ctx* from the option fields of *kwargs* (the own class's and the
    registered classes', never a plain parameter's); injects *ctx* into a parameter annotated
    ``OttoContext`` and ``ctx.options(cls)`` into one annotated with a
    registered class; passes every other kwarg through. An own class that is
    itself registered for ``run`` is taken from ``ctx.options`` even when
    *own_instance* is given, so the parameter and ``ctx.options`` hold one
    object. Used by the CLI leaf wrapper and by :func:`run_instruction`, so
    the two cannot drift.

    Raises:
        otto.params.OptionsValidationError: a value fails validation.
        TypeError: *entry* is a project instruction.
    """
    from typing import get_type_hints

    from .params import build_options, drop_unset_secrets, verb_option_classes
    from .run_context import OttoContext

    handler = _handler_of(entry)
    own_cls = entry.options_cls
    own_param = options_parameter(handler, own_cls)
    own_fields = {f.name for f in dataclasses.fields(own_cls)} if own_cls is not None else set()
    registered = {origin.cls for origin in verb_option_classes("run")}
    option_fields = own_fields | {
        f.name for cls in registered for f in dataclasses.fields(cast("DataclassInstance", cls))
    }
    ctx.bind_verb_options("run", {k: v for k, v in kwargs.items() if k in option_fields})
    if own_cls is not None and any(own_cls is cls for cls in registered):
        own_instance = ctx.options(own_cls)
    elif own_cls is not None and own_instance is None:
        own_kw = {k: v for k, v in kwargs.items() if k in own_fields}
        own_instance = build_options(own_cls, drop_unset_secrets(own_cls, own_kw))
    hints = get_type_hints(handler)
    bound: dict[str, Any] = {}
    for name in inspect.signature(handler).parameters:
        hint = hints.get(name)
        if name == own_param:
            bound[name] = own_instance
        elif hint is OttoContext:
            bound[name] = ctx
        elif hint is not None and any(hint is cls for cls in registered):
            bound[name] = ctx.options(hint)
        elif name in kwargs:
            bound[name] = kwargs[name]
    return bound


async def run_instruction(ctx: "OttoContext", name: str, opts: list[object] | None = None) -> Any:
    """Run instruction *name* on *ctx* as ``otto run <name>`` would, and return its value.

    *opts* are options instances: the instruction's own class and any class
    registered for ``run``. A registered class not given is built from its
    defaults. *ctx* becomes the active context for the call unless it
    already is (the handler's ``get_host()`` reads it); whatever was active
    before is restored afterwards, as is the verb binding on *ctx*.

    A project instruction accepts instances of its bodies' own options
    classes and of any class registered for ``run``. Their fields are
    flattened into the one set of values ``otto run <name>`` would parse, and
    each body builds its own class from them; every applicable body's class
    is validated before any body runs, as ``otto run <name>`` does.

    Raises:
        ValueError: *name* is not registered (with did-you-mean suggestions).
        otto.params.OptionsRegistrationError: an instance's class is neither
            one the instruction declares (its own, or a project instruction's
            body class) nor registered for ``run``.
        otto.params.OptionsValidationError: a value fails validation, or a
            required field has none.
        otto.session.InstructionInactiveError: *name* belongs to a repo that
            is inactive in *ctx* (excluded, out of the loaded labs' scope,
            or host-starved).
    """
    from .params import flatten_option_instances
    from .project.actions import declare
    from .run_context import reset_context, set_context, try_get_context

    # Never relies on otto.project.actions having been imported: a library
    # caller that never bootstrapped, or a table a test emptied, still gets
    # otto's six. Declaring again is a no-op.
    declare()
    entry = INSTRUCTIONS.get(name)
    from .session.activation import check_instruction_active

    # Before any option is flattened or validated: an inactive repo's
    # instruction is refused for the reason it is inactive, not for a value
    # its options would also have rejected — the order `otto run` keeps.
    check_instruction_active(name, INSTRUCTIONS.repo(name), ctx)
    instances = list(opts or [])
    extras: list[type] = []
    if entry.project is not None:
        from .project.commands import body_origins

        extras = [origin.cls for origin in body_origins(entry.project)]
    elif entry.options_cls is not None:
        extras = [entry.options_cls]
    flat = flatten_option_instances(instances, verb="run", extras=extras)
    own = next((i for i in instances if type(i) is entry.options_cls), None)
    token = None if try_get_context() is ctx else set_context(ctx)
    try:
        with ctx.verb_binding_preserved():
            if entry.project is not None:
                from .project import orchestrator

                ctx.bind_verb_options("run", flat)
                # Validation only; the run's own walk announces the skips.
                orchestrator.project_instruction_body_options(name, ctx, flat, announce=False)
                return await orchestrator.run_project_instruction(name, flat)
            bound = bind_handler_kwargs(ctx, entry, flat, own_instance=own)
            return await _handler_of(entry)(**bound)
    finally:
        if token is not None:
            reset_context(token)
