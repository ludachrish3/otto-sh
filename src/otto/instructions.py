"""Registry of user-defined ``otto run`` instructions (pure data, CLI-free).

:func:`otto.cli.run.instruction` builds each instruction's Typer sub-app and
registers it here as init modules are imported during startup. The registry
itself is deliberately CLI-free (the ``typer.Typer`` field is a
TYPE_CHECKING-only annotation), so core consumers — ``Repo``'s instruction
panel and the completion cache — read the registered set without importing
the CLI stack. An unpopulated registry simply yields no entries: instructions
only exist once init modules have run their ``@instruction()`` decorators.
"""

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .errors import OttoError
from .registry import Registry

if TYPE_CHECKING:
    import typer
    from _typeshed import DataclassInstance


@dataclasses.dataclass(frozen=True)
class InstructionEntry:
    """One registered instruction: its Typer sub-app, defining module, owning repo."""

    name: str
    sub_app: "typer.Typer"
    module: str

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


# Populated by @instruction() as init modules are imported during startup;
# consumed lazily by run_app's RegistryBackedGroup, Repo's instruction
# panel, and the completion cache's live-registry snapshot.
INSTRUCTIONS: Registry[InstructionEntry] = Registry(
    "instruction", register_hint="@otto.cli.run.instruction()"
)

FIRST_PARTY_INSTRUCTIONS: frozenset[str] = frozenset(
    ["install", "uninstall", "cleanup", "get-logs", "install-tools", "status"]
)
"""Names otto's default instructions claim (see :mod:`otto.project.actions`).

A repo instruction may not take one -- :func:`otto.cli.run.instruction` refuses
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


PROJECT_INSTRUCTIONS: Registry[ProjectInstruction] = Registry(
    "project instruction",
    register_hint="@otto.cli.run.instruction() on a ProjectActions method",
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
