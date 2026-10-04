"""
otto host — run commands, transfer files, and log in to lab hosts.

Commands are synthesised dynamically from ``@cli_exposed`` methods on the
resolved host's class — see ``otto.cli.expose``.
"""

from typing import TYPE_CHECKING, Annotated

import typer
from rich import print as rprint

from .callbacks import list_hosts_callback
from .completers import completion_source
from .expose import HostGroup
from .invoke import fail, print_error

if TYPE_CHECKING:
    # Annotations only: the verb menu comes from the resolved host's class
    # (`otto.cli.expose`), so resolving `otto host` needs no host class, and
    # importing one here would put the whole host stack on `otto host --help`.
    # The fleet and the run context are imported where a host is resolved,
    # for the same reason: both import the host base class.
    from ..host.remote_host import RemoteHost
    from ..host.unix_host import UnixHost


@completion_source(kind="payload", key="hosts", lab_scoped=True, sort=True)
def _host_id_completer(ctx: typer.Context, incomplete: str) -> list[str]:
    """Shell-completion source for the ``host_id`` positional argument.

    Scoped to the selected lab via the shared resolution in
    :mod:`otto.cli.completers` — see :func:`~otto.cli.completers.lab_scoped_host_ids`.
    """
    from .completers import lab_scoped_host_ids

    return sorted(h for h in lab_scoped_host_ids(ctx) if h.startswith(incomplete))


@completion_source(kind="payload", key="term_backends", sort=True)
def _term_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--term``: registered term backends.

    Prefers the completion-cache snapshot (populated by the slow path so custom
    per-repo backends complete without re-running user code);
    falls back to the live registry, where otto's built-ins are always present.
    """
    from ..config import get_completion_names

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("term_backends"), list):
        names = cached["term_backends"]
    else:
        from ..host.connections import TERM_BACKENDS

        names = TERM_BACKENDS.names()
    return sorted(n for n in names if n.startswith(incomplete))


@completion_source(kind="payload", key="transfer_backends", family="unix", sort=True)
def _transfer_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--transfer``: unix-applicable transfer backends.

    Same cache-then-live strategy as :func:`_term_completer`. The unified
    transfer registry spans both host families; ``otto host`` operates on a unix
    host, so only backends whose ``host_families`` include ``'unix'`` are offered.
    Cached entries are ``{"name": str, "host_families": [...]}``.
    """
    from ..config import get_completion_names

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("transfer_backends"), list):
        names = [
            e["name"]
            for e in cached["transfer_backends"]
            if isinstance(e, dict) and "unix" in e.get("host_families", [])
        ]
    else:
        from ..host.transfer import TRANSFER_BACKENDS

        names = [n for n, c in TRANSFER_BACKENDS.items() if "unix" in c.host_families]
    return sorted(n for n in names if n.startswith(incomplete))


