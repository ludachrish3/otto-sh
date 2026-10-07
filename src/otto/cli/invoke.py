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
from collections.abc import Callable, Iterator
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast, get_type_hints

import typer
from rich.markup import escape
from typing_extensions import assert_never, override

from ..errors import OttoError
from ..params import (
    OptionsOrigin,
    OptionsValidationError,
    build_options,
    drop_unset_secrets,
    merge_option_params,
    options_params,
    sensitive_field_names,
    verb_option_classes,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from _typeshed import DataclassInstance
    from typer.core import TyperGroup

    from ..context import OttoContext
    from ..coverage.config import DestinationError
    from ..errors import FieldError
    from ..registry import Registry
    from ..reservations import ReservationBackendError
    from ..result import Result
    from ..session import (
        DemotedRepo,
        InstructionInactiveError,
        LabBuildError,
        ProjectSelectionError,
    )
    from .registry import CommandSpec


def _ctx_param_name(func: Callable[..., Any]) -> str | None:
    """Return the name of any parameter annotated as OttoContext, or None."""
    from ..context import OttoContext

    hints = get_type_hints(func)
    for name, hint in hints.items():
        if hint is OttoContext:
            return name
    return None


def _inject_ctx(func: Callable[..., Any], ctx_name: str) -> Callable[..., Any]:
    """Wrap *func* so the OttoContext param is supplied from the active context.

    Supplied at call time and hidden from the Typer-facing signature.
    """
    from ..context import get_context

    sig = inspect.signature(func)
    exposed = [p for n, p in sig.parameters.items() if n != ctx_name]

    @functools.wraps(func)
    async def wrapper(**kw: Any) -> Any:
        kw[ctx_name] = get_context()
        return await func(**kw)

    # Drop ctx_name from __annotations__ too so get_type_hints() on the
    # wrapper doesn't see it (important when _wrap_with_options composes on top).
    wrapper.__annotations__ = {k: v for k, v in func.__annotations__.items() if k != ctx_name}
    wrapper.__signature__ = inspect.Signature(exposed)  # ty: ignore[unresolved-attribute]
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


def _bind_and_build_own(
    kw: "dict[str, Any]",
    *,
    opts_cls: "type[DataclassInstance] | None",
    own_field_names: "set[str]",
) -> Any:
    """Build a verb-less command's own options instance from *kw* alone.

    The verb-less binding: a command prepared without a verb (``@cli_command``
    and ``resolve_spec_command``'s function loaders) has only its own class,
    so there is nothing to bind on the context and no registered class to
    inject. The ``run`` verb goes through
    :func:`otto.instructions.bind_handler_kwargs` instead (see
    :func:`_bind_run_leaf`). The leaf wrapper's real dispatch and its own
    dry-run branch (``finish_dry_run``, called from the SAME wrapper once this
    returns) both use the instance built here, so a bad value fails a dry run
    exactly as it fails a real run: the same
    :class:`~otto.params.OptionsValidationError` from :func:`build_options`,
    which the caller translates to a usage error via :func:`usage_error_from`
    at the CLI boundary.

    *kw* may hold more keys than the own class needs (the command's other
    parameters); anything outside *own_field_names* is ignored. An unset
    secret field is dropped so its default applies. Returns ``None`` when
    *opts_cls* is ``None``.
    """
    if opts_cls is None:
        return None
    opts_kw = {k: v for k, v in kw.items() if k in own_field_names}
    return build_options(opts_cls, drop_unset_secrets(opts_cls, opts_kw))


def _bind_run_leaf(
    func: Callable[..., Any], opts_cls: "type[DataclassInstance] | None", kw: "dict[str, Any]"
) -> "dict[str, Any]":
    """Bind an ``otto run`` leaf's parsed flags into *func*'s keyword arguments.

    Through :func:`otto.instructions.bind_handler_kwargs`, the rule
    :func:`otto.instructions.run_instruction` uses too, so ``otto run <name>``
    and a library call cannot bind an instruction's arguments differently. A
    bad value becomes a usage error.
    """
    from ..context import get_context
    from ..instructions import InstructionEntry, bind_handler_kwargs

    entry = InstructionEntry(
        name=getattr(func, "__name__", repr(func)),
        module=func.__module__,
        handler=func,
        options_cls=opts_cls,
    )
    try:
        return bind_handler_kwargs(get_context(), entry, kw)
    except OptionsValidationError as e:
        raise usage_error_from(e) from e


def _wrap_with_options(
    func: Callable[..., Any],
    opts_cls: "type[DataclassInstance] | None",
    *,
    verb: str | None = None,
    repo: str | None = None,
) -> Callable[..., Any]:
    """Build a wrapper that expands options classes into CLI parameters.

    *opts_cls* is the command's own options class: its fields become flags,
    and at call time the populated instance is forwarded to *func* in the
    position of the parameter annotated with it.

    With *verb* (``"run"``, the only verb :func:`prepare_command_target`
    admits), the command also takes the flags of every options class
    registered for that verb, merged with the own class by declaring class: a
    field the own class inherits from a registered base is one flag, and the
    same name introduced by two unrelated classes raises
    ``OptionsCollisionError`` here, when the command is built. At call time
    the ``run`` verb delegates to
    :func:`otto.instructions.bind_handler_kwargs` through
    :func:`_bind_run_leaf`, which binds the parsed values on the active
    context and injects ``ctx.options(cls)`` into every parameter annotated
    with a registered class; a verb-less command builds its own class alone
    through :func:`_bind_and_build_own`. *repo* owns the own class, and is
    who a collision message names for it.
    """
    from ..context import get_context

    func_name = getattr(func, "__name__", repr(func))
    sig = inspect.signature(func)
    hints = get_type_hints(func, include_extras=True)

    # Find the parameter annotated with the options class
    opts_param_name: str | None = None
    if opts_cls is not None:
        for name, hint in hints.items():
            if hint is opts_cls:
                opts_param_name = name
                break
        if opts_param_name is None:
            raise TypeError(
                f"instruction {func_name!r} declares options={opts_cls.__name__} "
                f"but has no parameter annotated as {opts_cls.__name__}"
            )

    own_params = options_params(opts_cls) if opts_cls is not None else []
    own_field_names = {p.name for p in own_params}

    verb_params: list[inspect.Parameter] = []
    injected: dict[str, type] = {}
    registered: set[type] = set()
    sensitive_names: set[str] = sensitive_field_names(opts_cls) if opts_cls is not None else set()
    if verb is not None:
        verb_origins = verb_option_classes(verb)
        for origin in verb_origins:
            sensitive_names |= sensitive_field_names(cast("type[DataclassInstance]", origin.cls))
        # Matched by identity, as `_ctx_param_name` matches OttoContext: an
        # annotation IS a registered class, or it is an ordinary parameter.
        registered = {origin.cls for origin in verb_origins}
        injected = {
            name: hints[name]
            for name in sig.parameters
            if name != opts_param_name
            and name in hints
            and any(hints[name] is cls for cls in registered)
        }
        # The own class goes first, so a base field it inherits is counted
        # once, under the class that declares it, and a clash between an own
        # field and a verb field is reported naming both. Every other visible
        # parameter is reserved: a verb field of the same name would make the
        # signature ambiguous.
        own_origin = [OptionsOrigin(opts_cls, repo)] if opts_cls is not None else []
        reserved = {
            name: f"parameter {name!r} of instruction {func_name!r}"
            for name in sig.parameters
            if name != opts_param_name and name not in injected
        }
        merged = merge_option_params(
            own_origin + verb_origins, what=f"otto {verb} {func_name}", reserved=reserved
        )
        verb_params = [p for p in merged if p.name not in own_field_names]

    # Build new parameter list: replace the opts param with expanded fields,
    # drop the injected ones, and append the verb's fields. No hidden click
    # Context parameter: the wrapper reads the current one (see below), so the
    # public signature stays exactly what the own + verb fields declare.
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
    new_params.extend(verb_params)

    @functools.wraps(func)
    async def wrapper(**kw: Any) -> Any:
        ctx = get_context() if verb is not None else None
        if verb == "run":
            kw = _bind_run_leaf(func, opts_cls, kw)
            own_instance = kw.get(opts_param_name) if opts_param_name else None
        else:
            try:
                own_instance = _bind_and_build_own(
                    kw, opts_cls=opts_cls, own_field_names=own_field_names
                )
            except OptionsValidationError as e:
                raise usage_error_from(e) from e
            for name in own_field_names:
                kw.pop(name, None)
            if opts_cls is not None and opts_param_name is not None:
                kw[opts_param_name] = own_instance
        # Validation is already DONE by this point (either branch above),
        # exactly as it is on a real run -- so a dry run's `finish_dry_run`
        # never has to re-validate anything, only report what was just built.
        # `click_ctx` is `None` for a call that never went through the CLI at
        # all (a test, or a library caller reaching this callable directly)
        # -- and never having gone through the CLI seam is exactly the case a
        # dry run cannot apply to, so that IS the "not a dry run" answer here.
        from typer._click.globals import get_current_context

        # `typer.Context` (used everywhere else in this module) is a thin
        # subclass typer's OWN commands never actually instantiate --
        # `TyperCommand.context_class` is the vendored click base
        # (`typer._click.core.Context`) that `get_current_context` returns --
        # so every real dispatch already hands this module that base class
        # under the `typer.Context` alias; the cast just makes that existing
        # fact visible to the type checker instead of leaving it untyped.
        click_ctx = cast("typer.Context | None", get_current_context(silent=True))
        if click_ctx is not None and dry_run_requested(click_ctx):
            instances: list[Any] = [own_instance] if own_instance is not None else []
            if verb is not None and ctx is not None:
                for origin in verb_option_classes(verb):
                    if origin.cls is opts_cls:
                        continue
                    instances.append(ctx.options(origin.cls))
            # `finish_dry_run` decides itself, from *preview*, whether to
            # print+exit or just return -- the SAME distinction
            # `stop_at_dry_run_seam` always made for an options-free leaf,
            # made here now too: a previewing leaf still gets `--probe`'s
            # announcement/dial/report, then runs its real body regardless.
            preview = command_spec(click_ctx).dry_run_preview or _leaf_declares_preview(click_ctx)
            await finish_dry_run(click_ctx, instances, preview=preview)
        return await func(**kw)

    wrapper.__signature__ = inspect.Signature(new_params)  # ty: ignore[unresolved-attribute]
    setattr(wrapper, DRY_RUN_SELF_FINISHING_ATTR, True)
    setattr(wrapper, SENSITIVE_FIELDS_ATTR, frozenset(sensitive_names))
    return wrapper


def prepare_command_target(
    func: Callable[..., Any],
    options_cls: type | None = None,
    *,
    verb: str | None = None,
    repo: str | None = None,
) -> Callable[..., Any]:
    """Apply otto's CLI wrappers to *func*: OttoContext injection + options expansion.

    The shared machinery behind ``@instruction`` and ``@cli_command``: a
    parameter annotated ``OttoContext`` is stripped from the CLI signature and
    injected at call time; an *options_cls* dataclass parameter is expanded
    into individual CLI flags.

    With *verb*, the command also gains the flag of every options class
    registered for that verb, the parsed values are bound on the active
    context before *func* runs, and a parameter annotated with a registered
    class is injected with ``ctx.options(cls)`` (see ``_wrap_with_options``).
    *verb* is ``None`` or ``"run"``, the two shapes with a call-time binding:
    any other raises :exc:`ValueError`, checked before the idempotency
    short-circuit below so an already-prepared callable is refused too. The
    verb's classes are resolved HERE, which may import their modules, so
    a caller passes *verb* only where the command is being built for that
    verb's own dispatch. *repo* is the repo that registered *func* (``None``
    for otto), named for *options_cls* when one of its fields collides.

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

    Raises:
        ValueError: *verb* is neither ``None`` nor ``"run"``.
    """
    if verb not in (None, "run"):
        # The call-time binding exists for exactly these two shapes: `run`
        # binds through `bind_handler_kwargs`, and a verb-less command builds
        # its own class alone. Another verb's flags would be expanded and
        # then never bound.
        func_name = getattr(func, "__name__", repr(func))
        raise ValueError(f"command {func_name!r}: no call-time binding for verb {verb!r}")
    if getattr(func, "__otto_cli_prepared__", False):
        return func
    ctx_name = _ctx_param_name(func)
    target: Callable[..., Any] = func
    if ctx_name is not None:
        target = _inject_ctx(func, ctx_name)
    own_cls: "type[DataclassInstance] | None" = None
    if options_cls is not None and dataclasses.is_dataclass(options_cls):
        own_cls = options_cls
    if own_cls is not None or verb is not None:
        target = _wrap_with_options(target, own_cls, verb=verb, repo=repo)
    if target is not func:
        target.__otto_cli_prepared__ = True  # ty: ignore[unresolved-attribute]
        target.__otto_handler__ = func  # ty: ignore[unresolved-attribute]
    return target


# ---------------------------------------------------------------------------
# Leaf-invoke preamble: lazy lab loading, session setup, output dir, gate
# ---------------------------------------------------------------------------


def print_error(message: object, *, soft_wrap: bool = False) -> None:
    """Print *message* as a user-facing error, with rich markup ESCAPED.

    The one place a CLI command renders a failure, so the escaping happens
    once instead of at a dozen call sites that each had to remember it.
    ``coverage/`` and ``suite/run.py`` reached the same conclusion
    independently and call :func:`rich.markup.escape` inline at ten more
    sites; consolidating those is a separate pass, not a claim to make here.

    Escaping is load-bearing, not hygiene. Rich reads ``[word]`` as a style
    tag and DELETES it, so ``list[str]`` prints as ``list``, an install hint
    like ``otto-sh[monitor]`` prints as ``otto-sh`` — a runnable command for
    the wrong thing — and a pydantic detail like
    ``[type=missing, input_value={}]`` vanishes entirely. Every message that
    reaches here interpolates something a user, a path, or a library supplied,
    so none can be assumed bracket-free. Numeric subscripts (``argv[1]``)
    happen to survive; that is not a rule worth relying on.

    Lives here rather than in ``otto.console`` because every render site is a
    CLI one, and ``otto.cli`` deliberately does not depend on that module.

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

    get_console().print(f"[red]{escape(str(message))}[/red]", soft_wrap=soft_wrap or None)


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
    failing command looks like, so a new command cannot accidentally ship an
    unescaped one (``.ast-grep/rules/error-render-through-helper.yml`` keeps
    that true).

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
def lab_context_refusals() -> "Iterator[None]":
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
    from ..reservations import ReservationBackendError
    from ..session import LabBuildError

    try:
        yield
    except (LabBuildError, ReservationBackendError) as e:
        report_lab_context_error(e)


def report_lab_context_error(err: "LabBuildError | ReservationBackendError") -> "NoReturn":
    """Render a lab-context refusal the loud way, in today's words, and exit with today's code.

    Formatted from the error's fields: a missing lab is click's own plain
    usage line on stderr (exit 2); a bad source or a broken inventory is the
    bold-red frame (exit 1); an unreachable reservation backend names the
    ``-R`` break-glass (exit 1). An unknown lab is re-raised: the boundary
    frame in ``otto.cli.main`` has always printed it (``error: ...``, exit 1).
    """
    from rich import print as rprint

    from ..reservations import ReservationBackendError
    from ..session import LabBuildError

    if isinstance(err, LabBuildError):
        # Bound once so the type checker narrows the literal across the arms
        # and `assert_never` fails the build when the library grows a kind.
        kind = err.kind
        if kind == "no_labs":
            # Plain stderr, not rich: click's own usage-error stream. (A *real*
            # click.UsageError would escape Typer 0.26's vendored click fork
            # uncaught, hence the manual message.)
            typer.echo("Error: Missing option '--lab' / '-l' (env var: 'OTTO_LAB').", err=True)
            raise typer.Exit(code=2)
        if kind == "unknown_lab":
            raise err
        detail = err.detail or ""
        if kind == "sources":
            rprint(f"[bold red]Host source unavailable:[/bold red] {escape(detail)}")
        elif kind == "inventory":
            rprint(f"[bold red]Inventory unavailable:[/bold red] {escape(detail)}")
        else:
            assert_never(kind)
        raise typer.Exit(code=1)
    if isinstance(err, ReservationBackendError):
        rprint(
            f"[bold red]Reservation backend unavailable:[/bold red] {escape(str(err))}\n"
            f"Pass [bold]--skip-reservation-check[/bold] / [bold]-R[/bold] to proceed without the check."  # noqa: E501 — long rich markup string
        )
        raise typer.Exit(code=1)
    # Unreachable for a well-typed caller. A stray error is handed back to the
    # boundary frame as itself, never dressed up as a reservation failure.
    raise err


def ensure_inline_lab(ctx: typer.Context) -> None:
    """Load the lab for a flag that inspects it INLINE and exits (``--show-lab``, ``--list-hosts``).

    The lab loads lazily in :func:`command_preamble`, which runs at the LEAF
    verb — so a group-level flag that wants live lab state has to load it
    itself, and it must do so the way dispatch does. The root callback grew
    this sequence for its own two flags; ``otto host --list-hosts`` had kept
    an eager Typer callback from before the lazy load existed, which called
    ``get_lab()`` (``otto.config.fleet``) before any context could exist and tracebacked
    ``No active OttoContext`` on the documented form ``otto --lab L host
    --list-hosts`` (2026-09-03). One helper, both callers.

    Same two gates in the same order as :func:`command_preamble` — a typo'd
    ``-I``/``-E`` name is a typo here too, and a half-registered world fails
    loud rather than surfacing a secondary error — then the CLI session BEFORE
    the lab load: the session applies each repo's ``[logging.levels]`` floor
    and the HostFilter, so the load's own notices (the cross-source override
    warning from ``otto.labs.composite``, notably) render through it. A lab
    that cannot load reports and exits with the loud path's message and code.
    """
    validate_project_switches(ctx)
    fail_loud_on_bootstrap_errors(ctx)
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
    also sets it for the invocation with :func:`otto.context.set_variant`,
    which is what the registry and providers read."""
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


