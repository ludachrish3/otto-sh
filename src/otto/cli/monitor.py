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

import asyncio
import logging
import re
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from pydantic import ValidationError

from ..config import all_hosts, get_lab
from ..models import MIN_INTERVAL_SECONDS, MonitorExport

# The monitor runtime (collector/db/export/factory/session) is deliberately NOT
# imported here, matching this module's deferred-import convention and the
# already-deferred MonitorServer below: it drags otto.monitor.* plus aiosqlite
# onto every `otto ... --help` path for no benefit, since only command bodies
# ever touch it (import budget). Each use site imports what it needs.
if TYPE_CHECKING:
    from ..config import MonitorSettings
    from ..host.remote_host import RemoteHost
    from ..monitor.collector import MetricCollector
    from ..monitor.db import MetricDB
    from ..monitor.server import MonitorServer

logger = logging.getLogger(__name__)


_EMPTY_LAB = "No hosts available in the active lab."
"""Nothing was selected AT ALL — so this one must not mention the pattern.

Reachable with a ``--hosts`` regex as well as without: a pattern over an EMPTY
base set yields an empty walk rather than raising (the pattern is not what went
wrong when the lab holds nothing to select), so blaming the regex here would
misdirect exactly the reader who has the least to go on."""


_MAX_NAMED_HOSTS = 5
"""How many ids the no-monitorable-hosts message spells out before summarizing.

The ids are what make this message actionable — the fix is per-host lab config,
so "which host" IS the question — but this branch fires only when EVERY selected
host is unmonitorable, which on a large fleet is a whole-lab misconfiguration
and a wall of ids nobody reads."""


def _no_monitorable_hosts_message(walked: "list[RemoteHost]") -> str:
    """Explain an empty collection set that a NON-empty selection produced.

    One of ``otto monitor``'s four empty outcomes, and the only one the
    user's regex is innocent of. Two others — a pattern that fullmatched
    nothing, and one whose every match a membership flag removed — are raised as
    :class:`~otto.config.scope.EmptySelectionError` before this function is ever
    reached, which is exactly why this text must not offer to widen a pattern:
    getting here proves the selection worked. The fourth, an empty lab where
    there was nothing to match against, is announced separately (``_EMPTY_LAB``)
    without mentioning the pattern at all.

    What failed instead is collection. otto reads metrics over a shell (any
    :class:`~otto.host.unix_host.UnixHost`) or over SNMP (any host declaring an
    ``snmp`` block — the route embedded targets take, since an RTOS console
    cannot share its single session with a metrics poller). A host that offers
    neither cannot be sampled at all, so the message names both routes and the
    hosts that took neither.

    Args:
        walked: The hosts the selection actually yielded — non-empty by
            construction; the empty case is :data:`_EMPTY_LAB`.

    Returns:
        The one-paragraph stderr message, ids included.
    """
    ids = sorted(h.id for h in walked)
    shown = ", ".join(ids[:_MAX_NAMED_HOSTS])
    if len(ids) > _MAX_NAMED_HOSTS:
        shown += f" (+{len(ids) - _MAX_NAMED_HOSTS} more)"
    return (
        f"{len(ids)} host(s) selected, but none of them can be monitored: {shown}. "
        "otto samples metrics over a shell (Unix hosts) or over SNMP (any host "
        "declaring an `snmp` block in its lab entry), and these declare neither. "
        "The selection is not the problem — widening it will not help; give the "
        "host(s) you want monitored an `snmp` block, or point --hosts at a Unix host."
    )


