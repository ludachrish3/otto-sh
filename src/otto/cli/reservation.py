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
    announce_expiring,
    build_reservation_gate,
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

    from .invoke import maybe_root_options

    opts = maybe_root_options(ctx)
    if opts is None:
        return None

    from ..config import get_repos

    try:
        gate = build_reservation_gate(
            get_repos(),
            holder=opts.holder,
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
    if res is None or res.identity is None:
        rprint("[yellow]No identity resolved (did the top-level callback run?)[/yellow]")
        raise typer.Exit(1)
    info = res.identity_report()

    from ..config.lab import LAB_SEPARATOR
    from .invoke import maybe_root_options

    opts = maybe_root_options(ctx)
    labs = LAB_SEPARATOR.join(opts.labs) if opts is not None and opts.labs else "<none>"
    rprint(
        f"username: [bold]{info.username}[/bold]\n"
        f"source:   {info.source}\n"
        f"backend:  {info.backend_name}\n"
        f"lab:      {labs}"
    )


@reservation_app.command()
def check(ctx: typer.Context) -> None:
    """Check the reservations the selected lab requires, and report.

    The lab is the one chosen with `otto --lab LAB reservation check` (or
    `OTTO_LAB`). The table lists every required resource with where it is
    declared (its level and owner) over the hosts in play, whose count the title
    states, and shows whether you hold each one.

    The backend is consulted only when something is actually required, so a
    backend outage cannot fail this check when nothing is required. The `none`
    backend is never queried; its rows read `n/a`.
    """
    # Developer note: the table lists every requirement with its origin -- the
    # slot, not just the string -- over the hosts in play (spec 2026-08-28
    # three-level-reservations §5). A `[project]` declaration that admits no
    # host in the loaded lab is `0 host(s) in play`: the table then holds the
    # lab-level rows and only those, and this command still reports rather than
    # refusing -- the fleet-shaped abort is a fleet WALK's, and this walks
    # nothing. The `"none"` backend answers no `held` verdict, so its rows read
    # `n/a`.
    from ..config import get_lab

    # The group is lab_free (whoami needs no lab); check is the one subcommand
    # that does — the lab defines the required-resource list — so load it here,
    # the same loud way the preamble would. Still touches no remote host.
    if "otto_reservation" not in ctx.meta:
        from .invoke import ensure_lab_context, lab_context_refusals

        with lab_context_refusals():
            ensure_lab_context(ctx)

    res = ctx.meta.get("otto_reservation")
    if res is None or res.identity is None or (res.backend is None and res.backend_factory is None):
        rprint("[red]Reservation backend or identity not configured.[/red]")
        raise typer.Exit(1)

    lab = get_lab()

    from rich import box
    from rich.table import Table

    # Function-scope: ``otto.cli.reservation`` is a budgeted import surface, and
    # the fleet accessor and rich's table machinery would otherwise be charged
    # to every ``otto reservation`` invocation, ``--help`` included.
    from ..config.fleet import get_hosts_in_play

    # The shared reservation reader, not ``admissible_ids`` directly: an empty
    # declared fleet is 0 hosts in play (a verdict, not a refusal), and the
    # built-in ``local`` host is never in play.
    in_play = get_hosts_in_play()
    report = res.report(lab, in_play)

    if not report.rows:
        # No table: an empty bordered box says nothing. And no query was made.
        rprint("(this lab requires no reservation for the hosts in play)")
    else:
        table = Table(
            title=(
                f"reservations required by lab {lab.name} for {report.username} "
                f"({len(in_play)} host(s) in play)"
            ),
            box=box.ROUNDED,
        )
        for column in ("resource", "level", "owner", "held"):
            table.add_column(column)
        for row in report.rows:
            # escape(): identifiers are opaque to otto, and rich would read
            # 'rack[a]' as a style tag and drop it. "n/a" is the `none`
            # backend's absence of a verdict.
            if row.held is None:
                held_cell = "n/a"
            else:
                held_cell = "[green]yes[/green]" if row.held else "[red]no[/red]"
            table.add_row(escape(row.resource), row.level, escape(row.owner), held_cell)
        rprint(table)
        # After the table, before the verdict: a status report says everything
        # it knows. Not suppressed by -R, because this command's whole job is
        # reporting.
        announce_expiring(report)

    if not report.covered:
        fail(MissingReservationError.from_report(report))

    rprint("[green]OK — all required resources are reserved.[/green]")
