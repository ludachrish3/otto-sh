"""The lab-aware monitor layer: select hosts from the active lab and run a live session.

Its own tach module: the monitor engine (``otto.monitor``) reads no ambient
lab state and has tunnel discovery injected; this layer is where the active
lab, the repos, the driving repo's scope and full-lab tunnel discovery are
read, and handed to the engine.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..utils import compile_host_pattern, validate_interval
from .errors import MonitorInputError, NoMonitorableHostsError

if TYPE_CHECKING:
    from ..host.remote_host import RemoteHost

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveReport:
    """What a finished live run did. No verdict field: a live run serves or raises."""

    session_id: str
    hosts: list[str]
    db: Path | None
    start: datetime
    end: datetime


def select_monitor_hosts(pattern: "str | re.Pattern[str] | None") -> "list[RemoteHost]":
    """Select the hosts to monitor from the active lab.

    Args:
        pattern: Full-match regex over host ids; ``None`` or ``""`` selects
            the whole fleet of interest.

    Raises:
        MonitorInputError: *pattern* is not a valid regex (``field="hosts"``).
        EmptySelectionError: *pattern* matched no host.
        NoMonitorableHostsError: the selection holds no host otto can sample.
    """
    from ..config.fleet import all_hosts
    from .factory import monitorable

    if isinstance(pattern, str):
        try:
            pattern = compile_host_pattern(pattern) if pattern else None
        except ValueError as exc:
            raise MonitorInputError(str(exc), field="hosts") from exc
    # Materialized inside the call: all_hosts is a generator and raises its
    # empty-selection refusal at the first next().
    walked = list(all_hosts(pattern=pattern))
    selected = monitorable(walked)
    if not selected:
        raise NoMonitorableHostsError([h.id for h in walked])
    return selected


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
            ``[project]`` scope that admits no host here. The CLI frames it
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


async def run_live(
    *,
    hosts: "str | re.Pattern[str] | None" = None,
    interval: float = 5.0,
    db: Path | None = None,
    label: str | None = None,
    note: str | None = None,
    bind: str = "0.0.0.0",  # noqa: S104 — the dashboard is meant to be reachable on the LAN
    port: int = 0,
) -> LiveReport:
    """Collect from the active lab's hosts and serve the live dashboard until stopped.

    Every refusal comes before any file exists: the interval, the driving
    repo's scope, the selection, then the declared TLS. Only then is the
    ``db`` archive created.

    Raises:
        MonitorInputError: *interval* below the floor, or a bad *hosts* regex.
        ProjectScopeError: the driving repo's scope admits no host here.
        EmptySelectionError: *hosts* matched nothing.
        NoMonitorableHostsError: nothing selected can be sampled.
        MonitorTlsError: the repos' declared TLS cannot be served.
    """
    from ..config import get_repos
    from ..config.fleet import get_lab
    from ..tunnel.records import discover_tunnel_records
    from .server import MonitorServer
    from .session import MonitorSession
    from .tls import resolve_monitor_tls

    try:
        validate_interval(interval)
    except ValueError as exc:
        raise MonitorInputError(str(exc), field="interval") from exc
    _enforce_driving_repo_scope()
    selected = select_monitor_hosts(hosts)
    tls = resolve_monitor_tls(get_repos())
    active_lab = get_lab()
    session = MonitorSession.build(
        selected,
        interval=interval,
        db_path=db,
        label=label,
        note=note,
        declared=active_lab.links,
        # The WHOLE lab, not the selection: tunnels traverse hosts that metric
        # collection was never pointed at.
        tunnel_source=lambda: discover_tunnel_records(active_lab),
        owns_hosts=True,
    )
    server = MonitorServer(
        session.collector,
        bind,
        port,
        mode="live",
        frame=session.frame,
        lab=session.lab,
        tls_cert=tls.tls_cert if tls else None,
        tls_key=tls.tls_key if tls else None,
    )
    async with session:
        task = session.spawn()
        try:
            await server.serve()
        finally:
            logger.info("Server exiting...")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert session.frame.end is not None  # noqa: S101 — stamped by finish(); narrows the type
    return LiveReport(
        session_id=session.frame.id,
        hosts=[h.id for h in selected],
        db=db,
        start=session.frame.start,
        end=session.frame.end,
    )
