"""
otto monitor — interactive performance dashboard.

Live mode (polls all lab hosts, or a comma-separated subset):
    otto monitor --lab my.lab.toml
    otto monitor --lab my.lab.toml --hosts router1,switch2
    otto monitor --lab my.lab.toml --hosts router1 --interval 5 --port 8080

Historical mode (views saved data files):
    otto monitor --lab my.lab.toml --file metrics.db
    otto monitor --lab my.lab.toml --file metrics.json
    otto monitor --lab my.lab.toml --file metrics.csv
"""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Optional

import typer

# TODO: Create a SqlPath class that automatically adds the correct slashes and stuff in the front (something like sqlite:////path/to/file)
from ..configmodule import (
    all_hosts,
    get_host,
)
from ..host.remoteHost import RemoteHost
from ..logger import getOttoLogger
from ..monitor.collector import MetricCollector, MonitorTarget
from ..monitor.parsers import get_host_parsers
from ..monitor.server import MonitorServer
from ..utils import (
    splitOnCommas,
)

logger = getOttoLogger()

monitor_app = typer.Typer(
    help='Launch an interactive performance dashboard.',
)

@monitor_app.command(
)
def monitor(
    ctx: typer.Context,

    # ── Live mode ─────────────────────────────────────────────────────────
    hosts: Annotated[Optional[list[str]], typer.Argument(
        metavar='COMMA SEPARATED LIST',
        callback=splitOnCommas,
        help='List of host IDs to monitor (live mode). All hosts by default.',
    )] = None,

    interval: Annotated[float, typer.Option(
        '--interval', '-i', metavar='SECONDS',
        help='Collection interval in seconds.',
        min=1.0,
    )] = 5.0,

    # ── Historical mode ───────────────────────────────────────────────────
    file: Annotated[Optional[Path], typer.Option(
        '--file', '-f', metavar='PATH',
        help='Load historical data from a .db or .json file.',
        exists=True,
    )] = None,

    db: Annotated[Optional[Path], typer.Option(
        help='SQLite file to persist live metric data for later historical viewing.',
    )] = None,
):
    """Launch an interactive performance monitoring dashboard."""

    if ctx.resilient_parsing:
        return

    logger.create_output_dir("monitor")

    # ── Build collector ────────────────────────────────────────────────────
    if file is not None:
        # Historical mode — just serve the pre-loaded data; no collection loop.
        # No reservation check: replay reads from a local file and does not
        # touch live hardware.
        asyncio.run(_serve_historical(file))
    elif hosts:
        from ..configmodule import tryGetConfigModule
        from ..reservations import gate
        gate(tryGetConfigModule())
        collector = _build_collector(
            hosts=[get_host(host_id) for host_id in hosts],
            db_path=db,
        )
        asyncio.run(
            _run_monitor(
                collector=collector,
                server=MonitorServer(collector),
                interval=timedelta(seconds=interval),
            )
        )
    else:
        from ..configmodule import tryGetConfigModule
        from ..reservations import gate
        gate(tryGetConfigModule())

        collector = _build_collector(
            hosts=list(all_hosts()),
            db_path=db,
        )
        asyncio.run(
            _run_monitor(
                collector=collector,
                server=MonitorServer(collector),
                interval=timedelta(seconds=interval),
            )
        )


async def _load_historical(path: Path) -> MetricCollector:
    suffix = path.suffix.lower()
    if suffix == '.db':
        return await MetricCollector.from_sqlite(str(path))
    elif suffix == '.json':
        return MetricCollector.from_json(str(path))
    else:
        logger.error(f'Unsupported file format "{suffix}". Use .db or .json.')
        raise typer.Exit(1)


async def _serve_historical(path: Path) -> None:
    """Load historical data and serve the dashboard (no live collection)."""
    collector = await _load_historical(path)
    server = MonitorServer(collector)
    await server.serve()


def _build_collector(
    hosts: list[RemoteHost],
    db_path: Optional[Path] = None,
) -> MetricCollector:

    # Turn off logging on all hosts. Collecting statuses is very chatty, and just
    # slows everything down if it needs to be logged
    for host in hosts:
        host.log = False

    # Per-host registry lookup; falls back to DEFAULT_PARSERS for unregistered hosts
    targets = [MonitorTarget(host=h, parsers=get_host_parsers(h.id)) for h in hosts]

    return MetricCollector(
        targets=targets,
        db_path=str(db_path) if db_path else None,
    )


async def _run_monitor(
    collector: MetricCollector,
    server: MonitorServer,
    interval: timedelta,
    duration: Optional[timedelta] = None,
) -> None:
    """Run collection and the web server concurrently until Ctrl+C."""
    collection_task = asyncio.create_task(collector.run(interval=interval, duration=duration))

    try:
        await server.serve()
    finally:
        logger.info("Server exiting...")
        collection_task.cancel()
        await asyncio.gather(collection_task, return_exceptions=True)
        await collector.close()
