"""Registry of user-defined ``otto run`` instructions (pure data, CLI-free).

:func:`instruction` registers each instruction's handler (or
project spec and bodies) here as init modules are imported during startup;
the CLI builds the command when ``otto run`` resolves it. The registry itself
is deliberately CLI-free, so core consumers — ``Repo``'s instruction panel
and the completion cache — read the registered set without importing the CLI
stack. An unpopulated registry simply yields no entries: instructions only
exist once init modules have run their ``@instruction()`` decorators.
"""

import dataclasses
import inspect
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, ParamSpec, cast

from .errors import OttoError
from .registry import Registry, get_registering_repo

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

    from .context import OttoContext


@dataclasses.dataclass(frozen=True)
class InstructionEntry:
    """One registered instruction, as data: the CLI builds its command from this on resolution.

    Exactly one of ``handler`` (a standalone instruction: the undecorated
    async function) and ``project`` (a project instruction: its spec and
    every registered body) is set. ``options_cls`` is the class a standalone
    instruction named with ``options=``. ``help`` is an explicit ``help=``;
    ``None`` lets the command take the handler's docstring.
    """

    name: str
    module: str
    handler: "Callable[..., Awaitable[Any]] | None" = None
    options_cls: "type[DataclassInstance] | None" = None
    project: "ProjectInstruction | None" = None
    help: str | None = None
    registered_by: str | None = None
    """``get_registering_repo()`` at registration -- ``None`` means first-party.

    The dispatch gate (``otto.cli.invoke.refuse_inactive_instruction``, project
    -activation spec §5) refuses an instruction whose owning repo is inactive
    for this invocation; ``None`` is never refused. The repo NAME rather than
    the :attr:`module` because activation is keyed on ``Repo.name`` — the same
    spelling ``otto.config.scope.active`` requires — while ``module`` is an
    import path that a repo may share with a library it vendors.

    Only the ``@instruction()`` decorator records this. A repo that builds an
    :class:`InstructionEntry` and calls ``INSTRUCTIONS.register()`` itself gets
    ``None``, and so first-party treatment. That is the conservative failure
    direction and is intended: an omission can never REFUSE something that
    used to run, it can only decline to refuse.
    """

    def __post_init__(self) -> None:
        if (self.handler is None) == (self.project is None):
            raise ValueError(
                f"instruction {self.name!r}: exactly one of handler and project must be set"
            )


# Populated by @instruction() as init modules are imported during startup;
# consumed lazily by run_app's RegistryBackedGroup, Repo's instruction
# panel, and the completion cache's live-registry snapshot.
INSTRUCTIONS: Registry[InstructionEntry] = Registry(
    "instruction", register_hint="@otto.instructions.instruction()"
)

FIRST_PARTY_INSTRUCTIONS: frozenset[str] = frozenset(
    ["install", "uninstall", "cleanup", "get-logs", "install-tools", "status"]
)
"""Names otto's default instructions claim (see :mod:`otto.project.actions`).

A repo instruction may not take one -- :func:`instruction` refuses
it while a repo's init modules are being imported. The sanctioned override is a
:class:`~otto.project.actions.ProjectActions` subclass, which keeps
``otto run install`` and an ``ensure("installed")`` marker on one code path.

Declared HERE rather than beside the instructions themselves so the guard can
read it without importing them: the check runs inside the decorator, on every
registration, including the ones that happen long before
:mod:`otto.project.actions` is reachable.
"""


MARK_ATTR = "__otto_project_instruction__"
"""Attribute ``@instruction`` sets on a ``ProjectActions`` method it decorated."""


class ProjectInstructionError(OttoError):
    """A project instruction was declared in a way the table refuses."""


@dataclasses.dataclass(frozen=True)
class ProjectInstructionMark:
    """What ``@instruction`` records on a method; consumed when its class registers.

    ``shape`` holds ONLY the walk-shape keywords the declaration passed
    explicitly, so a later declaration that omits one inherits the first
    declaration's value rather than silently restating a default.
    """

    name: str
    options_cls: "type[DataclassInstance] | None"
    shape: dict[str, Any]
    help: str | None


