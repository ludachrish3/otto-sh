"""
otto monitor — interactive performance dashboard.

Live mode (collects from lab hosts; explicit opt-in, never the default):
    otto monitor --live
    otto monitor --live --hosts '(router|switch).*'
    otto monitor --live --hosts router1 --interval 5
    otto monitor --live --db metrics.db --label "regression run" --note "pre-release smoke"

``--hosts`` is a FULL match against each host id, so the alternation above is
wrapped and wildcarded: bare ``router|switch`` selects the hosts named exactly
``router`` or ``switch`` and nothing else.

Review mode (serves a previously saved export; no live collection):
    otto monitor metrics.db
    otto monitor metrics.json
"""

import logging
from pathlib import Path
from typing import Annotated

import typer

from ..models import MIN_INTERVAL_SECONDS

# The monitor library (otto.monitor.*: the collector, aiosqlite, uvicorn) is
# deliberately NOT imported here, matching this module's deferred-import
# convention: it would drag the whole runtime onto every `otto ... --help`
# path for no benefit, since only the command body ever touches it (import
# budget). The body imports what it needs.

logger = logging.getLogger(__name__)


_FLAGS = {"hosts": "--hosts", "interval": "--interval", "source": "SOURCE"}
"""The library's input fields, spelled as this command's flags (the exit-2 refusals)."""


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
            help=(
                "Regex matched against whole host IDs (fullmatch): 'sensor' does not "
                "select 'sensor-1' — write 'sensor.*'. Default: all hosts."
            ),
        ),
    ] = None,
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            "-i",
            metavar="SECONDS",
            help=f"Collection interval in seconds (at least {MIN_INTERVAL_SECONDS}s).",
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

    One command, two modes, and the leaf holds only the rules that are about
    its own shape: exactly one of ``--live`` or ``<source>`` must be given.
    Both together is a usage error; neither prints usage. Both exit 2.
    Everything else is the monitor library's: the interval floor, the host
    selection, the driving repo's scope, the TLS declaration and the review
    source are all checked by ``otto.monitor.live.run_live`` and
    ``otto.monitor.review.serve_review``, which this body calls with the
    parsed flags as they are.

    The preamble is per mode, because monitor's spec is ``lab_free`` (review
    needs no lab) and ``gate=False`` (review touches no hardware), so the
    shared :func:`~otto.cli.invoke.command_preamble` skips both modes. Review
    runs :func:`~otto.cli.invoke.ensure_cli_session` only: the repos'
    ``[logging.levels]`` and the console's host filter, with no lab loaded
    and no output dir. ``--live`` runs
    :func:`~otto.cli.invoke.ensure_lab_session` and then
    :func:`~otto.cli.invoke.present_reservation_gate`.

    The library's refusals are translated at one site. An input refusal
    (``MonitorInputError``, ``ReviewSourceError``, both in
    ``otto.monitor.errors``) goes through
    :func:`~otto.cli.invoke.usage_error_from` and names the flag at fault:
    exit 2. A selection, scope or TLS refusal is framed by
    :func:`~otto.cli.invoke.fail`: one line, exit 1, no traceback.
    """
    if ctx.resilient_parsing:
        return

    if live and source is not None:
        typer.echo("--live and a review source are mutually exclusive.", err=True)
        raise typer.Exit(2)

    if not live and source is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(2)

    from ..bootstrap import ProjectScopeError
    from ..config.scope import EmptySelectionError
    from ..lifecycle import run_command
    from ..monitor.errors import (
        MonitorInputError,
        MonitorTlsError,
        NoMonitorableHostsError,
        ReviewSourceError,
    )
    from .invoke import fail, usage_error_from

    try:
        if source is not None:
            from ..config import get_repos
            from ..monitor.review import serve_review
            from .invoke import ensure_cli_session

            # Session state only (the repo's [logging.levels], the console's
            # HostFilter): review reads a local file, so no lab is loaded.
            ensure_cli_session(ctx)
            run_command(serve_review(source, repos=get_repos()))
            return

        from ..monitor.live import run_live
        from .invoke import command_spec, ensure_lab_session, present_reservation_gate

        # monitor's spec is lab_free (review needs no lab), so the shared
        # preamble skips the lab for both modes; --live loads it here.
        if not ctx.meta.get("_otto_lab_ready"):
            ensure_lab_session(ctx, command_spec(ctx))
        present_reservation_gate(ctx)
        report = run_command(
            run_live(hosts=hosts, interval=interval, db=db, label=label, note=note)
        )
    except (MonitorInputError, ReviewSourceError) as e:
        raise usage_error_from(e, flags=_FLAGS) from e
    except (EmptySelectionError, ProjectScopeError, NoMonitorableHostsError, MonitorTlsError) as e:
        fail(e)
    if report.db is not None:
        logger.info(f"Monitor session {report.session_id} archived to {report.db}")
