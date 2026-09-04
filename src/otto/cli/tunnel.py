"""``otto tunnel`` — manage host-resident bidirectional tunnels (spec §6/§9/§10).

Thin consumer of the ``otto.tunnel`` library API. Reservation-group shaped
(Typer group + callback + command leaves). Runs no per-invocation output dir and
keeps internal host I/O quiet (only warnings/errors surface).
"""

from typing import TYPE_CHECKING

import typer
from rich import get_console
from rich import print as rprint
from rich.markup import escape

from ..config import get_lab, get_repos
from ..config.completion_cache import read_tunnel_ids, record_tunnel_ids
from ..tunnel import (
    DEFAULT_CARRIER,
    add_tunnel,
    discover_tunnels,
    remove_all_tunnels,
    remove_tunnel,
)
from ..utils import complete_separated_list
from .invoke import fail, print_error

if TYPE_CHECKING:
    from ..config.repo import Repo
    from ..tunnel import DryRunPlan, Tunnel

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
    """Best-effort ``{host_id: ip}`` map from each repo's configured host source.

    Feeds :func:`_l2_reachable`'s completion narrowing. Goes through the same
    enumeration :func:`~otto.config.completion_cache.collect_host_ids` uses, so
    ids line up with what the base completer offers — and, unlike the old
    read-lab.json-directly path, a project with a custom host source narrows
    too. This runs on every TAB after a comma, so it deliberately avoids
    building hosts. Malformed entries are silently skipped: the caller falls
    back to the unnarrowed host list on any error, so this must never raise.
    """
    from ..config.completion_cache import repo_host_summaries, resolve_process_inventory

    ip_by_host: dict[str, str] = {}
    resolution = resolve_process_inventory(repos)
    for repo in repos:
        for summary in repo_host_summaries(repo, resolution):
            if summary.ip:
                ip_by_host[summary.id] = summary.ip
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


def _row(text: str) -> None:
    """Print one detail row verbatim: no markup parsing, no width wrapping.

    Every field on a ``--dry-run`` row is user-supplied lab data (a host id, an
    interface) or a shell command built from it, and rich would read the
    ``socat`` address ``TCP4-LISTEN:49152,fork,reuseaddr`` — or an interface
    named ``eth0[dataplane]`` — as markup and print something otto never sends.
    Soft-wrapped for the same reason: a broken argv is worse than a long line.
    """
    get_console().print(text, markup=False, soft_wrap=True)


def _print_dry_run_plan(header: str, plan: "DryRunPlan") -> None:
    """Render one command's ``--dry-run`` preview: what would happen, and what was not checked.

    BOTH SECTIONS, ALWAYS. ``would`` alone reads as a verified promise — the
    carrier ports in every argv were picked without probing a host, and the
    refusals that could stop the call outright have not run. ``not checked``
    alone is a dry run with no preview, which is useless rather than safe. The
    ``would`` section is legitimately short for a chain with a container
    endpoint, whose argv cannot be built without ``docker inspect``; the header
    still prints, so the command never answers with silence.
    """
    get_console().print(
        f"[cyan]dry run[/cyan] {escape(header)}: no device was contacted — "
        f"nothing was read and nothing was changed",
        soft_wrap=True,
    )
    for line in plan.would:
        _row(f"  would: {line}")
    for line in plan.unchecked:
        _row(f"  not checked: {line}")


@tunnel_app.command()
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
        fail(e)
    t = added.tunnel
    if added.plan is not None:
        # A dry run launched nothing, so the `added …` line below would be
        # three claims — that it exists, that it came up, and which two ports
        # it holds — none of them measured. `carrier_fwd`/`carrier_rev` are
        # None here by construction; the provisional pair is in the plan,
        # marked as provisional.
        _print_dry_run_plan(f"{t.path[0].host} <-> {t.path[-1].host}", added.plan)
        return
    rprint(
        f"[green]added[/green] {t.id} "
        f"({t.path[0].host} <-> {t.path[-1].host}, via {_fmt_via(t)}, "
        f"carriers {added.carrier_fwd}/{added.carrier_rev})"
    )


@tunnel_app.command(name="list")
async def list_tunnels() -> None:
    """List live tunnels (observed truth; spec §9)."""
    from rich.table import Table

    lab = get_lab()
    discovery = await discover_tunnels(lab)
    if discovery.not_measured:
        # BEFORE `record_tunnel_ids`, which is the part that would otherwise
        # outlive the command: caching `[]` from a scan that never ran empties
        # the `remove <TAB>` completion set for the next REAL invocation, and
        # nothing repairs it until someone runs `list` again. A dry run must
        # not leave that behind — when it declined to look. On a lab with no
        # `has_bash` host there was nothing to look AT, `not_measured` is
        # False, and the cache write below happens and is correct: it stores
        # the same `[]` a real pass stores, from the same lab data.
        #
        # Says the consequence, not just the cause. The rest of this command's
        # output is a table of observed processes, so a dry run has literally
        # no row to hang a "not read" cell on — the whole answer has to be this
        # line, or the command is silently indistinguishable from a lab with no
        # tunnels running. It also does NOT print `partial scan — could not
        # reach: …`: nobody was unreachable, nobody was asked.
        get_console().print(
            "[cyan bold]dry run[/cyan bold] — no device was contacted, so no host was "
            "scanned; otto cannot say which tunnels are live, which are degraded, or "
            "which hosts are unreachable",
            soft_wrap=True,
        )
        return
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
        fail(e)
    if report.plan is not None:
        # Never the line below under a dry run: `removed (none found)` is a
        # claim about live processes on hosts nobody scanned, and it exits 0
        # byte-identically to a real reap of a clean lab. The cache is left
        # alone for the same reason `list` leaves it alone — invalidating it
        # here would make a preview change state a real run changes.
        _print_dry_run_plan("--all" if all_ else (tunnel_id or ""), report.plan)
        return
    record_tunnel_ids(get_repos(), [])  # invalidate; next scan refreshes
    removed = ", ".join(report.removed_ids) if report.removed_ids else "(none found)"
    rprint(f"[green]removed[/green] {removed}")
    if report.survivors:
        pretty = ", ".join(f"{h}/{pid}" for h, pid in report.survivors)
        print_error(f"still running after kill: {pretty}")
        raise typer.Exit(1)
    if report.unreachable:
        rprint(f"[yellow]could not reach:[/yellow] {', '.join(report.unreachable)}")
        raise typer.Exit(1)
