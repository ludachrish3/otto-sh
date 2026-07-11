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

from ..reservations import (
    MissingReservationError,
    ReservationBackendError,
    ReservationGate,
    build_reservation_gate,
    check_reservations,
    required_resources,
)

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
        rprint(f"[bold red]Reservation backend unavailable:[/bold red] {e}")
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

    opts = ctx.meta.get("_otto_root_options")
    labs = ", ".join(opts.labs) if opts is not None and opts.labs else "<none>"
    rprint(
        f"username: [bold]{identity.username}[/bold]\n"
        f"source:   {identity.source}\n"
        f"backend:  {backend_name}\n"
        f"lab:      {labs}"
    )


@reservation_app.command()
def check(ctx: typer.Context) -> None:
    """Run the reservation check for the top-level ``--lab`` and report."""
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
    needed = required_resources(lab)

    rprint(f"Checking reservations for [bold]{username}[/bold] against lab [bold]{lab.name}[/bold]")
    rprint(f"Required resources: {sorted(needed)}")

    try:
        check_reservations(lab, username, backend)
    except MissingReservationError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    rprint("[green]OK — all required resources are reserved.[/green]")
