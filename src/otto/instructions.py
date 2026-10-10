"""The ``otto run`` instruction tables (pure data, CLI-free).

Two tables are written. :data:`STANDALONE_INSTRUCTIONS` holds what
``@instruction()`` registers on a plain async function, and
:data:`PROJECT_ACTIONS` holds one :class:`ProjectActionsEntry` per repo: its
``ProjectActions`` subclass with the bodies its marked methods declare. Two
tables are derived from them and are read-only:
:data:`PROJECT_INSTRUCTIONS` (one :class:`ProjectInstruction` per name, otto's
own bodies first) and :data:`INSTRUCTIONS` (every ``otto run`` command). One
check over both sources refuses a registration that would leave them
inconsistent, so a derived view is never asked to reconcile anything.

The tables are CLI-free, so core consumers (``Repo``'s instruction panel and
the completion cache) read them without importing the CLI stack; the CLI
builds a command when ``otto run`` resolves it.
"""

import dataclasses
import inspect
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, ParamSpec, cast

from .errors import OttoError
from .registry import (
    Derived,
    FrozenMap,
    Justified,
    Proposed,
    Registry,
    RegistryView,
    RequireRepo,
    get_registering_repo,
    registration_boundary,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from _typeshed import DataclassInstance

    from .context import OttoContext

__all__ = ["instruction", "run_instruction"]


@dataclasses.dataclass(frozen=True)
class InstructionEntry:
    """One registered instruction, as data: the CLI builds its command from this on resolution.

    Exactly one of ``handler`` (a standalone instruction: the undecorated
    async function) and ``project`` (a project instruction: its spec and
    every registered body) is set. ``options_cls`` is the class a standalone
    instruction named with ``options=``. ``help`` is an explicit ``help=``;
    ``None`` lets the command take the handler's docstring.

    The repo that owns an instruction is not a field: it is
    ``INSTRUCTIONS.repo(name)``, the repo whose init import registered it
    (``None`` for otto's own, and for every project instruction, whose bodies
    each belong to a repo of their own).
    """

    name: str
    module: str
    handler: "Callable[..., Awaitable[Any]] | None" = None
    options_cls: "type[DataclassInstance] | None" = None
    project: "ProjectInstruction | None" = None
    help: str | None = None

    def __post_init__(self) -> None:
        if (self.handler is None) == (self.project is None):
            raise ValueError(
                f"instruction {self.name!r}: exactly one of handler and project must be set"
            )


FIRST_PARTY_INSTRUCTIONS: frozenset[str] = frozenset(
    ["install", "uninstall", "cleanup", "get-logs", "install-tools", "status"]
)
"""Names otto's default instructions claim (see :mod:`otto.project.actions`).

A repo instruction may not take one -- :data:`STANDALONE_INSTRUCTIONS`
refuses it while a repo's init modules are being imported. The sanctioned
override is a :class:`~otto.project.actions.ProjectActions` subclass, which
keeps ``otto run install`` and an ``ensure("installed")`` marker on one code
path.

Declared HERE rather than beside the instructions themselves so the guard can
read it without importing them, in a process that never imported
:mod:`otto.project.actions`.
"""


MARK_ATTR = "__otto_project_instruction__"
"""Attribute ``@instruction`` sets on a ``ProjectActions`` method it decorated."""


PREVIEWABLE_INSTRUCTIONS: list[str] = ["install", "uninstall", "install-tools"]
"""The project instructions ``dry_run_preview=True`` is honoured for.

These are the names :func:`otto.project.plan.plan_instruction` can plan; it
raises for any other, and ``instruction()`` refuses the keyword on any other at
declaration. Declared here, not in ``otto.project.plan``, because the
first-party declarations in ``otto.project.actions`` run at import and
``plan`` imports that module.
"""


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
    dry_run_preview: bool = False
    combine_results: "Callable[[dict[str, Any]], Any] | None" = None
    render: "Callable[[Any, Any], Any] | None" = None


@dataclasses.dataclass(frozen=True)
class ProjectInstructionBody:
    """One body for a project instruction: a marked method on an actions class."""

    name: str
    """The project instruction this body runs for."""

    owner_class: type
    """The ``ProjectActions`` class that declares the method."""

    method_name: str
    """The attribute name of the method on :attr:`owner_class`."""

    options_cls: "type[DataclassInstance] | None"
    """The options class the method takes, or ``None``."""

    repo: str | None
    """The repo whose actions declare the body; ``None`` for otto's own."""

    help: str | None
    """The summary the published command shows, if this body fixes the spec."""

    shape: "FrozenMap[str, object]"
    """Only the walk-shape keywords the declaration passed explicitly.

    An omitted keyword inherits what the current first declarer of the name
    fixes, so it never conflicts with it."""


@dataclasses.dataclass(frozen=True)
class ProjectActionsEntry:
    """One repo's actions: its ``ProjectActions`` subclass and the bodies it declares."""

    cls: type
    """The registered ``ProjectActions`` subclass."""

    bodies: "tuple[ProjectInstructionBody, ...]"
    """One body per ``@instruction``-marked method declared on :attr:`cls` itself."""


@dataclasses.dataclass(frozen=True)
class ProjectInstruction:
    """A spec plus every declared body, otto's first, then the repos' in registration order."""

    spec: ProjectInstructionSpec
    bodies: "tuple[ProjectInstructionBody, ...]"

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


def _who(repo: str | None) -> str:
    return "otto" if repo is None else f"repo {repo!r}"


def _bodies_of(cls: type, repo: str | None) -> "tuple[ProjectInstructionBody, ...]":
    """Build one body per ``@instruction``-marked method declared ON *cls*, as *repo*'s.

    Reads only ``vars(cls)`` and each method's mark, never a table.
    ``vars(cls)``, not ``dir()``: an inherited, undecorated method is the
    parent's body already, so a subclass that overrides nothing declares
    nothing.

    TWO MARKED METHODS ON ONE CLASS CLAIMING ONE NAME ARE REFUSED: a class
    holds at most one body per name, and a typo'd ``@instruction("install")``
    on a second method would otherwise simply never run.

    Raises:
        ProjectInstructionError: Two methods of *cls* claim one name.
    """
    bodies: list[ProjectInstructionBody] = []
    claimed: dict[str, str] = {}
    for attr, value in vars(cls).items():
        mark = getattr(value, MARK_ATTR, None)
        if not isinstance(mark, ProjectInstructionMark):
            continue
        if mark.name in claimed:
            raise ProjectInstructionError(
                f"{cls.__name__} declares project instruction {mark.name!r} twice, on "
                f"{claimed[mark.name]}() and {attr}() -- a class has at most one body per "
                "name; rename one of them, or drop the duplicate decorator"
            )
        claimed[mark.name] = attr
        bodies.append(
            ProjectInstructionBody(
                name=mark.name,
                owner_class=cls,
                method_name=attr,
                options_cls=mark.options_cls,
                repo=repo,
                help=mark.help,
                shape=FrozenMap(mark.shape),
            )
        )
    return tuple(bodies)


def _effective_spec(first: ProjectInstructionBody) -> ProjectInstructionSpec:
    """Return the spec *first* fixes: the defaults overlaid with its explicit keywords."""
    return ProjectInstructionSpec(
        name=first.name,
        module=first.owner_class.__module__,
        declared_by=first.repo,
        help=first.help,
        **first.shape,  # ty: ignore[invalid-argument-type]
    )


def _declared_bodies(
    actions: "Mapping[str, ProjectActionsEntry]",
) -> "list[ProjectInstructionBody]":
    """Return every declared body: otto's constant first, then *actions* in registration order."""
    from .project.actions import FIRST_PARTY_BODIES  # function-scope: project imports this module

    return [*FIRST_PARTY_BODIES, *(b for entry in actions.values() for b in entry.bodies)]


def _check_tables(
    actions: "Mapping[str, ProjectActionsEntry]",
    standalone: "Mapping[str, InstructionEntry]",
    *,
    registering_standalone: bool,
) -> None:
    """Refuse a shape change, a foreign first-party options class, a duplicate body, a clash.

    The one consistency rule over both sources, run by each source's
    ``validate`` on the table as it would be after the write. The first
    declarer of a name is recomputed from the sources every time, so removing
    or replacing it lifts what it fixed; only the keywords a body passed
    explicitly are compared.

    *registering_standalone* says which source is being written. A clash
    names the standalone instruction's owner: while a standalone instruction
    is registered, the clashing one is that registration (committed
    standalone and project instructions never share a name), so its owner is
    the repo whose init import is running; while project actions are
    registered, it is a committed entry, whose owner the table recorded.
    """
    first: dict[str, ProjectInstructionBody] = {}
    specs: dict[str, ProjectInstructionSpec] = {}
    seen: dict[tuple[type, str], ProjectInstructionBody] = {}
    for body in _declared_bodies(actions):
        name = body.name
        earlier = seen.get((body.owner_class, name))
        if earlier is not None and earlier.repo != body.repo:
            raise ProjectInstructionError(
                f"{_who(body.repo)} registers class {body.owner_class.__name__}, which "
                f"{_who(earlier.repo)} already registered -- each repo registers its own "
                "ProjectActions subclass"
            )
        if earlier is not None:
            raise ProjectInstructionError(
                f"{_who(body.repo)} declares project instruction {name!r} twice on "
                f"{body.owner_class.__name__} -- a class has at most one body per name"
            )
        seen[(body.owner_class, name)] = body
        fixed = first.setdefault(name, body)
        effective = specs.get(name) or specs.setdefault(name, _effective_spec(fixed))
        for keyword, value in body.shape.items():
            current = getattr(effective, keyword)
            if current is not value and current != value:
                raise ProjectInstructionError(
                    f"project instruction {name!r} was declared by {_who(fixed.repo)} "
                    f"with {keyword}={current!r}; {_who(body.repo)} may not restate it as "
                    f"{value!r} -- the first declaration fixes the walk shape"
                )
        base = fixed.options_cls if fixed.repo is None else None
        if (
            base is not None
            and body is not fixed
            and not (body.options_cls is not None and issubclass(body.options_cls, base))
        ):
            shown = "no options class" if body.options_cls is None else body.options_cls.__name__
            raise ProjectInstructionError(
                f"{_who(body.repo)} overrides first-party instruction {name!r} with {shown}; "
                f"its options class must inherit otto.project.{base.__name__} so the "
                f"first-party flags stay on the command and super() can read them"
            )
    for name in sorted(set(first) & set(standalone)):
        body = first[name]
        owner = (
            get_registering_repo() if registering_standalone else STANDALONE_INSTRUCTIONS.repo(name)
        )
        raise ProjectInstructionError(
            f"project instruction {name!r} (declared by {_who(body.repo)} on "
            f"{body.owner_class.__name__}) and a standalone instruction registered by "
            f"{_who(owner)} ({standalone[name].module}) share a name; rename one of them"
        )


def _current_standalone() -> "dict[str, InstructionEntry]":
    return dict(STANDALONE_INSTRUCTIONS.raw_items())


def _current_actions() -> "dict[str, ProjectActionsEntry]":
    return dict(PROJECT_ACTIONS.raw_items())


def _check_standalone(
    name: str, entry: InstructionEntry, proposed: Proposed[InstructionEntry]
) -> None:
    """``STANDALONE_INSTRUCTIONS``'s validate: a handler, its own name, no project name."""
    if entry.handler is None or entry.project is not None:
        raise ValueError(
            f"standalone instruction {name!r}: a standalone entry holds a handler; a project "
            "instruction is declared on a ProjectActions subclass (register_project_actions)"
        )
    if entry.name != name:
        raise ValueError(
            f"standalone instruction {name!r}: the entry is named {entry.name!r}; "
            "an instruction is registered under its own name"
        )
    # A repo may not claim a project instruction's name. Overriding lab
    # behaviour happens in ProjectActions -- which `otto run install` AND an
    # ensure("installed") marker both route through -- so shadowing the
    # instruction would move only the CLI half and let the two answer
    # differently. Keyed on the registering-repo marker, never on the name
    # alone: otto's own registrations run outside any repo's init import.
    # FIRST_PARTY_INSTRUCTIONS is tested first, so the guard answers for the
    # six without importing otto.project.actions.
    repo = get_registering_repo()
    actions = _current_actions()
    if repo is not None and (
        name in FIRST_PARTY_INSTRUCTIONS or any(b.name == name for b in _declared_bodies(actions))
    ):
        raise ValueError(
            f"repo {repo!r} defines instruction {name!r}, which is a "
            "project instruction. Override lab behavior by declaring the method on a "
            "ProjectActions subclass instead (see docs/cli/run/defaults.md), "
            "or rename the instruction."
        )
    _check_tables(actions, proposed, registering_standalone=True)


def _check_project_actions(
    name: str, entry: ProjectActionsEntry, proposed: Proposed[ProjectActionsEntry]
) -> None:
    """``PROJECT_ACTIONS``' validate: the key is the registering repo; the bodies are its own."""
    from .project.actions import ProjectActions  # function-scope: project imports this module

    if not (isinstance(entry.cls, type) and issubclass(entry.cls, ProjectActions)):
        raise TypeError(
            f"project actions for repo {name!r}: {entry.cls!r} is not a ProjectActions subclass"
        )
    repo = get_registering_repo()
    if name != repo:
        raise ValueError(
            f"project actions are keyed by the repo that registers them: {_who(repo)} may not "
            f"register actions for repo {name!r}"
        )
    for body in entry.bodies:
        if body.owner_class is not entry.cls or body.repo != name:
            raise ValueError(
                f"project actions for repo {name!r}: the body for {body.name!r} belongs to "
                f"{body.owner_class.__name__} of {_who(body.repo)}, not to "
                f"{entry.cls.__name__} of repo {name!r}"
            )
    _check_tables(proposed, _current_standalone(), registering_standalone=False)


STANDALONE_INSTRUCTIONS: Registry[InstructionEntry] = Registry(
    "standalone instruction",
    entry=InstructionEntry,
    register_hint="@otto.instructions.instruction()",
    validate=_check_standalone,
)
"""The standalone instructions by name, which ``@instruction()`` registers on a plain function."""

PROJECT_ACTIONS: Registry[ProjectActionsEntry] = Registry(
    "project actions",
    entry=ProjectActionsEntry,
    register_hint="otto.register_project_actions()",
    validate=_check_project_actions,
    capabilities=[
        Justified(RequireRepo(), reason="one actions class per repo; the repo is the key"),
    ],
)
"""Each repo's :class:`~otto.instructions.ProjectActionsEntry`, keyed by repo name."""


def _derive_project_instructions() -> "Iterator[Derived[ProjectInstruction]]":
    first: dict[str, ProjectInstructionBody] = {}
    bodies: dict[str, list[ProjectInstructionBody]] = {}
    for body in _declared_bodies(_current_actions()):
        first.setdefault(body.name, body)
        bodies.setdefault(body.name, []).append(body)
    for name, fixed in first.items():
        yield Derived(
            name,
            ProjectInstruction(_effective_spec(fixed), tuple(bodies[name])),
            origin=fixed.owner_class.__module__,
            repo=None,
        )


PROJECT_INSTRUCTIONS: RegistryView[ProjectInstruction] = RegistryView(
    "project instruction",
    register_hint="@otto.instructions.instruction() on a ProjectActions method",
    sources=[PROJECT_ACTIONS],
    derive=_derive_project_instructions,
)
"""The project instructions by name, derived from otto's bodies and then each repo's.

Each name's spec is fixed by its first declarer (otto's for the six)."""


def _derive_instructions() -> "Iterator[Derived[InstructionEntry]]":
    for name, project in PROJECT_INSTRUCTIONS.items():
        yield Derived(
            name,
            InstructionEntry(name=name, module=project.spec.module, project=project),
            origin=PROJECT_INSTRUCTIONS.origin(name),
            repo=None,
        )
    for name, entry in STANDALONE_INSTRUCTIONS.raw_items():
        yield Derived(
            name,
            entry,
            origin=STANDALONE_INSTRUCTIONS.origin(name),
            repo=STANDALONE_INSTRUCTIONS.repo(name),
        )


INSTRUCTIONS: RegistryView[InstructionEntry] = RegistryView(
    "instruction",
    register_hint="@otto.instructions.instruction()",
    sources=[PROJECT_INSTRUCTIONS, STANDALONE_INSTRUCTIONS],
    derive=_derive_instructions,
)
"""Every ``otto run`` command by name: the project instructions, then the standalone ones.

``INSTRUCTIONS.repo(name)`` is the repo that owns an instruction: the repo
whose init import registered a standalone one, ``None`` for otto's own and
for every project instruction."""


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
    dry_run_preview: bool | None = None,
    combine_results: "Callable[[dict[str, Any]], Any] | None" = None,
    render: "Callable[[Any, Any], Any] | None" = None,
    name: str | None = None,
    help: str | None = None,  # noqa: A002 -- typer's spelling, as in command(help=...)
    overwrite: bool = False,
) -> Callable[[_Handler[P]], _Handler[P]]:
    """Register an async function as an ``otto run`` subcommand.

    Always called, as ``@instruction()``: the bare form raises :exc:`TypeError`.
    The decorator returns the function unchanged; ``otto run`` builds the
    command from the registered entry when it resolves it. The name is the
    positional argument or *name* (not both), else the function's name with
    underscores turned into hyphens; *help* overrides the docstring's summary.
    A name already registered raises
    :class:`~otto.registry.DuplicateRegistration` unless *overwrite* is true.

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
    refusal of a project instruction's name is the table's own, so it covers a
    direct ``STANDALONE_INSTRUCTIONS.register()`` too.

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
    marks into its repo's :class:`ProjectActionsEntry`, and
    :data:`INSTRUCTIONS` derives one merged command per name. The six
    walk-shape keywords (``walk``,
    ``continue_on_failure``, ``require_dependencies``, ``dry_run_preview``,
    ``combine_results``, ``render``) are legal only there, and
    ``dry_run_preview=True`` only on :data:`PREVIEWABLE_INSTRUCTIONS`; only the
    ones passed explicitly are recorded, so a later declaration inherits the first one's
    values.
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

    @registration_boundary
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
                ("dry_run_preview", dry_run_preview),
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
            if dry_run_preview and cmd_name not in PREVIEWABLE_INSTRUCTIONS:
                raise ValueError(
                    f"instruction {func_name!r}: dry_run_preview=True is honoured only for "
                    f"{', '.join(PREVIEWABLE_INSTRUCTIONS)}, the verbs a plan can be "
                    f"built for; {cmd_name!r} cannot be previewed"
                )
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
        # not at the first `otto run`. A repo claiming a project instruction's
        # name is refused by the table itself (STANDALONE_INSTRUCTIONS'
        # validate), so a direct register() meets the same refusal.
        options_parameter(func, options)
        STANDALONE_INSTRUCTIONS.register(
            cmd_name,
            InstructionEntry(
                name=cmd_name,
                module=getattr(func, "__module__", "<unknown>"),
                handler=func,
                options_cls=options,
                help=help,
            ),
            overwrite=overwrite,
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
        otto.session.InstructionInactiveError: *name* belongs to a repo that
            is inactive in *ctx* (excluded, out of the loaded labs' scope,
            or host-starved).
    """
    from .context import reset_context, set_context, try_get_context
    from .params import flatten_option_instances

    entry = INSTRUCTIONS.get(name)
    from .session import check_instruction_active

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