host_app = typer.Typer(
    name="host",
    help="Run commands and transfer files on lab hosts.",
    cls=HostGroup,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


def _resolve_host(host_id: str) -> "UnixHost":
    from ..config.fleet import get_host
    from ..context import get_context

    try:
        return get_host(host_id)
    except KeyError:
        print_error(f"No host with ID {host_id!r}.")
        rprint("Available hosts:")
        # The lab's mapping directly, NOT `all_hosts`: this listing enumerates
        # what `otto host <id>` can ADDRESS, and explicit targeting is
        # deliberately unscoped by the project-universe design — so a host
        # outside every repo's fleet of interest must still be offered here,
        # exactly as `get_host` would still resolve it. Going through the fleet
        # generator would also make this error path raise its own error when
        # the universe came out empty, replacing "No host with ID x" with a
        # scoping complaint about a lab the user was not asking about.
        for known_id in get_context().lab.hosts:
            rprint(f"  - {known_id}")
        raise typer.Exit(1) from None


@host_app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    host_id: Annotated[
        str,
        typer.Argument(
            help="Host ID to operate on.",
            autocompletion=_host_id_completer,
        ),
    ] = "",
    hop: Annotated[
        str, typer.Option("--hop", help="Host ID to use as an SSH hop to reach the target.")
    ] = "",
    term: Annotated[
        str | None,
        typer.Option(
            "--term",
            autocompletion=_term_completer,
            help="Override the terminal protocol for this session.",
        ),
    ] = None,
    transfer: Annotated[
        str | None,
        typer.Option(
            "--transfer",
            autocompletion=_transfer_completer,
            help="Override the file transfer protocol for this session.",
        ),
    ] = None,
    list_hosts: Annotated[
        bool,
        typer.Option("--list-hosts", help="Show all valid host IDs."),
    ] = False,
) -> None:
    """Record the host request; the resolved host is built lazily by the leaf verb.

    The host can no longer be built here: the lab loads lazily in the
    leaf-invoke :func:`~otto.cli.invoke.command_preamble`, which runs *after*
    this group callback. So this callback only stashes the raw inputs on
    ``ctx.meta``; the verb's ``_cmd`` calls :func:`resolve_cli_host` once the
    lab is ready. Output-dir creation and the reservation gate likewise moved to
    the preamble (per-verb output dir keyed off each verb's
    ``__cli_output_dir__`` marker), so a ``--help`` on a verb builds nothing.
    """
    if ctx.resilient_parsing:
        return

    if list_hosts:
        # The same inline load the root flag uses: the lab is otherwise built
        # lazily at the leaf verb, so the eager Typer callback this option
        # carried until 2026-09-03 (the shape from before the lazy load) ran
        # before any context existed and tracebacked on the documented
        # `otto --lab L host --list-hosts`.
        from .invoke import ensure_inline_lab

        ensure_inline_lab(ctx)
        list_hosts_callback(True)
        raise typer.Exit

    if not host_id or ctx.invoked_subcommand is None:
        rprint(ctx.get_help())
        raise typer.Exit

    ctx.meta["_otto_host_request"] = {
        "host_id": host_id,
        "hop": hop,
        "term": term,
        "transfer": transfer,
    }


def _check_named_host_reservations(ctx: typer.Context, named: "list[RemoteHost]") -> None:
    """Refuse an out-of-fleet named host whose slot is not held.

    The rules are :meth:`otto.reservations.ReservationGate.check_hosts`'s.
    Local imports: ``otto host`` is a budgeted import surface.
    """
    gate = ctx.meta.get("otto_reservation")
    if gate is None:
        return
    from ..config import get_lab
    from ..reservations import MissingReservationError

    try:
        gate.check_hosts(get_lab(), named)
    except MissingReservationError as e:
        fail(e)


def resolve_cli_host(ctx: typer.Context) -> "RemoteHost":
    """Build the host the ``otto host`` callback recorded (lab is ready by now).

    Reproduces the construction the callback used to do inline: resolve the
    host by ID, validate/attach a ``--hop``, and apply ``--term`` / ``--transfer``
    override-copies. An already-resolved ``ctx.obj`` is honoured as a fast path.

    This is also where the reservation gate learns which hosts were NAMED
    (``_check_named_host_reservations`` below) — the group callback cannot,
    because it runs before the lab loads and so only stashes the raw ids.
    Memoized by ``ctx.obj`` like the rest of the construction, so the check
    runs at most once per invocation.
    """
    if ctx.obj is not None:
        # Today ONLY test scaffolding pre-installs ctx.obj. Anything set here
        # bypasses hop validation, the --term/--transfer override-copies and
        # the named-host reservation check below, so a future upstream
        # writer (e.g. a group callback building the host early) must do that
        # work itself or leave ctx.obj unset.
        return ctx.obj

    from ..config.fleet import _apply_option_overrides

    request = ctx.meta["_otto_host_request"]
    host: "RemoteHost" = _resolve_host(request["host_id"])

    # Resolved BEFORE the reservation check so the hop is one of the hosts
    # that check covers, and its id is what gets stored — a hop that names no
    # host therefore reports "No host with ID" ahead of any reservation
    # verdict, which is the same order the target already had.
    hop = request.get("hop")
    hop_host: "RemoteHost | None" = _resolve_host(hop) if hop else None

    # Before ANY connection is wired: a host the caller may not have is not a
    # host to start opening a session to — and `rebuild_connections` below
    # builds the jump transport through the hop.
    _check_named_host_reservations(ctx, [h for h in (host, hop_host) if h is not None])

    if hop_host is not None:
        host.hop = hop_host.id
        host.rebuild_connections()

    term = request.get("term")
    if term:
        try:
            host = _apply_option_overrides(host, term=term)
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--term") from None

    transfer = request.get("transfer")
    if transfer:
        try:
            host = _apply_option_overrides(host, transfer=transfer)
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--transfer") from None

    ctx.obj = host
    return host
