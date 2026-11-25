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

import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .instruction_catalog import INSTRUCTIONS, InstructionEntry, instruction, options_parameter

if TYPE_CHECKING:
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
    ctx: "OttoContext", entry: InstructionEntry, kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Turn a standalone handler's parameters into keyword arguments, by the one rule.

    Injects only: the own-class parameter and one annotated with a class
    registered for ``run`` receive ``ctx.options(cls)`` (the values in
    force, so the parameter and ``ctx.options`` hold one object), a
    parameter annotated ``OttoContext`` receives *ctx*, and every other
    parameter named in *kwargs* passes through. It binds nothing on *ctx*:
    the command's preparation (``otto.cli.invoke.prepare_leaf_options``)
    recorded the values, and :func:`run_instruction` lays its own. It
    validates nothing either: validation happens in that preparation, which
    builds the command's classes before the body. Used by the CLI leaf
    wrapper and by :func:`run_instruction`, so the two cannot drift.

    Raises:
        otto.params.OptionsValidationError: the first read of a class the
            preparation did not build rejects the values in force.
        TypeError: *entry* is a project instruction.
    """
    from typing import get_type_hints

    from .params import verb_option_classes
    from .run_context import OttoContext

    handler = _handler_of(entry)
    own_param = options_parameter(handler, entry.options_cls)
    registered = {origin.cls for origin in verb_option_classes("run")}
    hints = get_type_hints(handler)
    bound: dict[str, Any] = {}
    for name in inspect.signature(handler).parameters:
        hint = hints.get(name)
        if name == own_param and entry.options_cls is not None:
            bound[name] = ctx.options(entry.options_cls)
        elif hint is OttoContext:
            bound[name] = ctx
        elif hint is not None and any(hint is cls for cls in registered):
            bound[name] = ctx.options(hint)
        elif name in kwargs:
            bound[name] = kwargs[name]
    return bound


async def run_instruction(
    ctx: "OttoContext", *, name: str, opts: list[object] | None = None
) -> Any:
    """Run instruction *name* on *ctx* as ``otto run <name>`` would, and return its value.

    *opts* are instances of any options class, registered or not. They are
    laid as one :func:`otto.params.use_options` block for the call: a read
    of an instance's own class returns that instance, and every other class
    takes the values whose field names it carries. A class not handed in
    keeps the values of the command the call is made from (*ctx*'s recorded
    flags); *ctx*'s record and ``ctx.verb`` are never changed, so two
    concurrent calls on one context each see their own *opts*. *ctx*
    becomes the active context for the call unless it already is (the
    handler's ``get_host()`` reads it); whatever was active before is
    restored afterwards.

    A project instruction accepts them too. Their fields are
    flattened into the one set of values ``otto run <name>`` would parse, and
    each body builds its own class from them; every applicable body's class
    is validated before any body runs, as ``otto run <name>`` does.

    Raises:
        ValueError: *name* is not registered (with did-you-mean suggestions),
            or two instances give one field two values.
        otto.params.OptionsRegistrationError: an entry of *opts* is a class
            or not a dataclass instance.
        otto.params.OptionsValidationError: a value fails validation, or a
            required field has none.
        otto.session.InstructionInactiveError: *name* belongs to a project that
            is inactive in *ctx* (excluded, out of the loaded labs' scope,
            or host-starved).
    """
    from .params import flatten_option_instances, use_options
    from .project.actions import declare
    from .run_context import reset_context, set_context, try_get_context

    # Never relies on otto.project.actions having been imported: a library
    # caller that never bootstrapped, or a table a test emptied, still gets
    # otto's seven. Declaring again is a no-op.
    declare()
    entry = INSTRUCTIONS.get(name)
    from .session.activation import check_instruction_active

    # Before any option is flattened or validated: an inactive project's
    # instruction is refused for the reason it is inactive, not for a value
    # its options would also have rejected — the order `otto run` keeps.
    check_instruction_active(name=name, owner=INSTRUCTIONS.project(name=name), ctx=ctx)
    instances = list(opts or [])
    layer = use_options(*instances)  # refuses a non-instance before anything runs
    flat = flatten_option_instances(instances)
    token = None if try_get_context() is ctx else set_context(ctx)
    try:
        with layer:
            if entry.project is not None:
                from .project import orchestrator

                # Built and validated once; the run's own walk announces the skips.
                prepared = orchestrator.prepare_project_instruction(name, ctx, flat, announce=False)
                return await orchestrator.run_project_instruction(
                    name, flat, source=prepared.source
                )
            bound = bind_handler_kwargs(ctx, entry, flat)
            return await _handler_of(entry)(**bound)
    finally:
        if token is not None:
            reset_context(token)
