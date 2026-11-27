"""Shared command-wrapper machinery: OttoContext injection + options expansion.

The plumbing behind both ``@instruction`` (``otto run`` subcommands) and
``@cli_command`` (top-level commands): a parameter annotated ``OttoContext``
is stripped from the CLI signature and supplied at call time from the active
context, and an *options* dataclass parameter is expanded into individual CLI
flags. Factored out of ``cli/run.py`` so both decorators share one
implementation.
"""

import contextlib
import dataclasses
import functools
import inspect
from collections.abc import Callable, Collection, Generator, Iterable, Mapping, Sequence
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast, get_type_hints

import typer
from rich.markup import escape
from typing_extensions import assert_never, override

from ..config._typer import (
    OWN_PARAMS_ATTR,
    Parameter,
    attached_position,
    built_spellings,
    carrier,
    get_current_context,
    own_params,
)
from ..errors import is_containable
from ..params import (
    OptionsNotAvailableError,
    OptionsOrigin,
    OptionsRegistrationError,
    OptionsValidationError,
    Registration,
    command_options,
    covering,
    merge_option_params,
    options_key,
    options_params,
    recorded_in_force,
    resolve_registration,
    sensitive_field_names,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from _typeshed import DataclassInstance
    from typer.core import TyperGroup

    from ..config.project import Project
    from ..config.scope import Participation, PreLabReason, ScopeSelection
    from ..coverage.config import DestinationError
    from ..errors import FieldError
    from ..inventory.view import InventoryView
    from ..params import RecordedOptions
    from ..reservations.check import ReservationBackendError
    from ..result import Result
    from ..run_context import OttoContext
    from ..session.errors import InstructionInactiveError, LabBuildError, ProjectSelectionError
    from ..session.projects import DemotedProject, RequiredProject
    from .registry import CommandSpec


def _ctx_param_name(func: Callable[..., Any]) -> str | None:
    """Return the name of any parameter annotated as OttoContext, or None."""
    from ..run_context import OttoContext

    hints = get_type_hints(func)
    for name, hint in hints.items():
        if hint is OttoContext:
            return name
    return None


def _inject_ctx(func: Callable[..., Any], ctx_name: str) -> Callable[..., Any]:
    """Wrap *func* so the OttoContext param is supplied from the active context.

    Supplied at call time and hidden from the Typer-facing signature.
    """
    from ..run_context import get_context

    sig = inspect.signature(func)
    exposed = [p for n, p in sig.parameters.items() if n != ctx_name]

    @functools.wraps(func)
    async def wrapper(**kw: Any) -> Any:
        kw[ctx_name] = get_context()
        return await func(**kw)

    # Drop ctx_name from __annotations__ too so get_type_hints() on the
    # wrapper doesn't see it (important when _wrap_with_options composes on top).
    wrapper.__annotations__ = {k: v for k, v in func.__annotations__.items() if k != ctx_name}
    wrapper.__signature__ = inspect.Signature(exposed)  # ty: ignore[missing-attribute]
    return wrapper


def spell_flags(text: str, flags: "Mapping[str, str]") -> str:
    """Rewrite a library message's field names as the flags this command exposes.

    Keys that carry a value (``cov=False``) are replaced first, then
    ``set <field>=True`` becomes ``pass <flag>``, then bare field names as
    whole words, longest first, so ``cov_report`` never matches inside
    ``cov_report_dir`` and no field matches inside a flag already spelled.
    """
    import re

    for key, flag in flags.items():
        if "=" in key:
            text = text.replace(key, flag)
    for key, flag in flags.items():
        if "=" not in key:
            text = text.replace(f"set {key}=True", f"pass {flag}")
    for key in sorted((k for k in flags if "=" not in k), key=len, reverse=True):
        # Not `\b`: a hyphen is a word boundary, so `cov` would match inside
        # the `--no-cov` the first pass just wrote.
        #
        # The replacement is a FUNCTION, not the flag string itself: `re.sub`
        # treats a string replacement as a template, where a backslash (or
        # `\1`) in the flag's own spelling would be read as a backreference
        # and either corrupt the output or raise `re.error`. A lambda's
        # return value is inserted literally, no template parsing at all.
        flag = flags[key]
        text = re.sub(rf"(?<![\w-]){re.escape(key)}(?![\w-])", lambda _m, flag=flag: flag, text)
    return text


def usage_error_from(
    exc: "OptionsValidationError | DestinationError | FieldError",
    *,
    flags: "Mapping[str, str] | None" = None,
) -> typer.BadParameter:
    """Translate a library options, destination, or field-named input failure into a usage error.

    The one place this translation happens. With *flags* (field name to
    flag) the message is spelled in the command's flags and ``param_hint``
    names the offending one; without, the message passes through.

    A :class:`~otto.coverage.config.DestinationError`'s message embeds the
    user's own filesystem path verbatim (``exc.path``), which must never be
    rewritten — a path that happens to contain a field name as a substring
    (``cov`` inside ``cov_dir``) is not the field, and text-rewriting the
    rendered message would corrupt it either way. So a ``DestinationError``
    is never spelled by rewriting its text: its message is rebuilt from
    scratch via :func:`~otto.coverage.config.destination_message`, passing
    the flag spellings in as the subject/remedy instead of the field names.
    A :class:`~otto.errors.FieldError`'s message (a ``DockerVerbError``, a
    ``CoverageInputError``) is treated the same way: it can embed text the
    user typed, so it passes through untouched and only ``param_hint``
    carries the flag.

    Anything else (an :class:`~otto.params.OptionsValidationError`) still
    goes through :func:`spell_flags`, which is a pure text-rewriting helper.

    When the spelled message already leads with the flag ``param_hint``
    would name (every real destination message does: ``"cov_dir target ..."``
    spells to ``"--cov-dir target ..."``), the hint is dropped — click
    prefixes a param hint as ``"Invalid value for --cov-dir: "``, so keeping
    it would repeat the flag the message already opens with
    (``"Invalid value for --cov-dir: --cov-dir target ..."``).

    ``typer.BadParameter``, not ``click.BadParameter``: typer >= 0.26 vendors
    click, and only its own class is caught by its exception handling.
    """
    message = str(exc)
    hint = None
    if flags:
        from ..coverage.config import DestinationError, destination_message
        from ..errors import FieldError

        if isinstance(exc, DestinationError):
            message = destination_message(
                exc.kind,
                exc.path,
                subject=flags.get(exc.field, exc.field),
                remedy=f"pass {flags.get(exc.remedy_field, exc.remedy_field)}",
                reason=exc.reason,
            )
        elif isinstance(exc, FieldError):
            pass  # the message embeds user text; pass through byte-identical
        else:
            message = spell_flags(message, flags)
        field = getattr(exc, "field", None)
        hint = flags.get(field) if field else None
        if hint is not None and message.startswith(hint):
            hint = None
    return typer.BadParameter(message, param_hint=hint)


def _owns_options(ctx: typer.Context) -> bool:
    return getattr(getattr(ctx.command, "callback", None), OWN_OPTIONS_ATTR, None) is not None


def pending_options(ctx: typer.Context) -> "RecordedOptions | None":
    """Return the pending record a command holds before it reads its flags, else ``None``.

    The one place that decides whether a command marks: it does when it
    accepts registered options (:func:`leaf_accepts_options`, over the
    invocation's command spec and :func:`command_path`), or its callback
    carries :data:`OWN_OPTIONS_ATTR`. The
    record's ``group`` is the top-level command when it accepts, else
    ``None``. For the root context, before any subcommand runs, it answers
    for the top-level command being dispatched, from its registration
    (:func:`spec_accepts_options`). A context with no command spec in its
    invocation (``meta['_otto_command_spec']``) accepts nothing. While the
    record is in force, a read of any options class raises
    ``OptionsNotAvailableError``.
    """
    from ..params import RecordedOptions

    spec = cast("CommandSpec | None", ctx.meta.get(_COMMAND_SPEC_KEY))
    group = None
    if spec is not None:
        path = command_path(ctx, spec)
        callback = getattr(ctx.command, "callback", None)
        if leaf_accepts_options(spec, callback, beneath_group=len(path) > 1):
            group = path[0]
    if group is None and not _owns_options(ctx):
        return None
    return RecordedOptions(group=group, pending=True)


def settle_options() -> None:
    """Clear the pending mark, so what runs from here on reads defaults.

    When the record in force (``recorded_in_force``) is pending, its
    ``pending`` is set ``False``; it keeps its group and is not replaced. A
    command whose options are prepared when it is invoked
    (:func:`prepare_command_options`) records over it; everything else, a
    parse-time listing (``otto test --list-tests -l``) included, reads
    defaults once it is settled.
    """
    recorded = recorded_in_force()
    if recorded is not None and recorded.pending:
        recorded.pending = False


_LEAF_PREPARED = "otto.leaf_options_prepared"
"""``click.Context.meta`` key: a preparation has read this parser context's record."""


@dataclasses.dataclass(frozen=True)
class LeafOptionSpec:
    """What a command leaf offers: built once per command, read on every call.

    *group* is the verb the leaf records under (``"run"``, ``"test"``, or
    ``None`` for a project's own command). *build* holds the classes built
    and validated before the body, in display order: the own class, then
    the covering classes. *walk* holds classes also offered but built by
    the caller's own walk (a project instruction's body classes).
    """

    group: str | None
    build: "tuple[type, ...]"
    walk: "tuple[type, ...]" = ()


@dataclasses.dataclass(frozen=True)
class LeafOptions:
    """What :func:`prepare_leaf_options` prepared for one call of a leaf.

    *recorded* is the call's record, *instances* the built instance of each
    class of the spec's *build*, in order, and *rest* the leaf's keyword
    arguments that are not option fields. *sensitive* names the fields,
    over every class the spec offers, whose value a dry run masks.

    For a command prepared when it is invoked (:func:`prepare_command_options`)
    *finishes_dry_run* is the command's own answer (it awaits its own dry-run
    tail) and *raw* holds each attached parameter's value as the parser
    produced it, for the ``would run:`` line.
    """

    recorded: "RecordedOptions"
    instances: "dict[type, Any]"
    rest: "dict[str, Any]"
    _hold: "Callable[[], AbstractContextManager[None]]" = dataclasses.field(
        default=contextlib.nullcontext, repr=False, compare=False
    )
    finishes_dry_run: bool = False
    sensitive: frozenset[str] = frozenset()
    raw: "Mapping[str, Any]" = dataclasses.field(default_factory=dict)

    def scope(self) -> "AbstractContextManager[None]":
        """Hold the record for the leaf's body.

        A contextless command's record is held for the block (``command_options``);
        a direct call of a verb-less leaf records on the context for the block
        and hands the context's binding back after; otherwise a no-op, the
        context already holds it.
        """
        return self._hold()


def _invoking(leaf: Callable[..., Any]) -> "typer.Context | None":
    """Return the current parser context when its command is *leaf*, else ``None``.

    Compared through :func:`inspect.unwrap`, so a wrapper and the function it
    wraps are the same command.
    """
    parser_ctx = cast("typer.Context | None", get_current_context(silent=True))
    if parser_ctx is None:
        return None
    callback = getattr(parser_ctx.command, "callback", None)
    if callback is None or inspect.unwrap(callback) is not inspect.unwrap(leaf):
        return None
    return parser_ctx


def _parsed_by(leaf: Callable[..., Any]) -> "typer.Context | None":
    """Return the parser context whose record *leaf*'s first preparation reads, else ``None``.

    The current parser context, when its command is *leaf* (:func:`_invoking`)
    and no preparation has read it yet; this marks it, so a leaf that awaits
    its own prepared target is a direct call the second time. ``None`` means
    a direct call from other code.
    """
    parser_ctx = _invoking(leaf)
    if parser_ctx is None or parser_ctx.meta.get(_LEAF_PREPARED):
        return None
    parser_ctx.meta[_LEAF_PREPARED] = True
    return parser_ctx


@contextlib.contextmanager
def _recorded_for_block(ctx: "OttoContext", recorded: "RecordedOptions") -> "Generator[None]":
    """Record *recorded* on *ctx* for the block, handing *ctx*'s binding back after."""
    from ..run_context import record_options

    with ctx.verb_binding_preserved():
        record_options(ctx, recorded)
        yield


def _option_values(kw: "Mapping[str, Any]", classes: "Iterable[type]") -> "dict[str, Any]":
    """Return the values of *kw* that are a field of one of *classes*: the one split.

    What is left of *kw* is the command's function's own arguments.
    """
    from ..params import init_field_names

    names = {name for cls in classes for name in init_field_names(cls)}
    return {k: v for k, v in kw.items() if k in names}


def prepare_leaf_options(
    kw: "dict[str, Any]", spec: LeafOptionSpec, *, leaf: Callable[..., Any]
) -> LeafOptions:
    """Split, record and build a leaf's option values: the one preparation.

    Synchronous, in this order: *kw* is projected onto the fields of every
    class *spec* offers (the rest is :attr:`LeafOptions.rest`); the names
    given are the parser's when the current parser context's command is
    *leaf* and no preparation has read it yet, else every name (a direct
    call from other code); each class of ``spec.build`` is built from
    everything parsed; and the record is made. With a context installed it
    is recorded on it (for a direct call of a verb-less leaf, only for
    :meth:`LeafOptions.scope`'s block, the context's binding handed back
    after); with none, :meth:`LeafOptions.scope` holds it. Callers enter
    :meth:`LeafOptions.scope` before anything reads the context's option
    values. :attr:`LeafOptions.sensitive` is collected over every class
    *spec* offers.

    A value that fails a class's validation raises ``typer.BadParameter``.
    """
    from ..config._typer import given_names
    from ..params import RecordedOptions, build_offered, command_options, sensitive_names
    from ..run_context import record_options, try_get_context

    offered = spec.build + spec.walk
    parsed = _option_values(kw, offered)
    rest = {k: v for k, v in kw.items() if k not in parsed}
    parser_ctx = _parsed_by(leaf)
    given = given_names(parser_ctx, parsed)
    try:
        instances = build_offered(RecordedOptions(parsed=parsed, given=given, offered=spec.build))
    except OptionsValidationError as e:
        raise usage_error_from(e) from e
    recorded = RecordedOptions(
        group=spec.group, parsed=parsed, given=given, offered=offered, instances=dict(instances)
    )
    sensitive = sensitive_names(offered)
    ctx = try_get_context()
    if ctx is None:
        return LeafOptions(
            recorded, instances, rest, lambda: command_options(recorded), sensitive=sensitive
        )
    if parser_ctx is None and spec.group is None:
        return LeafOptions(
            recorded,
            instances,
            rest,
            lambda: _recorded_for_block(ctx, recorded),
            sensitive=sensitive,
        )
    record_options(ctx, recorded)
    return LeafOptions(recorded, instances, rest, sensitive=sensitive)


def _own_option_names(cls: "type | None") -> frozenset[str]:
    """Return the names a command's own options class *cls* puts in its signature.

    The one split of a command's parameters into its own class's fields
    (:func:`~otto.params.options_params`) and the rest; none for no class.
    """
    if cls is None:
        return frozenset()
    return frozenset(p.name for p in options_params(cast("type[DataclassInstance]", cls)))


def _wrap_with_options(
    func: Callable[..., Any],
    opts_cls: "type[DataclassInstance] | None",
    *,
    project: str | None = None,
    path: "Sequence[str] | None" = None,
) -> Callable[..., Any]:
    """Build a wrapper that expands a command's own options class into CLI parameters.

    *opts_cls* is the command's own options class: its fields become flags,
    and at call time the populated instance is forwarded to *func* in the
    position of the parameter annotated with it. *project* owns it, and is
    who a collision message names for it.

    With *path*, the command's path (an instruction's,
    ``otto.instruction_catalog.instruction_path``), a parameter annotated with
    a class that covers *path* (``otto.instructions.covered_parameters``) is
    taken out of the signature and receives ``otto.get_options`` of its class
    at call time: its defaults when the command does not offer the class (its
    project does not take part). *func*'s arguments are bound by
    ``otto.instructions.bind_arguments``, the binding a library call
    (``otto.instructions.run_instruction``) makes too. Nothing is resolved or
    imported when the command is built.

    The command says what is its own (:class:`OwnOptions`: *opts_cls*,
    *project*, that it finishes its own dry run, and, with *path*, that its
    plain parameters are reserved names, so a covering class whose field
    takes one is refused naming that parameter) and prepares nothing when it
    is dispatched: the invocation prepares it with the classes covering it
    (:func:`prepare_command_options`), and the wrapper hands *func* the own
    class's prepared instance and its other keyword arguments. Called by
    other code (no parser context of its own), it prepares itself, every
    value given: its own class alone, or with *path* its own class and every
    class covering *path*, every project kept.
    """
    from .. import instructions
    from ..instruction_catalog import options_parameter
    from ..instructions import covered_parameters
    from ..params import get_options

    sig = inspect.signature(func)
    opts_param_name = options_parameter(func, opts_cls)
    injected: "dict[str, type]" = {}
    if path is not None:
        injected = {
            name: cls
            for name, cls in covered_parameters(func, path).items()
            if name != opts_param_name
        }
    own_params = options_params(opts_cls) if opts_cls is not None else []
    func_name = getattr(func, "__name__", repr(func))
    reserved = {
        name: f"parameter {name!r} of instruction {func_name!r}"
        for name in sig.parameters
        if path is not None and name != opts_param_name and name not in injected
    }
    own = OwnOptions(
        cls=opts_cls,
        project=project,
        finishes_dry_run=True,
        reserved=reserved,
    )

    # Replace the opts param with its expanded fields and drop the injected
    # ones. No hidden click Context parameter: the wrapper reads the current
    # one (see below), so the public signature is exactly what it declares.
    new_params: list[inspect.Parameter] = []
    for p in sig.parameters.values():
        if p.name == opts_param_name:
            new_params.extend(own_params)
        elif p.name not in injected:
            # Ensure all params are KEYWORD_ONLY for a consistent Typer signature
            kw_only_p = (
                p
                if p.kind == inspect.Parameter.KEYWORD_ONLY
                else p.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            )
            new_params.append(kw_only_p)

    @functools.wraps(func)
    async def wrapper(**kw: Any) -> Any:
        prepared = leaf_preparation(wrapper, kw, own, path)
        with prepared.scope():
            offered = _option_values(kw, prepared.recorded.offered)
            call_kw = instructions.bind_arguments(
                func,
                own_cls=opts_cls,
                path=path,
                kwargs={k: v for k, v in kw.items() if k not in offered},
                read=get_options,
            )
            # Validation is already DONE by this point, exactly as it is on a
            # real run -- so a dry run's `finish_dry_run` never has to
            # re-validate anything, only report what was just built.
            # `click_ctx` is `None` for a call that never went through the CLI
            # at all (a test, or a library caller reaching this callable
            # directly) -- and never having gone through the CLI seam is
            # exactly the case a dry run cannot apply to, so that IS the "not a
            # dry run" answer here. `typer.Context` (used everywhere else in
            # this module) is a thin subclass typer's OWN commands never
            # actually instantiate -- `TyperCommand.context_class` is the
            # vendored click base (`typer._click.core.Context`) that
            # `get_current_context` returns -- so the cast just makes that
            # existing fact visible to the type checker.
            click_ctx = cast("typer.Context | None", get_current_context(silent=True))
            if click_ctx is not None and dry_run_requested(click_ctx):
                # `finish_dry_run` decides itself, from *preview*, whether to
                # print+exit or just return -- a previewing leaf still gets
                # `--probe`'s announcement/dial/report, then runs its real body.
                preview = command_spec(click_ctx).dry_run_preview or _leaf_declares_preview(
                    click_ctx
                )
                await finish_dry_run(click_ctx, list(prepared.instances.values()), preview=preview)
            return await func(**call_kw)

    wrapper.__signature__ = inspect.Signature(new_params)  # ty: ignore[missing-attribute]
    setattr(wrapper, OWN_OPTIONS_ATTR, own)
    return wrapper


_PREPARED_HANDED = "otto.prepared_options_handed"
"""``click.Context.meta`` key: a wrapper has been handed the invocation's preparation."""


def _handed_options(leaf: Callable[..., Any]) -> "LeafOptions | None":
    """Return the invocation's preparation for *leaf*'s first call as the command invoked.

    The current parser context's :func:`prepared_options`, when its command
    is *leaf* (:func:`_invoking`) and nothing has been handed it yet; this
    marks it, so a later call of *leaf* from its own body is a direct call.
    ``None`` otherwise: a direct call from other code, or a command driven
    without otto's invoke wrapper.
    """
    parser_ctx = _invoking(leaf)
    if parser_ctx is None or parser_ctx.meta.get(_PREPARED_HANDED):
        return None
    prepared = prepared_options(parser_ctx)
    if prepared is not None:
        parser_ctx.meta[_PREPARED_HANDED] = True
    return prepared


def leaf_preparation(
    leaf: Callable[..., Any],
    kw: "dict[str, Any]",
    own: "OwnOptions",
    path: "Sequence[str] | None" = None,
) -> LeafOptions:
    """Return the preparation a leaf's body runs with: the invocation's, else its own.

    The one sequence every leaf that says what is its own (:class:`OwnOptions`)
    follows. When *leaf* is the command being invoked, the invocation
    prepared it (:func:`prepare_command_options`) and that preparation is
    handed to its first call (``_handed_options``). Otherwise other code
    calls it, and it prepares *kw* itself (:func:`prepare_leaf_options`),
    every value given: over its own class (*own*) alone, or, with *path*,
    its command's path, over its own class, every class covering *path*
    (resolved here) and every body of *own*, every project kept. The body
    runs inside :meth:`LeafOptions.scope` either way.
    """
    handed = _handed_options(leaf)
    if handed is not None:
        return handed
    if path is None:
        spec = LeafOptionSpec(group=None, build=(own.cls,) if own.cls is not None else ())
    else:
        origins = [resolve_registration(registration) for registration in covering(path)]
        spec = _leaf_spec(path, own, origins, own.bodies() if own.bodies is not None else [])
    return prepare_leaf_options(kw, spec, leaf=leaf)


def prepare_command_target(
    func: Callable[..., Any],
    options_cls: type | None = None,
    *,
    project: str | None = None,
    path: "Sequence[str] | None" = None,
) -> Callable[..., Any]:
    """Apply otto's CLI wrappers to *func*: OttoContext injection + options expansion.

    The shared machinery behind ``@instruction`` and ``@cli_command``: a
    parameter annotated ``OttoContext`` is stripped from the CLI signature and
    injected at call time; an *options_cls* dataclass parameter is expanded
    into individual CLI flags. *project* is the project that registered
    *func* (``None`` for otto), named for *options_cls* when one of its
    fields collides.

    With *path*, an instruction's command path, a parameter annotated with
    a class covering *path* is injected with ``otto.get_options(cls)``, and
    the covering classes' flags are attached for each invocation, like every
    command's (:func:`offered_options`); nothing is resolved here.

    Idempotent by contract, not coincidence: a callable this function already
    wrapped is returned unchanged (sentinel attribute). The dispatch path
    prepares twice — ``@cli_command`` at decoration, then
    ``resolve_spec_command``'s function-loader branch, which serves every
    function loader and can't know one was pre-prepared. Without the sentinel
    that was safe only because ``_inject_ctx`` happens to strip the ctx
    annotation that triggers it.

    A wrapped target also carries ``__otto_handler__`` pointing at *func*
    itself (typer's own ``__dict__``-copying wrap carries it onto the click
    callback too — see ``_require_async_leaf``), so the async-leaf guard can
    always find the real registered handler beneath otto's own wrapping
    without having to walk (and so trust) a ``__wrapped__`` chain a user's
    decorator might also have contributed to.
    """
    if getattr(func, "__otto_cli_prepared__", False):
        return func
    ctx_name = _ctx_param_name(func)
    target: Callable[..., Any] = func
    if ctx_name is not None:
        target = _inject_ctx(func, ctx_name)
    own_cls: "type[DataclassInstance] | None" = None
    if options_cls is not None and dataclasses.is_dataclass(options_cls):
        own_cls = options_cls
    if own_cls is not None or path is not None:
        target = _wrap_with_options(target, own_cls, project=project, path=path)
    if target is not func:
        target.__otto_cli_prepared__ = True  # ty: ignore[missing-attribute]
        target.__otto_handler__ = func  # ty: ignore[missing-attribute]
    return target


# ---------------------------------------------------------------------------
# Leaf-invoke preamble: lazy lab loading, session setup, output dir, gate
# ---------------------------------------------------------------------------


def print_error(message: object, *, soft_wrap: bool = False) -> None:
    """Print *message* as a user-facing error, in red, exactly as written.

    The one place a CLI command renders a failure. The message reaches the
    console as a :class:`~rich.text.Text` (``otto.console.literal_cell``), so
    Rich never parses it: no markup, no ``:emoji:`` codes. That is
    load-bearing, not hygiene. Rich reads ``[word]`` as a style tag and
    DELETES it, so ``list[str]`` would print as ``list``, an install hint like
    ``otto-sh[monitor]`` as ``otto-sh`` (a runnable command for the wrong
    thing), and a pydantic detail like ``[type=missing, input_value={}]``
    would vanish; a host id ``:smile:`` would print as an emoji. Every message
    that reaches here interpolates something a user, a path, or a library
    supplied, so none can be assumed free of either.

    Red is the BASE style, so a caller that wants part of the message styled
    passes a ``Text.assemble(...)``: its spans survive on top of the red. A
    ``Text`` has no parse step to guard, which is why this is not
    ``emoji=False`` on an escaped string: that would be the same decision
    spelled a second way, with an ``escape()`` still left to forget. Unlike
    ``otto.console.print_literal`` the message still folds at the console
    width, because an error is prose (see *soft_wrap*).

    *soft_wrap* turns the hard word-wrap off for messages that carry a token
    the reader is meant to COPY. Rich folds at the console width, and it folds
    between words -- so a hint ending ``or: -I firmware-integration`` breaks
    after the ``-I`` at 80 columns and hands the reader a switch they cannot
    paste. Same reasoning, and the same call shape, as ``otto.cli.link._row``
    and its twin ``otto.cli.tunnel._row``: a long line beats a broken argv.

    PASS IT when any part of the message is meant to be copied and re-run --
    a CLI switch, a shell command, a path, a host id. LEAVE IT OFF for prose,
    which is most errors: folding is what a reader wants there, and turning it
    off would leave one long line to scroll. The test is not "is this message
    long?" but "would a line break inside it destroy something the reader has
    to retype?".

    Note it also turns CROPPING off, which is the part that surprises people:
    with ``no_wrap`` alone rich TRUNCATES an over-wide line at the width
    instead of folding it, so the message quietly loses its tail while still
    looking complete.

    Off, this is the byte-identical call ``rich.print`` itself makes:
    ``rich.print`` forwards only ``sep`` and ``end`` to ``Console.print``, and
    that method's own ``soft_wrap`` default is ``None`` (meaning "defer to
    ``Console.soft_wrap``"). Passing ``None`` here is therefore that same call,
    not an approximation of it -- checked against a corpus of otto's real error
    messages at three widths, byte for byte.
    """
    from rich import get_console

    from ..console import literal_cell

    get_console().print(literal_cell(message), style="red", soft_wrap=soft_wrap or None)


def render_instrumentation_refusal(error: BaseException) -> str:
    """Print a coverage refusal's per-product verdict TABLE; return the line to print beside it.

    ``CoverageNotInstrumentedError`` carries the same verdicts twice: as the
    plain listing inside ``str(error)`` (what the run log keeps, because a log
    file cannot hold a Rich table) and as
    ``otto.coverage.instrumentation.InstrumentationReport`` structure. On a
    console the structure wins — the rounded table, with the "unknown" remedy
    as its caption — so this renders it here and hands the caller back only
    the message's FIRST line. Printing the whole message beside the table
    would put the same verdicts on the console twice.

    Any other exception (and a refusal raised without a report, or with an
    empty one) comes back as its own full ``str()``, so a caller can use this
    unconditionally in an error path.

    The ``report`` attribute is probed before the class is imported on
    purpose: ``from ..coverage.errors import ...`` executes
    ``otto.coverage.__init__``, which pulls the collector, the fetcher and the
    reporter. Only otto's own refusal carries that attribute, and by the time
    one exists ``otto.coverage`` is already imported, so the guarded import is
    a cache hit rather than a new edge on the ``test`` import surface.
    """
    if getattr(error, "report", None) is None:
        return str(error)
    from ..coverage.errors import CoverageNotInstrumentedError

    if not isinstance(error, CoverageNotInstrumentedError):
        return str(error)
    report = error.report
    if report is None or not report.rows:
        return str(error)
    from rich import get_console

    get_console().print(report.table())
    return str(error).splitlines()[0]


def fail(message: object, code: int = 1, *, soft_wrap: bool = False) -> "NoReturn":
    """Render *message* as a user-facing error and exit with *code*.

    The exiting half of :func:`print_error`: one place that decides what a
    failing command looks like, so a new command cannot accidentally ship one
    Rich parses as markup (``.ast-grep/rules/error-render-through-helper.yml``
    keeps that true).

    *soft_wrap* is threaded straight through -- see :func:`print_error` for
    when to ask for it. Keyword-only, and defaulted off, so every existing
    ``fail(msg)`` and ``fail(msg, 2)`` call is unchanged in both spelling and
    behavior.
    """
    print_error(message, soft_wrap=soft_wrap)
    # `from None`: typer.Exit is a control-flow signal, not a consequence of
    # whatever was caught. Chaining it would attach a __cause__ that click's
    # standalone mode discards anyway, and one converted site (expose.py) had
    # asked for exactly this suppression explicitly.
    raise typer.Exit(code) from None


def print_dry_run_plan(header: str, would: list[str], unchecked: list[str]) -> None:
    """Render one ``--dry-run`` preview: what would happen, and what was not checked.

    The one renderer for ``otto link`` and ``otto tunnel``. It takes the two
    line lists rather than a plan because the two ``DryRunPlan`` classes are
    unrelated and a plan-typed signature would need a cross-package import.
    Every line is user-supplied lab data (a link name, a host id, a netdev, a
    shell command built from them), so each prints literally.

    BOTH SECTIONS, ALWAYS. ``would`` alone reads as a verified promise: every
    command line in it is the one a CLEAN device would get (or a carrier port
    picked without probing a host), and the refusals that could stop the call
    outright have not run. ``not checked`` alone is a dry run with no preview,
    which is useless rather than safe. The ``would`` section is legitimately
    short or empty (an in-path link, or a chain with a container endpoint,
    whose commands cannot be built without a live lookup); the header still
    prints, so the command never answers with silence.
    """
    from rich.text import Text

    from ..console import print_literal

    print_literal(
        Text.assemble(
            ("dry run", "cyan"),
            f" {header}: no device was contacted — nothing was read and nothing was changed",
        )
    )
    for line in would:
        print_literal(f"  would: {line}")
    for line in unchecked:
        print_literal(f"  not checked: {line}")


def print_check_dry_run(header: str, lines: list[str]) -> None:
    """Render a ``check --dry-run`` preview: the plan, and nothing else.

    Unlike :func:`print_dry_run_plan`'s ``would``/``unchecked`` split, a
    check hands back one flat, already-ordered line list, so there is nothing
    to split, only to print literally: every line is lab/host data (a netdev,
    a host id, a socat argv).
    """
    from rich.text import Text

    from ..console import print_literal

    print_literal(Text.assemble(("dry run", "cyan"), f" {header}:"))
    for line in lines:
        print_literal(f"  {line}")


def report_project_selection_error(err: "ProjectSelectionError") -> "NoReturn":
    """Render a project-switch refusal in today's words.

    An overlap is a usage error (exit 2, click's frame); an unknown name exits
    2 through :func:`fail`. Formatted from the error's fields, never by
    rewriting its message: the library says ``include_projects``, the command
    line said ``-I``.
    """
    if err.kind == "overlap":
        raise typer.BadParameter(
            f"project(s) {', '.join(err.names)} appear in both --include-projects "
            "and --exclude-projects — pick one",
            param_hint="--include-projects / --exclude-projects",
        ) from None
    hint = f" — did you mean {err.suggestion!r}?" if err.suggestion else ""
    fail(f"no project {err.names[0]!r}{hint}", 2)


@contextlib.contextmanager
def lab_context_refusals() -> "Generator[None, None, None]":
    """Render a lab-context refusal the loud way: today's words, today's exit code.

    Wraps the loud callers' :func:`ensure_lab_context` (the preamble, the
    inline ``--show-lab`` / ``--list-hosts`` branch, ``otto reservation
    check``): a :class:`~otto.session.LabBuildError` or a
    :class:`~otto.reservations.check.ReservationBackendError` raised inside goes
    through :func:`report_lab_context_error`; anything else propagates.

    The two classes are imported when the ``with`` block is entered, not at
    module scope: keeping them function-local keeps ``otto.session`` and
    ``otto.reservations`` off this module's import, and this module sits on
    the CLI's budgeted import surface (``scripts/import_budget.py``), which a
    bare ``otto --help`` must not pay for.
    """
    from ..reservations.check import ReservationBackendError
    from ..session.errors import LabBuildError

    try:
        yield
    except (LabBuildError, ReservationBackendError) as e:
        report_lab_context_error(e)


def report_lab_context_error(err: "LabBuildError | ReservationBackendError") -> "NoReturn":
    """Render a lab-context refusal the loud way, in today's words, and exit with today's code.

    Formatted from the error's fields: a missing lab is click's own plain
    usage line on stderr (exit 2); a bad source or a broken inventory is the
    bold-red frame (exit 1); an unreachable reservation backend names the
    ``-R`` break-glass (exit 1). An unknown or unavailable lab is re-raised: the boundary
    frame in ``otto.cli.main`` has always printed it (``error: ...``, exit 1).
    No project at all prints as :func:`fail_without_projects` prints it for a
    listing (``error: ...``, unfolded, exit 1).
    """
    from rich import print as rprint
    from rich.text import Text

    from ..reservations.check import ReservationBackendError
    from ..session.errors import LabBuildError

    if isinstance(err, LabBuildError):
        # Bound once so the type checker narrows the literal across the arms
        # and `assert_never` fails the build when the library grows a kind.
        kind = err.kind
        if kind == "no_labs":
            # Plain stderr, not rich: click's own usage-error stream. (A *real*
            # click.UsageError would escape Typer 0.26's vendored click fork
            # uncaught, hence the manual message.)
            typer.echo(
                "Error: Missing option '--lab' / '-l' (env var: 'OTTO_LAB'). See otto --list-labs.",
                err=True,
            )
            raise typer.Exit(code=2)
        if kind in ("unknown_lab", "unavailable_lab"):
            raise err
        if kind == "no_project":
            # As the root listings print it, unfolded: it may end in an
            # `export OTTOPATH=...` line meant to be copied whole.
            print_error(f"error: {err}", soft_wrap=True)
            raise typer.Exit(code=1)
        detail = err.detail or ""
        if kind == "sources":
            rprint(Text.assemble(("Host source unavailable:", "bold red"), " ", detail))
        elif kind == "inventory":
            rprint(Text.assemble(("Inventory unavailable:", "bold red"), " ", detail))
        else:
            assert_never(kind)
        raise typer.Exit(code=1)
    if isinstance(err, ReservationBackendError):
        rprint(
            Text.assemble(
                ("Reservation backend unavailable:", "bold red"),
                " ",
                str(err),
                "\nPass ",
                ("--skip-reservation-check", "bold"),
                " / ",
                ("-R", "bold"),
                " to proceed without the check.",
            )
        )
        raise typer.Exit(code=1)
    # Unreachable for a well-typed caller. A stray error is handed back to the
    # boundary frame as itself, never dressed up as a reservation failure.
    raise err


def fail_without_projects(projects: "Sequence[object]") -> None:
    """Exit 1, saying why, when discovery found no project at all.

    For the root listings (``--list-labs``, ``--list-products``,
    ``--list-tools``), which would otherwise print an empty answer and exit 0.
    The words are :func:`~otto.config.project.no_project_found_message`'s, the
    same a lab build refuses with, printed the way the boundary frame prints
    an error.
    """
    if projects:
        return
    from ..config.project import no_project_found_message

    print_error(f"error: {no_project_found_message()}", soft_wrap=True)
    raise typer.Exit(code=1)


def ensure_inline_lab(ctx: typer.Context) -> None:
    """Load the lab for a flag that inspects it INLINE and exits (``--show-lab``, ``--list-hosts``).

    The lab loads lazily in :func:`command_preamble`, which runs at the LEAF
    verb — so a group-level flag that wants live lab state has to load it
    itself, and it must do so the way dispatch does. The root callback grew
    this sequence for its own two flags; ``otto host --list-hosts`` had kept
    an eager Typer callback from before the lazy load existed, which called
    ``get_lab()`` (``otto.fleet``) before any context could exist and tracebacked
    ``No active OttoContext`` on the documented form ``otto --lab L host
    --list-hosts`` (2026-09-03). One helper, both callers.

    Same two gates in the same order as :func:`command_preamble` — a typo'd
    ``-I``/``-E`` name is a typo here too, and a half-registered world fails
    loud rather than surfacing a secondary error — then the CLI session BEFORE
    the lab load: the session applies each project's ``[logging.levels]`` floor
    and the _HostFilter, so the load's own notices (the cross-source override
    warning from ``otto.labs.composite``, notably) render through it. A lab
    that cannot load reports and exits with the loud path's message and code.
    """
    selection = scope_selection(ctx)
    validate_project_switches(selection)
    fail_loud_on_bootstrap_errors(selection)
    ensure_cli_session(ctx)
    with lab_context_refusals():
        ensure_lab_context(ctx)


@dataclasses.dataclass(frozen=True)
class RootOptions:
    """The root-callback options the preamble needs, stashed on ``ctx.meta``.

    The root callback shrinks to recording these; the lab-loading /
    session-setup work reads them back lazily from ``ctx.meta`` at the moment a
    real (non-help) command invocation begins.
    """

    labs: "list[str] | None"
    xdir: Path
    log_days: int
    log_level: str
    rich_log_file: bool
    show_time: bool
    dry_run: bool
    holder: "str | None"
    skip_reservation_check: bool
    field: bool = False
    """``--field``: the run installs each product's field variant (spec
    2026-10-03 §5); ``False`` is ``--debug``, the default. The root callback
    also installs it as the variant of the run's policy
    (:class:`otto.context.RunPolicy`), which is what the registry and
    providers read."""
    probe: bool = False
    """``--probe``: under a dry run, open a connection to each host the command
    names (spec §3). Defaults so a caller that predates the flag still builds;
    the root callback always passes it, and rejects it without ``--dry-run``."""
    include_projects: tuple[str, ...] = ()
    """``-I``: PEP-503-normalized names forced active (spec §2)."""
    exclude_projects: tuple[str, ...] = ()
    """``-E``: PEP-503-normalized names switched off (spec §2)."""


def root_options(ctx: typer.Context) -> RootOptions:
    """Typed read of the root callback's stash — ``meta['_otto_root_options']``.

    ``ctx.meta`` is click's untyped per-invocation dict; this is the one seam
    that narrows the stash back to :class:`RootOptions`, so the readers don't
    each carry an unsound ``Any`` assignment. Absence stays a ``KeyError``:
    the strict readers run only after the root callback has stored the options.
    """
    opts = ctx.meta["_otto_root_options"]
    assert isinstance(opts, RootOptions), (  # noqa: S101 — internal invariant: the root callback stashed a RootOptions
        f"meta['_otto_root_options'] holds {type(opts).__name__}, not RootOptions"
    )
    return opts


def maybe_root_options(ctx: "typer.Context | None") -> "RootOptions | None":
    """:func:`root_options` for paths where the root callback may not have run.

    Library callers and tests invoke some gates without a ctx (or with a ctx
    the root callback never saw); both simply mean "no root options".
    """
    if ctx is None or ctx.meta.get("_otto_root_options") is None:
        return None
    return root_options(ctx)


_COMMAND_SPEC_KEY = "_otto_command_spec"
"""``click.Context.meta`` key: the spec of the invocation's command (:func:`stash_command_spec`)."""


def stash_command_spec(ctx: typer.Context, spec: "CommandSpec") -> None:
    """Keep *spec* as the spec of the command *ctx*'s invocation runs.

    The root group stashes the dispatched command's spec before it dispatches,
    so its pending mark and a group's lab probe are decided for that command;
    the leaf-invoke wrapper stashes its own before the preamble runs.
    ``ctx.meta`` is shared down the context chain, so every context of the
    invocation reads the same one.
    """
    ctx.meta[_COMMAND_SPEC_KEY] = spec


def settle_for_root_listing(ctx: typer.Context) -> None:
    """Clear the dispatch's mark for a root listing, which never runs its command.

    ``otto --list-hosts run x`` and ``--show-lab``, ``--list-products``,
    ``--list-tools`` answer in the root callback and exit: the invocation
    records nothing, so the root's mark is settled and the stashed command
    spec is dropped, and the lab they load reads defaults.
    """
    settle_options()
    ctx.meta.pop(_COMMAND_SPEC_KEY, None)


def command_spec(ctx: typer.Context) -> "CommandSpec":
    """Typed read of the invoked command's spec — ``meta['_otto_command_spec']``.

    Stashed by :func:`stash_command_spec` before the preamble runs; same
    seam-per-key rule as :func:`root_options`.
    """
    from .registry import CommandSpec

    spec = ctx.meta[_COMMAND_SPEC_KEY]
    assert isinstance(spec, CommandSpec), (  # noqa: S101 — internal invariant: the invoke wrapper stashed a CommandSpec
        f"meta['_otto_command_spec'] holds {type(spec).__name__}, not CommandSpec"
    )
    return spec


def ensure_cli_session(ctx: typer.Context) -> None:
    """Initialise CLI logging once per invocation (idempotent).

    Split out of :func:`ensure_lab_context` so a soft lab probe (class-scoped
    ``otto host`` menus via :func:`try_ensure_lab`) never touches logging.
    Guarded by ``ctx.meta['_otto_session_ready']``. The banner is not printed
    here — it shows on help screens only, via :func:`ensure_help_banner`.

    ``init_cli_logging`` configures the ROOT logger, so there is nothing to
    register: product code, suite modules and third-party libraries all reach
    otto's sinks through plain propagation. The one per-logger knob is the
    noise floor: otto's defaults go on with the console handler, and each
    project's ``[logging.levels]`` is merged over them here — the first point at
    which project settings are guaranteed parsed.

    The console handler itself is NOT news by this point: the root callback
    raised it as soon as it had ``--log-level`` (spec 2026-08-30 §3.1), so
    ``init_cli_logging``'s install is an idempotent re-affirmation. What this
    call owns is the per-invocation state the file sinks read later (xdir,
    retention, rich-file flag). The project-aware half — the merged
    ``[logging.levels]`` overrides and the ``_HostFilter`` on the console — is
    :func:`otto.session.install_logging`, the same call a library caller makes.
    """
    meta = ctx.meta
    if meta.get("_otto_session_ready"):
        return
    meta["_otto_session_ready"] = True

    from ..logger import management

    opts = root_options(ctx)

    management.init_cli_logging(
        xdir=opts.xdir,
        log_level=opts.log_level,
        keep_days=opts.log_days,
        show_time=opts.show_time,
        rich_log_file=opts.rich_log_file,
    )

    logger = getLogger(__name__)
    if opts.dry_run:
        # Says which of the two dry runs this is. The old wording ("Connections
        # will still be verified") became false the moment the seam landed: a
        # bare dry run now touches NOTHING, and a connection happens only when
        # the operator asks for one with --probe.
        contact = (
            "--probe will open a connection to each named host, and run no command."
            if opts.probe
            else "No device will be contacted."
        )
        logger.info(
            f"[magenta][DRY RUN] Commands and file transfers will be skipped. {contact}",
            extra={"markup": True},
        )

    # The project-aware half, shared with library callers: every project's
    # [logging.levels] over otto's defaults (a conflict refuses), and the
    # _HostFilter on the console. After the dry-run notice, which stays the
    # first line of a dry run. init_cli_logging above owns the CLI-only file
    # sinks; this re-affirms the console it installed with the same level.
    from ..session.logs import install_logging

    install_logging(log_level=opts.log_level, show_time=opts.show_time)


def ensure_lab_context(ctx: typer.Context) -> "OttoContext":
    """Load the lab, build reservation state, and install the runtime context (idempotent).

    Builds the lab through :func:`otto.session.build_lab_and_view` (which enforces
    ``--lab``, aggregates the projects' sources, loads the lab and registers the
    declared docker placeholder hosts) and keeps the inventory view it was built
    through, for :func:`session_inventory_view`; resolves reservation state (stashed on
    ``ctx.meta['otto_reservation']``), and installs an ``OttoContext`` with
    :func:`~otto.context.set_context`, registering its reset on the root Click
    context's ``call_on_close`` so it ends with the invocation. Guarded by
    ``ctx.meta['_otto_lab_ready']`` so repeated calls are cheap and register once.
    No banner, no logging init, no output dir — those belong to
    :func:`ensure_cli_session` / :func:`command_preamble`.

    Raises (never prints) :class:`~otto.session.LabBuildError` or
    :class:`~otto.reservations.check.ReservationBackendError`: the loud callers wrap
    the call in :func:`lab_context_refusals`, which renders through
    :func:`report_lab_context_error`; the soft ``HostGroup`` probe
    (:func:`try_ensure_lab`) swallows them.

    The whole call runs inside the command's pending mark
    (:func:`pending_options`), so for a command that accepts registered
    options (or carries its own class) the lab build and every constructor
    it runs are refused an option read (``OptionsNotAvailableError``) rather
    than answered with defaults; the installed context carries that mark
    until the command records its flags or :func:`settle_options` clears it.
    A leaf that does not mark has settled the root group's mark before its
    preamble (the leaf-invoke wrapper), so its build reads defaults. A command
    that has already read its flags (a lab-free leaf whose body opens the
    lab) is past that window: the record in force is carried onto the
    context instead, so its body and hooks keep reading its values.
    """
    # The flag first: a read of the record in force may create an empty one
    # on an installed context, which a command that has not read its flags
    # must not do.
    in_force = recorded_in_force() if ctx.meta.get(_LEAF_PREPARED) else None
    if in_force is not None and not in_force.pending:
        recorded: "RecordedOptions | None" = in_force
    else:
        recorded = pending_options(ctx)
    with command_options(recorded):
        return _ensure_lab_context(ctx, recorded)


def _ensure_lab_context(ctx: typer.Context, recorded: "RecordedOptions | None") -> "OttoContext":
    """:func:`ensure_lab_context`'s body, run inside the pending mark *recorded*."""
    from ..run_context import fill_teardown_deadline, get_context

    meta = ctx.meta
    if meta.get("_otto_lab_ready"):
        return get_context()

    opts = root_options(ctx)

    from ..bootstrap import bootstrap
    from ..invocation import RunPolicy, install_policy, installed_policy, reset_binding
    from ..session.lab import build_lab_and_view

    # The lab build reads the run policy (ingest picks each product's variant
    # entry by it), so the policy is installed before it. The root callback
    # installed one; a caller driving the group directly ran none, and the
    # root options carry it. That policy is prepared as the root callback's
    # is: its reset registered at once, then its deadline filled from discovery.
    policy = installed_policy()
    if policy is None:
        policy = RunPolicy(dry_run=opts.dry_run, variant="field" if opts.field else "debug")
        binding = install_policy(policy)
        ctx.find_root().call_on_close(lambda: reset_binding(binding))
        fill_teardown_deadline(policy)

    result = bootstrap()  # already run by entry(): the cached composition root
    projects = result.projects
    # `--lab` is not a hard-required Typer option (so lab-free subcommands can
    # run without it); the build refuses an empty selection (`no_labs`) before
    # any lab side effect, for everything that does need a lab.
    built = build_lab_and_view(projects, labs=list(opts.labs or []))
    lab = built.lab
    # The build registered the declared container placeholders. Its view is the
    # session's: a verb that reads the inventories again reads this one.
    meta[_INVENTORY_VIEW_KEY] = built.view

    # Resolve reservation identity + backend (first project with a [reservations]
    # section wins). With -R the backend is NOT constructed at all, so a broken
    # or hanging scheduler can never block lab access (break-glass). A broken
    # backend's ReservationBackendError propagates to the loud callers.
    from ..reservations.factory import build_reservation_gate

    reservation_gate = build_reservation_gate(
        projects=projects,
        holder=opts.holder,
        skip_reservation_check=opts.skip_reservation_check,
        cwd_fallback=Path.cwd(),
    )

    identity = reservation_gate.identity
    if identity is not None and identity.source == "--holder":
        getLogger(__name__).info(
            rf"[bold magenta]\[reservations] acting as {escape(repr(identity.username))}"
            rf" (--holder)[/bold magenta]",
            extra={"markup": True, "emoji": False},
        )

    meta["otto_reservation"] = reservation_gate

    # Install the runtime context: lab + the run policy installed above + the
    # per-invocation project switches (-I/-E), which every activation question
    # reads back through otto.config.scope.active. Its reset runs when Click
    # closes this invocation's root context, before the policy's (registered
    # first, by the root callback or the fallback above), so the context
    # never outlives the invocation. Installed here, outside the command's
    # event loop: the leaf's coroutine runs later under run_command, in a copy
    # of this execution context.
    from ..run_context import OttoContext, record_options, reset_context, set_context

    context = OttoContext(
        lab=lab,
        include_projects=opts.include_projects,
        exclude_projects=opts.exclude_projects,
        policy=policy,
        bootstrap=result,
    )
    token = set_context(context)
    ctx.find_root().call_on_close(lambda: reset_context(token))
    if recorded is not None:
        # The mark outlives the block on the context, until the leaf records
        # or ``settle_options`` clears it.
        record_options(context, recorded)
    meta["_otto_lab_ready"] = True
    return get_context()


_INVENTORY_VIEW_KEY = "otto_inventory_view"
"""Where :func:`ensure_lab_context` keeps the session's inventory view on ``ctx.meta``."""


def session_inventory_view(ctx: typer.Context) -> "InventoryView":
    """Return the inventory view the session's lab was built through: its one reader.

    :func:`ensure_lab_context` records it beside the lab, so a verb that reads
    the inventories after the build (``otto lab explain``) reads the very
    objects the build read, and builds no second view.

    Raises:
        RuntimeError: The session has no lab yet; call
            :func:`ensure_lab_session` (or :func:`ensure_lab_context`) first.
    """
    view = ctx.meta.get(_INVENTORY_VIEW_KEY)
    if view is None:
        raise RuntimeError("no lab session: ensure_lab_context has not built the lab")
    return view


def try_ensure_lab(ctx: typer.Context) -> "OttoContext | None":
    """Soft variant of :func:`ensure_lab_context`: return None instead of raising.

    Used by ``HostGroup`` class-scoping — a soft probe where any failure (no
    ``--lab``, unknown lab, broken backend) simply means "no class scoping
    available", falling back to the full unscoped verb menu. The one failure
    it does not swallow is ``OptionsNotAvailableError``: a constructor that
    read an option before the command read its flags is a defect to report.
    """
    try:
        return ensure_lab_context(ctx)
    except OptionsNotAvailableError:
        raise
    except Exception:  # noqa: BLE001 — soft scoping probe: ANY failure (incl. typer.Exit, an Exception subclass) → no scoping
        return None


def validate_project_switches(selection: "ScopeSelection") -> None:
    """Exit 2 when *selection*'s ``-I``/``-E`` names a project discovery never found (spec §2).

    Takes the selection :func:`scope_selection` returned, so it validates
    wherever the selection is read: in the preamble, in an inline lab load,
    and before the root callback has recorded anything. It claims no saving
    on user code: ``entry()`` has already called ``bootstrap()`` before argv
    parsing on every non-completion invocation, and the gate that follows it
    calls it again. The early return below is just "nothing was asked for, so
    look nothing up".

    The name is normalized on BOTH sides — the switch values arrive
    PEP-503-normalized from the root parameters' callback, so comparing them
    against a raw ``project.name`` would reject the only spelling the CLI can
    produce.

    The rule is :func:`otto.session.select_projects`'s, the same one
    :func:`~otto.context.open_context` applies; this function only renders its refusal,
    through :func:`report_project_selection_error`. Printed manually +
    ``typer.Exit(2)`` rather than raised as a ``click.UsageError``: a real one
    raised after parse escapes Typer 0.26's vendored click fork uncaught, which
    is why :func:`report_lab_context_error` hand-writes its missing-``--lab``
    usage text too. The overlap arm of :func:`report_project_selection_error` is
    unreachable from here: the root callback has already refused an overlap.
    """
    if not (selection.include or selection.exclude):
        return
    from ..bootstrap import bootstrap
    from ..config.scope import refused_names
    from ..session.errors import ProjectSelectionError
    from ..session.projects import select_projects_with_refused

    try:
        result = bootstrap()
        select_projects_with_refused(
            result.projects,
            list(selection.include),
            list(selection.exclude),
            refused=refused_names(result.errors),
        )
    except ProjectSelectionError as e:
        report_project_selection_error(e)


OTTO_ROOT_ATTR = "__otto_root__"
"""Set ``True`` on otto's root group class (``otto.cli.main._OttoGroup``).

How :func:`scope_selection` knows a context's root is otto's: a sub-app
driven on its own (a test's ``DispatchRunner``, ``run_cli`` on a group) has a
root that does not carry it, and so has no selection."""


def selection_from_params(params: "Mapping[str, Any]") -> "ScopeSelection":
    """Turn the root command's parsed parameters into the ``-l`` / ``-I`` / ``-E`` selection.

    The one reader of those three parameters. Pure: it takes the ``str`` items
    of each when it is a list, as they are, and anything else as empty. It
    splits and normalises nothing: the root parameters' callbacks
    (``otto.cli.main.parse_lab_selection`` and ``parse_project_list``) have
    already split ``-l`` on ``+`` and ``-I``/``-E`` on commas, and normalised
    the project names, before the values reach the parsed parameters, under
    a completion parse too. ``OTTO_LAB`` arrives through ``labs``.

    The root callback builds its record (:class:`RootOptions`) through it, and
    :func:`scope_selection` calls it, so the two never disagree.
    """
    from ..config.scope import ScopeSelection

    def _names(key: str) -> "tuple[str, ...]":
        value = params.get(key)
        if not isinstance(value, list):
            return ()
        return tuple(item for item in value if isinstance(item, str))

    return ScopeSelection(
        labs=_names("labs"),
        include=_names("include_projects"),
        exclude=_names("exclude_projects"),
    )


def scope_selection(ctx: typer.Context) -> "ScopeSelection":
    """Read the invocation's ``-l`` / ``-I`` / ``-E`` selection (``OTTO_LAB`` too).

    Read from the parsed parameters of *ctx*'s root context through
    :func:`selection_from_params`, not from the root callback's record: the
    root's own parse sets them on every path, so the answer is the same on a
    dispatch, in a command's ``--help``, under a completion parse (where the
    root callback records nothing) and for a top-level command resolved before
    the root callback runs. A root that is not otto's (no
    :data:`OTTO_ROOT_ATTR` on its command: a sub-app driven on its own)
    selected nothing: an empty selection, so every project takes part.
    """
    root = ctx.find_root()
    if getattr(root.command, OTTO_ROOT_ATTR, False):
        return selection_from_params(root.params)
    from ..config.scope import ScopeSelection

    return ScopeSelection(labs=(), include=(), exclude=())


def unknown_lab_on_failed_parse(ctx: typer.Context) -> None:
    """Report an unknown lab in place of a command line's failed parse; else return.

    Called by otto's root group when a command's parse fails with ``No such
    option``, before that error is printed. With no lab selected
    (:func:`scope_selection`: ``-l`` or ``OTTO_LAB``; a root that is not otto's
    selects none) it returns at once. Otherwise the lab is loaded by the load
    step of the lab build (``otto.session.lab.load_lab_and_view``: no context
    is bound, no reservation gate is built, no container host is registered).
    An unknown lab is raised through :func:`report_lab_context_error`, in the
    words and with the exit status a line that parses gets. Every other
    outcome returns, so the parser's error stands: the load succeeds, the lab
    is out of reach, a source or the inventory cannot be read, or anything
    else is raised. With several labs the load's first error decides. Which
    option failed is not asked. Only this failure path pays the load: a line
    that parses, its ``--help`` and a completion parse never reach it.
    """
    labs = list(scope_selection(ctx).labs)
    if not labs:
        return
    from ..bootstrap import bootstrap
    from ..session.errors import LabBuildError
    from ..session.lab import load_lab_and_view

    try:
        load_lab_and_view(bootstrap().projects, labs=labs)
    except LabBuildError as e:
        if e.kind == "unknown_lab":
            report_lab_context_error(e)
    except Exception:  # any other outcome leaves the parser's error
        getLogger(__name__).debug("lab check after a failed parse raised", exc_info=True)


def listing_participation(ctx: typer.Context, projects: "Sequence[Project]") -> "Participation":
    """Answer which of *projects* take part in this listing -- the one listing preamble.

    Validates the switches and the bootstrap world like the leaf preamble, once:
    with a lab selected, :func:`ensure_inline_lab` runs both gates before it
    loads the lab; without one, they run here. With a lab the answer is the
    lab's verdict; without one it says so (``print_no_lab_cue``) and answers
    with the pre-lab selection.

    A listing records no options: the lab context is installed under the
    command's pending mark (:func:`ensure_lab_context`), and the mark is
    cleared as soon as the lab is in, so a conftest or test module that reads
    an options class while the listing collects gets its defaults, as in a
    lab-free leaf, instead of ``OptionsNotAvailableError``
    (:func:`settle_options`: ``otto test``'s listings answer at parse time,
    before its options are prepared).
    """
    from ..config.scope import participating
    from ..run_context import get_context
    from .listing import print_no_lab_cue

    selection = scope_selection(ctx)
    if selection.labs:
        ensure_inline_lab(ctx)
        settle_options()
        return participating(projects, ctx=get_context())
    validate_project_switches(selection)
    fail_loud_on_bootstrap_errors(selection)
    print_no_lab_cue()
    return participating(projects, selection=selection)


def fail_loud_on_bootstrap_errors(selection: "ScopeSelection | None" = None) -> None:
    """Exit(1) on an ACTIVE project's load error, or one an active project requires — loud gate.

    The per-error ``warning:`` lines were already printed by ``entry()`` at
    startup; print ONLY the framed summary here (don't re-print each error
    in red) — the summary points back at those warnings. Used by the leaf
    preamble AND the root ``--show-lab``/``--list-hosts`` branch, so anything
    that inspects the registered world fails the same way.

    *selection* is the invocation's ``-l`` / ``-I`` / ``-E``, the one
    :func:`scope_selection` returned; the caller reads it once and hands the
    same value to :func:`validate_project_switches`, so the two gates never
    judge different selections. With it, an error attributable to a project
    that is inactive for this invocation demotes to one warning line (spec
    §3): a broken sibling must not fail a run it is not part of. Inactivity
    here is the PRE-LAB projection — the explicit switches plus lab-name inference
    (:func:`otto.config.scope.inactive_before_lab`) — because this gate is what
    protects lab construction from a half-registered world and so has to run
    before it; a project that is inactive only because it is host-starved
    therefore keeps its errors fatal. So does a project an active project
    requires, directly or through a chain of required dependencies, however it
    was made inactive: one ``error:`` line names the requiring projects and the
    fixes. Attribution is by ``root``: an error belonging to no discovered
    project (a ``settings.toml`` that failed to PARSE, so no
    :class:`~otto.config.project.Project` object exists) can never be judged
    inactive and stays fatal. Without *selection* — library callers, tests —
    nothing is selected and no lab is named, so every error is fatal.

    The rule is :func:`otto.session.check_projects`'s, the same one
    :func:`~otto.context.open_context` applies; this function only renders its verdict.
    """
    from ..bootstrap import bootstrap
    from ..session.errors import ProjectLoadError
    from ..session.projects import ProjectSelection, check_projects

    result = bootstrap()
    if not result.errors:
        return
    # No selection (a test, a library caller): nothing is selected and no lab
    # is named, so check_projects demotes nothing: every error is fatal.
    switches = (
        ProjectSelection(include=list(selection.include), exclude=list(selection.exclude))
        if selection is not None
        else ProjectSelection()
    )
    labs = list(selection.labs) if selection is not None else []
    try:
        check = check_projects(result=result, labs=labs, selection=switches)
    except ProjectLoadError as e:
        _echo_demotions(e.demoted)
        _echo_required(e.required)
        from rich import print as rprint

        rprint("[red]Cannot run commands while a project fails to load (see warnings above).[/red]")
        raise typer.Exit(1) from None
    _echo_demotions(check.demoted)


def _echo_demotions(demoted: "list[DemotedProject]") -> None:
    """Print each demoted load error as today's stderr ``warning:`` line, in flag spelling."""
    # PRINTED, not logged — and no longer because logging is missing.
    # The root callback installs the console handler before this gate
    # runs (spec 2026-08-30 §3.1), so a `logger.warning` here WOULD be
    # seen. Two reasons that never had anything to do with when
    # logging starts keep it a print:
    #
    # STREAM. `entry()` has already printed `warning: <err>` for this
    # very error, from `_emit_bootstrap_findings`, which runs before
    # Typer parses argv and so can never become a log record. That line
    # goes to stderr; otto's console handler writes to stdout. Logging
    # this one would put the retraction on a different stream from the
    # scary line it retracts, where a redirect separates them — and the
    # run CONTINUES past here, so stdout belongs to the command.
    #
    # MARKUP. The lab-inferred reason interpolates the selection in
    # SQUARE BRACKETS, and `rich.print` reads `[unix]` as a style tag and
    # deletes it, leaving `lab(s) )`. `typer.echo` renders the sentence as
    # written.
    for d in demoted:
        typer.echo(
            f"warning: project {d.name!r} failed to load, but is inactive "
            f"for this run ({_inactive_flag_reason(d.reason, d.normalized_name, d.labs)})"
            " — continuing without it",
            err=True,
        )


def _echo_required(required: "list[RequiredProject]") -> None:
    """Print why each inactive project an active project requires still refused the run.

    One ``error:`` line per project, in flag spelling, printed beside the
    demotion lines and for the same reasons (see :func:`_echo_demotions`).
    The sentence is the library's (:attr:`otto.session.RequiredProject.message`),
    built by the same helper with the flag spelled in.
    """
    from ..session.projects import _required_sentence

    for r in required:
        why = _inactive_flag_reason(r.reason, r.normalized_name, r.labs)
        typer.echo(
            f"error: {_required_sentence(r, why=why, switch='--exclude-projects')}", err=True
        )


def _inactive_flag_reason(reason: "PreLabReason", normalized_name: str, labs: list[str]) -> str:
    """Say why a project is inactive before the lab, in flag spelling."""
    from ..session.projects import _inactive_reason_text

    return _inactive_reason_text(reason, normalized_name, labs, switch="--exclude-projects")


def present_reservation_gate(ctx: typer.Context) -> None:
    """Evaluate the active reservation gate (if any) and present its warning.

    Reads ``ctx.meta["otto_reservation"]`` — a no-op when absent (e.g. a
    lab-free command, or a test that never populated it) — and calls
    :meth:`~otto.reservations.check.ReservationGate.evaluate`. ``evaluate()``
    returns a :class:`~otto.reservations.check.ReservationGateResult` whose
    ``warning`` is deliberately plain text (the library has no Typer/rich
    dependency); this function OWNS the presentation of that text — it is
    the single place that styles it bold red (as a literal ``Text``, so a
    holder name in it prints as written).
    Its one caller is :func:`open_live_lab`, which both ``command_preamble``
    and the live branch of ``otto monitor`` go through, so the gate is
    evaluated and styled in exactly one place.

    ``MissingReservationError`` (raised by ``evaluate()`` when a required
    resource isn't held) is not caught here — it propagates to the caller
    unchanged, exactly as it did before this adapter existed.
    """
    res = ctx.meta.get("otto_reservation")
    if res is None:
        return
    outcome = res.evaluate()
    if outcome.warning:
        from rich import print as rprint
        from rich.text import Text

        rprint(Text.assemble((outcome.warning, "bold red")))


def ensure_lab_session(ctx: typer.Context, spec: "CommandSpec") -> None:
    """Run the lab-requiring slice of the leaf-invoke preamble: session, lab, output dir.

    Called only by :func:`open_live_lab`, the one live-lab opening that
    :func:`command_preamble` (every ordinary, non-``lab_free`` command) and the
    live branch of ``otto monitor`` (:mod:`otto.cli.monitor`, a ``lab_free``
    command whose ONE branch needs a lab) both run. Idempotent
    (``ensure_cli_session``/``ensure_lab_context`` each guard their own
    re-entry), except for the output-dir creation, which a caller should
    therefore invoke at most once per command invocation.

    Does not touch the refusals or the reservation gate; :func:`open_live_lab`
    owns their order.
    """
    ensure_cli_session(ctx)
    with lab_context_refusals():
        ensure_lab_context(ctx)

    leaf_wants_dir = bool(getattr(ctx.command.callback, "__cli_output_dir__", True))
    if spec.output_dir and leaf_wants_dir:
        from ..logger import management
        from ..run_context import get_context

        # A single command (``test``) or a flattened single-command group
        # (``monitor``) IS the top-level command: its leaf name equals
        # ``spec.name``, so there is no meaningful sub-name — pass None to keep
        # the base ``test/<TS>`` dir (not ``test/<TS>_test``). Real sub-groups
        # (run/host) keep their ``<name>/<TS>_<sub>`` layout since
        # ``ctx.command.name`` differs.
        leaf_name = ctx.command.name
        sub = None if leaf_name == spec.name else (leaf_name or spec.name)
        get_context().policy.output_dir = management.create_output_dir(spec.name, sub)


def open_live_lab(ctx: typer.Context, spec: "CommandSpec", *, gate: bool) -> None:
    """Open a live lab for one command: session and lab, the refusals, the gate.

    The ONE sequence every command that reaches lab hosts runs, in this order:
    :func:`ensure_lab_session` (the lab verdicts it installs are the input to
    what follows), :func:`refuse_inactive_instruction`,
    :func:`refuse_unsatisfied_dependencies`, then, when *gate* is true,
    :func:`present_reservation_gate`. A run that is being refused must not
    first be warned about the reservations it would have needed, so the gate
    is last.

    *gate* is the caller's decision, because the two callers reach it
    differently: :func:`command_preamble` asks :func:`leaf_is_gated`, while
    ``otto monitor --live`` (registered ``gate=False`` so its review branch,
    a local file, is exempt) always gates. The dry-run seam is not part of
    this sequence; :func:`command_preamble` runs it after, for every command.
    """
    ensure_lab_session(ctx, spec)
    refuse_inactive_instruction(ctx)
    refuse_unsatisfied_dependencies()
    if gate:
        present_reservation_gate(ctx)


def refuse_inactive_instruction(inner_ctx: typer.Context) -> None:
    """Refuse dispatching a project-owned instruction whose project is inactive (spec §5).

    Runs in the leaf preamble AFTER the lab session exists, so the verdict can
    come from :func:`otto.config.scope.active` — the one authority — rather than
    from a pre-lab projection of it. Ordering is load-bearing, not cosmetic:
    before the session there are no ``scopes_of(ctx)`` verdicts at all, and a
    missing verdict resolves ACTIVE, so a gate hoisted above it would refuse
    nothing except an explicit ``-E``.

    Help and completion never reach here (click exits during parse), so every
    registered instruction stays listed. That is deliberate: activation is
    per-invocation, and hiding entries would make ``-I`` undiscoverable —
    the reader would have no way to learn the name the hint tells them to pass.

    Exit 1, not 2: the command line is well-formed; the configuration excludes
    it. A usage error would tell the reader to fix their typing, which is the
    wrong place to look.
    """
    # Climb to the node whose PARENT is the ``run`` group -- for an ordinary
    # instruction that is the leaf itself, and for one registered as a
    # sub-group (``add_typer``) it is the group, whose name is the registry
    # key. Anything else -- ``otto host <id> get``, ``otto link impair`` --
    # exits at the root with ``parent is None`` and is left alone; this gate
    # is about instruction dispatch, not about every command in the CLI.
    from ..instruction_catalog import INSTRUCTION_GROUP, INSTRUCTIONS

    node = inner_ctx
    while (
        node.parent is not None and getattr(node.parent.command, "name", None) != INSTRUCTION_GROUP
    ):
        node = node.parent
    if node.parent is None:
        return  # not dispatched through `otto run`

    # `or ""`: click types `info_name` as `str | None`, and an unnamed node owns
    # nothing -- "" is never a registered instruction, so the one lookup below
    # covers both "no name" and "not an instruction" without a second branch.
    name = node.info_name or ""
    if name not in INSTRUCTIONS:
        return  # a statically-declared `run` child; nobody owns it
    owner = INSTRUCTIONS.project(name=name)

    from ..run_context import get_context
    from ..session.activation import check_instruction_active
    from ..session.errors import InstructionInactiveError

    try:
        check_instruction_active(name=name, owner=owner, ctx=get_context())
    except InstructionInactiveError as e:
        # Through `fail`, never a hand-rolled `[red]` f-string: the message
        # interpolates a lab list and the literal table name `[project]`, and rich
        # DELETES `[word]` as markup unless it is escaped -- the demotion warning
        # a few functions up was observed rendering "lab(s) [unix]" as "lab(s) )"
        # for exactly this reason. `print_error` prints it literally; the
        # `error-render-through-helper` ast-grep rule keeps this route the only one.
        # soft_wrap: the hint's whole job is to hand over a runnable switch. Rich
        # folds between WORDS at the console width, so at 80 columns an ordinary
        # project name splits `or: -I firmware-integration` across the fold and the
        # token cannot be pasted. The detail line above is prose and would be fine
        # folded; the hint is not, and they share one render.
        fail(_inactive_instruction_text(e), soft_wrap=True)


def _inactive_instruction_text(err: "InstructionInactiveError") -> str:
    """Today's two-line refusal, in flag spelling, formatted from the library's facts."""
    loaded = ", ".join(err.loaded_labs)
    # Bound once so the type checker narrows the literal across the arms and
    # `assert_never` below fails the build when the library grows a reason.
    reason = err.reason
    if reason == "excluded":
        detail = f"which was switched off for this run (--exclude-projects {err.project})"
        hint = f"  activate it: remove --exclude-projects {err.project}    or: -I {err.project}"
    elif reason == "out_of_lab_scope":
        # No loaded lab applies. The fix is a different `-l`, so the hint
        # names the patterns the project actually declared.
        patterns = ", ".join(err.lab_patterns) or "(none)"
        detail = f"which is inactive for the loaded lab(s) [{loaded}] (lab_patterns: {patterns})"
        wanted = " / -l ".join(sorted(err.lab_patterns)) or "<a matching lab>"
        hint = f"  activate it: -l {wanted}    or: -I {err.project}"
    elif reason == "host_starved":
        # Labs match, hosts do not. Different cause, different fix -- the
        # same distinction `otto.config.scope._unusable_scope_message`
        # draws for the walks, so the two surfaces stay readable together.
        patterns = ", ".join(err.host_patterns) or "(none)"
        detail = (
            f"which is inactive: its [project] host_patterns ({patterns}) "
            f"match no host in the loaded lab(s) [{loaded}]"
        )
        hint = f"  activate it: widen host_patterns, or: -I {err.project}"
    else:
        assert_never(reason)
    return f"{err.instruction!r} belongs to project {err.owner!r}, {detail}\n{hint}"


def refuse_unsatisfied_dependencies() -> None:
    """Refuse when an ACTIVE project declares a Python requirement this env lacks (spec §3).

    Runs in the leaf preamble AFTER the lab session exists, for the same reason
    :func:`refuse_inactive_instruction` does and no other: severity here is an
    ACTIVATION question, and :func:`otto.config.scope.active` -- the one
    authority -- needs the lab verdicts the session installs. The spec asked for
    this check inside ``bootstrap()``, which runs before any of that exists;
    those two requirements cannot both hold, and the authority is the half worth
    keeping. The pre-lab projection :func:`otto.config.scope.inactive_before_lab`
    would have refused a HOST-STARVED project -- one whose labs match but whose
    ``host_patterns`` select nothing -- because that shape is undetectable
    before the lab is built. Here it is simply inactive, and warns.

    Two consequences of the position, both deliberate:

    * **Library callers are checked by the same rule.** ``open_context`` runs
      the same :func:`otto.session.check_dependencies` once its context is
      installed; this function only renders the verdict in the CLI's words.
    * **An EAGER import failure never gets here.** ``bootstrap()``'s per-project
      init loop runs first, so a project whose init module imports a missing
      package at module scope is already a ``BootstrapError`` and
      :func:`fail_loud_on_bootstrap_errors` reports it generically. Only the
      LAZY shape -- the import inside an instruction body, the one that would
      otherwise fail mid-run with hosts already touched -- is what this gate
      exists to pre-empt.

    Called only by :func:`open_live_lab`, the one live-lab opening that
    :func:`command_preamble` runs for every non-``lab_free`` leaf and the live
    branch of ``otto monitor`` runs for itself. That is also what keeps
    ``otto env sync`` -- the verb the refusal names -- outside the gate that
    would otherwise refuse to let you run the fix.
    """
    from ..run_context import get_context
    from ..session.dependencies import check_dependencies
    from ..session.errors import DependencyRefusedError

    # PRINTED to stderr, not logged and not through rich -- the same call and
    # the same reasons as the demotion warning in
    # `fail_loud_on_bootstrap_errors`: the run CONTINUES past here so stdout
    # belongs to the command, and a requirement string carries brackets
    # (`otto-sh[monitor] >= 1`) that every rich-rendering route -- the console
    # handler included -- would delete as a style tag. Not the swallowed-record
    # reason, which the early console handler retired (spec 2026-08-30 §3.1).
    try:
        warnings = check_dependencies(ctx=get_context())
    except DependencyRefusedError as e:
        for warning in e.warnings:
            typer.echo(f"warning: {warning}", err=True)
        lines = [
            f"error: project {bad.project!r} requires {bad.requirement!r} — not satisfied in "
            f"this environment (found: {bad.found})"
            for bad in e.unsatisfied
        ]
        # `env sync` ALWAYS, and always second: it is the verb that fixes this
        # whatever the cause, and it is the one an operator who has never built an
        # orchestration venv needs to be told about. The direct install is third
        # because it is for the operator who manages the environment by hand --
        # correct, but it leaves otto's record of the env untouched.
        lines.append("  fix: otto env sync")
        installs = " ".join(f"{bad.requirement!r}" for bad in e.unsatisfied)
        lines.append(f"  or:  uv pip install {installs}")
        # Through `fail`, never a hand-rolled `[red]` f-string. A requirement
        # carries brackets whenever it names an extra (`otto-sh[monitor] >= 1`) and
        # rich DELETES `[word]` as markup -- printing a runnable command for the
        # wrong package. `print_error` prints it literally; the `error-render-through-helper`
        # ast-grep rule keeps this route the only one. soft_wrap because both fix
        # lines are meant to be PASTED, and rich folds between words at the console
        # width.
        fail("\n".join(lines), 1, soft_wrap=True)
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)


