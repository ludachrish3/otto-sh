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
from typing import TYPE_CHECKING

from .registry import Registry

if TYPE_CHECKING:
    import typer


@dataclasses.dataclass(frozen=True)
class InstructionEntry:
    """One registered instruction: its Typer sub-app + defining module."""

    name: str
    sub_app: "typer.Typer"
    module: str


# Populated by @instruction() as init modules are imported during startup;
# consumed lazily by run_app's RegistryBackedGroup, Repo's instruction
# panel, and the completion cache's live-registry snapshot.
INSTRUCTIONS: Registry[InstructionEntry] = Registry(
    "instruction", register_hint="@otto.instruction()"
)

FIRST_PARTY_INSTRUCTIONS: frozenset[str] = frozenset(
    ["install", "uninstall", "cleanup", "get-logs", "install-tools", "status"]
)
"""Names otto's default instructions claim (see :mod:`otto.project.instructions`).

A repo instruction may not take one -- :func:`otto.cli.run.instruction` refuses
it while a repo's init modules are being imported. The sanctioned override is a
:class:`~otto.project.actions.ProjectActions` subclass, which keeps
``otto run install`` and the ``ensure_installed`` fixture on one code path.

Declared HERE rather than beside the instructions themselves so the guard can
read it without importing them: the check runs inside the decorator, on every
registration, including the ones that happen long before
:mod:`otto.project.instructions` is reachable.
"""
