"""``otto --list-products`` / ``--list-tools``: render the listing library's answer.

A thin layer: :mod:`otto.host.listing` decides what the rows are; this module
builds the Rich table and prints. Without ``--lab`` it shows what the repos
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
    """Print one plain line: no markup (a ``[project]`` in it is literal), no wrapping."""
    from rich import get_console

    get_console().print(text, markup=False, highlight=False, soft_wrap=True)


def show_listing(ctx: "typer.Context", seam: str) -> None:
    """Print the *seam* listing (``"products"`` or ``"dev_tools"``) for this invocation.

    The lab view is chosen exactly when ``--lab`` was given; the declared view
    never demands one.
    """
    from rich import box
    from rich import print as rprint
    from rich.markup import escape
    from rich.table import Table

    from ..host import listing
    from .invoke import (
        ensure_inline_lab,
        fail_loud_on_bootstrap_errors,
        root_options,
        validate_project_switches,
    )

    title = _TITLES[seam]
    if root_options(ctx).labs:
        from ..context import get_context

        ensure_inline_lab(ctx)
        lab = get_context().lab
        result = listing.lab_rows(lab, listing.active_repos(), seam)
        table = Table(title=f"{title} in lab {escape(lab.name)}", box=box.ROUNDED)
        for column in ("name", "kind", "repo", "hosts", "artifact", "stage dir"):
            table.add_column(column)
        for row in result.rows:
            table.add_row(
                escape(row.name),
                escape(row.kind),
                escape(row.repo),
                escape(", ".join(row.hosts)),
                escape(row.artifact),
                escape(row.stage_dir),
            )
        rprint(table)
        if result.unused:
            _print_line("not used in this lab:")
            for entry in result.unused:
                _print_line(f"  {entry.name} ({entry.kind}, {entry.repo}): {entry.reason}")
        return

    # A half-registered world would list a partial answer as if it were whole.
    validate_project_switches(ctx)
    fail_loud_on_bootstrap_errors(ctx)
    repos = listing.active_repos()
    table = Table(title=title, box=box.ROUNDED)
    for column in ("name", "kind", "repo", "match", "artifact"):
        table.add_column(column)
    for declared in listing.declared_rows(repos, seam):
        table.add_row(
            escape(declared.name),
            escape(declared.kind),
            escape(declared.repo),
            escape(declared.match),
            escape(declared.artifact),
        )
    rprint(table)
    for note in listing.provider_notes(repos, seam):
        _print_line(note)