def ensure_help_banner(ctx: typer.Context) -> None:
    """Print the banner before rendered help text, once per invocation (idempotent).

    Independent of :func:`ensure_cli_session`: every help screen shows the
    banner regardless of which command it belongs to, while real command
    execution never shows it. Guarded by ``ctx.meta['_otto_help_banner_shown']``
    — ``ctx.meta`` is shared by reference down the whole context chain, so one
    guard covers the root context and every leaf/group context beneath it.
    """
    meta = ctx.meta
    if meta.get("_otto_help_banner_shown"):
        return
    meta["_otto_help_banner_shown"] = True

    from .banner import print_banner

    print_banner()


LAB_FREE_ATTR = "__cli_lab_free__"
"""Per-leaf opt-out from the lab slice of the preamble, for a leaf under a
lab-bound group that needs no lab (``otto cov kmodcov export``). Read the same
way as ``__cli_output_dir__``; the dry-run seam still applies."""

GATE_ATTR = "__cli_gate__"
"""Per-leaf opt-out from the reservation gate, for a leaf under a gated group
that loads a lab but contacts no host (``otto cov report``, ``otto docker
use-cases``). ``False`` exempts the leaf; absent means gated. Read the same way
as ``__cli_output_dir__``, by :func:`leaf_is_gated`."""