def command_spec(ctx: typer.Context) -> "CommandSpec":
    """Typed read of the invoked command's spec — ``meta['_otto_command_spec']``.

    Stashed by the leaf-invoke wrapper (and ``otto test``'s direct entry) before
    the preamble runs; same seam-per-key rule as :func:`root_options`.
    """
    from .registry import CommandSpec

    spec = ctx.meta["_otto_command_spec"]
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
    repo's ``[logging.levels]`` is merged over them here — the first point at
    which repo settings are guaranteed parsed.

    The console handler itself is NOT news by this point: the root callback
    raised it as soon as it had ``--log-level`` (spec 2026-08-30 §3.1), so
    ``init_cli_logging``'s install is an idempotent re-affirmation. What this
    call owns is the per-invocation state the file sinks read later (xdir,
    retention, rich-file flag). The repo-aware half — the merged
    ``[logging.levels]`` overrides and the ``HostFilter`` on the console — is
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
        logger.info(f"[magenta][DRY RUN] Commands and file transfers will be skipped. {contact}")

    # The repo-aware half, shared with library callers: every repo's
    # [logging.levels] over otto's defaults (a conflict refuses), and the
    # HostFilter on the console. After the dry-run notice, which stays the
    # first line of a dry run. init_cli_logging above owns the CLI-only file
    # sinks; this re-affirms the console it installed with the same level.
    from ..session import install_logging

    install_logging(log_level=opts.log_level, show_time=opts.show_time)


