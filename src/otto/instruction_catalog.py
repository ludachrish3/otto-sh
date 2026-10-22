"""The ``otto run`` instruction catalog: the tables, the decorator and their checks.

Three tables are written. :data:`STANDALONE_INSTRUCTIONS` holds what
``@instruction()`` registers on a plain async function,
:data:`PROJECT_ACTIONS` holds one :class:`ProjectActionsEntry` per project: its
``ProjectActions`` subclass with the bodies its marked methods declare, and
:data:`FIRST_PARTY_ACTIONS` holds otto's own class and its seven bodies, which
:mod:`otto.project.actions` declares (:func:`declare_first_party_actions`).
Two tables are derived from them and are read-only:
:data:`PROJECT_INSTRUCTIONS` (one :class:`ProjectInstruction` per name, otto's
own bodies first) and :data:`INSTRUCTIONS` (every ``otto run`` command). One
check over the sources refuses a registration that would leave them
inconsistent, so a derived view is never asked to reconcile anything.

The catalog is data and rules only. At run time it imports :mod:`otto.errors`
and :mod:`otto.registry` and nothing else, so it sits in the foundation layer
and every layer reads it: ``Project``'s instruction panel, the completion cache,
the project layer and the CLI see the registered set without loading the CLI
stack or the run machinery. Running an instruction is
:mod:`otto.instructions`' work, and that module serves :func:`instruction` at
its public path; the CLI builds a command when ``otto run`` resolves it.
"""

import dataclasses
import inspect
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, ParamSpec

