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