def ensure_lab_context(ctx: typer.Context) -> "OttoContext":
    """Load the lab, build reservation state, and install the runtime context (idempotent).

    Builds the lab through :func:`otto.session.build_lab` (which enforces
    ``--lab``, aggregates the repos' sources, loads the lab and registers the
    declared docker placeholder hosts), resolves reservation state (stashed on
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
    """
    from ..context import get_context

    meta = ctx.meta
    if meta.get("_otto_lab_ready"):
        return get_context()

    opts = root_options(ctx)

    from ..bootstrap import get_repos
    from ..session import build_lab

    repos = get_repos()
    # `--lab` is not a hard-required Typer option (so lab-free subcommands can
    # run without it); build_lab refuses an empty selection (`no_labs`) before
    # any lab side effect, for everything that does need a lab.
    lab = build_lab(repos, list(opts.labs or []))
    # build_lab registered the declared container placeholders

    # Resolve reservation identity + backend (first repo with a [reservations]
    # section wins). With -R the backend is NOT constructed at all, so a broken
    # or hanging scheduler can never block lab access (break-glass). A broken
    # backend's ReservationBackendError propagates to the loud callers.
    from ..reservations import build_reservation_gate

    reservation_gate = build_reservation_gate(
        repos,
        holder=opts.holder,
        skip_reservation_check=opts.skip_reservation_check,
        cwd_fallback=Path.cwd(),
    )

    identity = reservation_gate.identity
    if identity is not None and identity.source == "--holder":
        getLogger(__name__).info(
            rf"[bold magenta]\[reservations] acting as {identity.username!r}"
            rf" (--holder)[/bold magenta]"
        )

    meta["otto_reservation"] = reservation_gate

    # Install the runtime context: lab + dry_run flag + the per-invocation
    # project switches (-I/-E), which every activation question reads back
    # through otto.config.scope.active. Its reset runs when Click closes this
    # invocation's root context, before the variant's (the root callback
    # registered that one first), so the context never outlives the invocation.
    # Installed here, outside the command's event loop: the leaf's coroutine
    # runs later under run_command, in a copy of this execution context.
    from ..context import OttoContext, reset_context, set_context

    token = set_context(
        OttoContext(
            lab=lab,
            dry_run=opts.dry_run,
            include_projects=opts.include_projects,
            exclude_projects=opts.exclude_projects,
        )
    )
    ctx.find_root().call_on_close(lambda: reset_context(token))
    meta["_otto_lab_ready"] = True
    return get_context()