@dataclasses.dataclass(frozen=True)
class ProjectInstructionSpec:
    """One project instruction's walk shape -- fixed by whoever declared the name first."""

    name: str
    module: str
    declared_by: str | None
    help: str | None
    walk: str = "forward"
    continue_on_failure: bool = False
    require_dependencies: bool = True
    combine_results: "Callable[[dict[str, Any]], Any] | None" = None
    render: "Callable[[Any, Any], Any] | None" = None


@dataclasses.dataclass(frozen=True)
class ProjectInstructionBody:
    """One repo's body for a project instruction: the method on its actions class."""

    owner_class: type
    method_name: str
    options_cls: "type[DataclassInstance] | None"
    repo: str | None


@dataclasses.dataclass(frozen=True)
class ProjectInstruction:
    """A spec plus every registered body, in registration (dependency) order."""

    spec: ProjectInstructionSpec
    bodies: list[ProjectInstructionBody]

    def body_for(self, cls: type) -> ProjectInstructionBody | None:
        """Return the body the nearest class in *cls*'s MRO declared, or None."""
        by_owner = {body.owner_class: body for body in self.bodies}
        for klass in cls.__mro__:
            if klass in by_owner:
                return by_owner[klass]
        return None

    @property
    def first_party_options(self) -> "type[DataclassInstance] | None":
        """Return the options class of otto's own body (``repo is None``), if any."""
        for body in self.bodies:
            if body.repo is None:
                return body.options_cls
        return None


def command_name(func_name: str) -> str:
    """Return the ``otto run`` name a function name becomes: lowercase, underscores to hyphens.

    The same result ``typer.main.get_command_name`` gives (pinned by a test),
    computed here so this module never imports typer.
    """
    return func_name.lower().replace("_", "-")


def options_parameter(func: "Callable[..., Any]", opts_cls: type | None) -> str | None:
    """Name the parameter of *func* annotated exactly *opts_cls*, or ``None`` for no class.

    Raises:
        TypeError: *opts_cls* is given and no parameter carries it.
    """
    if opts_cls is None:
        return None
    from typing import get_type_hints

    for name, hint in get_type_hints(func, include_extras=True).items():
        if name != "return" and hint is opts_cls:
            return name
    func_name = getattr(func, "__name__", repr(func))
    raise TypeError(
        f"instruction {func_name!r} declares options={opts_cls.__name__} "
        f"but has no parameter annotated as {opts_cls.__name__}"
    )


PROJECT_INSTRUCTIONS: Registry[ProjectInstruction] = Registry(
    "project instruction",
    register_hint="@otto.instructions.instruction() on a ProjectActions method",
)
"""Project instructions by name; a ``Registry`` so the test isolation fixture sees it."""


def _who(repo: str | None) -> str:
    return "otto" if repo is None else f"repo {repo!r}"


def register_project_instruction_body(
    owner_class: type, method_name: str, mark: ProjectInstructionMark, *, repo: str | None
) -> None:
    """Add *owner_class*'s body for ``mark.name``, creating the spec on first declaration.

    Entries are replaced, never mutated: the isolation fixture snapshots the
    registry's objects, so appending to a shared list would leak across tests.

    The standalone-name collision check below only applies when *name* is not
    already a key in :data:`PROJECT_INSTRUCTIONS`: once a name lives in the
    table, every subsequent declaration under that name is, by construction,
    another project-instruction body for it (never a standalone instruction),
    so it must not be refused as though it collided with one.
    """
    name = mark.name
    if name not in PROJECT_INSTRUCTIONS and name in INSTRUCTIONS:
        taken = INSTRUCTIONS.get(name)
        raise ProjectInstructionError(
            f"{_who(repo)} declares project instruction {name!r} on "
            f"{owner_class.__name__}, but a standalone instruction with that name is "
            f"already registered by {_who(taken.registered_by)} ({taken.module}); "
            "rename one of them"
        )
    body = ProjectInstructionBody(
        owner_class=owner_class, method_name=method_name, options_cls=mark.options_cls, repo=repo
    )
    if name not in PROJECT_INSTRUCTIONS:
        spec = ProjectInstructionSpec(
            name=name,
            module=owner_class.__module__,
            declared_by=repo,
            help=mark.help,
            **mark.shape,
        )
        PROJECT_INSTRUCTIONS.register(
            name, ProjectInstruction(spec, [body]), origin=owner_class.__module__
        )
        return
    entry = PROJECT_INSTRUCTIONS.get(name)
    for keyword, value in mark.shape.items():
        fixed = getattr(entry.spec, keyword)
        if fixed is not value and fixed != value:
            raise ProjectInstructionError(
                f"project instruction {name!r} was declared by {_who(entry.spec.declared_by)} "
                f"with {keyword}={fixed!r}; {_who(repo)} may not restate it as {value!r} -- "
                "the first declaration fixes the walk shape"
            )
    base = entry.first_party_options
    if base is not None and (mark.options_cls is None or not issubclass(mark.options_cls, base)):
        shown = "no options class" if mark.options_cls is None else mark.options_cls.__name__
        raise ProjectInstructionError(
            f"{_who(repo)} overrides first-party instruction {name!r} with {shown}; "
            f"its options class must inherit otto.project.{base.__name__} so the "
            f"first-party flags stay on the command and super() can read them"
        )
    PROJECT_INSTRUCTIONS.register(
        name,
        ProjectInstruction(entry.spec, [*entry.bodies, body]),
        overwrite=True,
        origin=owner_class.__module__,
    )


