"""
otto host — run commands, transfer files, and log in to lab hosts.

Commands are synthesised dynamically from ``@cli_exposed`` methods on the
resolved host's class — see ``otto.cli.expose``.
"""

from typing import Annotated

import typer
from rich import print as rprint

from ..configmodule import all_hosts, get_host
from ..configmodule.configmodule import _apply_option_overrides
from ..host.remote_host import RemoteHost
from ..host.unix_host import UnixHost
from .callbacks import list_hosts_callback
from .expose import HostGroup


def _host_id_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Shell-completion source for the ``host_id`` positional argument.

    Prefers the completion-cache entry populated by the slow path (same file
    that backs suite/instruction completion, wiped by
    ``--clear-autocomplete-cache``). Falls through to a live ``hosts.json``
    scan on cache miss so first-run completion still works.
    """
    from ..configmodule import get_completion_names, get_repos
    from ..configmodule.completion_cache import collect_host_ids

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("hosts"), list):
        ids = cached["hosts"]
    else:
        ids = collect_host_ids(get_repos())

    return sorted(h for h in ids if h.startswith(incomplete))


def _term_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--term``: registered term backends.

    Prefers the completion-cache snapshot (populated by the slow path so custom
    per-repo backends complete without re-running user code — see WS#4 Task 10);
    falls back to the live registry, where otto's built-ins are always present.
    """
    from ..configmodule import get_completion_names

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("term_backends"), list):
        names = cached["term_backends"]
    else:
        from ..host.connections import TERM_BACKENDS

        names = TERM_BACKENDS.names()
    return sorted(n for n in names if n.startswith(incomplete))


def _transfer_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--transfer``: unix-applicable transfer backends.

    Same cache-then-live strategy as :func:`_term_completer`. The unified
    transfer registry spans both host families; ``otto host`` operates on a unix
    host, so only backends whose ``host_families`` include ``'unix'`` are offered.
    Cached entries are ``{"name": str, "host_families": [...]}`` (see Task 10).
    """
    from ..configmodule import get_completion_names

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


def _resolve_host(host_id: str) -> UnixHost:
    try:
        return get_host(host_id)
    except KeyError:
        rprint(f"[red]Error:[/red] No host with ID '{host_id}'.")
        rprint("Available hosts:")
        for h in all_hosts(include_containers=True):
            rprint(f"  - {h.id}")
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
    list_hosts: Annotated[  # noqa: ARG001 — required by Typer eager callback option signature
        bool,
        typer.Option(
            "--list-hosts",
            callback=list_hosts_callback,
            is_eager=True,
            help="Show all valid host IDs.",
        ),
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

    if not host_id or ctx.invoked_subcommand is None:
        rprint(ctx.get_help())
        raise typer.Exit

    ctx.meta["_otto_host_request"] = {
        "host_id": host_id,
        "hop": hop,
        "term": term,
        "transfer": transfer,
    }


def resolve_cli_host(ctx: typer.Context) -> RemoteHost:
    """Build the host the ``otto host`` callback recorded (lab is ready by now).

    Reproduces the construction the callback used to do inline: resolve the
    host by ID, validate/attach a ``--hop``, and apply ``--term`` / ``--transfer``
    override-copies. An already-resolved ``ctx.obj`` (e.g. a test that installs a
    host directly) is honoured as a fast path.
    """
    if ctx.obj is not None:
        return ctx.obj

    request = ctx.meta["_otto_host_request"]
    host: RemoteHost = _resolve_host(request["host_id"])

    hop = request.get("hop")
    if hop:
        _resolve_host(hop)  # Validate the hop host exists
        host.hop = hop
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