def _enforce_driving_repo_scope() -> None:
    """Raise when the DRIVING repo's own fleet declaration cannot work (D3, spec §5).

    Spec §5 names the monitor fleet build as one of D3's project-layer entries,
    alongside the default instructions and a suite's ``ensure`` marker steps,
    and this is that entry. A single-repo world fails loud without it — the union comes
    out empty and ``require_nonempty_fleet`` refuses at the walk — but a lab
    where a DEPENDENCY admits hosts has a healthy union, so the driving
    project's own "this lab is not my world" verdict would go unread and the
    dashboard would quietly monitor the dependency's machines.

    THE DRIVING REPO IS ``bootstrap().repos[0]`` -- the first ``OTTO_SUT_DIRS``
    entry -- and NOT the head of ``get_ordered_repos()``, which is a
    topological reorder whose first element is a dependency. Gating on that one
    would let a dependency's declaration veto this project's run, which is
    exactly the asymmetry D3 exists to prevent (see
    :func:`otto.project.orchestrator._enforce_current_scope`, the same reading).

    THE GUARD SITS ON THE LOOKUPS, NEVER AROUND THE REFUSAL. ``otto monitor``
    runs in worlds with no repos and no bootstrap at all -- a library caller's
    lab, a checkout with no ``OTTO_SUT_DIRS`` -- and monitoring one of those is
    not a project activity to refuse. But a ``try`` wide enough to cover
    :func:`~otto.config.scope.require_current_scope` would swallow the very
    error this exists to raise.

    Raises:
        otto.bootstrap.ProjectScopeError: The driving repo declared a
            ``[project]`` scope that admits no host here. The caller frames it
            like the leaf's other refusals -- one line, no traceback.
    """
    from ..config import get_repos
    from ..config.scope import require_current_scope
    from ..context import get_context

    try:
        repos = get_repos()
        scopes = get_context().scopes
    except Exception as exc:  # noqa: BLE001 — no repos/context to read ⇒ no verdict to enforce
        logger.debug(f"monitor: fleet scoping unavailable ({exc!r}); not enforcing D3")
        return
    if repos:
        require_current_scope(scopes, repos[0].name)


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
        # load. Without it the `'otto'` logger has no handler, so every
        # otto.* record during review (warnings, errors, serve()'s keyless
        # "Monitor dashboard started" line, "Server exiting") silently
        # vanishes into Python's lastResort (WARNING+ only) handler. The
        # keyed dashboard URL itself is printed straight to the terminal by
        # MonitorServer.serve() via CONSOLE — never the logger, so the access
        # key stays out of the log files — and thus survives regardless; this
        # call is what gives the rest of review mode's log trail somewhere to
        # go. Pull in just the session/logging slice here —
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
            tls = _resolve_monitor_tls()
        else:
            tls = None

        archive_path = source if source.suffix.lower() == ".db" else None

        from ..lifecycle import run_command

        run_command(_serve_review(export, source.name, tls, archive_path))
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
    from .invoke import ensure_lab_session, fail, present_reservation_gate

    if ctx.meta.get("_otto_root_options") is not None and not ctx.meta.get("_otto_lab_ready"):
        ensure_lab_session(ctx, ctx.meta["_otto_command_spec"])

    present_reservation_gate(ctx)

    from ..bootstrap import ProjectScopeError
    from ..config.scope import EmptySelectionError
    from ..host import UnixHost

    # D3 at the fleet build (spec §5), BEFORE the walk: a project whose own
    # declaration admits no host here must not dashboard the lab through a
    # dependency's universe. Framed like the selection refusal below it — one
    # line, an exit code, no traceback — because both are user configuration
    # mistakes rather than otto bugs.
    try:
        _enforce_driving_repo_scope()
    except ProjectScopeError as e:
        fail(e)

    pattern = re.compile(hosts) if hosts else None
    # Monitorable hosts: any Unix host (shell metrics), plus any host declaring
    # an `snmp` block (collected over SNMP — this is how embedded targets, which
    # can't share their single shell session, get monitored).
    #
    # The try wraps the `list(...)`, not just the `all_hosts(...)` call:
    # `all_hosts` is a generator, so its empty-selection refusal is raised at the
    # first `next()` — inside that list. Guarding only the call would leave the
    # error to typer, which renders a full traceback with locals for what is a
    # plain "your --hosts regex selected nothing" message.
    try:
        walked = list(all_hosts(pattern=pattern))
    except EmptySelectionError as e:
        fail(e)
    selected = [
        h for h in walked if isinstance(h, UnixHost) or getattr(h, "snmp", None) is not None
    ]
    if not selected:
        # Two DIFFERENT emptinesses, and the old single message described
        # neither once the D6 guard landed upstream. Reaching this branch with a
        # pattern means the pattern MATCHED — a pattern that matched nothing, or
        # only flag-excluded hosts, raised EmptySelectionError above and never
        # got here. So "No hosts match regex" was false every time it fired with
        # a pattern, and it sent the reader to widen a regex that was already
        # right. `walked` is materialized above precisely to tell the two apart.
        typer.echo(
            _no_monitorable_hosts_message(walked) if walked else _EMPTY_LAB,
            err=True,
        )
        raise typer.Exit(1)

    from ..monitor.export import build_session_metric_db
    from ..monitor.factory import build_monitor_collector
    from ..monitor.server import MonitorServer
    from ..monitor.session import new_frame, snapshot_lab

    frame = new_frame(label=label, note=note)
    # The active lab's already-resolved DECLARED links (resolved once, at lab
    # load time, by otto.link.derive.resolve_declared_links — see
    # JsonFileLabRepository.load) live on Lab.links; implicit hop edges are
    # derived fresh by snapshot_lab itself from `selected`.
    lab = snapshot_lab(selected, get_lab().links)

    # Resolved before build_session_metric_db() below: a doomed TLS config
    # (disagreeing repos, missing/unparseable cert or key) must exit before a
    # --db archive file is created on disk, not after — otherwise a run that
    # never actually served anything still leaves a half-created database
    # behind.
    tls = _resolve_monitor_tls() if ctx.meta.get("_otto_root_options") is not None else None

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

    # Tunnel discovery scans the WHOLE lab, not `selected`: stats gathering
    # may target a few hosts while tunnels traverse hosts outside that set
    # (spec 2026-07-16, decision 3). Deferred import, matching this module's
    # convention — and keeping otto.tunnel out of CLI startup (import budget).
    from ..tunnel.records import discover_tunnel_records

    active_lab = get_lab()
    collector = build_monitor_collector(
        hosts=selected,
        db=monitor_db,
        tunnel_source=lambda: discover_tunnel_records(active_lab),
    )

    from ..lifecycle import run_command

    run_command(
        _run_monitor(
            collector=collector,
            server=MonitorServer(
                collector,
                mode="live",
                frame=frame,
                lab=lab,
                tls_cert=tls.tls_cert if tls else None,
                tls_key=tls.tls_key if tls else None,
            ),
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
        from ..monitor.db import UnsupportedDBError
        from ..monitor.export import build_db_export

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


def _resolve_monitor_tls() -> "MonitorSettings | None":
    """Resolve the [monitor] TLS declaration across all configured repos.

    Fail-loud rules from the spec: more than one repo declaring *different*
    values is a configuration error (name the repos, exit 1); a declared
    cert/key path whose file is missing is an error (never a silent fall-back
    to HTTP — a quiet security downgrade); no declaration at all means plain
    HTTP, which is simply "not configured".
    """
    import otto.config

    declaring = [
        (r.name, r.monitor_settings)
        for r in otto.config.get_repos()
        if r.monitor_settings.tls_cert is not None
    ]
    if not declaring:
        return None
    if len({(ms.tls_cert, ms.tls_key) for _, ms in declaring}) > 1:
        names = ", ".join(sorted(name for name, _ in declaring))
        typer.echo(
            f"[monitor] TLS settings disagree across repos ({names}); "
            "make them identical or declare TLS in only one settings.toml.",
            err=True,
        )
        raise typer.Exit(1)
    settings = declaring[0][1]
    for field_name, path in (("tls_cert", settings.tls_cert), ("tls_key", settings.tls_key)):
        if path is not None and not path.is_file():
            typer.echo(
                f"[monitor] {field_name} {path} does not exist or is not a file — "
                "fix .otto/settings.toml or create the certificate "
                "(see the monitor guide's 'Securing the dashboard' section).",
                err=True,
            )
            raise typer.Exit(1)

    # Both files exist, but existence doesn't mean valid: a corrupted/
    # truncated/wrong-format PEM passes is_file() cleanly and then kills
    # uvicorn's serve task deep inside MonitorServer.serve() (ssl.SSLError
    # out of Config.load()) — which used to hang the startup poll loop
    # forever rather than surface anything (see the task-death check in
    # server.py's _uvicorn_signalled_started). Load the pair here, at the
    # point we can still
    # typer.echo + exit 1 cleanly, instead of ever falling back to HTTP.
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
            str(settings.tls_cert),
            str(settings.tls_key) if settings.tls_key is not None else None,
        )
    except (ssl.SSLError, OSError) as err:
        key_desc = (
            str(settings.tls_key) if settings.tls_key is not None else "(none; bundled in tls_cert)"
        )
        typer.echo(
            f"[monitor] tls_cert {settings.tls_cert} / tls_key {key_desc} "
            f"could not be loaded as a TLS certificate/key pair: {err}",
            err=True,
        )
        raise typer.Exit(1) from err
    return settings


async def _serve_review(
    export: MonitorExport,
    source_name: str,
    tls: "MonitorSettings | None" = None,
    archive_path: Path | None = None,
) -> None:
    """Serve a previously saved format:1 export (no live collection).

    ``archive_path`` is the ``.db`` file event mutations persist to
    when the review source is a SQLite archive; ``None`` for a ``.json``
    source, which stays permanently read-only.
    """
    from ..monitor.collector import MetricCollector
    from ..monitor.server import MonitorServer

    server = MonitorServer(
        collector=MetricCollector(targets=[]),
        mode="review",
        document=export,
        source_name=source_name,
        tls_cert=tls.tls_cert if tls else None,
        tls_key=tls.tls_key if tls else None,
        archive_path=archive_path,
    )
    await server.serve()


async def _run_monitor(
    collector: "MetricCollector",
    server: "MonitorServer",
    interval: timedelta,
    db: "MetricDB | None" = None,
    duration: timedelta | None = None,
) -> None:
    """Run collection and the web server concurrently until Ctrl+C.

    On exit (clean or otherwise) the collection task is cancelled first, then
    — while the DB connection is still open — the session's ``end`` timestamp
    is finalized via :meth:`~otto.monitor.db.MetricDB.finalize` (a no-op once
    the connection is closed, so this must run *before* ``collector.close()``
    below), and finally the collector (and its DB) is closed.
    """
    # spawn_collection owns the open-before-spawn ordering (issues #136 etc.:
    # an in-task open races Ctrl+C into a partial DB; a locked/unsupported
    # --db must fail loud here, not inside the task where the
    # gather(return_exceptions=True) below would swallow it).
    collection_task = await collector.spawn_collection(interval, duration=duration)

    # Imported before the try: an ImportError inside the finally would mask
    # serve()'s own exception.
    from ..host.connections import teardown_step

    try:
        await server.serve()
    finally:
        logger.info("Server exiting...")
        collection_task.cancel()
        await asyncio.gather(collection_task, return_exceptions=True)
        if db is not None:
            await db.finalize(datetime.now(tz=timezone.utc))
        with teardown_step("monitor", "collector close"):
            await collector.close()
