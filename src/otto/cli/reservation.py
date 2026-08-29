"""``otto reservation`` — read-only helpers over the configured reservation backend.

Subcommands:

- ``otto reservation whoami`` — show the resolved identity and backend. Needs
  no lab: identity and backend come from repo settings + root options.
- ``otto --lab LAB reservation check`` — run the reservation check and print a
  human-readable report. Useful as a pre-flight before a long ``otto test``.
  Loads the lab (which defines the required resources) lazily; never contacts
  a host.

The group is registered ``lab_free`` — ``check`` is the one subcommand that
needs lab *data*, and it pulls the lab in itself via ``ensure_lab_context``.
"""

from pathlib import Path

import typer
from rich import print as rprint
from rich.markup import escape

from ..reservations import (
    MissingReservationError,
    ReservationBackendError,
    ReservationGate,
    build_reservation_gate,
    check_reservations,
    is_null_backend,
    required_resource_origins,
)
from .invoke import fail

reservation_app = typer.Typer(
    name="reservation",
    no_args_is_help=True,
    help="Inspect and verify lab reservations.",
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


@reservation_app.callback()
def reservation_callback(ctx: typer.Context) -> None:
    """Inspect and verify lab reservations.

    Reservation queries are informational and touch no remote host, so this
    command creates no per-invocation output directory.
    """
    if ctx.resilient_parsing:
        return


def _reservation_gate(ctx: typer.Context) -> ReservationGate | None:
    """Return the per-invocation reservation gate, resolving it lab-free if needed.

    Commands that already went through ``ensure_lab_context`` find the gate in
    ``ctx.meta``; the lab-free path (``whoami`` without ``--lab``) builds it
    here from repo settings + root options — identity and backend never depend
    on the lab.
    """
    res = ctx.meta.get("otto_reservation")
    if res is not None:
        return res
    opts = ctx.meta.get("_otto_root_options")
    if opts is None:
        return None

    from ..config import get_repos

    try:
        gate = build_reservation_gate(
            get_repos(),
            as_user=opts.as_user,
            skip_reservation_check=opts.skip_reservation_check,
            cwd_fallback=Path.cwd(),
        )
    except ReservationBackendError as e:
        rprint(f"[bold red]Reservation backend unavailable:[/bold red] {escape(str(e))}")
        raise typer.Exit(1) from e
    ctx.meta["otto_reservation"] = gate
    return gate


@reservation_app.command()
def whoami(ctx: typer.Context) -> None:
    """Show the resolved reservation identity and backend (no lab required)."""
    res = _reservation_gate(ctx)
    backend = None
    if res is not None:
        backend = res.backend or (res.backend_factory() if res.backend_factory else None)
    backend_name = backend.backend_name() if backend else "<none>"
    identity = res.identity if res else None
    if identity is None:
        rprint("[yellow]No identity resolved (did the top-level callback run?)[/yellow]")
        raise typer.Exit(1)

    from ..config.lab import LAB_SEPARATOR

    opts = ctx.meta.get("_otto_root_options")
    labs = LAB_SEPARATOR.join(opts.labs) if opts is not None and opts.labs else "<none>"
    rprint(
        f"username: [bold]{identity.username}[/bold]\n"
        f"source:   {identity.source}\n"
        f"backend:  {backend_name}\n"
        f"lab:      {labs}"
    )


@reservation_app.command()
def check(ctx: typer.Context) -> None:
    """Run the reservation check for the top-level ``--lab`` and report.

    The table lists every requirement with its origin — the slot, not just the
    string — over the hosts in play (spec 2026-08-28 three-level-reservations
    §5), whose count the title states. A ``[project]`` declaration that admits
    no host in the loaded lab is ``0 host(s) in play``: the table then holds
    the lab-level rows and only those, and this command still reports rather
    than refusing — the fleet-shaped abort is a fleet WALK's, and this walks
    nothing.

    The backend is consulted only when something is actually required, so an
    outage cannot fail a run that needs no reservation, and the ``"none"``
    backend is never queried at all — its rows read ``n/a`` rather than a
    ``held`` verdict it has no way to give.
    """
    from ..config import get_lab

    # The group is lab_free (whoami needs no lab); check is the one subcommand
    # that does — the lab defines the required-resource list — so load it here,
    # the same loud way the preamble would. Still touches no remote host.
    if "otto_reservation" not in ctx.meta:
        from .invoke import LabContextError, ensure_lab_context, report_lab_context_error

        try:
            ensure_lab_context(ctx)
        except LabContextError as e:
            report_lab_context_error(e)

    res = ctx.meta.get("otto_reservation")

    backend = None
    if res is not None:
        backend = res.backend or (res.backend_factory() if res.backend_factory else None)
    if res is None or backend is None or res.identity is None:
        rprint("[red]Reservation backend or identity not configured.[/red]")
        raise typer.Exit(1)

    lab = get_lab()
    username = res.identity.username

    from rich import box
    from rich.table import Table

    # Function-scope: ``otto.cli.reservation`` is one of the budgeted import
    # surfaces, and pulling the fleet accessor (and rich's table machinery) in
    # at module scope would move the snapshot for every ``otto`` invocation,
    # not just this subcommand's.
    from ..config.fleet import get_hosts_in_play

    # NOT rebound onto ``ctx`` — that name is the typer Context this command
    # was handed, and shadowing it here would be a live bug the moment
    # anything below reached for ctx.meta again.
    # The shared reservation reader, not ``admissible_ids`` directly: it bakes
    # in the two rules this table must agree with the gate about — a
    # declaration that admits nothing is 0 hosts in play (a lab-level-only
    # requirement, which is a verdict, and the fleet-shaped refusal belongs to
    # the walk a run would do next rather than to a read-only report about it),
    # and the built-in ``local`` host is never in play at all.
    in_play = get_hosts_in_play()
    origins = required_resource_origins(lab, host_ids=in_play)

    if not origins:
        # No table: an empty bordered header box is chrome that says nothing,
        # and the sentence is the whole message. Nor is the backend queried —
        # check_reservations returns on an empty requirement before it ever
        # asks, and a table that asked first would fail this command on a
        # backend outage where it used to succeed.
        rprint("(this lab requires no reservation for the hosts in play)")
    else:
        # The null backend reserves nothing and check_reservations
        # short-circuits on it, so "does alice hold this?" has no answer to
        # give; asking anyway returns set() and would render every row unheld
        # directly above the OK line. Same predicate as the verdict's, so the
        # table and the check can never disagree about what "none" means.
        null = is_null_backend(backend)
        held = set() if null else backend.get_reserved_resources(username)
        table = Table(
            title=(
                f"reservations required by lab {lab.name} for {username} "
                f"({len(in_play)} host(s) in play)"
            ),
            box=box.ROUNDED,
        )
        for column in ("resource", "level", "owner", "held"):
            table.add_column(column)
        for origin in origins:
            # escape(): a resource identifier is OPAQUE to otto and an owner can
            # be any host id, while rich reads '[a]' in a cell as a style tag and
            # drops it — 'rack[a]' would render as 'rack', naming a resource that
            # is not the one being checked. ``level`` is a closed Literal, so it
            # needs none, and "n/a" is plain: it is an absence, not a verdict.
            if null:
                held_cell = "n/a"
            else:
                held_cell = "[green]yes[/green]" if origin.resource in held else "[red]no[/red]"
            table.add_row(
                escape(origin.resource),
                origin.level,
                escape(origin.owner),
                held_cell,
            )
        rprint(table)

    try:
        check_reservations(lab, username, backend, host_ids=in_play)
    except MissingReservationError as e:
        fail(e)

    rprint("[green]OK — all required resources are reserved.[/green]")