def leaf_is_lab_free(spec: "CommandSpec", callback: object) -> bool:
    """Whether the preamble skips the lab slice (and so the gate) for this leaf."""
    return spec.lab_free or bool(getattr(callback, LAB_FREE_ATTR, False))


def leaf_is_gated(spec: "CommandSpec", callback: object) -> bool:
    """Decide whether the reservation gate runs before this leaf touches anything.

    A lab-free leaf never loads a lab, so it has no reservation to evaluate; a
    group registered ``gate=False`` and a leaf marked ``__cli_gate__ = False``
    opt out explicitly. Everything else is gated.
    """
    return (
        not leaf_is_lab_free(spec, callback)
        and spec.gate
        and bool(getattr(callback, GATE_ATTR, True))
    )


ACCEPTS_OPTIONS_ATTR = "__cli_accepts_options__"
"""Per-leaf answer to whether a command offers the flags of registered options
classes, for a leaf beneath a group that differs from its group's registration.
``True`` or ``False``; absent means the leaf says nothing. Read by
:func:`leaf_accepts_options`, beneath a group only: a top-level command is
judged by its registration, and a marker on one that disagrees is refused when
a real parse resolves it (:func:`refuse_a_disagreeing_marker`)."""


def _accepts_marker(callback: object) -> "bool | None":
    """Read *callback*'s :data:`ACCEPTS_OPTIONS_ATTR`: ``None`` when it carries none."""
    marker = getattr(callback, ACCEPTS_OPTIONS_ATTR, None)
    return None if marker is None else bool(marker)


