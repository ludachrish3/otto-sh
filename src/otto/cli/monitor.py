"""
otto monitor — interactive performance dashboard.

Live mode (collects from lab hosts; explicit opt-in, never the default):
    otto monitor --live
    otto monitor --live --hosts 'router|switch'
    otto monitor --live --hosts router1 --interval 5
    otto monitor --live --db metrics.db --label "regression run" --note "pre-release smoke"

Review mode (serves a previously saved export; no live collection):
    otto monitor metrics.db
    otto monitor metrics.json
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from pydantic import ValidationError

from ..config import all_hosts, get_lab
from ..models import MIN_INTERVAL_SECONDS, MonitorExport
from ..monitor.collector import MetricCollector
from ..monitor.db import MetricDB, UnsupportedDBError
from ..monitor.export import build_db_export, build_session_metric_db
from ..monitor.factory import build_monitor_collector
from ..monitor.session import new_frame, snapshot_lab

if TYPE_CHECKING:
    from ..monitor.server import MonitorServer

logger = logging.getLogger(__name__)

monitor_app = typer.Typer(
    help="Launch an interactive performance dashboard.",
)


@monitor_app.command()
def monitor(
    ctx: typer.Context,
    # ── Live mode ─────────────────────────────────────────────────────────
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Collect from lab hosts (explicit opt-in; never the default).",
        ),
    ] = False,
    hosts: Annotated[
        str | None,
        typer.Option(
            "--hosts",
            metavar="REGEX",
            help="Regex matched against host IDs (via re.search). Default: all hosts.",
        ),
    ] = None,
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            "-i",
            metavar="SECONDS",
            help="Collection interval in seconds.",
            min=MIN_INTERVAL_SECONDS,
        ),
    ] = 5.0,
    db: Annotated[
        Path | None,
        typer.Option(
            help="SQLite file to persist live metric data for later historical viewing.",
        ),
    ] = None,
    label: Annotated[
        str | None,
        typer.Option("--label", help="Human-readable label to store with this live session."),
    ] = None,
    note: Annotated[
        str | None,
        typer.Option("--note", help="Free-form note to store with this live session."),
    ] = None,
    # ── Review mode ───────────────────────────────────────────────────────
    source: Annotated[
        Path | None,
        typer.Argument(
            exists=True,
            help="Review a saved .json or .db monitor export instead of collecting live.",
        ),
    ] = None,
) -> None:
    """Launch an interactive performance monitoring dashboard, or review a saved export.

    Exactly one of ``--live`` or ``<source>`` must be given — never both, and
    never neither (bare ``otto monitor`` prints usage and exits 2). Output-dir
    creation moved to the shared leaf-invoke
    :func:`~otto.cli.invoke.command_preamble` (monitor's spec declares
    ``output_dir=True``), so a ``--help`` invocation can never create a
    spurious dir. Neither the reservation gate nor the lab requirement is
    uniform: monitor's spec declares ``gate=False`` AND ``lab_free=True``, so
    this body gates and lab-loads only the ``--live`` branch (via
    :func:`~otto.cli.invoke.ensure_lab_session`) — reviewing a saved
    ``<source>`` is both gate-exempt and lab-free (it reads a local file and
    never touches live hardware or a lab).
    """
    if ctx.resilient_parsing:
        return

    if live and source is not None:
        typer.echo("--live and a review source are mutually exclusive.", err=True)
        raise typer.Exit(2)

    if not live and source is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(2)

    if source is not None:
        export = _load_review_document(source)

        # monitor's spec is lab_free, so the shared command_preamble
        # early-returns entirely for BOTH branches — including
        # ensure_cli_session (init_cli_logging), not just the lab
        # load. Without it the `'otto'` logger has no handler, so
        # MonitorServer.serve()'s `logger.info(f"Server running at {url}")`
        # silently vanishes into Python's lastResort (WARNING+ only)
        # handler: review mode printed nothing at all, not even the URL a
        # user needs to open. Pull in just the session/logging slice here —
        # NOT ensure_lab_session, which would also load a lab (review reads
        # a local file only) and create a per-invocation output dir. A
        # console-only log trail is Chris's accepted tradeoff for review
        # mode (see item 7 of the sessionized-producer follow-ups doc under
        # ``todo/``). Guarded on `_otto_root_options` the same way the
        # --live branch below is: a direct call to monitor() with a
        # hand-built context (this file's own unit tests) never went
        # through the root callback. Run after the source is validated so a
        # doomed invocation (bad file) doesn't initialise logging for nothing.
        from .invoke import ensure_cli_session

        if ctx.meta.get("_otto_root_options") is not None:
            ensure_cli_session(ctx)

        asyncio.run(_serve_review(export, source.name))
        return

    # ── Live mode ────────────────────────────────────────────────────────
    # monitor's spec is lab_free (review touches no lab at all), so the
    # shared command_preamble skips lab loading entirely for BOTH branches.
    # --live still needs one — pull it in here ourselves, the same loud way
    # `otto reservation check` pulls in ensure_lab_context for its one
    # lab-needing subcommand. Guarded on `_otto_root_options`: a direct call
    # to monitor() with a hand-built context (this file's own unit tests,
    # which mock out get_lab()/all_hosts() instead) never went through the
    # root callback, so there is no root state to resolve a lab from and
    # nothing to enforce.
    from .invoke import ensure_lab_session, present_reservation_gate

    if ctx.meta.get("_otto_root_options") is not None and not ctx.meta.get("_otto_lab_ready"):
        ensure_lab_session(ctx, ctx.meta["_otto_command_spec"])

    present_reservation_gate(ctx)

    from ..host import UnixHost

    pattern = re.compile(hosts) if hosts else None
    # Monitorable hosts: any Unix host (shell metrics), plus any host declaring
    # an `snmp` block (collected over SNMP — this is how embedded targets, which
    # can't share their single shell session, get monitored).
    selected = [
        h
        for h in all_hosts(pattern=pattern)
        if isinstance(h, UnixHost) or getattr(h, "snmp", None) is not None
    ]
    if not selected:
        msg = (
            f'No hosts match regex "{hosts}".' if hosts else "No hosts available in the active lab."
        )
        typer.echo(msg, err=True)
        raise typer.Exit(1)

    from ..monitor.server import MonitorServer

    frame = new_frame(label=label, note=note)
    # The active lab's already-resolved DECLARED links (resolved once, at lab
    # load time, by otto.link.derive.resolve_declared_links — see
    # JsonFileLabRepository.load) live on Lab.links; implicit hop edges are
    # derived fresh by snapshot_lab itself from `selected`.
    lab = snapshot_lab(selected, get_lab().links)

    monitor_db: MetricDB | None = None
    if db is not None:
        # build_monitor_collector(hosts=selected) here is a throwaway collector
        # purely to derive the parser-catalog metadata for
        # build_session_metric_db: the meta depends only on the selected
        # hosts/parsers, never on the DB, but MetricDB's constructor needs
        # meta_json up front — and the collector that will actually own *this*
        # db object can't be built until the db object itself exists.
        # chart_map is deliberately NOT passed here (or anywhere): it only
        # exists once points start arriving, so the collector writes it into
        # the session row itself as new labels appear (MetricDB.write_chart_map,
        # called from MetricCollector._record_point) — that is also what keeps
        # a crashed session's grouping intact.
        #
        # `interval` MUST be passed explicitly: the collector only records its
        # own interval once run() starts, which is after this row is written,
        # so reading it off the model here would persist null forever (nothing
        # repairs it later) and leave the replayed session's derived health
        # unresolvable. We have the number right here — it's the CLI option.
        monitor_db = build_session_metric_db(
            str(db), frame, lab, build_monitor_collector(hosts=selected), interval=interval
        )

    collector = build_monitor_collector(hosts=selected, db=monitor_db)
    asyncio.run(
        _run_monitor(
            collector=collector,
            server=MonitorServer(collector, mode="live", frame=frame, lab=lab),
            interval=timedelta(seconds=interval),
            db=monitor_db,
        )
    )


def _load_review_document(path: Path) -> MonitorExport:
    """Load a saved format:1 export for review mode. Exits 1 on any failure."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            return MonitorExport.model_validate_json(path.read_bytes())
        except ValidationError as err:
            typer.echo(
                f"'{path}' is not a valid format:1 monitor export: {err}",
                err=True,
            )
            raise typer.Exit(1) from err
    if suffix == ".db":
        try:
            return build_db_export(str(path))
        except UnsupportedDBError as err:
            typer.echo(str(err), err=True)
            raise typer.Exit(1) from err
    typer.echo(
        f"Unsupported source '{path}' (suffix '{suffix}'); use a .json or .db monitor export.",
        err=True,
    )
    raise typer.Exit(1)


async def _serve_review(export: MonitorExport, source_name: str) -> None:
    """Serve a previously saved format:1 export (no live collection)."""
    from ..monitor.server import MonitorServer

    server = MonitorServer(
        collector=MetricCollector(targets=[]),
        mode="review",
        document=export,
        source_name=source_name,
    )
    await server.serve()


async def _run_monitor(
    collector: MetricCollector,
    server: "MonitorServer",
    interval: timedelta,
    db: MetricDB | None = None,
    duration: timedelta | None = None,
) -> None:
    """Run collection and the web server concurrently until Ctrl+C.

    On exit (clean or otherwise) the collection task is cancelled first, then
    — while the DB connection is still open — the session's ``end`` timestamp
    is finalized via :meth:`~otto.monitor.db.MetricDB.finalize` (a no-op once
    the connection is closed, so this must run *before* ``collector.close()``
    below), and finally the collector (and its DB) is closed.
    """
    collection_task = asyncio.create_task(collector.run(interval=interval, duration=duration))

    try:
        await server.serve()
    finally:
        logger.info("Server exiting...")
        collection_task.cancel()
        await asyncio.gather(collection_task, return_exceptions=True)
        if db is not None:
            await db.finalize(datetime.now(tz=timezone.utc))
        await collector.close()
