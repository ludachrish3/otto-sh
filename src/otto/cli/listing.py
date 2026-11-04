"""``otto --list-products`` / ``--list-tools``: render the listing library's answer.

A thin layer: :mod:`otto.host.listing` decides what the rows are; this module
builds the Rich table and prints. Without ``--lab`` it shows what the projects
declare; with ``--lab`` it loads the lab the way ``--list-hosts`` does (no
reservation gate, no output directory) and shows what each host gets. Neither
view contacts a host.

Imported function-scope from the root callback, so ``otto --help`` and every
other invocation never pay for it.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import typer

_TITLES = {"products": "products", "dev_tools": "dev tools"}


def _print_line(text: str) -> None:
    """Print one line literally, through ``otto.console.print_literal``."""
    from ..console import print_literal

    print_literal(text)


def show_listing(ctx: "typer.Context", seam: str) -> None:
    """Print the *seam* listing (``"products"`` or ``"dev_tools"``) for this invocation.

    The lab view is chosen exactly when ``--lab`` was given; the declared view
    never demands one.
    """
    from rich import box
    from rich import print as rprint
    from rich.markup import escape
    from rich.table import Table

    from ..console import id_column, literal_cell
    from ..declared import get_projects
    from ..host import listing
    from .invoke import (
        ensure_inline_lab,
        fail_loud_on_bootstrap_errors,
        fail_without_projects,
        root_options,
        validate_project_switches,
    )

    title = _TITLES[seam]
    if root_options(ctx).labs:
        from ..run_context import get_context

        ensure_inline_lab(ctx)
        lab = get_context().lab
        result = listing.lab_rows(lab, listing.active_projects(), seam)
        table = Table(title=f"{title} in lab {escape(lab.name)}", box=box.ROUNDED)
        id_column(table, "name")
        table.add_column("kind")
        id_column(table, "variant")
        table.add_column("instrumented")
        for column in ("project", "hosts", "artifact", "stage dir"):
            id_column(table, column)
        for row in result.rows:
            table.add_row(
                literal_cell(row.name),
                escape(row.kind),
                literal_cell(row.variant),
                escape(row.instrumented),
                literal_cell(row.project),
                literal_cell(", ".join(row.hosts)),
                literal_cell(row.artifact),
                literal_cell(row.stage_dir),
            )
        rprint(table)
        if result.unused:
            _print_line("not used in this lab:")
            for entry in result.unused:
                _print_line(f"  {entry.name} ({entry.kind}, {entry.project}): {entry.reason}")
        return

    # A half-registered world would list a partial answer as if it were whole.
    validate_project_switches(ctx)
    fail_loud_on_bootstrap_errors(ctx)
    fail_without_projects(get_projects())
    projects = listing.active_projects()
    table = Table(title=title, box=box.ROUNDED)
    id_column(table, "name")
    table.add_column("kind")
    id_column(table, "variant")
    table.add_column("instrumented")
    for column in ("project", "match", "artifact"):
        id_column(table, column)
    for declared in listing.declared_rows(projects, seam):
        table.add_row(
            literal_cell(declared.name),
            escape(declared.kind),
            literal_cell(declared.variant),
            escape(declared.instrumented),
            literal_cell(declared.project),
            literal_cell(declared.match),
            literal_cell(declared.artifact),
        )
    rprint(table)
    for note in listing.provider_notes(projects, seam):
        _print_line(note)
