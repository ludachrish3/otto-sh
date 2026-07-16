"""``otto tunnel`` — manage host-resident bidirectional tunnels (spec §6/§9/§10).

Thin consumer of the ``otto.tunnel`` library API. Reservation-group shaped
(Typer group + callback + command leaves). Runs no per-invocation output dir and
keeps internal host I/O quiet (only warnings/errors surface).
"""

from typing import TYPE_CHECKING

import typer
from rich import print as rprint

from ..config import get_lab, get_repos
from ..config.completion_cache import read_tunnel_ids, record_tunnel_ids
from ..tunnel import (
    DEFAULT_CARRIER,
    add_tunnel,
    discover_tunnels,
    remove_all_tunnels,
    remove_tunnel,
)
from ..utils import async_typer_command, complete_separated_list

if TYPE_CHECKING:
    from ..config.repo import Repo
    from ..tunnel import Tunnel

tunnel_app = typer.Typer(
    name="tunnel",
    help="Create, list, and remove host-resident bidirectional tunnels.",
    no_args_is_help=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


@tunnel_app.callback()
def tunnel_callback(ctx: typer.Context) -> None:
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
    per-host construction :func:`~otto.config.completion_cache.collect_host_ids`
    relies on (``create_host_from_dict``), so ids line up with what the base
    completer offers, while also keeping each host's top-level ``ip`` field
    around. Malformed / unvalidatable entries are silently skipped — this
    only ever feeds a narrowing that falls back to the full host list on any
    error, so it must never raise on bad user data.
    """
    from ..config.completion_cache import LAB_FILENAME, _read_lab_hosts
    from ..host.factory import create_host_from_dict, validate_host_dict

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


def _hosts_completer(ctx: typer.Context, incomplete: str) -> list[str]:
    from .completers import lab_scoped_host_ids

    try:
        ids = lab_scoped_host_ids(ctx)
    except Exception:  # noqa: BLE001 — completion never crashes the shell
        ids = []

    # Once at least one host is already typed (there's a comma), narrow the
    # candidate set to hosts sharing the last-entered host's /24 (simple-L2
    # reachability, spec §11.3), intersected with the lab-scoped candidates so
    # a neighbor from another lab is never offered (issue #138). Best-effort:
    # any failure here — bad lab data, an unparsable last token, whatever —
    # falls back to the full, un-narrowed host list rather than ever breaking
    # tab completion. An empty narrowing (last host has no known L2 neighbors
    # in the lab) falls back the same way: offering nothing would be worse
    # than offering everything.
    head, sep, _frag = incomplete.rpartition(",")
    if sep:
        candidates = set(ids)
        try:
            last_host, _iface = _parse_endpoint(head.rsplit(",", 1)[-1])
            narrowed = [
                h for h in _l2_reachable(last_host, _ip_by_host(get_repos())) if h in candidates
            ]
        except Exception:  # noqa: BLE001 — narrowing is best-effort only; fall back below
            narrowed = []
        if narrowed:
            ids = narrowed

    return complete_separated_list(sorted(ids), incomplete)


def _tunnel_id_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001
    try:
        ids = read_tunnel_ids(get_repos()) or []
    except Exception:  # noqa: BLE001
        ids = []
    return sorted(i for i in ids if i.startswith(incomplete))


_AGE_UNITS = ((86400, "d"), (3600, "h"), (60, "m"))


def _fmt_age(seconds: int) -> str:
    for div, unit in _AGE_UNITS:
        if seconds >= div:
            return f"{seconds // div}{unit}"
    return f"{seconds}s"


def _fmt_via(tunnel: "Tunnel") -> str:
    parts = [hop.host for hop in tunnel.path[1:-1]]
    if tunnel.dest:
        parts.append(f"→ {tunnel.dest}")
    return " ".join(parts) or "-"


@tunnel_app.command()
@async_typer_command
async def add(
    hosts: str = typer.Option(
        ...,
        "--hosts",
        help="Ordered host path h1\\[@if],h2\\[@if],...",
        autocompletion=_hosts_completer,
    ),
    port: int = typer.Option(..., "--port", help="Service port (both ends)."),
    protocol: str = typer.Option("tcp", "--protocol", help="tcp or udp."),
    dest: str | None = typer.Option(None, "--dest", help="Far-end delivery target host\\[@if]."),
    carrier: str = typer.Option(
        DEFAULT_CARRIER, "--carrier", help="Tunnel transport carrier (registered name)."
    ),
) -> None:
    """Create a bidirectional tunnel along an explicit host path. See spec §6."""
    lab = get_lab()
    try:
        dest_spec = _parse_endpoint(dest) if dest else None
        added = await add_tunnel(
            lab, _parse_hosts(hosts), port=port, protocol=protocol, dest=dest_spec, carrier=carrier
        )
    except (ValueError, RuntimeError) as e:
        # Known, expected failures (unknown host, ambiguous/empty interface,
        # an "already exists" conflict, missing carrier tools, a bad protocol):
        # a normal user outcome, never a traceback.
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    t = added.tunnel
    rprint(
        f"[green]added[/green] {t.id} "
        f"({t.path[0].host} <-> {t.path[-1].host}, via {_fmt_via(t)}, "
        f"carriers {added.carrier_fwd}/{added.carrier_rev})"
    )


@tunnel_app.command(name="list")
@async_typer_command
async def list_tunnels() -> None:
    """List live tunnels (observed truth; spec §9)."""
    from rich.table import Table

    lab = get_lab()
    discovery = await discover_tunnels(lab)
    record_tunnel_ids(get_repos(), [d.tunnel.id for d in discovery.tunnels])
    if discovery.tunnels:
        # Borderless + single-space gaps: the worst-case row (20-char id,
        # 22-char endpoints, "degraded (3/4)") must survive an 80-column
        # terminal without wrapping or truncating — ids get copy-pasted
        # into `otto tunnel remove`.
        table = Table(
            "ID",
            "ENDPOINTS",
            "VIA",
            "PORT",
            "PROTO",
            "AGE",
            "STATUS",
            box=None,
            pad_edge=False,
            padding=(0, 1, 0, 0),
        )
        for d in discovery.tunnels:
            t = d.tunnel
            a, b = t.path[0], t.path[-1]
            table.add_row(
                t.id,
                f"{a.host}@{a.interface or '-'} <-> {b.host}@{b.interface or '-'}",
                _fmt_via(t),
                str(t.service_port),
                t.protocol,
                _fmt_age(d.age_seconds),
                d.status,
            )
        rprint(table)
    if discovery.unreachable:
        rprint(
            f"[yellow bold]partial scan[/yellow bold] — could not reach: "
            f"{', '.join(sorted(discovery.unreachable))}"
        )


@tunnel_app.command()
@async_typer_command
async def remove(
    tunnel_id: str | None = typer.Argument(None, autocompletion=_tunnel_id_completer),
    all_: bool = typer.Option(False, "--all", help="Reap every otto tunnel."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the --all confirmation."),
) -> None:
    """Remove a tunnel by id (all hops, both directions), or all tunnels. Spec §10."""
    # These two usage-error exits are deliberately kept OUT of the try/except
    # below: typer's vendored click fork makes ``typer.Exit`` a ``RuntimeError``
    # subclass, so raising them inside a ``try`` guarded by
    # ``except (ValueError, RuntimeError)`` would get them re-wrapped as a
    # spurious "[red]1[/red]" / "[red]2[/red]" message instead of exiting clean.
    if all_:
        if not yes and not typer.confirm("Reap ALL otto tunnels?"):
            raise typer.Exit(1)
    elif not tunnel_id:
        rprint("[red]give a tunnel id or --all[/red]")
        raise typer.Exit(2)

    lab = get_lab()
    try:
        if all_:
            report = await remove_all_tunnels(lab)
        else:
            report = await remove_tunnel(lab, tunnel_id or "")
    except (ValueError, RuntimeError) as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    record_tunnel_ids(get_repos(), [])  # invalidate; next scan refreshes
    removed = ", ".join(report.removed_ids) if report.removed_ids else "(none found)"
    rprint(f"[green]removed[/green] {removed}")
    if report.survivors:
        pretty = ", ".join(f"{h}/{pid}" for h, pid in report.survivors)
        rprint(f"[red]still running after kill:[/red] {pretty}")
        raise typer.Exit(1)
    if report.unreachable:
        rprint(f"[yellow]could not reach:[/yellow] {', '.join(report.unreachable)}")
        raise typer.Exit(1)