def spec_accepts_options(spec: "CommandSpec") -> bool:
    """Whether a registration offers the flags of registered options classes.

    The registration alone, so it is known without importing the command: its
    ``accepts_options`` when set, else whether it is not ``lab_free``.
    """
    if spec.accepts_options is not None:
        return spec.accepts_options
    return not spec.lab_free


def leaf_accepts_options(spec: "CommandSpec", callback: object, *, beneath_group: bool) -> bool:
    """Whether this command offers the flags of registered options classes.

    The one reader. Beneath a group: the callback's
    :data:`ACCEPTS_OPTIONS_ATTR` when it carries one; else the registration's
    ``accepts_options`` when set; else whether the leaf is not lab-free
    (:func:`leaf_is_lab_free`, which honours the leaf's own lab-free marker).
    A top-level command (*beneath_group* false) is judged by its registration
    alone (:func:`spec_accepts_options`): a lab-free marker on its callback
    keeps its meaning for the lab and does not change this answer.
    """
    if not beneath_group:
        return spec_accepts_options(spec)
    marker = _accepts_marker(callback)
    if marker is not None:
        return marker
    if spec.accepts_options is not None:
        return spec.accepts_options
    return not leaf_is_lab_free(spec, callback)


def refuse_a_disagreeing_marker(spec: "CommandSpec", command: object) -> None:
    """Refuse a top-level single command whose marker disagrees with its registration.

    *command* is the command *spec* resolved to. A group passes (its leaves'
    markers are theirs to carry); so does a single command (a function, or an
    app Typer flattens to its one command) with no :data:`ACCEPTS_OPTIONS_ATTR`
    on its callback, or one that agrees with :func:`spec_accepts_options`.

    The root group calls it when a real parse (a dispatch, the command's own
    ``--help``) resolves the command, never under a resilient one (a TAB, the
    completion cache's rebuild walk), so a misregistered command still
    completes.

    Raises:
        OptionsRegistrationError: the marker disagrees; the message names the
            command, both answers and the fix.
    """
    if hasattr(command, "commands"):
        return
    marker = _accepts_marker(getattr(command, "callback", None))
    registered = spec_accepts_options(spec)
    if marker is None or marker == registered:
        return
    raise OptionsRegistrationError(
        f"command {spec.name!r}: its callback's {ACCEPTS_OPTIONS_ATTR} says "
        f"{marker} but its registration answers {registered} for whether it "
        "accepts registered options; set accepts_options= on the registration"
    )


