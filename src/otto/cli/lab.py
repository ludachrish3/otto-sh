"""``otto lab`` — inspect what the lab sources make of their files.

- ``render ENTRY [--source NAME]`` — print the document a Jsonnet entry file
  renders to, as its ``jsonnet`` lab source renders it for a load.
- ``explain HOST_ID`` — show which layer supplied each of a host's values, in
  the lab the root ``-l`` option names.

A thin layer: every decision is :mod:`otto.labs.inspect`'s, and this module
parses the arguments and formats the answer. ``render`` names a file, not a
lab, so it needs no ``--lab``; the group is lab-free, and a verb that reads a
lab takes it from the root ``-l`` option.

IMPORT DISCIPLINE: :mod:`otto.labs` and the inventory are imported INSIDE
the verb that uses them, as :mod:`otto.cli.inventory` does: this module is
reachable from ``otto --help``.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from .invoke import fail

if TYPE_CHECKING:
    from ..labs.inspect import HostExplanation

lab_app = typer.Typer(
    name="lab",
    no_args_is_help=True,
    help="Inspect what the lab sources make of their files.",
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


@lab_app.callback()
def lab_callback(ctx: typer.Context) -> None:
    """Inspect what the lab sources make of their files.

    Every verb is read-only and touches no host, so this command creates no
    per-invocation output directory.
    """
    if ctx.resilient_parsing:
        return


@lab_app.command("render")
def render(
    entry: Annotated[
        Path,
        typer.Argument(
            metavar="ENTRY",
            help="The Jsonnet entry file to render, one a `jsonnet` lab source's `paths` name.",
            dir_okay=False,
        ),
    ],
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            metavar="NAME",
            help="The label of the `jsonnet` lab source to render with, when several read ENTRY.",
        ),
    ] = None,
) -> None:
    """Print the JSON document a Jsonnet entry file renders to.

    The entry renders exactly as its `jsonnet` lab source renders it for a
    load, with the inventories the source declares, and prints before any
    validation, so a document otto would refuse to load still shows. A
    document that holds a literal password or passphrase is not printed: the
    command names where the secret is and exits 1. Errors never print the
    document.
    """
    # The composition root: importing it installs the initiator that composes
    # the projects this reads (spec 2026-10-11 placement-and-phases §3.12).
    from .. import bootstrap

    del bootstrap
    import json

    from ..declared import get_projects
    from ..inventory.config import deferred_inventory_view
    from ..labs.inspect import render_entry
    from ..labs.views import LISTING_ERRORS

    projects = get_projects()
    try:
        rendered = render_entry(
            entry=entry,
            source=source,
            projects=projects,
            view=deferred_inventory_view(projects=projects),
        )
    except LISTING_ERRORS as e:
        fail(str(e), soft_wrap=True)
    if rendered.secret_at is not None:
        fail(
            f"refusing to print {entry}: it holds a literal secret at {rendered.secret_at}; "
            "move it to the credentials store",
            soft_wrap=True,
        )
    typer.echo(json.dumps(rendered.document, indent=2))


@lab_app.command("explain")
def explain(
    ctx: typer.Context,
    host_id: Annotated[
        str,
        typer.Argument(metavar="HOST_ID", help="The id of the host to explain, in the `-l` lab."),
    ],
) -> None:
    """Show which layer supplied each of a host's values.

    The lab is the one the root `-l`/`--lab` option names, built exactly as
    any other verb builds it, and the answer is read from that build: for
    each value, the layer whose value the host got (the credentials store,
    the `os_type` profile, an inventory record, the lab file or template, or
    the host preferences), then the entry's source, file and template
    provenance, and how old each inventory's snapshot is. No host is
    contacted, and no password is shown.
    """
    from ..host.provenance import recording
    from ..labs.errors import LabRepositoryError
    from ..labs.inspect import explain_host
    from ..run_context import get_context
    from .invoke import command_spec, ensure_lab_session, session_inventory_view

    # The lab slice the preamble skips for this lab-free group, the way
    # `otto monitor --live` pulls it in; recorded, so the session's own build
    # is the one explained. No reservation gate: this verb touches no host.
    with recording() as trace:
        ensure_lab_session(ctx, command_spec(ctx))
    try:
        explanation = explain_host(
            host_id=host_id,
            lab=get_context().lab,
            view=session_inventory_view(ctx),
            trace=trace,
        )
    except LabRepositoryError as e:
        fail(str(e), soft_wrap=True)
    _print_explanation(explanation)


def _print_explanation(explanation: "HostExplanation") -> None:
    """Print *explanation*: the winners as a rounded table, then where the entry came from."""
    from rich import box
    from rich import print as rprint
    from rich.table import Table

    from ..console import id_column, literal_cell

    table = Table(box=box.ROUNDED)
    id_column(table, "field")
    id_column(table, "layer")
    id_column(table, "detail")
    for winner in explanation.winners:
        table.add_row(
            literal_cell(winner.field),
            literal_cell(explanation.layer_label(winner)),
            literal_cell(winner.detail or ""),
        )
    rprint(table)
    _line("host", explanation.host_id)
    _line("source", explanation.source_label)
    _line(
        "entry",
        f"{explanation.entry} (element {explanation.element!r}, hosts[{explanation.index}])",
    )
    provenance = explanation.provenance or {}
    for key, label in (("instance", "instance"), ("role", "role"), ("host", "host label")):
        if key in provenance:
            _line(label, _spelled(provenance[key]))
    for inventory in explanation.inventories:
        stale = " (served stale: the inventory was unreachable)" if inventory.served_stale else ""
        _line(f"inventory {inventory.name}", f"{inventory.age}{stale}")


def _line(label: str, value: str) -> None:
    """Print one ``label: value`` line of an explanation, exactly as written."""
    from ..console import print_literal

    print_literal(f"{label}: {value}")


def _spelled(value: object) -> str:
    """Return a provenance value as the template wrote it: a string bare, anything else as JSON."""
    import json

    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)