def try_ensure_lab(ctx: typer.Context) -> "OttoContext | None":
    """Soft variant of :func:`ensure_lab_context`: return None instead of raising.

    Used by ``HostGroup`` class-scoping — a soft probe where any failure (no
    ``--lab``, unknown lab, broken backend) simply means "no class scoping
    available", falling back to the full unscoped verb menu.
    """
    try:
        return ensure_lab_context(ctx)
    except Exception:  # noqa: BLE001 — soft scoping probe: ANY failure (incl. typer.Exit, an Exception subclass) → no scoping
        return None


def validate_project_switches(ctx: typer.Context) -> None:
    """Exit 2 when ``-I``/``-E`` names a repo discovery never found (spec §2).

    Runs at first use (the preamble / the ``--show-lab`` branch) rather than in
    the root callback, because the check needs the discovered repo set and the
    root callback also runs for ``--help``, where there is no invocation to
    validate. It claims no saving on user code: ``entry()`` has already called
    ``bootstrap()`` before argv parsing on every non-completion invocation, and
    the gate on the very next line calls it again. The early return below is
    just "nothing was asked for, so look nothing up".

    The name is normalized on BOTH sides — the switch values arrive
    PEP-503-normalized from the root callback, so comparing them against a raw
    ``repo.name`` would reject the only spelling the CLI can produce.

    The rule is :func:`otto.session.select_projects`'s, the same one
    :func:`~otto.context.open_context` applies; this function only renders its refusal,
    through :func:`report_project_selection_error`. Printed manually +
    ``typer.Exit(2)`` rather than raised as a ``click.UsageError``: a real one
    raised after parse escapes Typer 0.26's vendored click fork uncaught, which
    is why :func:`report_lab_context_error` hand-writes its missing-``--lab``
    usage text too. The overlap arm of :func:`report_project_selection_error` is
    unreachable from here: the root callback has already refused an overlap.
    """
    opts = maybe_root_options(ctx)
    if opts is None or not (opts.include_projects or opts.exclude_projects):
        return
    from ..bootstrap import bootstrap
    from ..session import ProjectSelectionError, select_projects

    try:
        select_projects(bootstrap().repos, list(opts.include_projects), list(opts.exclude_projects))
    except ProjectSelectionError as e:
        report_project_selection_error(e)


def fail_loud_on_bootstrap_errors(ctx: "typer.Context | None" = None) -> None:
    """Exit(1) when bootstrap contained an ACTIVE repo's error — shared loud gate.

    The per-error ``warning:`` lines were already printed by ``entry()`` at
    startup; print ONLY the framed summary here (don't re-print each error
    in red) — the summary points back at those warnings. Used by the leaf
    preamble AND the root ``--show-lab``/``--list-hosts`` branch, so anything
    that inspects the registered world fails the same way.

    With *ctx*, an error attributable to a repo that is inactive for this
    invocation demotes to one warning line (spec §3): a broken sibling must not
    fail a run it is not part of. Inactivity here is the PRE-LAB projection —
    the explicit switches plus lab-name inference
    (:func:`otto.config.scope.inactive_before_lab`) — because this gate is what
    protects lab construction from a half-registered world and so has to run
    before it; a repo that is inactive only because it is host-starved
    therefore keeps its errors fatal. Attribution is by ``sut_dir``: an error
    belonging to no discovered repo (a ``settings.toml`` that failed to PARSE,
    so no :class:`~otto.config.repo.Repo` object exists) can never be judged
    inactive and stays fatal. Without *ctx* — library callers, tests — every
    error is fatal, unchanged.

    The rule is :func:`otto.session.check_repos`'s, the same one
    :func:`~otto.context.open_context` applies; this function only renders its verdict.
    """
    from ..bootstrap import bootstrap
    from ..session import ProjectSelection, RepoLoadError, check_repos

    result = bootstrap()
    if not result.errors:
        return
    opts = maybe_root_options(ctx)
    # Without root options (a test, a library caller) nothing is selected and
    # no lab is named, so check_repos demotes nothing: every error is fatal.
    selection = (
        ProjectSelection(include=list(opts.include_projects), exclude=list(opts.exclude_projects))
        if opts is not None
        else ProjectSelection()
    )
    labs = list(opts.labs or []) if opts is not None else []
    try:
        check = check_repos(result, labs, selection)
    except RepoLoadError as e:
        _echo_demotions(e.demoted)
        from rich import print as rprint

        rprint("[red]Cannot run commands while a repo fails to load (see warnings above).[/red]")
        raise typer.Exit(1) from None
    _echo_demotions(check.demoted)