from .errors import OttoError
from .registry import (
    Derived,
    FrozenMap,
    Justified,
    Proposed,
    Registry,
    RegistryView,
    RequireProject,
    _registering_for_otto,
    get_registering_project,
    registration_boundary,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from _typeshed import DataclassInstance


@dataclasses.dataclass(frozen=True)
class InstructionEntry:
    """One registered instruction, as data: the CLI builds its command from this on resolution.

    Exactly one of ``handler`` (a standalone instruction: the undecorated
    async function) and ``project`` (a project instruction: its spec and
    every registered body) is set. ``options_cls`` is the class a standalone
    instruction named with ``options=``. ``help`` is an explicit ``help=``;
    ``None`` lets the command take the handler's docstring.

    The project that owns an instruction is not a field: it is
    ``INSTRUCTIONS.project(name)``, the project whose init import registered it
    (``None`` for otto's own, and for every project instruction, whose bodies
    each belong to a project of their own).
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
    ["install", "uninstall", "cleanup", "get-logs", "install-tools", "uninstall-tools", "status"]
)
"""Names otto's default instructions claim (see :mod:`otto.project.actions`).

A project instruction may not take one -- :data:`STANDALONE_INSTRUCTIONS`
refuses it while a project's init modules are being imported. The sanctioned
override is a :class:`~otto.project.actions.ProjectActions` subclass, which
keeps ``otto run install`` and an ``ensure("installed")`` marker on one code
path.

Declared HERE rather than beside the instructions themselves so the guard can
read it without importing them, in a process that never imported
:mod:`otto.project.actions`.
"""


MARK_ATTR = "__otto_project_instruction__"
"""Attribute ``@instruction`` sets on a ``ProjectActions`` method it decorated."""


PREVIEWABLE_INSTRUCTIONS: list[str] = [
    "install",
    "uninstall",
    "cleanup",
    "install-tools",
    "uninstall-tools",
]
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

    project: str | None
    """The project whose actions declare the body; ``None`` for otto's own."""

    help: str | None
    """The summary the published command shows, if this body fixes the spec."""

    shape: "FrozenMap[str, object]"
    """Only the walk-shape keywords the declaration passed explicitly.

    An omitted keyword inherits what the current first declarer of the name
    fixes, so it never conflicts with it."""


@dataclasses.dataclass(frozen=True)
class ProjectActionsEntry:
    """One project's actions: its ``ProjectActions`` subclass and the bodies it declares."""

    cls: type
    """The registered ``ProjectActions`` subclass."""

    bodies: "tuple[ProjectInstructionBody, ...]"
    """One body per ``@instruction``-marked method declared on :attr:`cls` itself."""


@dataclasses.dataclass(frozen=True)
class ProjectInstruction:
    """A spec plus every declared body, otto's first, then the projects' in registration order."""

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
        """Return the options class of otto's own body (``project is None``), if any."""
        for body in self.bodies:
            if body.project is None:
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


def _who(project: str | None) -> str:
    return "otto" if project is None else f"project {project!r}"


def _bodies_of(cls: type, project: str | None) -> "tuple[ProjectInstructionBody, ...]":
    """Build one body per ``@instruction``-marked method declared ON *cls*, as *project*'s.

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
                project=project,
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
        declared_by=first.project,
        help=first.help,
        **first.shape,  # ty: ignore[invalid-argument-type]
    )


def _declared_bodies(
    first_party: "Mapping[str, ProjectActionsEntry]",
    actions: "Mapping[str, ProjectActionsEntry]",
) -> "list[ProjectInstructionBody]":
    """Return every declared body: otto's own first, then *actions* in registration order."""
    return [
        *(b for entry in first_party.values() for b in entry.bodies),
        *(b for entry in actions.values() for b in entry.bodies),
    ]


def _check_tables(
    first_party: "Mapping[str, ProjectActionsEntry]",
    actions: "Mapping[str, ProjectActionsEntry]",
    standalone: "Mapping[str, InstructionEntry]",
    *,
    registering_standalone: bool,
) -> None:
    """Refuse a shape change, a foreign first-party options class, a duplicate body, a clash.

    The one consistency rule over the sources, run by each source's
    ``validate`` on the table as it would be after the write. The first
    declarer of a name is recomputed from the sources every time, so removing
    or replacing it lifts what it fixed; only the keywords a body passed
    explicitly are compared.

    A standalone instruction may not take one of otto's seven names
    (:data:`FIRST_PARTY_INSTRUCTIONS`) even while *first_party* is still
    empty, so the refusal never depends on whether otto has declared its
    bodies yet.

    *registering_standalone* says which source is being written. A clash
    names the standalone instruction's owner: while a standalone instruction
    is registered, the clashing one is that registration (committed
    standalone and project instructions never share a name), so its owner is
    the project whose init import is running; while project actions are
    registered, it is a committed entry, whose owner the table recorded.
    """
    first: dict[str, ProjectInstructionBody] = {}
    specs: dict[str, ProjectInstructionSpec] = {}
    seen: dict[tuple[type, str], ProjectInstructionBody] = {}
    for body in _declared_bodies(first_party, actions):
        name = body.name
        earlier = seen.get((body.owner_class, name))
        if earlier is not None and earlier.project != body.project:
            raise ProjectInstructionError(
                f"{_who(body.project)} registers class {body.owner_class.__name__}, which "
                f"{_who(earlier.project)} already registered -- each project registers its own "
                "ProjectActions subclass"
            )
        if earlier is not None:
            raise ProjectInstructionError(
                f"{_who(body.project)} declares project instruction {name!r} twice on "
                f"{body.owner_class.__name__} -- a class has at most one body per name"
            )
        seen[(body.owner_class, name)] = body
        fixed = first.setdefault(name, body)
        effective = specs.get(name) or specs.setdefault(name, _effective_spec(fixed))
        for keyword, value in body.shape.items():
            current = getattr(effective, keyword)
            if current is not value and current != value:
                raise ProjectInstructionError(
                    f"project instruction {name!r} was declared by {_who(fixed.project)} "
                    f"with {keyword}={current!r}; {_who(body.project)} may not restate it as "
                    f"{value!r} -- the first declaration fixes the walk shape"
                )
        base = fixed.options_cls if fixed.project is None else None
        if (
            base is not None
            and body is not fixed
            and not (body.options_cls is not None and issubclass(body.options_cls, base))
        ):
            shown = "no options class" if body.options_cls is None else body.options_cls.__name__
            raise ProjectInstructionError(
                f"{_who(body.project)} overrides first-party instruction {name!r} with {shown}; "
                f"its options class must inherit otto.project.{base.__name__} so the "
                f"first-party flags stay on the command and super() can read them"
            )
    for name in sorted((set(first) | FIRST_PARTY_INSTRUCTIONS) & set(standalone)):
        body = first.get(name)
        owner = (
            get_registering_project()
            if registering_standalone
            else STANDALONE_INSTRUCTIONS.project(name)
        )
        declared = (
            "one of otto's first-party instructions"
            if body is None
            else f"declared by {_who(body.project)} on {body.owner_class.__name__}"
        )
        raise ProjectInstructionError(
            f"project instruction {name!r} ({declared}) and a standalone instruction "
            f"registered by {_who(owner)} ({standalone[name].module}) share a name; "
            "rename one of them"
        )


def _current_standalone() -> "dict[str, InstructionEntry]":
    return dict(STANDALONE_INSTRUCTIONS.raw_items())


def _current_actions() -> "dict[str, ProjectActionsEntry]":
    return dict(PROJECT_ACTIONS.raw_items())


def _current_first_party() -> "dict[str, ProjectActionsEntry]":
    return dict(FIRST_PARTY_ACTIONS.raw_items())


def _first_party_class() -> type | None:
    """Return otto's declared ``ProjectActions`` class, or ``None`` before it is declared."""
    entry = _current_first_party().get(FIRST_PARTY_KEY)
    return None if entry is None else entry.cls


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
    # A project may not claim a project instruction's name. Overriding lab
    # behaviour happens in ProjectActions -- which `otto run install` AND an
    # ensure("installed") marker both route through -- so shadowing the
    # instruction would move only the CLI half and let the two answer
    # differently. Keyed on the registering-project marker, never on the name
    # alone: otto's own registrations run outside any project's init import.
    # FIRST_PARTY_INSTRUCTIONS is tested first, so the guard answers for the
    # seven whether or not FIRST_PARTY_ACTIONS has been filled yet.
    project = get_registering_project()
    first_party = _current_first_party()
    actions = _current_actions()
    if project is not None and (
        name in FIRST_PARTY_INSTRUCTIONS
        or any(b.name == name for b in _declared_bodies(first_party, actions))
    ):
        raise ValueError(
            f"project {project!r} defines instruction {name!r}, which is a "
            "project instruction. Override lab behavior by declaring the method on a "
            "ProjectActions subclass instead, or rename the instruction. See "
            "https://otto-sh.readthedocs.io/en/stable/cli/run/defaults.html#the-four-surfaces-and-the-one-override-point"
        )
    _check_tables(first_party, actions, proposed, registering_standalone=True)


def _check_project_actions(
    name: str, entry: ProjectActionsEntry, proposed: Proposed[ProjectActionsEntry]
) -> None:
    """``PROJECT_ACTIONS``' validate: the key is the registering project; the bodies are its own.

    The class must inherit otto's own ``ProjectActions``, the class
    :data:`FIRST_PARTY_ACTIONS` holds; while that table is empty, nothing
    qualifies, and the refusal is the same.
    """
    base = _first_party_class()
    if base is None or not (isinstance(entry.cls, type) and issubclass(entry.cls, base)):
        raise TypeError(
            f"project actions for project {name!r}: {entry.cls!r} is not a ProjectActions subclass"
        )
    project = get_registering_project()
    if name != project:
        raise ValueError(
            f"project actions are keyed by the project that registers them: {_who(project)} "
            f"may not register actions for project {name!r}"
        )
    for body in entry.bodies:
        if body.owner_class is not entry.cls or body.project != name:
            raise ValueError(
                f"project actions for project {name!r}: the body for {body.name!r} belongs to "
                f"{body.owner_class.__name__} of {_who(body.project)}, not to "
                f"{entry.cls.__name__} of project {name!r}"
            )
    _check_tables(
        _current_first_party(), proposed, _current_standalone(), registering_standalone=False
    )


def _check_first_party_actions(
    name: str, entry: ProjectActionsEntry, proposed: Proposed[ProjectActionsEntry]
) -> None:
    """``FIRST_PARTY_ACTIONS``' validate: otto's one row, keyed ``"otto"``, declaring the seven.

    The row is otto's, never a project's: its writer resets the registering-project
    marker, so a write made under a project's marker went around it.
    """
    project = get_registering_project()
    if project is not None:
        raise ValueError(
            f"first-party actions are otto's, but {_who(project)} is registering them; "
            "otto.project.actions declares them (declare())"
        )
    if name != FIRST_PARTY_KEY:
        raise ValueError(
            f"first-party actions are keyed {FIRST_PARTY_KEY!r}, not {name!r}: the table "
            "holds otto's own ProjectActions class, which otto.project.actions declares"
        )
    if not isinstance(entry.cls, type):
        raise TypeError(f"first-party actions: {entry.cls!r} is not a class")
    for body in entry.bodies:
        if body.owner_class is not entry.cls or body.project is not None:
            raise ValueError(
                f"first-party actions: the body for {body.name!r} belongs to "
                f"{body.owner_class.__name__} of {_who(body.project)}, not to otto's "
                f"{entry.cls.__name__}"
            )
    declared = {body.name for body in entry.bodies}
    if declared != FIRST_PARTY_INSTRUCTIONS:
        raise ProjectInstructionError(
            f"first-party actions {entry.cls.__name__} declare {sorted(declared)}; otto's "
            f"first-party instructions are {sorted(FIRST_PARTY_INSTRUCTIONS)}"
        )
    _check_tables(proposed, _current_actions(), _current_standalone(), registering_standalone=False)


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
        Justified(RequireProject(), reason="one actions class per project; the project is the key"),
    ],
)
"""Each project's :class:`~otto.instruction_catalog.ProjectActionsEntry`, keyed by project name."""