def command_path(ctx: typer.Context, spec: "CommandSpec") -> list[str]:
    """Name the command *ctx* runs: *spec*'s registered name, then the commands beneath it.

    A list of command names from the top level down to the leaf, the shape
    ``otto.params.covering`` takes.

    The top-level command's context is the child of otto's root context
    (:data:`OTTO_ROOT_ATTR`), or the root itself when the root is not otto's (a
    sub-app driven on its own), so the first word is the registration's name
    either way. An argument such as a host id is not a context and is not in
    the path: ``otto host r1 install`` is ``["host", "install"]``.
    """
    below = [c.info_name or c.command.name or "" for c in _from_top_level(ctx)[1:]]
    return [spec.name, *below]


def _from_top_level(ctx: Any) -> list[Any]:
    """Return the contexts from the top-level command's down to *ctx*.

    The top-level command's context is the child of otto's root context
    (:data:`OTTO_ROOT_ATTR`), or the root itself when the root is not otto's.
    Empty for otto's root context itself.
    """
    # Any: a parent is the vendored click fork's Context, not typer.Context.
    chain: list[Any] = []
    node: Any = ctx
    while node is not None:
        chain.append(node)
        node = node.parent
    chain.reverse()
    top = 1 if getattr(chain[0].command, OTTO_ROOT_ATTR, False) else 0
    return chain[top:]


def command_preamble(ctx: typer.Context) -> None:
    """Run once when a real (non-help) command invocation starts.

    Order: ``-I``/``-E`` names are validated → an active project's bootstrap
    errors fail loud → lab-free commands skip the lab slice → CLI session
    (logging) → lab context → per-command output dir → an inactive project's
    instruction is refused → an active project's unsatisfied Python dependencies
    are refused → reservation gate → the command's options are prepared
    (:func:`prepare_command_options`; a bad value is a usage error here, and a
    leaf that records no options has its pending mark cleared) → the dry-run
    seam. ``--help`` paths never
    reach this function: click's help option exits during leaf parse, before
    ``Command.invoke``.

    The seam is LAST on purpose. Everything above it is the validating half of
    the dry-run contract (spec §1: arguments coerce, the lab loads, references
    resolve, the gate speaks), and a dry run has to pay all of it before it is
    allowed to claim the command "would run" — otherwise ``-n`` degrades into
    print-and-exit and a typo'd host reference exits 0. The seam adds only the
    stop, and it applies to ``lab_free`` commands too: ``lab_free`` means "I
    drive the lifecycle myself", which is exactly the command that could still
    reach a device (``otto monitor --live``) with nobody watching.
    """
    meta = ctx.meta
    if meta.get("_otto_preamble_done"):
        return
    meta["_otto_preamble_done"] = True

    # Order matters: a typo'd -I/-E name is reported as a typo, rather than
    # being silently absorbed into a demotion decision the gate makes next.
    # One read of the selection serves both gates, so they cannot disagree.
    selection = scope_selection(ctx)
    validate_project_switches(selection)
    fail_loud_on_bootstrap_errors(selection)

    spec = command_spec(ctx)
    # A leaf under a lab-bound group may opt out of the lab slice with the
    # same per-leaf mechanism ``ensure_lab_session`` reads for the output dir:
    # ``otto cov kmodcov export`` writes a local directory and touches no host.
    callback = ctx.command.callback
    if not leaf_is_lab_free(spec, callback):
        open_live_lab(ctx, spec, gate=leaf_is_gated(spec, callback))

    # The values are validated before the seam, so a dry run fails the same
    # way; a leaf that records nothing reads defaults from here on.
    prepare_command_options(ctx)
    stop_at_dry_run_seam(ctx, spec)


# ---------------------------------------------------------------------------
# The --dry-run seam: validate, print what would run, stop before the body
# ---------------------------------------------------------------------------

DRY_RUN_PREVIEW_ATTR = "__cli_dry_run_preview__"
"""Per-leaf opt-out marker read off the resolved command's callback.

Stamped by ``@cli_exposed(dry_run_preview=True)`` (host verbs) and by
``otto.cli.test`` (the ``otto test`` leaf); read here the same way
``ensure_lab_session`` reads ``__cli_output_dir__``, so a third-party leaf
opts in through exactly the mechanism otto's own leaves use.
"""

DRY_RUN_REFS_ATTR = "__otto_dry_run_refs__"
"""Per-leaf hook naming the lab references a dry run must still resolve.

A callable ``(ctx) -> Iterable[LabReference]``. The seam runs it BEFORE it
prints anything, so an unknown host/link/tunnel reference fails the way it
always did instead of being echoed back as if it existed. It exists because
reference resolution lives in command BODIES, and the seam's whole job is not
running those -- the hook lets a leaf lend the seam the same resolver its body
would have used (``otto host`` lends :func:`~otto.cli.host.resolve_cli_host`),
so there is one authority rather than a mirrored copy that can drift.
"""


@dataclasses.dataclass(frozen=True)
class LabReference:
    """One lab entity a command names, resolved before the dry run reports it."""

    kind: str
    """What kind of entity this is -- ``"host"``, ``"link"`` or ``"tunnel"``."""

    name: str
    """The resolved identifier, as the block should print it."""

    host_ids: "list[str]" = dataclasses.field(default_factory=list)
    """Lab host ids this reference names, for ``--dry-run --probe`` to dial."""

    term: "str | None" = None
    """Per-invocation terminal-protocol override applying to :attr:`host_ids`
    (``otto host --term``), or ``None`` for the host's configured default.

    Carried on the reference so ``--probe`` dials the transport the command
    would actually have used. Without it the probe re-fetches the lab-default
    instance and can report a host reachable over SSH for an invocation that
    was going to use telnet -- a true statement about the wrong question."""

    transfer: "str | None" = None
    """Per-invocation file-transfer override applying to :attr:`host_ids`
    (``otto host --transfer``), or ``None``. Same reason as :attr:`term`, and it
    is not cosmetic: ``transfer='ftp'`` makes one probe open the FTP control
    channel as well as the term channel."""


def dry_run_requested(ctx: typer.Context) -> bool:
    """Whether this invocation is a dry run.

    Prefers the root callback's recorded options (the flag the user actually
    typed) and falls back to :func:`~otto.host.host.is_dry_run`, the installed
    run policy — which is what a sub-app driven without the root callback
    (unit tests, an embedder) installs, and what the root callback builds from
    the flag anyway, so the two can only ever agree on the real dispatch path.

    Read with ``getattr``, not attribute access: ``_otto_root_options`` is a
    plain ``ctx.meta`` slot, and several preamble tests park a bare sentinel
    there. A seam that raised ``AttributeError`` on an unfamiliar stash would
    fail the invocation at the one line whose job is to answer a yes/no.
    """
    flag = getattr(ctx.meta.get("_otto_root_options"), "dry_run", None)
    if flag is not None:
        return bool(flag)
    from ..host.host import is_dry_run

    return is_dry_run()


def probe_requested(ctx: typer.Context) -> bool:
    """Whether this dry run may open connections (``--probe``, spec §3).

    Read ONLY from the root callback's recorded options — no run-policy
    fallback, unlike :func:`dry_run_requested`. ``dry_run`` lives on the run
    policy (the context reads it) because the LIBRARY layer branches on it at
    every device boundary; ``--probe`` is a CLI presentation choice that
    nothing below the seam consults, and putting it on the policy would invite
    exactly that. A sub-app driven without the root
    callback therefore never probes, which is the safe answer.
    """
    return bool(getattr(ctx.meta.get("_otto_root_options"), "probe", False))


def _leaf_declares_preview(ctx: typer.Context) -> bool:
    """Whether the resolved leaf stamped itself as owning its own dry-run preview."""
    return bool(getattr(getattr(ctx.command, "callback", None), DRY_RUN_PREVIEW_ATTR, False))


def _leaf_self_finishes_dry_run(ctx: typer.Context) -> bool:
    """Whether the resolved leaf finishes its own dry run.

    Its preparation's answer (:attr:`LeafOptions.finishes_dry_run`, from what
    the command says is its own); a command prepared nothing finishes none.
    Such a leaf awaits :func:`finish_dry_run` itself, after its options are
    built from the values its function receives (never ``ctx.params``, which
    holds values before Typer's conversion), so a dry run builds the objects
    a real run would.
    """
    prepared = prepared_options(ctx)
    return prepared is not None and prepared.finishes_dry_run


def resolve_dry_run_references(ctx: typer.Context) -> "list[LabReference]":
    """Resolve the lab references the leaf names, or return an empty list.

    Any failure propagates: a leaf's resolver raises/exits exactly as its body
    would have, so ``otto host nosuchbox exec 'uptime' -n`` still exits
    non-zero with the resolution error.
    """
    resolver = getattr(getattr(ctx.command, "callback", None), DRY_RUN_REFS_ATTR, None)
    if resolver is None:
        return []
    return list(resolver(ctx))


def _param_words(ctx: typer.Context) -> "list[str]":
    """Echo one context's non-default parameters back as command-line words.

    Every parameter the command parsed (``get_params``: its own and those
    the invocation attached). A command prepared when it was invoked
    (:func:`prepared_options`) gives the attached values as the parser held
    them (:attr:`LeafOptions.raw`) and the names to mask
    (:attr:`LeafOptions.sensitive`: a ``SecretStr``/``SecretBytes`` field or
    one declared ``repr=False``, over every class it offers); any other
    command masks nothing. A masked
    name has its VALUE replaced with the literal ``<hidden>`` -- the flag
    still shows (an operator needs to know the switch was given), only what
    followed it is masked. A boolean flag carries no separate value word to
    mask.
    """
    import shlex

    prepared = prepared_options(ctx)
    sensitive = prepared.sensitive if prepared is not None else frozenset()
    values = {**ctx.params, **(prepared.raw if prepared is not None else {})}
    words: list[str] = []
    for param in ctx.command.get_params(ctx):
        name = getattr(param, "name", None)
        if name is None or name not in values:
            continue
        value = values[name]
        if value is None or value == param.default:
            continue
        opts = [o for o in (getattr(param, "opts", None) or []) if o.startswith("-")]
        masked = name in sensitive
        items = list(value) if isinstance(value, (list, tuple)) else [value]
        for item in items:
            word = "<hidden>" if masked else shlex.quote(str(item))
            if not opts:  # a positional argument: the value IS the word
                words.append(word)
            elif isinstance(item, bool):
                # An on/off pair (`--random/--no-random`) turned OFF is echoed
                # by its off switch; a plain flag is only ever echoed on.
                off = [o for o in (getattr(param, "secondary_opts", None) or []) if o]
                if item:
                    words.append(max(opts, key=len))
                elif off:
                    words.append(max(off, key=len))
            else:
                words.extend((max(opts, key=len), word))
    return words


def would_run_line(ctx: typer.Context) -> str:
    """Rebuild the invocation from the parsed context chain, root command first.

    Reconstructed from what click PARSED rather than from ``sys.argv``, so it
    reflects the values the command would actually have received (env-var
    defaults included) and stays correct under any runner. The root context's
    own options are omitted: ``--lab`` is what the lab line reports, and
    echoing ``--dry-run`` back at someone who just typed it is noise.
    """
    chain: list[typer.Context] = []
    node: Any = ctx
    while node is not None:
        chain.append(node)
        node = node.parent
    chain.reverse()

    words: list[str] = []
    for depth, node in enumerate(chain):
        if node.info_name:
            words.append(node.info_name)
        if depth:
            words.extend(_param_words(node))
    return " ".join(words)


def _lab_line(references: "list[LabReference]") -> str:
    """Describe the lab the dry run validated against, and what resolved in it."""
    from ..run_context import try_get_context

    active = try_get_context()
    lab = getattr(active, "lab", None) if active is not None else None
    if lab is None:
        line = "lab: not loaded (lab-free command)"
    else:
        count = len(getattr(lab, "hosts", ()) or ())
        line = f"lab: {lab.name} ({count} host{'' if count == 1 else 's'})"
    if references:
        resolved = ", ".join(f"{ref.kind} {ref.name!r}" for ref in references)
        line = f"{line}; references resolve: {resolved}"
    return line


def _options_line(instance: Any) -> str:
    """Render one resolved options instance: ``ClassName: field=value, field=value``.

    Field DEFINITION order (``dataclasses.fields``, which walks the MRO base
    first, same order :func:`~otto.params.options_params` builds CLI flags
    in). A field :func:`~otto.params.sensitive_field_names` names -- ``repr=False``
    (``dataclasses.field``, or a pydantic ``Field``), or a type that IS or
    CONTAINS ``pydantic.SecretStr``/``SecretBytes`` -- prints ``name=<hidden>``
    unconditionally; every other field prints through plain ``repr()``.

    That SAME set, not a bare ``repr=False`` check, is the whole point: a
    pydantic (``@options``) class's ``SecretStr`` field already reprs masked
    (``SecretStr('**********')``) because pydantic COERCED the parsed string
    into a real ``SecretStr`` instance -- but a plain stdlib
    ``@dataclasses.dataclass`` field typed ``SecretStr``/``Optional[SecretStr]``
    never goes through pydantic construction at all, so its runtime value is
    still the raw parsed string, and a bare ``repr()`` of THAT would print the
    secret in full. Checking the set here closes that gap the same way
    :func:`_param_words` already had to.
    """
    sensitive = sensitive_field_names(type(instance))
    parts = [
        f"{f.name}=<hidden>" if f.name in sensitive else f"{f.name}={getattr(instance, f.name)!r}"
        for f in dataclasses.fields(instance)
    ]
    return f"{type(instance).__name__}: {', '.join(parts)}"


def print_dry_run_block(
    ctx: typer.Context,
    references: "list[LabReference] | None" = None,
    contacted: bool = False,
    options: "list[Any] | None" = None,
) -> None:
    """Print the dry run's product: what would run, and what it was checked against.

    Printed straight to the console, never through a logger. A dry run whose
    output is empty is a bug (spec §2), so the announcement must not be
    foldable by ``LogMode.QUIET``, a log level, or a capture filter — the
    payload is what gets suppressed under a dry run, never the announcement.

    *contacted* is ``True`` when ``--probe`` actually dialed at least one host,
    and it selects the headline. The default headline ends "and no device was
    contacted" — printing that immediately below a table reporting a host
    reachable would be a plain falsehood, and the one thing this contract
    cannot ship is a dry run that says something untrue about device contact.

    *options* is every option instance the caller already bound and built for
    this leaf (own class first, then the bound verb's classes) — one
    ``options:`` line, then one line per class via ``_options_line``.
    Empty or ``None`` (a leaf with no options at all) prints no ``options:``
    section, the same "suppress the payload, not its absence" rule the lab
    line already follows.

    Every line prints through ``otto.console.print_literal``, so a long
    resolved value stays one line.
    """
    from rich import get_console
    from rich.text import Text

    from ..console import print_literal
    from ..utils import DRY_RUN_HEADLINE, DRY_RUN_HEADLINE_PROBED

    console = get_console()
    headline = DRY_RUN_HEADLINE_PROBED if contacted else DRY_RUN_HEADLINE
    print_literal(Text(headline, style="magenta"), console=console)
    print_literal(f"  would run: {would_run_line(ctx)}", console=console)
    if options:
        print_literal("  options:", console=console)
        for instance in options:
            print_literal(f"    {_options_line(instance)}", console=console)
    print_literal(f"  {_lab_line(references or [])}", console=console)


