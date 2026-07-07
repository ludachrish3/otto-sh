"""otto schema — export JSON Schema for the user-edited otto files.

Commands:
    otto schema export [--out DIR] [--builtins-only]

The schemas are generated from the installed otto's pydantic models, so they
always match the running version. Point your editor at the emitted files for
autocomplete + typo-catching on ``lab.json``, ``settings.toml``, and the
reservations JSON. See the "Editor schemas" user guide.
"""

import json
from pathlib import Path
from typing import Annotated

import typer
from rich import print as rprint

schema_app = typer.Typer(
    name="schema",
    no_args_is_help=True,
    help="Export JSON Schema for lab.json / settings.toml / reservations.",
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


@schema_app.callback()
def schema_callback(ctx: typer.Context) -> None:
    """Export JSON Schema for lab.json / settings.toml / reservations."""


@schema_app.command("export")
def export(
    out: Annotated[
        Path,
        typer.Option("--out", "-o", help="Directory to write *.schema.json into."),
    ] = Path("schemas"),
    builtins_only: Annotated[
        bool,
        typer.Option(
            "--builtins-only",
            help=(
                "Emit only the built-in host types (unix / embedded / zephyr), "
                "excluding any custom types registered via init modules."
            ),
        ),
    ] = False,
) -> None:
    """Generate the schema files into ``out``.

    Custom host classes registered via ``.otto/settings.toml`` init modules are
    already loaded by the time this runs (the otto package applies repo settings
    at import), so they appear automatically; pass ``--builtins-only`` to emit
    just the in-tree types.
    """
    from ..models.jsonschema import build_schemas

    out.mkdir(parents=True, exist_ok=True)
    for stem, doc in build_schemas(builtins_only=builtins_only).items():
        path = out / f"{stem}.schema.json"
        path.write_text(json.dumps(doc, indent=2) + "\n")
        rprint(f"  wrote [cyan]{path.name}[/cyan]")
    rprint(f"[green]Wrote schemas to[/green] {out}")