FIRST_PARTY_KEY = "otto"
"""The one key of :data:`FIRST_PARTY_ACTIONS`."""

FIRST_PARTY_ACTIONS: Registry[ProjectActionsEntry] = Registry(
    "first-party actions",
    entry=ProjectActionsEntry,
    register_hint="otto.project.actions.declare()",
    validate=_check_first_party_actions,
)
"""Otto's own ``ProjectActions`` class and its seven bodies: one row, keyed ``"otto"``.

The class lives in :mod:`otto.project.actions`, a layer above this module, so
these tables never import it: that module declares it here when it is
imported (:func:`declare_first_party_actions`), and its ``declare()`` declares
it again for a caller that must not rely on the import, such as
:func:`otto.instructions.run_instruction` and ``bootstrap()``. A table, not a
variable: filling it moves its revision, so :data:`PROJECT_INSTRUCTIONS` and
:data:`INSTRUCTIONS` derive the seven on their next read, however early they
were first read. Before it is filled, the seven names are still refused to a
standalone instruction (:data:`FIRST_PARTY_INSTRUCTIONS`), and no project's
actions class qualifies, because none can inherit the class yet."""


@registration_boundary
def declare_first_party_actions(cls: type) -> None:
    """Declare *cls*, with the bodies it marks, as otto's own actions: the table's one writer.

    Only :mod:`otto.project.actions` calls it, and
    ``tests/unit/test_slot_installers.py`` fails on any other caller. Declaring
    the class already declared does nothing and moves no revision; the write
    goes through the table's transactional ``register``, so a refused class
    leaves the table as it was. The row is otto's, outside any project, even
    when a project's init import is what first imported the installer.

    Raises:
        otto.registry.DuplicateRegistration: A different class is already declared.
        TypeError: *cls* is not a class.
        ValueError: A body *cls* marks belongs to another class or to a project.
        ProjectInstructionError: *cls* does not mark exactly the seven first-party
            names, or its bodies break a table rule.
    """
    current = _current_first_party().get(FIRST_PARTY_KEY)
    if current is not None and current.cls is cls:
        return
    with _registering_for_otto():
        FIRST_PARTY_ACTIONS.register(
            FIRST_PARTY_KEY, ProjectActionsEntry(cls, _bodies_of(cls, None))
        )