def _echo_demotions(demoted: "list[DemotedRepo]") -> None:
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
    # SQUARE BRACKETS, and both console routes — `rich.print` and the
    # console handler, a RichHandler with `markup=True` — read `[unix]`
    # as a style tag and delete it, leaving `lab(s) )`. `typer.echo`
    # renders the sentence as written.
    for d in demoted:
        reason = (
            f"--exclude-projects {d.project}"
            if d.reason == "excluded"
            else f"not applicable to lab(s) [{', '.join(d.labs)}]"
        )
        typer.echo(
            f"warning: repo {d.repo!r} failed to load, but is inactive "
            f"for this run ({reason}) — continuing without it",
            err=True,
        )


def present_reservation_gate(ctx: typer.Context) -> None:
    """Evaluate the active reservation gate (if any) and present its warning.

    Reads ``ctx.meta["otto_reservation"]`` — a no-op when absent (e.g. a
    lab-free command, or a test that never populated it) — and calls
    :meth:`~otto.reservations.check.ReservationGate.evaluate`. ``evaluate()``
    returns a :class:`~otto.reservations.check.ReservationGateResult` whose
    ``warning`` is deliberately plain text (the library has no Typer/rich
    dependency); this function OWNS the presentation of that text — it is
    the single place that wraps it in ``[bold red]...[/bold red]`` markup.
    Both CLI call sites (``command_preamble`` here and the live branch of
    ``otto monitor``) delegate to this one function rather than composing
    the markup themselves.

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

        rprint(f"[bold red]{escape(outcome.warning)}[/bold red]")


def ensure_lab_session(ctx: typer.Context, spec: "CommandSpec") -> None:
    """Run the lab-requiring slice of the leaf-invoke preamble: session, lab, output dir.

    Shared by :func:`command_preamble` (every ordinary, non-``lab_free``
    command) and any ``lab_free``-registered command whose ONE branch still
    needs a lab — e.g. ``otto monitor --live`` (:mod:`otto.cli.monitor`),
    which pulls this in itself the same loud way ``otto reservation check``
    pulls in :func:`ensure_lab_context`, mirroring the per-branch
    ``spec.gate`` pattern those commands already use for the reservation
    gate. Idempotent (``ensure_cli_session``/``ensure_lab_context`` each
    guard their own re-entry), except for the output-dir creation, which a
    caller should therefore invoke at most once per command invocation.

    Does not touch the reservation gate itself — callers decide when (or
    whether) to call :func:`present_reservation_gate`.
    """
    ensure_cli_session(ctx)
    with lab_context_refusals():
        ensure_lab_context(ctx)

    leaf_wants_dir = bool(getattr(ctx.command.callback, "__cli_output_dir__", True))
    if spec.output_dir and leaf_wants_dir:
        from ..context import get_context
        from ..logger import management

        # A single command (``test``) or a flattened single-command group
        # (``monitor``) IS the top-level command: its leaf name equals
        # ``spec.name``, so there is no meaningful sub-name — pass None to keep
        # the base ``test/<TS>`` dir (not ``test/<TS>_test``). Real sub-groups
        # (run/host) keep their ``<name>/<TS>_<sub>`` layout since
        # ``ctx.command.name`` differs.
        leaf_name = ctx.command.name
        sub = None if leaf_name == spec.name else (leaf_name or spec.name)
        get_context().output_dir = management.create_output_dir(spec.name, sub)


def refuse_inactive_instruction(inner_ctx: typer.Context) -> None:
    """Refuse dispatching a repo-owned instruction whose repo is inactive (spec §5).

    Runs in the leaf preamble AFTER the lab session exists, so the verdict can
    come from :func:`otto.config.scope.active` — the one authority — rather than
    from a pre-lab projection of it. Ordering is load-bearing, not cosmetic:
    before the session there are no ``ctx.scopes`` verdicts at all, and a
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
    node = inner_ctx
    while node.parent is not None and getattr(node.parent.command, "name", None) != "run":
        node = node.parent
    if node.parent is None:
        return  # not dispatched through `otto run`

    from ..instructions import INSTRUCTIONS

    # `or ""`: click types `info_name` as `str | None`, and an unnamed node owns
    # nothing -- "" is never a registered instruction, so the one lookup below
    # covers both "no name" and "not an instruction" without a second branch.
    name = node.info_name or ""
    if name not in INSTRUCTIONS:
        return  # a statically-declared `run` child; nobody owns it
    owner = INSTRUCTIONS.get(name).registered_by

    from ..context import get_context
    from ..session import InstructionInactiveError, check_instruction_active

    try:
        check_instruction_active(name, owner, get_context())
    except InstructionInactiveError as e:
        # Through `fail`, never a hand-rolled `[red]` f-string: the message
        # interpolates a lab list and the literal table name `[project]`, and rich
        # DELETES `[word]` as markup unless it is escaped -- the demotion warning
        # a few functions up was observed rendering "lab(s) [unix]" as "lab(s) )"
        # for exactly this reason. `print_error` escapes; the
        # `error-render-through-helper` ast-grep rule keeps this route the only one.
        # soft_wrap: the hint's whole job is to hand over a runnable switch. Rich
        # folds between WORDS at the console width, so at 80 columns an ordinary
        # repo name splits `or: -I firmware-integration` across the fold and the
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
        # names the patterns the repo actually declared.
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
    return f"{err.instruction!r} belongs to repo {err.owner!r}, {detail}\n{hint}"


