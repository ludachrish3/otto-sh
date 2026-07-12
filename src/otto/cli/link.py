"""``otto link`` — inspect and impair the lab's static links (spec §9/§10).

Thin consumer of the ``otto.link`` library API. Reservation-group shaped
(Typer group + callback + command leaves), like ``otto tunnel``. Runs no
per-invocation output dir. Every decision (direction mapping, merge,
refusals) lives in the library — this module only parses CLI strings via the
``otto.link`` parsers, calls the library, and renders the result.
"""

import typer
from rich import get_console
from rich import print as rprint

from ..config import get_lab, get_repos
from ..config.completion_cache import collect_link_ids
from ..link import (
    DirectionState,
    FlowDirection,
    ImpairmentParams,
    ImpairReport,
    LinkState,
    RepairReport,
    Selector,
    impair_link,
    parse_percent,
    parse_rate,
    parse_time_ms,
    read_link_states,
    repair_all,
    repair_link,
)
from ..utils import async_typer_command

link_app = typer.Typer(
    name="link",
    help="Inspect and impair the lab's static links.",
    no_args_is_help=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


@link_app.callback()
def link_callback(ctx: typer.Context) -> None:
    """Manage static links. Impair/repair touch hosts but create no output dir."""
    if ctx.resilient_parsing:
        return


def _link_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001
    try:
        ids = collect_link_ids(get_repos())
    except Exception:  # noqa: BLE001 — completion never crashes the shell
        ids = []
    return sorted(i for i in ids if i.startswith(incomplete))


def _parse_params(given: dict[str, str | None]) -> ImpairmentParams:
    """Parse the given ``--<param>`` strings into :class:`ImpairmentParams`.

    Unset (``None``) options are simply omitted, so ``impair_link`` merges
    only what was actually given over whatever is already applied.
    """
    kwargs: dict[str, float | str] = {}
    if given["--delay"] is not None:
        kwargs["delay_ms"] = parse_time_ms(given["--delay"], option="--delay")
    if given["--jitter"] is not None:
        kwargs["jitter_ms"] = parse_time_ms(given["--jitter"], option="--jitter")
    if given["--loss"] is not None:
        kwargs["loss_pct"] = parse_percent(given["--loss"], option="--loss")
    if given["--corrupt"] is not None:
        kwargs["corrupt_pct"] = parse_percent(given["--corrupt"], option="--corrupt")
    if given["--duplicate"] is not None:
        kwargs["duplicate_pct"] = parse_percent(given["--duplicate"], option="--duplicate")
    if given["--reorder"] is not None:
        kwargs["reorder_pct"] = parse_percent(given["--reorder"], option="--reorder")
    if given["--rate"] is not None:
        kwargs["rate"] = parse_rate(given["--rate"])
    return ImpairmentParams(**kwargs)  # ty: ignore[invalid-argument-type]


def _print_impair_report(report: ImpairReport) -> None:
    for applied in report.applied:
        placement = applied.placement
        desc = applied.params.describe() or "cleared"
        if applied.selector is not None:
            desc = f"{applied.selector.describe()} {desc}"
        rprint(
            f"[green]impaired[/green] {report.link_id} {placement.direction.value} "
            f"on {placement.host_id}/{placement.netdev}: {desc}"
        )


@link_app.command()
@async_typer_command
async def impair(  # noqa: PLR0913 — CLI command params
    link: str = typer.Argument(..., help="Link id or name.", autocompletion=_link_completer),
    delay: str | None = typer.Option(
        None, "--delay", help="Delay: bare number = ms, or an explicit us/ms/s suffix."
    ),
    jitter: str | None = typer.Option(
        None, "--jitter", help="Jitter (requires a delay, given now or already applied)."
    ),
    loss: str | None = typer.Option(
        None, "--loss", help="Packet loss: bare number = percent, or a % suffix."
    ),
    rate: str | None = typer.Option(
        None, "--rate", help="Rate limit; an explicit tc unit is required (e.g. 10mbit)."
    ),
    corrupt: str | None = typer.Option(
        None, "--corrupt", help="Corruption: bare number = percent, or a % suffix."
    ),
    duplicate: str | None = typer.Option(
        None, "--duplicate", help="Duplication: bare number = percent, or a % suffix."
    ),
    reorder: str | None = typer.Option(
        None, "--reorder", help="Reorder (requires a delay, given now or already applied)."
    ),
    from_host: str | None = typer.Option(
        None, "--from", help="Narrow to the direction originating at this host (both by default)."
    ),
    expire: int | None = typer.Option(
        None, "--expire", min=1, help="Auto-clear this impairment after N seconds."
    ),
    port: int | None = typer.Option(
        None,
        "--port",
        min=1,
        max=65535,
        help="Scope to one service port (matches source OR dest; see the guide).",
    ),
    proto: str | None = typer.Option(
        None, "--proto", help="With --port: narrow to tcp or udp (default: both)."
    ),
) -> None:
    """Impair a static link (merge-read-modify-replace, verified). See spec §9/§10."""
    given: dict[str, str | None] = {
        "--delay": delay,
        "--jitter": jitter,
        "--loss": loss,
        "--rate": rate,
        "--corrupt": corrupt,
        "--duplicate": duplicate,
        "--reorder": reorder,
    }
    # Usage errors are deliberately kept OUT of the try/except below: typer's
    # vendored click fork makes `typer.Exit` a `RuntimeError` subclass, so
    # raising them inside a try guarded by `except (ValueError, RuntimeError)`
    # would get them re-wrapped as a spurious error message instead of exiting
    # clean.
    try:
        params = _parse_params(given)
    except ValueError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(2) from e
    if all(v is None for v in given.values()):
        rprint("[red]impair needs at least one parameter option (--delay/--loss/--rate/...).[/red]")
        raise typer.Exit(2)
    if proto is not None and port is None:
        rprint("[red]--proto needs --port.[/red]")
        raise typer.Exit(2)
    selector: Selector | None = None
    if port is not None:
        try:
            selector = Selector(port, proto)
        except ValueError as e:
            rprint(f"[red]{e}[/red]")
            raise typer.Exit(2) from e
    lab = get_lab()
    try:
        report = await impair_link(
            lab, link, params, from_host=from_host, expire=expire, selector=selector
        )
    except (ValueError, RuntimeError) as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    _print_impair_report(report)


def _print_repair_report(report: RepairReport) -> None:
    cleared = ", ".join(f"{p.host_id}/{p.netdev}" for p in report.cleared)
    rprint(
        f"[green]repaired[/green] {report.link_id}: cleared {cleared or '(nothing to clear)'}, "
        f"timers cancelled {report.timers_cancelled}"
    )


@link_app.command()
@async_typer_command
async def repair(
    link: str | None = typer.Argument(
        None, help="Link id or name.", autocompletion=_link_completer
    ),
    all_: bool = typer.Option(False, "--all", help="Repair every static link in the lab."),
    port: int | None = typer.Option(
        None,
        "--port",
        min=1,
        max=65535,
        help="Repair only this service port's scoped impairment (single link only).",
    ),
    proto: str | None = typer.Option(
        None, "--proto", help="With --port: narrow to tcp or udp (default: both)."
    ),
) -> None:
    """Clear a link's impairment(s) and cancel its timers, or repair --all. See spec §9/§10."""
    # This usage-error exit is deliberately kept OUT of the try/except below,
    # for the same typer.Exit-is-a-RuntimeError reason as `impair` above.
    if bool(link) == bool(all_):
        rprint("[red]give a link id/name, or --all (not both).[/red]")
        raise typer.Exit(2)
    if proto is not None and port is None:
        rprint("[red]--proto needs --port.[/red]")
        raise typer.Exit(2)
    if all_ and port is not None:
        rprint("[red]--port repairs one selector on one link; it cannot combine with --all.[/red]")
        raise typer.Exit(2)
    selector: Selector | None = None
    if port is not None:
        try:
            selector = Selector(port, proto)
        except ValueError as e:
            rprint(f"[red]{e}[/red]")
            raise typer.Exit(2) from e
    lab = get_lab()
    if all_:
        reports, failures = await repair_all(lab)
        rprint(f"[green]repaired[/green] {len(reports)} link(s)")
        if failures:
            rprint("[red]failures:[/red]")
            for failure in failures:
                rprint(f"  [red]{failure}[/red]")
            raise typer.Exit(1)
        return
    try:
        report = await repair_link(lab, link or "", selector=selector)
    except (ValueError, RuntimeError) as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    _print_repair_report(report)


def _dir_text(state: LinkState, direction: FlowDirection) -> str:
    dstate: DirectionState | None = state.by_direction.get(direction)
    if dstate is None:
        return "?" if state.unreachable else "-"
    if dstate.foreign:
        return "foreign qdisc — not otto's"
    if dstate.scoped:
        return f"port-scoped ({len(dstate.scoped)})"
    if dstate.whole is not None:
        return dstate.whole.describe()
    return "-"


def _selector_rows(state: LinkState) -> list[str]:
    """One indented row per selector, a->b first, sorted by (port, proto)."""
    rows: list[str] = []
    for direction in (FlowDirection.A_TO_B, FlowDirection.B_TO_A):
        dstate = state.by_direction.get(direction)
        if dstate is None or not dstate.scoped:
            continue
        rows.extend(
            f"  {direction.value}  {sel.describe()}  {params.describe()}"
            for sel, params in sorted(
                dstate.scoped.items(), key=lambda kv: (kv[0].port, kv[0].proto or "")
            )
        )
    return rows


@link_app.command(name="list")
@async_typer_command
async def list_links() -> None:
    """List every static link's current impairment state (spec §9)."""
    lab = get_lab()
    states = await read_link_states(lab)
    for state in states:
        link = state.link
        via = link.impair or "-"
        if state.impairable:
            a_text = _dir_text(state, FlowDirection.A_TO_B)
            b_text = _dir_text(state, FlowDirection.B_TO_A)
        else:
            a_text = b_text = "n/a"
        # soft_wrap=True: rich's global console otherwise wraps at its
        # detected width (80 cols under CliRunner/no-tty, since COLUMNS isn't
        # set in CI) — long link ids/selector rows would get mangled
        # mid-line without it.
        get_console().print(
            f"{link.id}  {link.a.host}@{link.a.interface or '-'} <-> "
            f"{link.b.host}@{link.b.interface or '-'}  via {via}  "
            f"a->b: {a_text}  b->a: {b_text}",
            soft_wrap=True,
        )
        for row in _selector_rows(state):
            get_console().print(row, soft_wrap=True)
    unreachable_ids = sorted(state.link.id for state in states if state.unreachable)
    if unreachable_ids:
        get_console().print(
            f"[yellow bold]partial scan[/yellow bold] — could not fully read: "
            f"{', '.join(unreachable_ids)}",
            soft_wrap=True,
        )
