"""otto schema — export JSON Schema for the user-edited otto files.

Commands:
    otto schema export [--out DIR] [--builtins-only]

The schemas are generated from the installed otto's pydantic models, so they
always match the running version. By default, schemas land in ``.otto/schemas/``,
the same location that ``otto init`` scaffolds and where the doctor checks for
staleness. Point your editor at the emitted files for autocomplete + typo-catching
on ``lab.json``, ``settings.toml``, the reservations JSON, and a ``json``
inventory file. See the "Editor schemas" CLI reference.

Orphaned schemas are pruned: in ``.otto/schemas`` every ``*.schema.json`` this
otto no longer emits, elsewhere only otto-stamped ones.
"""

from pathlib import Path
from typing import Annotated

import typer
from rich import print as rprint

schema_app = typer.Typer(
    name="schema",
    no_args_is_help=True,
    help="Export JSON Schema for lab.json / settings.toml / reservations / inventory.",
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


@schema_app.callback()
def schema_callback(ctx: typer.Context) -> None:
    """Export JSON Schema for lab.json / settings.toml / reservations / inventory."""


@schema_app.command("export")
def export(
    out: Annotated[
        Path,
        typer.Option("--out", "-o", help="Directory to write *.schema.json into."),
    ] = Path(".otto/schemas"),
    builtins_only: Annotated[
        bool,
        typer.Option(
            "--builtins-only",
            help=(
                "Emit only the built-in host types (unix / embedded / zephyr), "
                "excluding any custom types registered via init modules "
                "(in .otto/schemas, their schemas are pruned)."
            ),
        ),
    ] = False,
) -> None:
    """Write the JSON Schema files into the `--out` directory.

    Custom host classes registered via `.otto/settings.toml` init modules are
    included automatically; pass `--builtins-only` to emit just the built-in
    host types. Orphaned schemas are pruned: in `.otto/schemas` every
    `*.schema.json` this otto no longer emits, elsewhere only otto-stamped
    ones. `--builtins-only` writes fewer files, so in `.otto/schemas` it
    prunes the custom types' schemas too.
    """
    # Developer note: custom host classes are already loaded by the time this
    # runs (the otto package applies repo settings at import), which is why they
    # appear without any extra step.
    from rich.markup import escape

    from ..models.jsonschema import write_schemas

    result = write_schemas(out, builtins_only=builtins_only)
    for path in result.written:
        rprint(f"  wrote [cyan]{path.name}[/cyan]")
    for path in result.pruned:
        # escape(): a pruned name came from the directory, not from otto.
        rprint(f"  pruned [yellow]{escape(path.name)}[/yellow]")
    rprint(f"[green]Wrote schemas to[/green] {out}")