def stop_at_dry_run_seam(ctx: typer.Context, spec: "CommandSpec") -> None:
    """Under ``--dry-run``, print what would run and exit 0 before the body.

    The default for every registered command, first- and third-party alike:
    zero author effort, and safe when the author never thought about dry runs
    at all. A command buys depth deliberately — ``dry_run_preview=True`` at
    registration (the whole group) or on the leaf's own ``@cli_exposed``
    stamp — and then owns its ``is_dry_run()`` branch.

    A no-op when this is not a dry run, which is every non-``-n`` invocation.

    A leaf that finishes its own dry run (``_leaf_self_finishes_dry_run``:
    its preparation says so) is handled not at all: it returns immediately, because such a
    leaf's OWN body calls :func:`finish_dry_run` itself with the options it
    was prepared with.

    Otherwise this function runs the probe step itself, synchronously, via
    ``run_probe`` — the ONLY place this path spins up a
    lifecycle event loop, and only when ``--probe`` was actually given — and
    then calls ``_conclude_dry_run``, the sync resolve/print/exit tail
    shared with :func:`finish_dry_run`. A plain ``-n`` stop (no ``--probe``)
    therefore starts no event loop at all: no signal handlers, no cleanup
    boundary, no teardown-deadline lookup, to do what is otherwise a few printed
    lines and a ``typer.Exit(0)``. This runs inside ``command_preamble``,
    before any leaf coroutine is even constructed, so staying loop-free here
    is not an optimization otto's own outer ``run_command`` would have made
    redundant — nothing else has spun one up yet. *spec*'s (or the leaf's
    own) ``dry_run_preview`` is passed straight through, so a previewing
    command (``link``/``tunnel``) still gets ``--probe``'s announcement/
    dial/report before its body runs for real, exactly as it always has.

    A command prepared when it was invoked (:func:`prepared_options`) shows
    the instances it was prepared with in the block's ``options:`` section.
    """
    if not dry_run_requested(ctx):
        return

    if _leaf_self_finishes_dry_run(ctx):
        return

    prepared = prepared_options(ctx)
    options = [] if prepared is None else list(prepared.instances.values())
    preview = spec.dry_run_preview or _leaf_declares_preview(ctx)
    step = _sync_probe_step(ctx)
    _conclude_dry_run(
        ctx, options, preview=preview, contacted=step.contacted, references=step.references
    )


@dataclasses.dataclass(frozen=True)
class _ProbeStep:
    """What the ``--probe`` step of a dry run did: whether it dialed, and what it resolved."""

    contacted: bool = False
    references: "list[LabReference] | None" = None
    """The references the probe resolved, or ``None`` when no probe ran."""


def _sync_probe_step(ctx: typer.Context) -> _ProbeStep:
    """Run ``--probe`` for a caller with no running loop; a no-op without the flag.

    Shared by :func:`stop_at_dry_run_seam` and :func:`print_preview_dry_run`.
    Uses the sync ``run_probe``, which spins up a lifecycle loop only when
    ``--probe`` was actually given.
    """
    if not probe_requested(ctx):
        return _ProbeStep()
    from .probe import print_probe_report, probe_contacted, run_probe

    # Resolution first, and its failure still wins: dialing a host set
    # derived from an unresolvable reference would be acting on a
    # reference the command could not use.
    references = resolve_dry_run_references(ctx)
    results = run_probe(references)
    print_probe_report(results)
    # `probe_contacted`, not `bool(results)`: a result set that is entirely
    # `not probed`, or entirely LocalHost, opened no socket, and the probed
    # headline would then assert a connection nobody made.
    return _ProbeStep(contacted=probe_contacted(results), references=references)


def print_preview_dry_run(ctx: typer.Context, options: "list[Any]") -> None:
    """Print a previewing sync leaf's dry-run block: probe (if requested), then the block.

    For a leaf that is :data:`DRY_RUN_PREVIEW_ATTR` and finishes its own dry
    run (its :class:`OwnOptions` says so) and runs without a loop of its own
    (``otto test``, whose body is sync because ``pytest.main`` is): it has
    already bound and built *options* from the real, typer-converted kwargs,
    exactly as a real run would, so a bad value has already failed. This runs
    the shared ``--probe`` step (its announcement prints whenever the flag was
    given, a no-hosts probe included), then prints the same ``would run:`` /
    ``options:`` / lab block the seam prints, with the same masking, and
    returns without exiting: the leaf prints its own preview after it.
    """
    step = _sync_probe_step(ctx)
    references = step.references
    if references is None:
        # Before the print, for the reason `_conclude_dry_run` gives.
        references = resolve_dry_run_references(ctx)
    print_dry_run_block(ctx, references, contacted=step.contacted, options=options)


def _conclude_dry_run(
    ctx: typer.Context,
    options: "list[Any]",
    *,
    preview: bool,
    contacted: bool,
    references: "list[LabReference] | None",
) -> None:
    """Resolve (if needed), print the dry-run block, and exit — never *preview*.

    The shared tail :func:`stop_at_dry_run_seam` and :func:`finish_dry_run`
    both call once their own probe step (sync ``run_probe``, or an ``await``
    of ``probe_references`` — the only difference between the two callers)
    is done. Deliberately sync end-to-end: resolving a reference, printing a
    block, and raising ``typer.Exit`` are ordinary sync work that never
    needed the event loop probing brings in, so factoring it out here is what
    lets the seam skip that loop entirely when ``--probe`` was not given. One
    authority for "what a dry run's tail prints", instead of two copies that
    could drift.

    *preview* is the escape hatch a command buys deliberately
    (``dry_run_preview=True`` at registration, or the leaf's own
    ``@cli_exposed`` stamp). ``False``, for a command that did not buy it,
    prints the ``would run:`` / ``options:`` / lab block and raises
    ``typer.Exit(0)``; ``True`` returns after the probe step, letting the
    caller's own body run for real under ``-n`` too.
    """
    if preview:
        return
    # Before the print, not after: the resolution error is the answer when a
    # reference does not exist, and a block claiming a bad command "would run"
    # is exactly the fabrication this whole contract exists to remove.
    if references is None:
        references = resolve_dry_run_references(ctx)
    print_dry_run_block(ctx, references, contacted=contacted, options=options)
    raise typer.Exit(0)


async def finish_dry_run(
    ctx: typer.Context, options: "list[Any]", *, preview: bool = False
) -> None:
    """Run the shared dry-run tail: probe (if requested), then resolve+print+exit, unless *preview*.

    Shared by two call sites, both leaves already running inside a coroutine
    that otto's OWN outer ``run_command`` is driving: a self-finishing leaf's
    own wrapper (``otto.cli.invoke._wrap_with_options``'s ``wrapper``, awaited
    directly once it has bound and built *options* from the real,
    typer-converted kwargs — so a bad value has already raised the identical
    ``typer.BadParameter`` a real run gives, before this is ever reached), and
    a project instruction's own leaf (``otto.cli.run._project_leaf``, likewise
    awaited after its own build). ``stop_at_dry_run_seam`` — the third dry-run
    stop, reached before any leaf coroutine exists — does not call this at
    all; it runs its own probe step synchronously and calls
    ``_conclude_dry_run`` directly, so that path never pays for a loop it
    does not need. This function and the seam share that same
    ``_conclude_dry_run`` tail, so "what a dry run's tail prints" still
    has one authority, not two.

    ``--probe`` (spec §3) always runs first when requested, whether or not
    *preview* is set — the reachability table prints for a previewing leaf
    and a stopped one alike, in both cases before the thing it informs.
    Without the flag not a single transport is opened here, which is what
    keeps "a dry run makes no device contact" true by default.

    *preview* is the escape hatch a command buys deliberately
    (``dry_run_preview=True`` at registration, or the leaf's own
    ``@cli_exposed`` stamp). ``False``, for a command that did not buy it,
    prints the ``would run:`` / ``options:`` / lab block and raises
    ``typer.Exit(0)``; ``True`` returns after the probe step, letting the
    caller's own body run for real under ``-n`` too.

    Async because dialing a host is (``otto.cli.probe.probe_references`` is
    awaited directly, never through a nested ``asyncio.run``) — both callers
    already own a running loop, so nothing here ever bridges in via a fresh
    ``run_command`` of its own.
    """
    contacted = False
    references: "list[LabReference] | None" = None
    if probe_requested(ctx):
        from .probe import print_probe_report, probe_contacted, probe_references

        # Resolution first, and its failure still wins: dialing a host set
        # derived from an unresolvable reference would be acting on a
        # reference the command could not use.
        references = resolve_dry_run_references(ctx)
        results = await probe_references(references)
        print_probe_report(results)
        # `probe_contacted`, not `bool(results)`: a result set that is entirely
        # `not probed`, or entirely LocalHost, opened no socket, and the probed
        # headline would then assert a connection nobody made.
        contacted = probe_contacted(results)

    _conclude_dry_run(ctx, options, preview=preview, contacted=contacted, references=references)


DRY_RUN_DECLINE = "dry run: this command was not run"
"""Per-RESULT decline announcement, distinct from ``DRY_RUN_HEADLINE``.

The run-level headline makes a device-contact claim; this one is printed for a
single declined result, possibly after ``--probe`` already dialed, so it says
only what that result knows. See ``_render_dry_run_decline``.
"""

RENDER_POLICY_KEY = "_otto_render_policy"


@dataclasses.dataclass(frozen=True)
class RenderPolicy:
    """Per-invocation presentation a leaf installs on ``ctx.meta`` for the wrapper."""

    success: str | None = None
    """Message rendered (green) for an ok, non-command Result."""

    none_message: str | None = None
    """Message rendered (green) when the leaf returns None; None = silent."""

    literal: bool = False
    """Print a text payload as plain text, never as Rich markup.

    Set by the ``otto host`` verbs: what they return is the device's text (a
    file's contents, a listing), and a bracket in it that Rich reads as a tag
    would be deleted -- or, for a lone ``[/x]``, raise -- instead of printed."""


def _print_value(value: Any, *, literal: bool) -> None:
    """Print one value: as plain text when *literal* and it is a ``str``, else via Rich."""
    from rich import print as rprint

    from ..console import print_literal

    if literal and isinstance(value, str):
        print_literal(value)
    else:
        rprint(value)


def _payload_or_decline(value: Any) -> Any:
    """Read a Result's payload, or report the decline and yield ``None``.

    A :class:`~otto.result.NotRunResult` raises on ``.value`` — deliberately,
    at the line that mistook a non-measurement for a measurement. That is
    right for a PARSER and wrong for a RENDERER: an error thrown at the print
    statement reports the mistake at a line that made none, which is the same
    misdirection the raising property exists to prevent, pointed the other
    way. So the renderer names the decline and prints nothing else.
    """
    from ..result import CommandNotRunError

    try:
        return value.value
    except CommandNotRunError as e:
        print_error(e)
        return None


def _render_dry_run_decline(value: Any) -> None:
    """Announce a result a dry run declined to produce, and never parse it.

    Exit code 0 is the point. ``NotRunResult.exit_code`` is 255 (ssh's
    "never connected"), which is the right answer to a LIBRARY caller asking
    whether the command ran, and the wrong answer to a USER who asked for a
    dry run and got exactly what they asked for. The seam stops most bodies
    long before here; this covers a command that opted into a preview and
    handed its decline back for rendering.

    The msg-less fallback deliberately does NOT reuse ``DRY_RUN_HEADLINE``.
    That constant ends "and no device was contacted" — a claim about the whole
    invocation, which a per-RESULT announcement is in no position to make, and
    which ``--dry-run --probe`` can falsify outright (spec §3): the preview path
    reaches this printer *after* the probe may have dialed. Saying only what
    this one result knows removes the claim instead of trying to keep it
    accurate from a function that cannot see the probe.
    """
    from rich import print as rprint
    from rich.text import Text

    if value.msg:
        rprint(Text.assemble((str(value.msg), "magenta")))
        return
    command = str(getattr(value, "command", "") or "")
    detail = f" ({command})" if command else ""
    rprint(Text.assemble((f"{DRY_RUN_DECLINE}{detail}", "magenta")))


def _has_rich_renderable(items: "list[Any]") -> bool:
    """Whether *items* carries a ``Table`` or ``Text`` renderable.

    Narrows the bare-list fallback below: a plain ``list[str]`` (``otto host
    <id> ls``, ``glob``) must keep printing as the one pre-existing pretty
    object, not one line per string — only a list actually carrying a Rich
    renderable needs the per-item path.
    """
    from rich.table import Table
    from rich.text import Text

    return any(isinstance(item, (Table, Text)) for item in items)


def _rprint_each(items: "list[Any]", *, literal: bool = False) -> None:
    """Print one item per line: a Table/Text renders itself, a str prints plain.

    Handing a whole list to a single ``rprint()`` call pretty-prints it as one
    Python object instead of rendering each item — the hazard this exists to
    avoid, shared by a ``Result`` payload list and a plain list return alike.
    *literal* prints a ``str`` item as plain text (see :attr:`RenderPolicy.literal`).
    """
    for item in items:
        _print_value(item, literal=literal)


def _render_ok_result(value: "Result", success: "str | None", *, literal: bool = False) -> None:
    """Render a successful ``Result``: the success message, or its payload."""
    from rich import print as rprint
    from rich.text import Text

    from ..result import CommandResult, Results
    from ..utils import Status

    if isinstance(value, (CommandResult, Results)):
        return  # command output already streamed during execution
    if value.status is Status.Skipped and value.msg:
        # A skip is a pass that did nothing; the generic success line would
        # read as though the work ran.
        rprint(Text.assemble((f"skipped: {value.msg}", "yellow")))
        return
    if success:
        rprint(f"[green]{success}[/green]")
        return
    payload = _payload_or_decline(value)
    if isinstance(payload, dict):
        for src, entry in payload.items():
            line = f"{src} -> {_payload_or_decline(entry)}"
            _print_value(line, literal=literal)
    elif isinstance(payload, list):
        _rprint_each(payload, literal=literal)
    elif payload is not None:
        _print_value(payload, literal=literal)


def _render_failed_result(value: "Result") -> None:
    """Print a failed ``Result``'s message(s) and raise its exit code.

    The failed entries' messages are printed one per line. The result's own
    message is printed above them unless it only restates them: a transfer's
    aggregate message is its entries' messages joined, and printing both said
    every file's error twice.
    """
    from ..result import Result, Results

    payload = _payload_or_decline(value)
    entries: list[Result] = []
    if isinstance(payload, dict):
        entries = [e for e in payload.values() if isinstance(e, Result)]
    elif isinstance(value, Results):
        entries = list(value)
    details = [e.msg for e in entries if not e.is_ok and e.msg]
    # Restated: nothing is left of the message once the entries' own messages
    # are taken out of it, but the punctuation that joined them.
    # Longest first: one entry's message can end with another's.
    rest = value.msg
    for detail in sorted(details, key=len, reverse=True):
        rest = rest.replace(detail, "")
    restated = bool(details) and not rest.strip(" ;,\n")
    if value.msg and not restated:
        print_error(value.msg)
    for detail in details:
        print_error(detail)
    raise typer.Exit(value.exit_code)


def render_leaf_value(value: Any, policy: "RenderPolicy | None" = None) -> None:
    """Render a leaf command's return value and signal failure via exit code.

    Implements the documented "Return values" contract
    (:doc:`/extend/extending-cli`) for every registered command and
    instruction: an ``otto.result`` family value derives the process exit code
    from its own ``exit_code`` (the ssh-like rules); a
    :class:`~otto.result.CliReport` prints its report text as plain text; a
    ``bool`` is printed as-is and exits 0 for ``True``, 1 for ``False`` (the
    predicate verbs); any other non-``None`` value is printed as-is, exit 0.
    ``None`` renders nothing by default —
    every side-effect-only first-party leaf returns it, so a leaf that wants a
    completion message must say so via :class:`RenderPolicy` (installed on
    ``ctx.meta[RENDER_POLICY_KEY]``, e.g. by the ``otto host`` verb bodies).

    A ``Status.NotRun`` result is a dry run's decline, not a failure: it is
    announced, never parsed, and never turned into a non-zero exit (the
    library-facing 255 answers a different question than the user's).
    """
    from rich import print as rprint

    from ..result import CliReport, Result
    from ..utils import Status

    if isinstance(value, Result):
        if value.status is Status.NotRun:
            _render_dry_run_decline(value)
            return
        if value.is_ok:
            _render_ok_result(
                value,
                policy.success if policy else None,
                literal=policy.literal if policy else False,
            )
            return
        _render_failed_result(value)
        return

    if value is None:
        if policy is not None and policy.none_message is not None:
            rprint(f"[green]{policy.none_message}[/green]")
        return

    literal = policy.literal if policy else False
    if isinstance(value, list) and _has_rich_renderable(value):
        _rprint_each(value, literal=literal)
        return
    if isinstance(value, CliReport):
        _print_value(value.__otto_cli_report__(), literal=True)
        return

    _print_value(value, literal=literal)  # documented third-party plain-value fallback
    if value is False:
        raise typer.Exit(1)  # a predicate answered False: print it, then fail like `test(1)`


def _require_async_leaf(cmd: Any, spec: "CommandSpec") -> None:
    """Refuse a sync leaf under a command whose lane demands coroutines.

    Only a coroutine reaches the bridge below, so a sync leaf runs with no
    cleanup boundary and no interrupt policy. ``@instruction`` checks this at
    its own decorator, but that is the SUGAR: a directly-registered
    ``InstructionEntry``, an ``@run_app.command()``, and a sub-group added
    with ``add_typer`` all reach ``otto run`` without passing it.

    Checked HERE — at invocation — rather than where commands resolve, and
    that placement is the whole point. ``TyperGroup.format_commands`` resolves
    every child to render a help table, so a resolve-time check makes
    ``otto run --help`` traceback for a user whose plugin ships one bad leaf,
    hiding the very list that would identify it. Completion has the same
    problem and would additionally false-positive on the synthesized stubs the
    cache serves. Invocation happens on exactly one path, reaches every leaf
    however it was registered, and cannot fire on a read-only one.

    Checks ``__otto_handler__`` before falling back to one ``__wrapped__``
    level, and deliberately does NOT walk the whole ``__wrapped__`` chain --
    that chain can include a user's own decorator, and a full unwrap cannot
    tell "otto's own wrapping, safe to see through" from "a user's sync
    bridging shim around an async leaf, the exact silent bypass this guard
    exists to catch" (that shim's ``def w(**k): fn(**k)`` never awaits
    ``fn(**k)``'s coroutine, so a full unwrap that reached the inner ``fn``
    would call the leaf async, wave the guard through, and still drop the
    coroutine on the floor). A data ``InstructionEntry`` now always passes
    through ``build_instruction_app`` -> ``prepare_command_target(path=...)``,
    which wraps every handler -- sync or async -- in its own ``async def``
    options-binding wrapper before typer wraps THAT again into the click
    callback, so a plain one-level unwrap would land on that (already-async)
    wrapper regardless of what the registered handler underneath it was.
    ``prepare_command_target`` closes exactly that gap by stamping
    ``__otto_handler__`` on its wrapped target -- a marker only OTTO'S OWN
    wrapping ever sets, so trusting it (when present) skips straight to the
    real handler without trusting anything a user's decorator wrote into the
    chain. One ``__wrapped__`` level remains the fallback for everything
    else (a directly-typer-registered leaf, an ``add_typer`` sub-group, or a
    user's own decorator with no ``__otto_handler__`` to shadow it), matching
    the group-callback guard below, which stays one level for the same
    reason and never needs the marker (a group callback never passes through
    ``_wrap_with_options``).
    """
    callback = getattr(cmd, "callback", None)
    if callback is None:
        return
    handler = getattr(callback, "__otto_handler__", None)
    checked = handler if handler is not None else getattr(callback, "__wrapped__", callback)
    if inspect.iscoroutinefunction(checked):
        return
    raise TypeError(
        f"{spec.name} command {getattr(cmd, 'name', '?')!r} is a plain `def`: only a "
        "coroutine reaches otto's lifecycle bridge, so its hosts are never swept and "
        "an interrupt never becomes a clean exit. Write it as `async def` — a body "
        "with nothing to await is still correct."
    )


