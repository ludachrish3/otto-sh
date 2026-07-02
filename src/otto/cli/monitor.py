"""
otto monitor — interactive performance dashboard.

Live mode (polls all lab hosts, or a regex-matched subset):
    otto monitor --lab my.lab.toml
    otto monitor --lab my.lab.toml --hosts 'router|switch'
    otto monitor --lab my.lab.toml --hosts router1 --interval 5 --port 8080

Historical mode (views saved data files):
    otto monitor --lab my.lab.toml --file metrics.db
    otto monitor --lab my.lab.toml --file metrics.json
    otto monitor --lab my.lab.toml --file metrics.csv
"""

import asyncio
import re
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

# TODO: Create a SqlPath class that automatically adds the correct slashes and stuff in the front (something like sqlite:////path/to/file)  # noqa: E501 — TODO comment
from ..configmodule import all_hosts
from ..logger import get_logger
from ..monitor.collector import MetricCollector
from ..monitor.factory import build_monitor_collector

if TYPE_CHECKING:
    from ..monitor.server import MonitorServer

logger = get_logger()

monitor_app = typer.Typer(
    help="Launch an interactive performance dashboard.",
)


@monitor_app.command()
def monitor(
    ctx: typer.Context,
    # ── Live mode ─────────────────────────────────────────────────────────
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
            min=1.0,
        ),
    ] = 5.0,
    # ── Historical mode ───────────────────────────────────────────────────
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            "-f",
            metavar="PATH",
            help="Load historical data from a .db or .json file.",
            exists=True,
        ),
    ] = None,
    db: Annotated[
        Path | None,
        typer.Option(
            help="SQLite file to persist live metric data for later historical viewing.",
        ),
    ] = None,
) -> None:
    """Launch an interactive performance monitoring dashboard.

    Output-dir creation moved to the shared leaf-invoke
    :func:`~otto.cli.invoke.command_preamble` (monitor's spec declares
    ``output_dir=True``), so a ``--help`` invocation can never create a
    spurious dir. The reservation gate is NOT uniform: monitor's spec
    declares ``gate=False`` and this body gates only the live branch below —
    historical ``--file`` replay is gate-exempt (see the comment there).
    """
    if ctx.resilient_parsing:
        return

    # ── Build collector ────────────────────────────────────────────────────
    if file is not None:
        # Historical mode — just serve the pre-loaded data; no collection loop.
        # No reservation check: replay reads from a local file and does not
        # touch live hardware.
        asyncio.run(_serve_historical(file))
        return

    from ..reservations import gate

    gate(ctx)

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

    collector = build_monitor_collector(hosts=selected, db_path=db)
    asyncio.run(
        _run_monitor(
            collector=collector,
            server=MonitorServer(collector),
            interval=timedelta(seconds=interval),
        )
    )


async def _load_historical(path: Path) -> MetricCollector:
    suffix = path.suffix.lower()
    if suffix == ".db":
        return await MetricCollector.from_sqlite(str(path))
    if suffix == ".json":
        return MetricCollector.from_json(str(path))
    logger.error(f'Unsupported file format "{suffix}". Use .db or .json.')
    raise typer.Exit(1)


async def _serve_historical(path: Path) -> None:
    """Load historical data and serve the dashboard (no live collection)."""
    from ..monitor.server import MonitorServer

    collector = await _load_historical(path)
    server = MonitorServer(collector)
    await server.serve()


async def _run_monitor(
    collector: MetricCollector,
    server: "MonitorServer",
    interval: timedelta,
    duration: timedelta | None = None,
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