def _derive_project_instructions() -> "Iterator[Derived[ProjectInstruction]]":
    first: dict[str, ProjectInstructionBody] = {}
    bodies: dict[str, list[ProjectInstructionBody]] = {}
    for body in _declared_bodies(_current_first_party(), _current_actions()):
        first.setdefault(body.name, body)
        bodies.setdefault(body.name, []).append(body)
    for name, fixed in first.items():
        yield Derived(
            name,
            ProjectInstruction(_effective_spec(fixed), tuple(bodies[name])),
            origin=fixed.owner_class.__module__,
            project=None,
        )


PROJECT_INSTRUCTIONS: RegistryView[ProjectInstruction] = RegistryView(
    "project instruction",
    register_hint="@otto.instructions.instruction() on a ProjectActions method",
    sources=[FIRST_PARTY_ACTIONS, PROJECT_ACTIONS],
    derive=_derive_project_instructions,
)
"""The project instructions by name, derived from otto's bodies and then each project's.

Each name's spec is fixed by its first declarer (otto's for the seven)."""


def _derive_instructions() -> "Iterator[Derived[InstructionEntry]]":
    for name, project in PROJECT_INSTRUCTIONS.items():
        yield Derived(
            name,
            InstructionEntry(name=name, module=project.spec.module, project=project),
            origin=PROJECT_INSTRUCTIONS.origin(name),
            project=None,
        )
    for name, entry in STANDALONE_INSTRUCTIONS.raw_items():
        yield Derived(
            name,
            entry,
            origin=STANDALONE_INSTRUCTIONS.origin(name),
            project=STANDALONE_INSTRUCTIONS.project(name),
        )