_ATTACHED_PARAMS = "otto.attached_params"
"""``ctx.meta`` key: the parameters attached to each command of the invocation, by path."""

_ATTACHED_VALUES = "otto.attached_values"
"""``ctx.meta`` key: the attached values taken out of each command's ``ctx.params``, by path."""


OWN_OPTIONS_ATTR = "__otto_own_options__"
"""Per-leaf marker holding the command's :class:`OwnOptions`: what it says is its own.

Set on the callback when the command is built, read by :func:`offered_options`.
"""


@dataclasses.dataclass(frozen=True)
class OwnOptions:
    """What a command says is its own, fixed when the command is built.

    *cls* is its own options class, whose fields are already in its
    signature; *project* registered the command and is who a collision names
    for *cls*. *bodies* returns the body classes its own walk builds, every
    project's. *finishes_dry_run* says the command awaits its own dry-run
    tail. *reserved* names, for a collision message, each of its own
    parameters that is not a field of *cls*.
    """

    cls: type | None = None
    project: str | None = None
    bodies: "Callable[[], list[OptionsOrigin]] | None" = None
    finishes_dry_run: bool = False
    reserved: "Mapping[str, str]" = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Offer:
    """What one command offers for one invocation, beyond its own parameters.

    *params* are attached after the command's own. *spec* is what the
    command records under (its top-level command's name), the classes built
    before its body (its own class, then the covering ones) and the body
    classes its walk builds. *convert* turns the parser's values, the own
    class's and the attached ones alike, into what a function declaring them
    would receive. *finishes_dry_run* is the command's own answer. *own*
    names the fields of its own class (``_own_option_names``), whose
    values stay in its ``ctx.params`` for its function.
    """

    params: tuple[Parameter, ...]
    spec: LeafOptionSpec
    convert: "Callable[[Mapping[str, Any]], dict[str, Any]]"
    finishes_dry_run: bool
    own: frozenset[str] = frozenset()


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One options class described on its own, for completion.

    *key* is its registry key, *project* the project that registered it (or
    owns the body), *entries* the entries it serves (a body class: the one
    command it is a body of), and *params* its own parameters as Typer builds
    them, with no other class's.
    """

    key: str
    project: str | None
    entries: "tuple[str, ...]"
    params: tuple[Parameter, ...]


_OFFERED = "otto.offered_options"
"""``ctx.meta`` key: each command's :class:`Offer` (or ``None``) for the invocation, by path."""


_KEPT_PROJECTS = "otto.kept_projects"
"""``ctx.meta`` key: :func:`kept_projects`' answer for the invocation."""


def kept_projects(ctx: typer.Context) -> "frozenset[str] | None":
    """Return the projects whose classes the invocation *ctx* offers; ``None`` keeps every one.

    The one answer to which projects take part in what a command offers,
    asked once per invocation (kept in the root context's ``ctx.meta``).
    A root that is not otto's (no :data:`OTTO_ROOT_ATTR`: a sub-app driven
    on its own) keeps every project. Otherwise the invocation's selection
    (:func:`scope_selection`) is validated first
    (:func:`validate_project_switches`: an unknown ``-I``/``-E`` name exits 2
    before any command's flags are built, in ``--help`` too), and the answer
    is the names of the projects that take part at the command-line tier
    (``otto.config.scope.participating`` over the ``-l`` names and the
    switches, never a loaded lab). A completion parse never asks.
    """
    root = ctx.find_root()
    if not getattr(root.command, OTTO_ROOT_ATTR, False):
        return None
    if _KEPT_PROJECTS not in root.meta:
        from ..bootstrap import bootstrap
        from ..config.scope import participating

        selection = scope_selection(ctx)
        validate_project_switches(selection)
        kept = participating(bootstrap().projects, selection=selection).kept
        root.meta[_KEPT_PROJECTS] = frozenset(project.name for project in kept)
    return cast("frozenset[str]", root.meta[_KEPT_PROJECTS])


def _no_conversion(values: "Mapping[str, Any]") -> "dict[str, Any]":
    """Return *values* as they are: a completion parse never invokes, so nothing converts."""
    return dict(values)


def candidate_of(
    key: str,
    project: "str | None",
    entries: "tuple[str, ...]",
    resolve: "Callable[[], type]",
) -> "Candidate | None":
    """Describe one options class on its own: the one description completion reads.

    Calls *resolve* for the class and builds its own parameters
    (:func:`~otto.params.options_params`, through Typer), with no other
    class's. A class that cannot be resolved or built gives ``None``: this
    never raises for it.
    """
    return _described(
        key,
        project,
        entries,
        lambda: options_params(cast("type[DataclassInstance]", resolve())),
    )


def _described(
    key: str,
    project: "str | None",
    entries: "tuple[str, ...]",
    signature: "Callable[[], Sequence[inspect.Parameter]]",
) -> "Candidate | None":
    """Build the candidate of *key* from the parameters *signature* returns; ``None`` on failure."""
    try:
        carried = carrier(signature())
    except BaseException as exc:
        if not is_containable(exc):
            raise
        getLogger(__name__).debug(f"options class {key!r} is left out of completion: {exc!r}")
        return None
    return Candidate(key=key, project=project, entries=entries, params=carried.params)


def body_entries(path: "Sequence[str]") -> list[str]:
    """Return the entries of a body class's use: the one command *path* it is a body of.

    The one spelling of it, for a body's use (:func:`body_uses`) and the
    uses a command that does not accept is offered (``_offered_uses``). A
    use's ``entries`` hold it as a tuple.
    """
    return [" ".join(path)]


@dataclasses.dataclass(frozen=True)
class OptionsUse:
    """One use of an options class: what it serves, and where its class comes from."""

    registration: Any
    """Its ``otto.params.Registration``: its key, the entries it serves and its project.

    What ``otto.params.covering`` judges. Typed ``Any`` here, as
    :func:`recorded_registrations` is, because the API reference documents
    no ``otto.params.Registration`` to link to.
    """
    origin: "OptionsOrigin | None" = None
    """A body class's class and project, known already; ``None`` for a registration."""

    def resolve(self) -> OptionsOrigin:
        """Return its class and project: a body's as known, a registration's resolved.

        A registration is resolved by ``otto.params.resolve_registration``,
        which raises ``OptionsRegistrationError`` for a class that cannot be
        imported.
        """
        if self.origin is not None:
            return self.origin
        return resolve_registration(self.registration)

    def candidate(self) -> "Candidate | None":
        """Describe its class on its own (:func:`candidate_of`); ``None`` when that fails."""
        registration = self.registration
        return candidate_of(
            registration.key,
            registration.project,
            registration.entries,
            lambda: self.resolve().cls,
        )


def body_uses(path: "Sequence[str]", origins: "Iterable[OptionsOrigin]") -> "list[OptionsUse]":
    """Return one use per body class in *origins* of the command *path*, in order.

    Each use's entries are the command itself (:func:`body_entries`) and its
    project the body's. With :func:`registered_uses`, the one enumeration of
    the uses a command may be offered, for the live offer
    (:func:`offered_options`), the live candidates (:func:`live_candidates`)
    and the cache's description of every use
    (``otto.cli.registry.collect_option_candidates``).
    """
    entries = tuple(body_entries(path))
    return [
        OptionsUse(
            Registration(key=options_key(origin.cls), entries=entries, project=origin.project),
            origin,
        )
        for origin in origins
    ]


def registered_uses() -> "list[OptionsUse]":
    """Return one use per registration (``otto.params.registrations``), in registration order.

    Nothing is resolved: a use resolves its class when asked
    (:meth:`OptionsUse.resolve`).
    """
    from ..params import registrations

    return [OptionsUse(registration) for registration in registrations()]


def _body_origins(own: "OwnOptions | None", path: "Sequence[str]") -> "list[OptionsOrigin]":
    """Return *own*'s body classes (``OwnOptions.bodies``) for completion; none when it fails."""
    if own is None or own.bodies is None:
        return []
    try:
        return own.bodies()
    except BaseException as exc:
        if not is_containable(exc):
            raise
        getLogger(__name__).debug(f"otto {' '.join(path)}: no body classes: {exc!r}")
        return []


def _offered_uses(
    path: "Sequence[str]",
    among: "Sequence[Registration]",
    *,
    covered: bool,
    bodies: "Collection[str]",
    kept: "Collection[str] | None" = None,
) -> "list[Registration]":
    """Return the uses of *among* the command *path* is offered, in order.

    The one answer for the live offer (:func:`offered_options`), a live TAB
    (:func:`live_candidates`), a warm TAB (:func:`completion_candidates`)
    and the completion cache's walk (:func:`covering_uses`). *covered* is
    the command's gate's accepts answer (:attr:`OfferGate.accepts`): a
    command that accepts is offered every use covering *path*, its own
    bodies' included; one that does not is offered only the uses of its own
    body classes, those whose key is in *bodies* and whose entries are the
    command itself (:func:`body_entries`). Either way the uses are those
    ``otto.params.covering`` keeps for *kept*, the projects that take part
    (``None``, as a TAB asks, keeps every project). Nothing is resolved.
    """
    if not covered:
        entries = tuple(body_entries(path))
        among = [use for use in among if use.entries == entries and use.key in bodies]
    return covering(path, kept=kept, among=among)


def _body_keys(own: "OwnOptions | None", path: "Sequence[str]") -> frozenset[str]:
    """Return the registry keys of *own*'s body classes (:func:`_body_origins`)."""
    return frozenset(options_key(origin.cls) for origin in _body_origins(own, path))


def completion_candidates(
    path: "Sequence[str]", own: "OwnOptions | None", *, covered: bool
) -> "list[Candidate]":
    """Return the candidates a completion parse of the command *path* offers.

    The one answer, cold or warm. When the invocation reads the completion
    cache (``otto.registry.get_completion_names``) and its snapshot records
    the candidates (an ``option_candidates`` mapping), they come from it:
    the recorded uses ``_offered_uses`` keeps, each class's rows rebuilt
    as completion stubs, and no class is resolved. A snapshot that records
    none (one built by hand) is a miss, as for every other key a completer
    reads, and the answer is the live registry's (:func:`live_candidates`).
    *covered* is the command's gate's accepts answer
    (:attr:`OfferGate.accepts`), and *own* what the command says is its own,
    whose body classes it is offered either way.
    """
    from ..registry import get_completion_names

    names = get_completion_names()
    recorded = names.get("option_candidates") if names is not None else None
    if isinstance(recorded, dict):
        return _cached_candidates(path, recorded, own, covered=covered)
    return live_candidates(path, own, covered=covered)


def _cached_candidates(
    path: "Sequence[str]",
    recorded: "Mapping[str, Any]",
    own: "OwnOptions | None",
    *,
    covered: bool,
) -> "list[Candidate]":
    """Return the candidates of the command *path* from the cache's recorded ``option_candidates``.

    Each recorded use ``_offered_uses`` keeps (one
    :class:`~otto.params.Registration` per use, whatever its project), its
    class's rows rebuilt as completion stubs
    (``otto.config.completion_stubs.stub_param_from_row``): each parses as the
    live parameter does and suggests no value. No options class is
    imported or resolved; a malformed record gives no candidate, and nothing
    raises.
    """
    try:
        rows = recorded["rows"]
        among = recorded_registrations(recorded["uses"])
    except BaseException as exc:
        if not is_containable(exc):
            raise
        getLogger(__name__).debug(f"otto {' '.join(path)}: no recorded candidates: {exc!r}")
        return []
    uses = _offered_uses(path, among, covered=covered, bodies=_body_keys(own, path))
    if not uses:
        return []
    from ..config.completion_stubs import stub_param_from_row

    found = (
        _described(
            use.key,
            use.project,
            use.entries,
            lambda u=use: [stub_param_from_row(row) for row in rows[u.key]],
        )
        for use in uses
    )
    return [candidate for candidate in found if candidate is not None]


def live_candidates(
    path: "Sequence[str]", own: "OwnOptions | None", *, covered: bool = True
) -> "list[Candidate]":
    """Return the candidates of the command *path*, from the live registry, for completion.

    The uses ``_offered_uses`` keeps among one use per body class of
    *own* (:func:`body_uses`), then every registration
    (:func:`registered_uses`), each described on its own
    (:meth:`OptionsUse.candidate`). *covered* is the command's gate's accepts answer
    (:attr:`OfferGate.accepts`): a command that does not accept registered
    options is offered its bodies only. A class that fails is left out and
    costs no other its place; nothing raises. :func:`completion_candidates`
    is its one caller on a TAB.
    """
    uses = [*body_uses(path, _body_origins(own, path)), *registered_uses()]
    found = (use.candidate() for use in _offered(path, uses, covered=covered))
    return [candidate for candidate in found if candidate is not None]


def _offered(
    path: "Sequence[str]",
    uses: "Sequence[OptionsUse]",
    *,
    covered: bool,
    kept: "Collection[str] | None" = None,
) -> "list[OptionsUse]":
    """Return the uses of *uses* the command *path* is offered: ``_offered_uses``' answer.

    Asked over the uses' registrations for *kept*, the keys of the body uses
    among them (those that know their class) as the command's bodies.
    """
    by_id = {id(use.registration): use for use in uses}
    bodies = {use.registration.key for use in uses if use.origin is not None}
    offered = _offered_uses(
        path, [use.registration for use in uses], covered=covered, bodies=bodies, kept=kept
    )
    return [by_id[id(registration)] for registration in offered]


def recorded_registrations(uses: "Iterable[Mapping[str, Any]]") -> "list[Any]":
    """Return one ``otto.params.Registration`` per recorded use, in order.

    *uses* are the ``uses`` the completion cache's ``option_candidates``
    holds (``otto.cli.registry.collect_option_candidates``): each a key, the
    entries it serves and its project. The one reading of a use as what
    ``otto.params.covering`` judges, for the cache's walk
    (:func:`covering_uses`) and a warm TAB (:func:`completion_candidates`)
    alike. Raises ``KeyError`` or ``TypeError`` on a malformed use.
    """
    return [
        Registration(key=use["key"], entries=tuple(use["entries"]), project=use["project"])
        for use in uses
    ]


def covering_uses(cmd: Any, ctx: Any, among: "Sequence[Any]") -> list[int]:
    """Return the indexes into *among* of the recorded uses that cover the command *cmd*, in order.

    For the completion cache's walk: *ctx* is *cmd*'s context in otto's
    tree, and *among* holds one ``otto.params.Registration`` per use
    ``otto.cli.registry.collect_option_candidates`` recorded (its key,
    entries and project). The command is judged by the live offer's own
    gate (:func:`offer_gate`), for the top-level registration its path
    starts with, and the uses are those ``_offered_uses`` keeps, as a
    TAB keeps them: a command that accepts is offered every use covering
    its path, whatever its project; one that does not, only its own bodies'
    uses. Nothing is resolved.
    """
    from .registry import CLI_COMMANDS

    contexts = _from_top_level(ctx)
    name = contexts[0].info_name if contexts else None
    if name is None or name not in CLI_COMMANDS:
        return []
    gate = offer_gate(cmd, ctx, CLI_COMMANDS.peek(name=name))
    bodies = _body_keys(gate.own, gate.path)
    offered = _offered_uses(gate.path, among, covered=gate.accepts, bodies=bodies)
    kept = {id(registration) for registration in offered}
    return [index for index, registration in enumerate(among) if id(registration) in kept]


def offered_options(ctx: typer.Context, spec: "CommandSpec") -> "Offer | None":
    """Return what the command *ctx* parses offers for this invocation, or ``None``.

    The one offer, decided once per command and invocation (kept in
    ``ctx.meta`` under the command's path). ``None`` for a command that
    neither accepts registered options (:func:`leaf_accepts_options`) nor
    carries :data:`OWN_OPTIONS_ATTR`.

    Under a completion parse (``ctx.resilient_parsing``: a TAB) it is the
    union of :func:`completion_candidates` (the completion cache's recorded
    candidates on a warm TAB, the live registry's otherwise), the first name
    and the first spelling winning, the command's own parameters skipped:
    nothing is merged and nothing can fail.

    Otherwise the uses ``_offered_uses`` keeps for :func:`kept_projects`,
    among the command's body classes (:func:`body_uses`) and every
    registration (:func:`registered_uses`), are the ones offered: its bodies
    whether or not it accepts, the covering registrations only when it
    does, and each only when no project owns it or its project takes part,
    so a project that does not take part offers nothing and has nothing
    imported. The registrations kept are resolved, stopping at the first
    that fails. With no use kept and no :class:`OwnOptions`, ``None``, and
    nothing is resolved or built. The own class, the kept body classes and the covering
    classes are merged
    (:func:`~otto.params.merge_option_params`) with the command's other own
    parameters reserved by name and by spelling, ``-h``/``--help``
    included. A collision raises ``OptionsCollisionError`` naming both
    owners, and a class that cannot be imported ``OptionsRegistrationError``,
    from this command's own parse or ``--help`` only.
    """
    offered = cast("dict[str, Offer | None]", ctx.meta.setdefault(_OFFERED, {}))
    key = _attached_key(ctx, spec)
    if key not in offered:
        offered[key] = _offer(ctx, spec)
    return offered[key]


@dataclasses.dataclass(frozen=True)
class OfferGate:
    """What decides whether a command is offered anything beyond its own parameters."""

    path: "list[str]"
    """The command's path (:func:`command_path`)."""
    own: "OwnOptions | None"
    """What its callback says is its own (:data:`OWN_OPTIONS_ATTR`)."""
    accepts: bool
    """Whether it accepts registered options (:func:`leaf_accepts_options`)."""

    @property
    def offers(self) -> bool:
        """Say whether anything may be offered, which is so when it accepts or carries its own."""
        return self.accepts or self.own is not None


def offer_gate(command: Any, ctx: Any, spec: "CommandSpec") -> OfferGate:
    """Read the gate of what *command*, parsed under *ctx* beneath *spec*, is offered.

    The one reading of it, for the live offer (:func:`offered_options`)
    and the completion cache's walk (:func:`covering_uses`) alike, so the
    cache answers as the live offer does.
    """
    callback = getattr(command, "callback", None)
    path = command_path(ctx, spec)
    return OfferGate(
        path=path,
        own=cast("OwnOptions | None", getattr(callback, OWN_OPTIONS_ATTR, None)),
        accepts=leaf_accepts_options(spec, callback, beneath_group=len(path) > 1),
    )


def _leaf_spec(
    path: "Sequence[str]",
    own: "OwnOptions | None",
    origins: "Sequence[OptionsOrigin]",
    bodies: "Sequence[OptionsOrigin]",
) -> LeafOptionSpec:
    """Return what a command of *path* prepares over: one spec, read by every preparation.

    It records under its top-level command; its own class (*own*'s) and the
    covering classes (*origins*) are built before its body, the own class
    first and each once, and its *bodies*' classes are walked.
    """
    own_cls = own.cls if own is not None else None
    built = [*([own_cls] if own_cls is not None else []), *(o.cls for o in origins)]
    return LeafOptionSpec(
        group=path[0], build=tuple(dict.fromkeys(built)), walk=tuple(o.cls for o in bodies)
    )


