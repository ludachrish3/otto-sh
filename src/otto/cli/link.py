"""``otto link`` — manage host-resident tunnels (spec §7/§9).

Thin consumer of the ``otto.link`` library API. Reservation-group shaped
(Typer group + callback + command leaves). Runs no per-invocation output dir and
keeps internal host I/O quiet (only warnings/errors surface).
"""

from typing import TYPE_CHECKING

import typer
from rich import print as rprint

from ..configmodule import get_lab, get_repos
from ..configmodule.completion_cache import read_dynamic_link_ids, record_dynamic_link_ids
from ..link import add_link, all_links, discover_dynamic_links_status, remove_all_links, remove_link
from ..utils import async_typer_command, complete_comma_list

if TYPE_CHECKING:
    from ..configmodule.repo import Repo

link_app = typer.Typer(
    name="link",
    help="Create, list, and remove host-resident tunnels.",
    no_args_is_help=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


@link_app.callback()
def link_callback(ctx: typer.Context) -> None:
    """Tunnel management. Discovery/teardown touch hosts but create no output dir."""
    if ctx.resilient_parsing:
        return


def _parse_endpoint(token: str) -> tuple[str, str | None]:
    host, sep, iface = token.partition("@")
    if not host:
        raise ValueError(f"empty host in {token!r}")
    return (host, iface if sep else None)


def _parse_hosts(value: str) -> list[tuple[str, str | None]]:
    parts = [p for p in value.split(",") if p]
    if not parts:
        raise ValueError("--hosts must name at least one host")
    return [_parse_endpoint(p) for p in parts]


_IPV4_DOT_COUNT = 3  # "a.b.c.d" has exactly 3 dots


def _l2_reachable(host_id: str, ip_by_host: dict[str, str]) -> list[str]:
    """Simple-L2 heuristic (spec §11.3): hosts sharing the /24 of ``host_id``.

    Refined to true per-interface subnets in a later phase.
    """

    def net24(ip: str) -> str:
        return ip.rsplit(".", 1)[0] if ip.count(".") == _IPV4_DOT_COUNT else ""

    mine = net24(ip_by_host.get(host_id, ""))
    if not mine:
        return []
    return sorted(h for h, ip in ip_by_host.items() if h != host_id and net24(ip) == mine)


def _ip_by_host(repos: list["Repo"]) -> dict[str, str]:
    """Best-effort ``{host_id: ip}`` map read straight from each repo's lab.json.

    Feeds :func:`_l2_reachable`'s completion narrowing. Reuses the same
    per-host construction :func:`~otto.configmodule.completion_cache.collect_host_ids`
    relies on (``create_host_from_dict``), so ids line up with what the base
    completer offers, while also keeping each host's top-level ``ip`` field
    around. Malformed / unvalidatable entries are silently skipped — this
    only ever feeds a narrowing that falls back to the full host list on any
    error, so it must never raise on bad user data.
    """
    from ..configmodule.completion_cache import LAB_FILENAME, _read_lab_hosts
    from ..storage.factory import create_host_from_dict, validate_host_dict

    ip_by_host: dict[str, str] = {}
    for repo in repos:
        for lab_path in repo.labs:
            for host_data in _read_lab_hosts(lab_path / LAB_FILENAME):
                if not isinstance(host_data, dict):
                    continue
                ip = host_data.get("ip")
                if not isinstance(ip, str) or not ip:
                    continue
                try:
                    validate_host_dict(host_data)
                    host = create_host_from_dict(host_data)
                except (ValueError, TypeError):
                    continue
                ip_by_host[host.id] = ip
    return ip_by_host


def _hosts_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001
    from ..configmodule.completion_cache import collect_host_ids

    try:
        ids = collect_host_ids(get_repos())
    except Exception:  # noqa: BLE001 — completion never crashes the shell
        ids = []

    # Once at least one host is already typed (there's a comma), narrow the
    # candidate set to hosts sharing the last-entered host's /24 (simple-L2
    # reachability, spec §11.3). Best-effort: any failure here — bad lab
    # data, an unparsable last token, whatever — falls back to the full,
    # un-narrowed host list rather than ever breaking tab completion. An
    # empty narrowing (last host has no known L2 neighbors) falls back the
    # same way: offering nothing would be worse than offering everything.
    head, sep, _frag = incomplete.rpartition(",")
    if sep:
        try:
            last_host, _iface = _parse_endpoint(head.rsplit(",", 1)[-1])
            narrowed = _l2_reachable(last_host, _ip_by_host(get_repos()))
        except Exception:  # noqa: BLE001 — narrowing is best-effort only; fall back below
            narrowed = []
        if narrowed:
            ids = narrowed

    return complete_comma_list(sorted(ids), incomplete)


def _link_id_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001
    try:
        ids = read_dynamic_link_ids(get_repos()) or []
    except Exception:  # noqa: BLE001
        ids = []
    return sorted(i for i in ids if i.startswith(incomplete))


@link_app.command()
@async_typer_command
async def add(
    hosts: str = typer.Option(
        ...,
        "--hosts",
        help="Ordered host path h1\\[@if],h2\\[@if].",
        autocompletion=_hosts_completer,
    ),
    port: int = typer.Option(..., "--port", help="Service port (both ends)."),
    protocol: str = typer.Option("tcp", "--protocol", help="tcp or udp."),
    dest: str | None = typer.Option(None, "--dest", help="Relay delivery target host\\[@if]."),
) -> None:
    """Create a tunnel. See spec §7."""
    lab = get_lab()
    try:
        dest_spec = _parse_endpoint(dest) if dest else None
        added = await add_link(
            lab, _parse_hosts(hosts), port=port, protocol=protocol, dest=dest_spec
        )
    except (ValueError, RuntimeError) as e:
        # Known, expected failures (unknown host, ambiguous/empty interface,
        # an "already exists" conflict, missing socat/bash, a bad protocol):
        # a normal user outcome, never a traceback.
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    rprint(
        f"[green]added[/green] {added.link.id} "
        f"({added.ingress_host} -> {added.exit_host}, carrier {added.carrier_port})"
    )


@link_app.command(name="list")
@async_typer_command
async def list_links(
    all_: bool = typer.Option(False, "--all", help="Include implicit + declared links."),
) -> None:
    """List tunnels (default: dynamic only). See spec §9.2."""
    lab = get_lab()
    unreachable: list[str] = []
    if all_:
        links = await all_links(lab)
    else:
        links, unreachable = await discover_dynamic_links_status(lab)
        record_dynamic_link_ids(get_repos(), [link.id for link in links])
    for link in links:
        rprint(
            f"{link.id}  {link.a.host}@{link.a.interface or '-'} <-> "
            f"{link.b.host}@{link.b.interface or '-'}  {link.protocol}"
        )
    if unreachable:
        rprint(
            f"[yellow bold]partial scan[/yellow bold] — could not reach: "
            f"{', '.join(sorted(unreachable))}"
        )


@link_app.command()
@async_typer_command
async def remove(
    link_id: str | None = typer.Argument(None, autocompletion=_link_id_completer),
    all_: bool = typer.Option(False, "--all", help="Reap every otto tunnel."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the --all confirmation."),
) -> None:
    """Remove a tunnel by id, or all tunnels. See spec §9.3."""
    lab = get_lab()
    try:
        if all_:
            if not yes and not typer.confirm("Reap ALL otto tunnels?"):
                raise typer.Exit(1)
            report = await remove_all_links(lab)
        elif link_id:
            report = await remove_link(lab, link_id)
        else:
            rprint("[red]give a link id or --all[/red]")
            raise typer.Exit(2)
    except (ValueError, RuntimeError) as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    record_dynamic_link_ids(get_repos(), [])  # invalidate; next scan refreshes
    rprint(f"[green]removed[/green] {report.removed_ids or '(none found)'}")
    if report.unreachable:
        rprint(f"[yellow]could not reach:[/yellow] {report.unreachable}")
        raise typer.Exit(1)