INSTRUCTIONS: RegistryView[InstructionEntry] = RegistryView(
    "instruction",
    register_hint="@otto.instructions.instruction()",
    sources=[PROJECT_INSTRUCTIONS, STANDALONE_INSTRUCTIONS],
    derive=_derive_instructions,
)
"""Every ``otto run`` command by name: the project instructions, then the standalone ones.

``INSTRUCTIONS.project(name)`` is the project that owns an instruction: the project
whose init import registered a standalone one, ``None`` for otto's own and
for every project instruction."""


P = ParamSpec("P")


# The handler's PARAMETERS are threaded through unchanged (``P``), so a decorated
# instruction keeps its signature at every call site instead of decaying to
# ``Any`` -- which is what ty's ``dynamic-function-decorator-return`` reports.
# Its RETURN stays ``Any`` on purpose: the leaf-invoke bridge renders whatever a
# handler hands back, and first-party and project instructions between them already
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
        class _Opts(ProjectOptions):
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
    uniform set of project-wide flags.

    ON A ``ProjectActions`` METHOD (first parameter ``self``) this registers
    nothing: it stamps a :class:`~otto.instruction_catalog.ProjectInstructionMark`
    on the function and hands it back. ``register_project_actions`` reads the
    marks into its project's :class:`~otto.instruction_catalog.ProjectActionsEntry`, and
    :data:`~otto.instruction_catalog.INSTRUCTIONS` derives one merged command per name. The six
    walk-shape keywords (``walk``,
    ``continue_on_failure``, ``require_dependencies``, ``dry_run_preview``,
    ``combine_results``, ``render``) are legal only there, and
    ``dry_run_preview=True`` only on
    :data:`~otto.instruction_catalog.PREVIEWABLE_INSTRUCTIONS`; only the
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
            # An explicit help= wins over the docstring, and the seven use it:
            # a project instruction's METHOD docstring describes what ONE
            # project's body does, while the published command walks every project,
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
        # not at the first `otto run`. A project claiming a project instruction's
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