def _offer(ctx: typer.Context, spec: "CommandSpec") -> "Offer | None":
    """Decide :func:`offered_options`' answer for the command *ctx* parses."""
    command: Any = ctx.command
    gate = offer_gate(command, ctx, spec)
    if not gate.offers:
        return None
    path, own, accepts = gate.path, gate.own, gate.accepts
    if ctx.resilient_parsing:
        mine = own_params(command, ctx)
        names = {param.name for param in mine}
        words = {word for param in mine for word in built_spellings(param)}
        union: list[Parameter] = []
        for candidate in completion_candidates(path, own, covered=accepts):
            for param in candidate.params:
                spelled = built_spellings(param)
                if param.name in names or any(word in words for word in spelled):
                    continue
                names.add(param.name)
                words.update(spelled)
                union.append(param)
        nothing_built = _leaf_spec(path, None, (), ())
        return Offer(tuple(union), nothing_built, _no_conversion, finishes_dry_run=False)
    kept = kept_projects(ctx)
    own_bodies = own.bodies() if own is not None and own.bodies is not None else []
    uses = [*body_uses(path, own_bodies), *registered_uses()]
    offered = _offered(path, uses, covered=accepts, kept=kept)
    if not offered and own is None:
        return None
    mine = own_params(command, ctx)
    origins = [use.resolve() for use in offered if use.origin is None]
    bodies = [use.origin for use in offered if use.origin is not None]
    own_cls = own.cls if own is not None else None
    own_origin = [OptionsOrigin(own_cls, own.project)] if own is not None and own_cls else []
    own_fields = _own_option_names(own_cls)
    what = f"otto {' '.join(path)}"
    described = own.reserved if own is not None else {}
    reserved = {
        param.name: described.get(param.name) or f"parameter {param.name!r} of {what}"
        for param in mine
        if param.name and param.name not in own_fields
    }
    reserved_flags = {
        word: reserved[param.name]
        for param in mine
        if param.name in reserved
        for word in built_spellings(param)
    }
    merged = merge_option_params(
        own_origin + bodies + origins,
        what=what,
        reserved=reserved,
        reserved_flags=reserved_flags,
    )
    carried = carrier(merged)
    return Offer(
        params=tuple(p for p in carried.params if p.name not in own_fields),
        spec=_leaf_spec(path, own, origins, bodies),
        convert=carried.convert,
        finishes_dry_run=own.finishes_dry_run if own is not None else False,
        own=own_fields,
    )


def attached_params(ctx: typer.Context, spec: "CommandSpec") -> Sequence[Parameter]:
    """Return the parameters this invocation attaches to the command *ctx* runs.

    The parameters of its :func:`offered_options`, or none. Asked once per
    context and command (:func:`attach_params` keeps the answer).
    """
    offer = offered_options(ctx, spec)
    return () if offer is None else offer.params


def _attached_key(ctx: typer.Context, spec: "CommandSpec") -> str:
    return " ".join(command_path(ctx, spec))


def _attached_for(
    ctx: typer.Context, spec: "CommandSpec", own: Sequence[Parameter]
) -> "tuple[Parameter, ...]":
    """Return what *ctx*'s command has attached, asking :func:`attached_params` once per path."""
    kept = cast("dict[str, tuple[Parameter, ...]]", ctx.meta.setdefault(_ATTACHED_PARAMS, {}))
    key = _attached_key(ctx, spec)
    if key not in kept:
        names = {param.name for param in own}
        kept[key] = tuple(p for p in attached_params(ctx, spec) if p.name not in names)
    return kept[key]


def attach_params(cmd: Any, spec: "CommandSpec") -> None:
    """Wrap leaf *cmd*'s ``get_params`` to add the invocation's attached parameters (idempotent).

    The wrapper returns the parameters the command's function declares, then
    :func:`attached_params` less any whose name the command already has, then
    every parameter Typer or click appends after the function's own, the help
    option among them (``otto.config._typer.attached_position``), so the attached flags keep the
    place they had when they were the function's own. Click's parse, both
    help renderers and shell completion read ``get_params``, so the attached
    flags parse, show and complete as the command's own. What was attached
    is kept in ``ctx.meta`` under the context's command path, so every call
    on one context returns the same objects (the parse compares parameters
    by identity) and nothing of one invocation stays on *cmd*, which
    outlives it. An error raised while attaching propagates from the parse:
    it stops that command only.
    """
    if getattr(cmd, "_otto_params_attached", False):
        return
    cmd._otto_params_attached = True  # noqa: SLF001 — own marker attribute on the command object
    original_get_params = cmd.get_params

    def _get_params_with_attached(inner_ctx: Any) -> list[Parameter]:
        own = original_get_params(inner_ctx)
        attached = _attached_for(inner_ctx, spec, own)
        if not attached:
            return own
        cut = attached_position(cmd, inner_ctx, own)
        return [*own[:cut], *attached, *own[cut:]]

    setattr(_get_params_with_attached, OWN_PARAMS_ATTR, original_get_params)
    cmd.get_params = _get_params_with_attached


def take_attached(ctx: typer.Context) -> "dict[str, Any]":
    """Take the attached values out of ``ctx.params`` and return them, keyed by name.

    *ctx* is the context of a command being invoked (its spec stamped, see
    :func:`command_spec`). The values are each attached parameter's as the
    parser produced it (already parsed by the parameter's type), and they are
    kept in ``ctx.meta``: a second call returns the same mapping. Called before
    the command's function, so the function is never passed a name it does
    not declare. With nothing attached: an empty mapping.
    """
    key = _attached_key(ctx, command_spec(ctx))
    taken = cast("dict[str, dict[str, Any]]", ctx.meta.setdefault(_ATTACHED_VALUES, {}))
    if key not in taken:
        attached = ctx.meta.get(_ATTACHED_PARAMS, {}).get(key, ())
        taken[key] = {
            p.name: ctx.params.pop(p.name) for p in attached if p.name and p.name in ctx.params
        }
    return taken[key]


_PREPARED = "otto.prepared_options"
"""``ctx.meta`` key: each invoked command's preparation for the invocation, by path."""


@dataclasses.dataclass(frozen=True)
class _Prepared:
    """One invoked command's preparation, with the command it was made for."""

    command: Any
    options: LeafOptions


def prepare_command_options(ctx: typer.Context) -> "LeafOptions | None":
    """Prepare the options of the command *ctx* invokes: validated before its body, once.

    *ctx* is the context of a command being invoked (its spec stamped, see
    :func:`command_spec`). With its :func:`offered_options`, the attached
    values (:func:`take_attached`) and the own class's values (the names
    :attr:`Offer.own` holds), which stay in ``ctx.params`` for the command's
    function, go through the offer's
    ``convert`` together, so each has the shape a function declaring it
    receives; then :func:`prepare_leaf_options` splits, records, builds and
    validates them over the offer's spec. The result, which also carries the
    attached values as parsed (:attr:`LeafOptions.raw`) and the command's
    own answer for its dry run, is kept in ``ctx.meta``
    (:func:`prepared_options`) and returned.

    With no offer the command records nothing: :func:`settle_options`
    clears its pending mark and this returns ``None``. Under a completion
    parse (``ctx.resilient_parsing``) it does nothing and returns ``None``.

    A value that fails a class's validation raises ``typer.BadParameter``.
    """
    if ctx.resilient_parsing:
        return None
    spec = command_spec(ctx)
    offer = offered_options(ctx, spec)
    if offer is None:
        settle_options()
        return None
    attached = take_attached(ctx)
    callback = cast("Callable[..., Any]", ctx.command.callback)
    own_values = {name: value for name, value in ctx.params.items() if name in offer.own}
    converted = offer.convert({**own_values, **attached})
    prepared = dataclasses.replace(
        prepare_leaf_options(converted, offer.spec, leaf=callback),
        finishes_dry_run=offer.finishes_dry_run,
        raw=dict(attached),
    )
    kept = cast("dict[str, _Prepared]", ctx.meta.setdefault(_PREPARED, {}))
    kept[_attached_key(ctx, spec)] = _Prepared(command=ctx.command, options=prepared)
    return prepared


def prepared_options(ctx: typer.Context) -> "LeafOptions | None":
    """Return what :func:`prepare_command_options` prepared for the command *ctx* invokes.

    Only the invoked command's own context answers. ``None`` for a command
    it prepared nothing for, and for any other context of the invocation (the
    root's, a group's), or of one where it has not run (a command driven
    without otto's invoke wrapper).
    """
    kept = cast("dict[str, _Prepared] | None", ctx.meta.get(_PREPARED))
    if not kept:
        return None
    found = kept.get(_attached_key(ctx, command_spec(ctx)))
    if found is None or found.command is not ctx.command:
        return None
    return found.options


def _wrap_invoke(cmd: Any, spec: "CommandSpec") -> Any:
    """Wrap a single leaf command's ``invoke``: preamble + async-leaf bridge (idempotent).

    The attached values are taken out of the leaf's ``ctx.params`` first
    (:func:`take_attached`), so its function receives only its own.

    A leaf that does not mark (:func:`pending_options`) settles the root
    group's dispatch mark before the preamble, so it reads defaults
    throughout. A lab-free leaf is invoked inside its pending mark, which
    :func:`settle_options` clears for a leaf that records nothing. A
    leaf the preamble prepared (:func:`prepared_options`) runs, its bridge
    included, inside :meth:`LeafOptions.scope`.
    """
    if getattr(cmd, "_otto_preambled", False):
        return cmd
    cmd._otto_preambled = True  # noqa: SLF001 — own marker attribute on the command object
    original_invoke = cmd.invoke

    def _invoke_with_preamble(inner_ctx: Any) -> Any:
        # Restamp on the leaf's own (inner) ctx: ctx.meta is shared by-reference
        # down the click context chain, but the spec must reflect THIS leaf.
        stash_command_spec(inner_ctx, spec)
        # A leaf that does not mark is free of the dispatch's mark for its
        # whole run (preamble, lab build, gate, body): settled before anything.
        mark = pending_options(inner_ctx)
        if mark is None:
            settle_options()
        # First: the function is passed only the names it declares.
        take_attached(inner_ctx)
        # Before the preamble, so a refused command creates no output dir and
        # loads no lab.
        if spec.async_leaves:
            _require_async_leaf(cmd, spec)
        command_preamble(inner_ctx)
        # No lab context holds the pending mark for a lab-free leaf: hold it
        # here, over the body and its bridge, settled for a leaf that records
        # nothing; a leaf that records holds its own record inside this one.
        lab_free = leaf_is_lab_free(spec, getattr(inner_ctx.command, "callback", None))
        prepared = prepared_options(inner_ctx)
        with contextlib.ExitStack() as held:
            held.enter_context(command_options(mark if lab_free else None))
            settle_options()
            if prepared is not None:
                held.enter_context(prepared.scope())
            result = original_invoke(inner_ctx)
            # The lifecycle bridge (wave 2 of the command-lifecycle-uniformity
            # spec): a plain ``async def`` leaf — typer never awaits callbacks, so
            # its invoke returns the coroutine object — runs under the full
            # command policy (a cleanup boundary, two-stage interrupts, teardown
            # deadline) with REGISTRATION as the only opt-in. Detection is on the
            # invoke RESULT, not the callback: typer wraps every callback in its
            # own sync shim, so ``iscoroutinefunction(cmd.callback)`` is always
            # False, while the coroutine itself passes through untouched.
            # Naturally idempotent: a leaf that self-bridges (``run_command``
            # inside a sync wrapper — the retired ``@async_typer_command``
            # migration pattern) returns a plain value and is skipped — no double
            # ``asyncio.run`` is reachable.
            if inspect.iscoroutine(result):
                from ..lifecycle import run_command

                result = run_command(result)
        # Rendering happens AFTER the bridge (on the awaited value), never on
        # --help paths (help exits during parse, before ``Command.invoke``),
        # and a leaf that raises (typer.Exit, SystemExit(128+n) from the
        # policy) never reaches it.
        render_leaf_value(result, inner_ctx.meta.get(RENDER_POLICY_KEY))
        return result

    cmd.invoke = _invoke_with_preamble
    return cmd


def _wrap_get_help(cmd: Any) -> None:
    """Wrap *cmd*'s ``get_help`` to print the banner first (idempotent).

    Covers every way help text gets rendered: the eager ``--help``/``-h``
    option, ``no_args_is_help``, and otto's own manual ``rprint(ctx.get_help())``
    call sites (e.g. ``otto host``'s class-scoped menu, bare ``otto monitor``)
    — they all resolve through ``ctx.get_help()`` → ``ctx.command.get_help(ctx)``.
    """
    if getattr(cmd, "_otto_help_wrapped", False):
        return
    cmd._otto_help_wrapped = True  # noqa: SLF001 — own marker attribute on the command object
    original_get_help = cmd.get_help

    def _get_help_with_banner(inner_ctx: Any) -> str:
        ensure_help_banner(inner_ctx)
        return original_get_help(inner_ctx)

    cmd.get_help = _get_help_with_banner


def _settled_when_childless(callback: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a group's *callback* so a group that runs with no child reads defaults.

    Click sets ``invoked_subcommand`` before it calls a group's callback, so
    the callback is the first place that can tell the two apart. With no
    child the group itself is the command that runs, and the dispatch's
    pending mark is settled (:func:`settle_options`) before its body; with a
    child nothing changes, and a read in the callback raises.
    """

    @functools.wraps(callback)
    def _callback(*args: Any, **kwargs: Any) -> Any:
        ctx = cast("typer.Context | None", get_current_context(silent=True))
        if ctx is not None and ctx.invoked_subcommand is None:
            settle_options()
        return callback(*args, **kwargs)

    return _callback


def wrap_leaf_callbacks(cmd: Any, spec: "CommandSpec") -> Any:
    """Wrap every leaf command under *cmd* so its invoke runs the preamble first.

    Wrapping ``Command.invoke`` (not the callback) means the preamble runs
    only on real execution: a ``--help`` on the leaf exits during parse and
    never reaches ``invoke``. Groups recurse into their static subcommands AND
    wrap their ``get_command`` so lazily-synthesized subcommands (e.g. the
    dynamic ``otto host <verb>`` commands) are wrapped on resolution too.
    Already-wrapped commands are skipped (resolution results are cached).
    Also wraps every node's ``get_help`` (see ``_wrap_get_help``) so any
    help screen reached through this tree shows the banner, every leaf's
    ``get_params`` (:func:`attach_params`) so the invocation's attached flags
    are the leaf's own, and every group's callback, where it has one, so a
    group that runs with no child settles the dispatch's pending mark before
    its body (:func:`settle_options`).
    """
    if getattr(cmd, "_otto_preambled", False):
        return cmd
    _wrap_get_help(cmd)
    if not hasattr(cmd, "commands"):
        attach_params(cmd, spec)
        return _wrap_invoke(cmd, spec)

    cmd._otto_preambled = True  # noqa: SLF001 — own marker attribute on the command object

    # A GROUP's own callback can never reach the lifecycle bridge: the
    # vendored click fork runs it via an unbound class call and DISCARDS its
    # return value (TyperGroup.invoke → super().invoke), so an ``async def``
    # group callback would silently no-op with exit 0 — the exact failure
    # class the bridge exists to kill. Reject it loudly at wrap time. One
    # ``__wrapped__`` level only (typer's get_callback update_wrapper's the
    # registered function): that is exactly what typer will call
    # synchronously, so the check cannot false-positive on a user's own
    # sync-bridging decorator.
    group_cb = getattr(cmd, "callback", None)
    if group_cb is not None and inspect.iscoroutinefunction(
        getattr(group_cb, "__wrapped__", group_cb)
    ):
        raise TypeError(
            f"async group callback on command group {cmd.name!r}: typer discards a "
            "group callback's return value, so it runs outside otto's lifecycle "
            "bridge and would silently do nothing — write group/root callbacks "
            "as plain `def` (only leaf commands may be `async def`)"
        )

    if group_cb is not None:
        cmd.callback = _settled_when_childless(group_cb)

    for sub in cmd.commands.values():
        wrap_leaf_callbacks(sub, spec)

    # Lazy groups (HostGroup) synthesize subcommands in get_command rather than
    # populating .commands up front — wrap the returned command as it resolves.
    original_get_command = cmd.get_command

    def _get_command_wrapped(gc_ctx: Any, name: str) -> Any:
        sub = original_get_command(gc_ctx, name)
        if sub is not None:
            wrap_leaf_callbacks(sub, spec)
        return sub

    cmd.get_command = _get_command_wrapped
    return cmd


# ---------------------------------------------------------------------------
# Shared lazy child-group factory — the "one attachment idiom" for
# registry-backed Typer groups (root CLI_COMMANDS and run/instructions
# resolve their children this way).
# ---------------------------------------------------------------------------


class ChildTable(Protocol):
    """What :func:`make_registry_group` reads: a registry or a view of one."""

    @property
    def revision(self) -> int:
        """A counter that moves whenever an entry changes."""
        ...

    def names(self) -> list[str]:
        """Return the child names, in order."""
        ...

    def get(self, name: str) -> Any:
        """Return the entry for *name*."""
        ...

    def __contains__(self, name: object) -> bool:
        """Whether *name* is a child."""
        ...


def make_registry_group(
    child_registry: ChildTable,
    *,
    app_of: "Callable[[Any], typer.Typer]",
) -> "type[TyperGroup]":
    """Build a TyperGroup class whose children come from *child_registry*.

    Children (instruction sub-apps) convert lazily on first access, and the
    converted children are kept until *child_registry*'s ``revision`` moves:
    a replaced entry is converted afresh.
    *app_of* returns an entry's Typer app: an instruction BUILDS its app then
    (``otto.cli.run.build_instruction_app``). Building resolves no class that
    covers the command, so it refuses nothing: a clash with one, or one that
    fails to import, is found when the command is parsed
    (:func:`prepare_command_options`), before any body runs.
    This follows the same idiom as ``HostGroup`` (``cli/expose.py``): the
    group only resolves children on demand — it does NOT itself wrap them
    with the leaf-invoke preamble. ``main.py``'s root dispatch wraps the
    WHOLE resolved group (and therefore every child it lazily resolves, via
    ``wrap_leaf_callbacks``'s ``get_command`` recursion) with the preamble
    for the top-level spec (``"run"``). This keeps ``run_app``
    usable standalone without going through the full ``otto``
    root app (unit tests drive them through the same seam via
    ``tests/_fixtures/dispatch.DispatchRunner``; note a plain ``async def``
    leaf needs that wrapper — bare, it fails loudly with an un-awaited
    coroutine) while ``otto run smoke`` still gets the
    preamble when dispatched for real.

    """
    from typer.core import TyperGroup

    class RegistryBackedGroup(TyperGroup):
        """Group whose subcommands resolve from a component registry."""

        _child_cache: dict[str, Any]
        _child_cache_revision: int | None = None

        @override
        def list_commands(self, ctx: Any) -> list[str]:
            static = super().list_commands(ctx)
            return static + [n for n in child_registry.names() if n not in static]

        @override
        def get_command(self, ctx: Any, cmd_name: str) -> Any:
            static = super().get_command(ctx, cmd_name)
            if static is not None:
                return static
            if cmd_name not in child_registry:
                return None
            # Converted children, dropped whenever the table changes: a
            # re-registration with overwrite=True is served afresh.
            revision = child_registry.revision
            if self._child_cache_revision != revision:
                self._child_cache = {}
                self._child_cache_revision = revision
            cache = self._child_cache
            if cmd_name not in cache:
                converted: Any = typer.main.get_command(app_of(child_registry.get(cmd_name)))
                cache[cmd_name] = (
                    converted.commands[cmd_name]
                    if hasattr(converted, "commands") and cmd_name in converted.commands
                    else converted
                )
            return cache[cmd_name]

    return RegistryBackedGroup