def refuse_unsatisfied_dependencies() -> None:
    """Refuse when an ACTIVE repo declares a Python requirement this env lacks (spec §3).

    Runs in the leaf preamble AFTER the lab session exists, for the same reason
    :func:`refuse_inactive_instruction` does and no other: severity here is an
    ACTIVATION question, and :func:`otto.config.scope.active` -- the one
    authority -- needs the lab verdicts the session installs. The spec asked for
    this check inside ``bootstrap()``, which runs before any of that exists;
    those two requirements cannot both hold, and the authority is the half worth
    keeping. The pre-lab projection :func:`otto.config.scope.inactive_before_lab`
    would have refused a HOST-STARVED repo -- one whose labs match but whose
    ``host_patterns`` select nothing -- because that shape is undetectable
    before the lab is built. Here it is simply inactive, and warns.

    Two consequences of the position, both deliberate:

    * **Library callers are checked by the same rule.** ``open_context`` runs
      the same :func:`otto.session.check_dependencies` once its context is
      installed; this function only renders the verdict in the CLI's words.
    * **An EAGER import failure never gets here.** ``bootstrap()``'s per-repo
      init loop runs first, so a repo whose init module imports a missing
      package at module scope is already a ``BootstrapError`` and
      :func:`fail_loud_on_bootstrap_errors` reports it generically. Only the
      LAZY shape -- the import inside an instruction body, the one that would
      otherwise fail mid-run with hosts already touched -- is what this gate
      exists to pre-empt.

    Called only from ``command_preamble``'s non-``lab_free`` branch, which is
    also what keeps ``otto env sync`` -- the verb the refusal names -- outside
    the gate that would otherwise refuse to let you run the fix.
    """
    from ..context import get_context
    from ..session import DependencyRefusedError, check_dependencies

    # PRINTED to stderr, not logged and not through rich -- the same call and
    # the same reasons as the demotion warning in
    # `fail_loud_on_bootstrap_errors`: the run CONTINUES past here so stdout
    # belongs to the command, and a requirement string carries brackets
    # (`otto-sh[monitor] >= 1`) that every rich-rendering route -- the console
    # handler included -- would delete as a style tag. Not the swallowed-record
    # reason, which the early console handler retired (spec 2026-08-30 §3.1).
    try:
        warnings = check_dependencies(get_context())
    except DependencyRefusedError as e:
        for warning in e.warnings:
            typer.echo(f"warning: {warning}", err=True)
        lines = [
            f"error: repo {bad.repo!r} requires {bad.requirement!r} — not satisfied in "
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
        # wrong package. `print_error` escapes; the `error-render-through-helper`
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


def command_preamble(ctx: typer.Context) -> None:
    """Run once when a real (non-help) command invocation starts.

    Order: ``-I``/``-E`` names are validated → an active repo's bootstrap
    errors fail loud → lab-free commands skip the lab slice → CLI session
    (logging) → lab context → per-command output dir → an inactive repo's
    instruction is refused → an active repo's unsatisfied Python dependencies
    are refused → reservation gate → the dry-run seam. ``--help`` paths never
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
    validate_project_switches(ctx)
    fail_loud_on_bootstrap_errors(ctx)

    spec = command_spec(ctx)
    # A leaf under a lab-bound group may opt out of the lab slice with the
    # same per-leaf mechanism ``ensure_lab_session`` reads for the output dir:
    # ``otto cov kmodcov export`` writes a local directory and touches no host.
    leaf_lab_free = bool(getattr(ctx.command.callback, LAB_FREE_ATTR, False))
    if not spec.lab_free and not leaf_lab_free:
        ensure_lab_session(ctx, spec)
        # After the session (the lab verdicts it installs ARE the input) and
        # before the gate: a run that is being refused must not first be
        # warned about the reservations it would have needed.
        refuse_inactive_instruction(ctx)
        # After the ownership gate, which refuses the thing the operator TYPED
        # and so owns the more specific sentence; before the reservation gate,
        # for the reason stated one line up.
        refuse_unsatisfied_dependencies()
        if spec.gate:
            present_reservation_gate(ctx)

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

SENSITIVE_FIELDS_ATTR = "__otto_sensitive_fields__"
"""Per-leaf set of option field names whose VALUE the ``would run:`` line must mask.

A ``frozenset[str]``, stamped by ``_wrap_with_options`` (and by
``otto.cli.run._project_leaf``) from ``otto.params.sensitive_field_names`` over every
options class contributing flags to the command. Read by ``_param_words``
so the generic argv echo -- which knows nothing about options classes, only
raw click parameters -- can still print ``<hidden>`` for a
``SecretStr``/``SecretBytes`` field or one declared ``repr=False``, the same
two conditions ``_options_line`` already masks in the ``options:`` block.
"""

DRY_RUN_SELF_FINISHING_ATTR = "__otto_dry_run_self_finishing__"
"""Per-leaf marker that a leaf validates its own options and finishes its own dry run.

Stamped ``True`` by ``_wrap_with_options`` on every leaf its ``options=``/verb
machinery builds (:func:`prepare_command_target`), and by the
project-instruction leaf ``otto.cli.run._project_leaf`` builds. Read by
:func:`stop_at_dry_run_seam`, which returns immediately for such a leaf --
no probe, no reference resolution, no print -- because the leaf's own body
does all three itself, via :func:`finish_dry_run`, AFTER it has bound and
built its options from the real, typer-CONVERTED kwargs (never
``ctx.params``, which holds pre-conversion values: a ``Path``, an ``Enum``, a
``list`` -- whatever typer's own callback shim converts them to -- so a
``-n`` build cannot construct a different object than a real run's, and a
project instruction's verb-registered classes (which used to reach the seam
not at all) validate under ``-n`` exactly as they do on the real path.
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
    typed) and falls back to the active :class:`~otto.context.OttoContext` —
    which is what a sub-app driven without the root callback (unit tests, an
    embedder) installs, and what ``ensure_lab_context`` derives from the flag
    anyway, so the two can only ever agree on the real dispatch path.

    Read with ``getattr``, not attribute access: ``_otto_root_options`` is a
    plain ``ctx.meta`` slot, and several preamble tests park a bare sentinel
    there. A seam that raised ``AttributeError`` on an unfamiliar stash would
    fail the invocation at the one line whose job is to answer a yes/no.
    """
    flag = getattr(ctx.meta.get("_otto_root_options"), "dry_run", None)
    if flag is not None:
        return bool(flag)
    from ..context import try_get_context

    active = try_get_context()
    return bool(active is not None and active.dry_run)


def probe_requested(ctx: typer.Context) -> bool:
    """Whether this dry run may open connections (``--probe``, spec §3).

    Read ONLY from the root callback's recorded options — no
    :class:`~otto.context.OttoContext` fallback, unlike
    :func:`dry_run_requested`. ``dry_run`` lives on the context because the
    LIBRARY layer branches on it at every device boundary; ``--probe`` is a CLI
    presentation choice that nothing below the seam consults, and putting it on
    the context would invite exactly that. A sub-app driven without the root
    callback therefore never probes, which is the safe answer.
    """
    return bool(getattr(ctx.meta.get("_otto_root_options"), "probe", False))


def _leaf_declares_preview(ctx: typer.Context) -> bool:
    """Whether the resolved leaf stamped itself as owning its own dry-run preview."""
    return bool(getattr(getattr(ctx.command, "callback", None), DRY_RUN_PREVIEW_ATTR, False))


def _leaf_self_finishes_dry_run(ctx: typer.Context) -> bool:
    """Whether the resolved leaf validates its own options and finishes its own dry run."""
    return bool(getattr(getattr(ctx.command, "callback", None), DRY_RUN_SELF_FINISHING_ATTR, False))


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

    A name :data:`SENSITIVE_FIELDS_ATTR` lists on this node's callback has its
    VALUE replaced with the literal ``<hidden>`` -- the flag still shows (an
    operator needs to know the switch was given), only what followed it is
    masked. A boolean flag carries no separate value word to mask.
    """
    import shlex

    sensitive = getattr(getattr(ctx.command, "callback", None), SENSITIVE_FIELDS_ATTR, frozenset())
    words: list[str] = []
    for param in getattr(ctx.command, "params", ()):
        name = getattr(param, "name", None)
        if name is None or name not in ctx.params:
            continue
        value = ctx.params[name]
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
    from ..context import try_get_context

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

    Every line prints ``soft_wrap=True`` (the same call ``fail()`` uses for a
    message meant to be re-typed): a long resolved value — a path, a token, a
    URL — must stay on ONE line when piped or captured, never hard-wrapped or
    cropped at the console width.
    """
    from rich import get_console

    from ..utils import DRY_RUN_HEADLINE, DRY_RUN_HEADLINE_PROBED

    console = get_console()
    headline = DRY_RUN_HEADLINE_PROBED if contacted else DRY_RUN_HEADLINE
    console.print(f"[magenta]{escape(headline)}[/magenta]", soft_wrap=True)
    console.print(f"  would run: {escape(would_run_line(ctx))}", soft_wrap=True)
    if options:
        console.print("  options:", soft_wrap=True)
        for instance in options:
            console.print(f"    {escape(_options_line(instance))}", soft_wrap=True)
    console.print(f"  {escape(_lab_line(references or []))}", soft_wrap=True)


def stop_at_dry_run_seam(ctx: typer.Context, spec: "CommandSpec") -> None:
    """Under ``--dry-run``, print what would run and exit 0 before the body.

    The default for every registered command, first- and third-party alike:
    zero author effort, and safe when the author never thought about dry runs
    at all. A command buys depth deliberately — ``dry_run_preview=True`` at
    registration (the whole group) or on the leaf's own ``@cli_exposed``
    stamp — and then owns its ``is_dry_run()`` branch.

    A no-op when this is not a dry run, which is every non-``-n`` invocation.

    A leaf marked :data:`DRY_RUN_SELF_FINISHING_ATTR` is handled not at all:
    it returns immediately, because such a leaf's OWN body binds and builds
    its options from the real, typer-converted kwargs and then calls
    :func:`finish_dry_run` itself.

    Otherwise this function runs the probe step itself, synchronously, via
    ``run_probe`` — the ONLY place this path spins up a
    lifecycle event loop, and only when ``--probe`` was actually given — and
    then calls ``_conclude_dry_run``, the sync resolve/print/exit tail
    shared with :func:`finish_dry_run`. A plain ``-n`` stop (no ``--probe``)
    therefore starts no event loop at all: no signal handlers, no host-scope
    sweep, no teardown-deadline lookup, to do what is otherwise a few printed
    lines and a ``typer.Exit(0)``. This runs inside ``command_preamble``,
    before any leaf coroutine is even constructed, so staying loop-free here
    is not an optimization otto's own outer ``run_command`` would have made
    redundant — nothing else has spun one up yet. *spec*'s (or the leaf's
    own) ``dry_run_preview`` is passed straight through, so a previewing
    command (``link``/``tunnel``) still gets ``--probe``'s announcement/
    dial/report before its body runs for real, exactly as it always has.
    """
    if not dry_run_requested(ctx):
        return

    if _leaf_self_finishes_dry_run(ctx):
        return

    preview = spec.dry_run_preview or _leaf_declares_preview(ctx)
    step = _sync_probe_step(ctx)
    _conclude_dry_run(
        ctx, [], preview=preview, contacted=step.contacted, references=step.references
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

    For a leaf that is both :data:`DRY_RUN_PREVIEW_ATTR` and
    :data:`DRY_RUN_SELF_FINISHING_ATTR` and runs without a loop of its own
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

    if value.msg:
        rprint(f"[magenta]{escape(str(value.msg))}[/magenta]")
        return
    command = str(getattr(value, "command", "") or "")
    detail = f" ({escape(command)})" if command else ""
    rprint(f"[magenta]{escape(DRY_RUN_DECLINE)}{detail}[/magenta]")


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


def _rprint_each(items: "list[Any]") -> None:
    """Print one item per line: a Table/Text renders itself, a str prints plain.

    Handing a whole list to a single ``rprint()`` call pretty-prints it as one
    Python object instead of rendering each item — the hazard this exists to
    avoid, shared by a ``Result`` payload list and a plain list return alike.
    """
    from rich import print as rprint

    for item in items:
        rprint(item)


def _render_ok_result(value: "Result", success: "str | None") -> None:
    """Render a successful ``Result``: the success message, or its payload."""
    from rich import print as rprint

    from ..result import CommandResult, Results
    from ..utils import Status

    if isinstance(value, (CommandResult, Results)):
        return  # command output already streamed during execution
    if value.status is Status.Skipped and value.msg:
        # A skip is a pass that did nothing; the generic success line would
        # read as though the work ran.
        rprint(f"[yellow]skipped: {escape(str(value.msg))}[/yellow]")
        return
    if success:
        rprint(f"[green]{success}[/green]")
        return
    payload = _payload_or_decline(value)
    if isinstance(payload, dict):
        for src, entry in payload.items():
            rprint(f"{src} -> {_payload_or_decline(entry)}")
    elif isinstance(payload, list):
        _rprint_each(payload)
    elif payload is not None:
        rprint(payload)


def _render_failed_result(value: "Result") -> None:
    """Print a failed ``Result``'s message(s) and raise its exit code."""
    from ..result import Result, Results

    if value.msg:
        print_error(value.msg)
    payload = _payload_or_decline(value)
    if isinstance(payload, dict):
        for entry in payload.values():
            if isinstance(entry, Result) and not entry.is_ok and entry.msg:
                print_error(entry.msg)
    elif isinstance(value, Results):
        for entry in value:
            if not entry.is_ok and entry.msg:
                print_error(entry.msg)
    raise typer.Exit(value.exit_code)