P = ParamSpec("P")


# The handler's PARAMETERS are threaded through unchanged (``P``), so a decorated
# instruction keeps its signature at every call site instead of decaying to
# ``Any`` -- which is what ty's ``dynamic-function-decorator-return`` reports.
# Its RETURN stays ``Any`` on purpose: the leaf-invoke bridge renders whatever a
# handler hands back, and first-party and repo instructions between them already
# return ``CommandResult``, ``Result``, ``None`` and bare payloads. The narrower
# ``CommandResult`` this used to claim was never true and never checked, because
# the outer ``Callable[..., Any]`` erased it before anything could look.
_Handler = Callable[P, Coroutine[Any, Any, Any]]


def _declares_self(func: Callable[..., Any]) -> bool:
    """Whether *func* is written as a method: its first parameter is ``self``."""
    params = list(inspect.signature(func).parameters)
    return bool(params) and params[0] == "self"


def instruction(
    *args: Any,
    options: "type[DataclassInstance] | None" = None,
    walk: str | None = None,
    continue_on_failure: bool | None = None,
    require_dependencies: bool | None = None,
    combine_results: "Callable[[dict[str, Any]], Any] | None" = None,
    render: "Callable[[Any, Any], Any] | None" = None,
    name: str | None = None,
    help: str | None = None,  # noqa: A002 -- typer's spelling, as in command(help=...)
) -> Callable[[_Handler[P]], _Handler[P]]:
    """Register an async function as an ``otto run`` subcommand.

    Always called, as ``@instruction()``: the bare form raises :exc:`TypeError`.
    The decorator returns the function unchanged; ``otto run`` builds the
    command from the registered entry when it resolves it. The name is the
    positional argument or *name* (not both), else the function's name with
    underscores turned into hyphens; *help* overrides the docstring's summary.

    The handler must be ``async def`` — a plain ``def`` raises :exc:`TypeError`
    at decoration, because only a coroutine reaches the lifecycle bridge that
    sweeps the instruction's hosts and converts an interrupt into a clean
    exit. ``async def`` is necessary, not sufficient: the interrupt policy is
    driven by the event loop, so a body that blocks it (a bare
    ``subprocess.run``, ``time.sleep``) is no more interruptible than a sync
    one. Lab work belongs in ``await host.…``; local blocking work belongs in
    ``asyncio.to_thread``.

    This is the sugar's check, and THE ASYNC RULE IS THE ONLY ONE THAT IS
    RE-APPLIED when ``otto run`` INVOKES a leaf (``CommandSpec.async_leaves``),
    so a directly-registered ``InstructionEntry``, an ``@run_app.command()``,
    or a sub-group added with ``add_typer`` cannot route around *that*. The
    first-party name guard further down this function has no such twin: it runs
    at decoration or not at all — see the comment beside it for what covers the
    routes it never sees.

    When *options* is a dataclass, the command expands its fields (including
    inherited ones) into individual CLI flags.  The function must declare a
    parameter annotated with the options class (checked at decoration); the
    command constructs the populated dataclass instance and passes it there.

    If the function declares a parameter annotated as ``OttoContext``, that
    parameter is stripped from the CLI signature and injected at call time from
    the active context (DI-friendly, additive — existing handlers are unaffected).

    Every instruction also takes the flags of every options class registered
    for ``run`` (:func:`otto.params.register_options`). A parameter annotated
    with one of those classes is stripped from the CLI signature and injected
    with the parsed instance, the same one ``ctx.options(Cls)`` returns. An
    *options* class that inherits a registered base shares that base's flags
    rather than repeating them. A field that clashes with a ``run`` flag is
    reported when ``otto run`` resolves the command, before any body runs.

    Usage without options (unchanged from before)::

        @instruction()
        async def deploy(debug: Annotated[bool, typer.Option()] = False): ...

    Usage with an options dataclass::

        @dataclass
        class _Opts(RepoOptions):
            debug: Annotated[bool, typer.Option()] = False


        @instruction(options=_Opts)
        async def deploy(opts: _Opts):
            print(opts.debug)

    Usage with OttoContext injection::

        @instruction()
        async def status(ctx: OttoContext) -> CommandResult:
            host = ctx.get_host("router")
            ...

    The *same* dataclass may be inherited by a suite's inner ``Options``
    class, giving both ``otto test`` and ``otto run`` subcommands a
    uniform set of repo-wide flags.

    ON A ``ProjectActions`` METHOD (first parameter ``self``) this registers
    nothing: it stamps a :class:`~otto.instructions.ProjectInstructionMark`
    on the function and hands it back. ``register_project_actions`` reads the
    marks when the class is attributed to its repo, and
    ``otto.project.commands`` publishes one merged command per name once every
    repo has spoken. The five walk-shape keywords (``walk``,
    ``continue_on_failure``, ``require_dependencies``, ``combine_results``,
    ``render``) are legal only there; only the ones passed explicitly are
    recorded, so a later declaration inherits the first one's values.
    """
    if len(args) > 1:
        raise TypeError("instruction() takes at most one positional argument, the name")
    if args and not isinstance(args[0], str):
        # Bare `@instruction` hands the function in as the name: the user's
        # name would be bound to `decorator` below and nothing registered.
        raise TypeError("write @instruction(), with parentheses")
    if args and name is not None:
        raise TypeError(
            f"instruction() got the name twice: positional {args[0]!r} and name={name!r}"
        )

    def decorator(func: _Handler[P]) -> _Handler[P]:
        # Checked on `func` itself, with no ``__wrapped__`` unwrap: unlike the
        # group-callback guard in cli/invoke.wrap_leaf_callbacks, which sees a
        # callback typer has already update_wrapper'd, this runs before typer
        # touches anything, so `func` IS the user's function. Stricter than the
        # bridge's own contract (which accepts anything RETURNING a coroutine)
        # and deliberately so: a sync instruction already died at runtime the
        # moment it used ctx or options=, since both _inject_ctx and
        # _wrap_with_options `await func(...)`. This makes a partial, late,
        # confusing failure into a total, early, explained one.
        if not inspect.iscoroutinefunction(func):
            raise TypeError(
                f"instruction {getattr(func, '__name__', repr(func))!r} must be "
                "`async def`: the leaf-invoke bridge detects the COROUTINE a leaf "
                "returns, so a plain `def` registers and runs but never enters the "
                "command lifecycle — hosts it opens are not swept, and SIGINT/SIGTERM "
                "are not turned into a clean exit. A body with nothing to await is "
                "still correct as `async def`; a wrapper around an async function "
                "should itself be `async def` and await it."
            )

        shape = {
            k: v
            for k, v in [
                ("walk", walk),
                ("continue_on_failure", continue_on_failure),
                ("require_dependencies", require_dependencies),
                ("combine_results", combine_results),
                ("render", render),
            ]
            if v is not None
        }
        func_name = getattr(func, "__name__", repr(func))
        explicit_name = args[0] if args else name
        cmd_name = explicit_name or command_name(func_name)
        if _declares_self(func):
            options_parameter(func, options)  # for its refusal; the name is not needed here
            # An explicit help= wins over the docstring, and the six use it:
            # a project instruction's METHOD docstring describes what ONE
            # repo's body does, while the published command walks every repo,
            # so the summary a user reads in `--help` is not the summary the
            # body's author is writing. The docstring is left untouched -- it
            # is still what a reader of the class sees.
            doc = inspect.getdoc(func)
            summary = help or (doc.splitlines()[0] if doc else None)
            setattr(
                func,
                MARK_ATTR,
                ProjectInstructionMark(
                    name=cmd_name,
                    options_cls=options,
                    shape=shape,
                    help=summary,
                ),
            )
            return func
        if shape:
            raise TypeError(
                f"instruction {func_name!r}: {', '.join(shape)} apply only to a "
                "ProjectActions method (a project instruction), not to a standalone instruction"
            )

        # No wrapping: the function is registered and handed back as is.
        # `otto run <name>` builds the command from the entry when it resolves
        # it, and the handler runs under the command lifecycle via the
        # leaf-invoke wrapper's coroutine bridge (cli/invoke._wrap_invoke).
        #
        # A missing options parameter is still refused here -- at decoration,
        # not at the first `otto run`.
        options_parameter(func, options)

        # A repo may not claim a first-party name. Overriding lab behavior
        # happens in ProjectActions -- which `otto run install` AND an
        # ensure("installed") marker both route through -- so shadowing the
        # instruction would move only the CLI half and let the two answer
        # differently. Refused BEFORE the register call below: otherwise the
        # repo's entry lands first and the collision surfaces (if at all) as
        # the registry's generic "already registered", which says nothing
        # about where the override belongs.
        #
        # Keyed on the registering-repo marker, never on the name alone:
        # otto's own registration runs outside any repo's init (bootstrap
        # phase 2) and must pass whatever order the imports happen in.
        #
        # THIS GUARD COVERS THE DECORATOR AND NOTHING ELSE. A repo that builds
        # an InstructionEntry and calls INSTRUCTIONS.register() itself never
        # reaches this line. What stops it there is bootstrap's ORDER —
        # otto.project.commands publishes every project instruction AFTER
        # the repo loop, so the registry refuses whichever of the two lands
        # second — with its generic "already registered", which is exactly the
        # message this guard exists to improve on. The publish's own
        # republish-only-my-own rule is what keeps that refusal, rather than
        # overwriting the repo's entry.
        #
        # The guard now covers every project instruction, not only the
        # original six: PROJECT_INSTRUCTIONS is populated by
        # otto.project.actions at import (bootstrap imports it before any
        # repo init), and FIRST_PARTY_INSTRUCTIONS keeps the guard honest in
        # a process that never imported it.
        repo_name = get_registering_repo()
        if repo_name is not None and (
            cmd_name in FIRST_PARTY_INSTRUCTIONS or cmd_name in PROJECT_INSTRUCTIONS
        ):
            raise ValueError(
                f"repo {repo_name!r} defines instruction {cmd_name!r}, which is a "
                "project instruction. Override lab behavior by declaring the method on a "
                "ProjectActions subclass instead (see docs/cli/run/defaults.md), "
                "or rename the instruction."
            )

        func_module = getattr(func, "__module__", "<unknown>")
        INSTRUCTIONS.register(
            cmd_name,
            InstructionEntry(
                name=cmd_name,
                module=func_module,
                handler=func,
                options_cls=options,
                help=help,
                # The SAME marker the first-party-name guard above reads, and
                # the only place `registered_by` is ever filled in: one read of
                # the contextvar, so the name that refuses a collision and the
                # name that owns the entry cannot disagree.
                registered_by=repo_name,
            ),
            origin=func_module,
        )
        return func

    return decorator


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

    from .context import OttoContext
    from .params import build_options, drop_unset_secrets, verb_option_classes

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
    """
    from .context import reset_context, set_context, try_get_context
    from .params import flatten_option_instances

    entry = INSTRUCTIONS.get(name)
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
