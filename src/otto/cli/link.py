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
from rich.markup import escape

from ..config import get_lab, get_repos
from ..config.completion_cache import collect_link_ids
from ..link import (
    DirectionState,
    DryRunPlan,
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
from .completers import lab_scoped_host_ids, selected_lab_names
from .invoke import fail, print_error

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


def _link_completer(ctx: typer.Context, incomplete: str) -> list[str]:
    """Offer the selected lab's declared links, matching what dispatch loads.

    Dispatch resolves through ``get_lab()``, which honours ``-l``/``--lab``/
    ``OTTO_LAB``, and a lab holds only the links touching its own hosts. So
    the completer scopes the same way — via the cached per-lab host map, the
    same source ``otto host <TAB>`` uses — rather than offering every lab
    file's links and letting ``find_link`` refuse most of them.
    """
    try:
        labs = selected_lab_names(ctx)
        loaded_ids = set(lab_scoped_host_ids(ctx)) if labs else None
        ids = collect_link_ids(get_repos(), loaded_ids=loaded_ids)
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


def _row(text: str) -> None:
    """Print one detail row verbatim: no markup parsing, no width wrapping.

    Shared by ``list``'s rows and the ``--dry-run`` plan rows below, and for
    the same reason: every field on them is user-supplied lab data (a link
    name, a host id, a netdev) or a shell command built from it, and rich
    would read ``eth0[dataplane]`` as a style tag and print ``eth0`` — naming
    an interface that does not exist.
    """
    get_console().print(text, markup=False, soft_wrap=True)


def _print_dry_run_plan(link_id: str, plan: DryRunPlan) -> None:
    """Render one link's ``--dry-run`` preview: what would happen, and what was not checked.

    BOTH SECTIONS, ALWAYS. ``would`` alone reads as a verified promise — every
    command line in it is the one a CLEAN netdev would get, and the refusals
    that could stop the call outright have not run. ``not checked`` alone is a
    dry run with no preview, which is useless rather than safe. The ``would``
    section is legitimately empty for an in-path link, whose placements cannot
    be resolved without the middlebox's live address table; the header still
    prints, so the command never answers with silence.
    """
    get_console().print(
        f"[cyan]dry run[/cyan] {escape(link_id)}: no device was contacted — "
        f"nothing was read and nothing was changed",
        soft_wrap=True,
    )
    for line in plan.would:
        _row(f"  would: {line}")
    for line in plan.unchecked:
        _row(f"  not checked: {line}")


def _print_impair_report(report: ImpairReport) -> None:
    if report.plan is not None:
        # A dry run applied nothing, so there is nothing for the loop below to
        # render — and `applied` being empty is exactly why the preview cannot
        # be left to it.
        _print_dry_run_plan(report.link_id, report.plan)
        return
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
async def impair(  # noqa: PLR0913 — CLI command params
    link: str = typer.Argument(..., help="Link id or name.", autocompletion=_link_completer),
    *,
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
        fail(e, 2)
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
            fail(e, 2)
    lab = get_lab()
    try:
        report = await impair_link(
            lab, link, params, from_host=from_host, expire=expire, selector=selector
        )
    except (ValueError, RuntimeError) as e:
        fail(e)
    _print_impair_report(report)


def _print_repair_report(report: RepairReport) -> None:
    if report.plan is not None:
        # Never the line below under a dry run: `cleared (nothing to clear),
        # timers cancelled 0` is three claims and none of them was measured.
        _print_dry_run_plan(report.link_id, report.plan)
        return
    cleared = ", ".join(f"{p.host_id}/{p.netdev}" for p in report.cleared)
    rprint(
        f"[green]repaired[/green] {report.link_id}: cleared {cleared or '(nothing to clear)'}, "
        f"timers cancelled {report.timers_cancelled}"
    )


@link_app.command()
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
            fail(e, 2)
    lab = get_lab()
    if all_:
        sweep = await repair_all(lab)
        if sweep.dry_run:
            # On `sweep.dry_run`, NOT on `sweep.planned` being non-empty: a lab
            # whose links are all structurally refused (every implicit link is)
            # previews nothing, and keying off the previews would print
            # `repaired 0 link(s)` — true, mute, and byte-identical to a real
            # sweep. `repaired` is empty here by construction.
            for planned in sweep.planned:
                _print_repair_report(planned)
            rprint(
                f"[cyan]dry run[/cyan] — previewed {len(sweep.planned)} link(s); "
                f"nothing was read and nothing was changed"
            )
        else:
            rprint(f"[green]repaired[/green] {len(sweep.repaired)} link(s)")
        if sweep.skipped:
            # Named, and deliberately BEFORE the failures block so it prints
            # even on the success path: a skip used to be a silent `continue`,
            # so a link carrying a foreign qdisc made `repair --all` report
            # "repaired 0 link(s)" and exit 0 with nothing said about it.
            # Exit code unchanged — declining a link otto never impaired is
            # not a failure.
            rprint(f"[yellow]skipped[/yellow] {len(sweep.skipped)} link(s):")
            for skip in sweep.skipped:
                print_error(f"  {skip}")
        if sweep.failures:
            rprint("[red]failures:[/red]")
            for failure in sweep.failures:
                print_error(f"  {failure}")
            raise typer.Exit(1)
        return
    try:
        report = await repair_link(lab, link or "", selector=selector)
    except (ValueError, RuntimeError) as e:
        fail(e)
    _print_repair_report(report)


def _dir_text(state: LinkState, direction: FlowDirection) -> str:
    dstate: DirectionState | None = state.by_direction.get(direction)
    if dstate is None:
        # Four ways to have no shape, and they are not the same news:
        # "not read" nobody was ASKED (--dry-run), "!" this direction's host
        # answered and the read failed (the message is on its own row below),
        # "?" nobody answered, "-" never read.
        #
        # `not_measured` is consulted FIRST because it is the strongest claim
        # and the only one that is link-wide-by-construction; the other three
        # describe an attempt, and under it there was none. It is also spelled
        # in words rather than given a fourth glyph: "-" is already overloaded
        # (this branch's "never read" and, below, a CLEAN placement), and that
        # overload is exactly how a dry run used to report every endpoint-mode
        # link as unimpaired.
        #
        # read_errors is consulted before `unreachable` and per direction —
        # `unreachable` is link-wide, so on a link with one endpoint down and
        # the other's tc broken it would otherwise claim "?" for both cells.
        if state.not_measured:
            return "not read"
        if direction in state.read_errors:
            return "!"
        return "?" if state.unreachable else "-"
    if dstate.foreign:
        return "foreign qdisc — not otto's"
    if dstate.scoped:
        return f"port-scoped ({len(dstate.scoped)})"
    if dstate.whole is not None:
        return dstate.whole.describe()
    return "-"


def _read_error_rows(state: LinkState) -> list[str]:
    """One row per DISTINCT read failure, naming the directions it hit.

    Deduped by message, not printed per direction: the whole-link failure
    path (placement resolution itself failed, so neither direction has a
    shape) records the same message under both keys, and these are full
    sentences naming a host and a command — the `refusal` row above avoids
    printing one twice for exactly this reason.
    """
    by_message: dict[str, list[str]] = {}
    for direction in (FlowDirection.A_TO_B, FlowDirection.B_TO_A):
        message = state.read_errors.get(direction)
        if message is not None:
            by_message.setdefault(message, []).append(direction.value)
    return [f"  read failed ({', '.join(dirs)}): {msg}" for msg, dirs in by_message.items()]


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
        # markup=False, not escape(): NOTHING on these rows is otto markup —
        # every field is a link name, a host id or a netdev, all user-supplied
        # and none validated against `[`. Rich would read `eth0[dataplane]` as
        # a style tag and print `eth0`, naming an interface that does not
        # exist. Same hazard as 1fbef92c, one column over; disabling the
        # parser is total where remembering to escape each field is not.
        # soft_wrap=True: rich's global console otherwise wraps at its
        # detected width (80 cols under CliRunner/no-tty, since COLUMNS isn't
        # set in CI) — long link ids/selector rows would get mangled
        # mid-line without it.
        _row(
            f"{link.id}  {link.a.host}@{link.a.interface or '-'} <-> "
            f"{link.b.host}@{link.b.interface or '-'}  via {via}  "
            f"a->b: {a_text}  b->a: {b_text}"
        )
        if state.refusal:
            # Once, on its own row, rather than in both direction cells: every
            # implicit link lands here, so on a lab that declares no links this
            # was the whole table saying only "n/a" — and the live refusals
            # (mgmt interface, hop transit) are full sentences that would be
            # printed twice on one line.
            _row(f"  not impairable: {state.refusal}")
        for row in _read_error_rows(state):
            # Same treatment as `refusal`, and for the same reason: each is a
            # full sentence naming a host and a command, not a table cell.
            _row(row)
        for row in _selector_rows(state):
            _row(row)
    unreachable_ids = sorted(state.link.id for state in states if state.unreachable)
    if unreachable_ids:
        # Markup ON here — the emphasis is otto's own, and the interpolation
        # is escaped rather than turning the parser off for the whole line.
        get_console().print(
            f"[yellow bold]partial scan[/yellow bold] — could not fully read: "
            f"{escape(', '.join(unreachable_ids))}",
            soft_wrap=True,
        )
    read_failed_ids = sorted(state.link.id for state in states if state.read_failed)
    if read_failed_ids:
        # A SECOND summary line, not a widening of the one above: these hosts
        # answered. Folding them into "could not fully read" is what sent an
        # operator to check the network for a link whose host simply has no
        # working tc.
        get_console().print(
            f"[yellow bold]read failed[/yellow bold] — host reachable, read command failed: "
            f"{escape(', '.join(read_failed_ids))}",
            soft_wrap=True,
        )
    if any(state.not_measured for state in states):
        # A THIRD summary line, and the one that has to say what the cells
        # cannot: the rest of every row above is lab data, which a dry run
        # reads perfectly well, so a reader skimming host/interface/via sees a
        # table that looks fully populated. Says the consequence, not just the
        # cause — the live refusals are the part an operator would otherwise
        # assume `list` had cleared them for.
        get_console().print(
            "[cyan bold]dry run[/cyan bold] — no device was contacted, so no link's "
            "impairment state was read; the management-interface and hop-transit refusals "
            "were not evaluated either",
            soft_wrap=True,
        )