def render_leaf_value(value: Any, policy: "RenderPolicy | None" = None) -> None:
    """Render a leaf command's return value and signal failure via exit code.

    Implements the documented "Return values" contract
    (``docs/cookbook/extending/extending-cli.md``) for every registered command and
    instruction: an ``otto.result`` family value derives the process exit code
    from its own ``exit_code`` (the ssh-like rules); any other non-``None``
    value is printed as-is, exit 0. ``None`` renders nothing by default —
    every side-effect-only first-party leaf returns it, so a leaf that wants a
    completion message must say so via :class:`RenderPolicy` (installed on
    ``ctx.meta[RENDER_POLICY_KEY]``, e.g. by the ``otto host`` verb bodies).

    A ``Status.NotRun`` result is a dry run's decline, not a failure: it is
    announced, never parsed, and never turned into a non-zero exit (the
    library-facing 255 answers a different question than the user's).
    """
    from rich import print as rprint

    from ..result import Result
    from ..utils import Status

    if isinstance(value, Result):
        if value.status is Status.NotRun:
            _render_dry_run_decline(value)
            return
        if value.is_ok:
            _render_ok_result(value, policy.success if policy else None)
            return
        _render_failed_result(value)
        return

    if value is None:
        if policy is not None and policy.none_message is not None:
            rprint(f"[green]{policy.none_message}[/green]")
        return

    if isinstance(value, list) and _has_rich_renderable(value):
        _rprint_each(value)
        return

    rprint(value)  # documented third-party plain-value fallback, exit 0


def _require_async_leaf(cmd: Any, spec: "CommandSpec") -> None:
    """Refuse a sync leaf under a command whose lane demands coroutines.

    Only a coroutine reaches the bridge below, so a sync leaf runs with no
    host-scope entry and no interrupt policy. ``@instruction`` checks this at
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
    through ``build_instruction_app`` -> ``prepare_command_target(verb="run")``,
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


def _wrap_invoke(cmd: Any, spec: "CommandSpec") -> Any:
    """Wrap a single leaf command's ``invoke``: preamble + async-leaf bridge (idempotent)."""
    if getattr(cmd, "_otto_preambled", False):
        return cmd
    cmd._otto_preambled = True  # noqa: SLF001 — own marker attribute on the command object
    original_invoke = cmd.invoke

    def _invoke_with_preamble(inner_ctx: Any) -> Any:
        # Before the preamble, so a refused command creates no output dir and
        # loads no lab.
        if spec.async_leaves:
            _require_async_leaf(cmd, spec)
        # Restamp on the leaf's own (inner) ctx: ctx.meta is shared by-reference
        # down the click context chain, but the spec must reflect THIS leaf.
        inner_ctx.meta["_otto_command_spec"] = spec
        command_preamble(inner_ctx)
        result = original_invoke(inner_ctx)
        # The lifecycle bridge (wave 2 of the command-lifecycle-uniformity
        # spec): a plain ``async def`` leaf — typer never awaits callbacks, so
        # its invoke returns the coroutine object — runs under the full
        # command policy (host-scope entry, two-stage interrupts, teardown
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


def wrap_leaf_callbacks(cmd: Any, spec: "CommandSpec") -> Any:
    """Wrap every leaf command under *cmd* so its invoke runs the preamble first.

    Wrapping ``Command.invoke`` (not the callback) means the preamble runs
    only on real execution: a ``--help`` on the leaf exits during parse and
    never reaches ``invoke``. Groups recurse into their static subcommands AND
    wrap their ``get_command`` so lazily-synthesized subcommands (e.g. the
    dynamic ``otto host <verb>`` commands) are wrapped on resolution too.
    Already-wrapped commands are skipped (resolution results are cached).
    Also wraps every node's ``get_help`` (see ``_wrap_get_help``) so any
    help screen reached through this tree shows the banner.
    """
    if getattr(cmd, "_otto_preambled", False):
        return cmd
    _wrap_get_help(cmd)
    if not hasattr(cmd, "commands"):
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


def make_registry_group(
    child_registry: "Registry[Any]", *, app_of: "Callable[[Any], typer.Typer]"
) -> "type[TyperGroup]":
    """Build a TyperGroup class whose children come from *child_registry*.

    Children (instruction sub-apps) convert lazily on first access.
    *app_of* returns an entry's Typer app: an instruction BUILDS its app then
    (``otto.cli.run.build_instruction_app``), so building it is where a clash between
    its flags and the ``run`` verb's is found, or a ``run`` options class that
    fails to import. ``get_command`` lets that ``OttoError`` propagate, so a
    reader that walks every child (the completion tree) can contain it and keep
    the rest of the CLI. ``resolve_command`` -- dispatch, which is how
    ``otto run <name>`` and ``otto run <name> --help`` both reach the command --
    renders it as the command's error and exits 1, before any body runs.
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

        @override
        def resolve_command(self, ctx: Any, args: list[str]) -> Any:
            # Dispatch only: a child that cannot be built is this command's
            # error, said in one line, never a traceback.
            try:
                return super().resolve_command(ctx, args)
            except OttoError as exc:
                fail(exc)

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
            # Converted-child cache with NO invalidation: fine for the CLI's
            # one-shot process lifetime, but a same-module re-registration
            # (sanctioned: overwrite=True within one module) that happens
            # AFTER this group already converted the child would keep serving
            # the earlier conversion. If long-lived embedders ever hit that,
            # key the cache on the registry entry (or clear it on register).
            cache = getattr(self, "_child_cache", None) or {}
            self._child_cache = cache
            if cmd_name not in cache:
                entry = child_registry.get(cmd_name)
                converted: Any = typer.main.get_command(app_of(entry))
                cache[cmd_name] = (
                    converted.commands[cmd_name]
                    if hasattr(converted, "commands") and cmd_name in converted.commands
                    else converted
                )
            return cache[cmd_name]

    return RegistryBackedGroup
