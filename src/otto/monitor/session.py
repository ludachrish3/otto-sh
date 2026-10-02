"""Session framing for live monitor runs (spec 2026-07-12).

The collector stays session-blind: one process run == one live session, and
:class:`MonitorSession` stamps the frame at its edges (the start in
:meth:`~MonitorSession.build`, the end in :meth:`~MonitorSession.finish`).
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from typing_extensions import Self

from ..host.connections import teardown_step
from ..host.remote_host import RemoteHost
from ..link.derive import implicit_links
from ..link.model import Link, Provenance
from ..models import HostSnapshot, LabSnapshot, LinkEndpointSnapshot, LinkSnapshot

if TYPE_CHECKING:
    from ..models.monitor import MonitorExport, TunnelRecord
    from .collector import MetricCollector, MonitorTarget
    from .db import MetricDB
    from .parsers import MetricParser


@dataclass
class SessionFrame:
    """Identity + lifetime of one live monitoring session.

    ``end is None`` means still-open — a crash never rewrites history, and
    readers fall back to the last sample's timestamp (producer's job).
    """

    id: str
    label: str | None
    note: str | None
    start: datetime
    end: datetime | None = field(default=None)


def new_frame(
    label: str | None,
    note: str | None,
    *,
    now: datetime | None = None,
) -> SessionFrame:
    """Create a frame stamped at *now* (wall-clock UTC when omitted).

    The id is the UTC start time as a filesystem/URL-safe slug — unique per
    database because two live runs can't write one file (flock guard).
    """
    start = now if now is not None else datetime.now(tz=timezone.utc)
    return SessionFrame(
        id=start.strftime("%Y-%m-%dT%H-%M-%SZ"),
        label=label,
        note=note,
        start=start,
    )


def _host_snapshot(host: RemoteHost) -> HostSnapshot:
    """Map the view-relevant subset of *host* into a :class:`HostSnapshot`.

    Deliberately never touches ``host.creds`` — the snapshot has no field for
    them, so omission is structural, not an oversight. ``labs`` stays empty:
    a ``RemoteHost`` carries no per-host lab-membership list (that mapping
    lives only at load time, in the lab repository), so there is nothing
    pure to read here; a future task may thread it through explicitly.
    ``is_virtual`` is read via ``getattr`` because it is declared on each
    concrete host subclass (``UnixHost``/``EmbeddedHost``/``DockerHost``),
    not on the abstract ``RemoteHost`` base.
    """
    return HostSnapshot(
        id=host.id,
        element=host.element.name,
        name=host.name,
        board=host.board,
        slot=host.slot,
        hop=host.hop,
        os_type=host.os_type,
        os_name=host.os_name,
        os_version=host.os_version,
        ip=host.ip,
        interfaces={name: iface.ip for name, iface in host.interfaces.items()},
        labs=[],
        is_virtual=getattr(host, "is_virtual", False),
    )


def _link_provenance(provenance: Provenance) -> Literal["implicit", "declared"]:
    """Narrow the runtime enum's three values to the snapshot wire's two.

    ``snapshot_lab`` only ever freezes ``implicit_links()``/
    ``resolve_declared_links()`` output, so ``Provenance.DYNAMIC`` cannot
    reach here structurally — but the raise keeps that invariant loud rather
    than silently mis-tagging a future caller's tunnel-derived link as
    ``declared``. Dynamic tunnels ride ``SessionRecord.tunnels`` instead
    (spec 2026-07-16 §1).
    """
    if provenance is Provenance.IMPLICIT:
        return "implicit"
    if provenance is Provenance.DECLARED:
        return "declared"
    raise ValueError(
        f"cannot freeze a {provenance.value!r}-provenance link into a static "
        "LinkSnapshot — dynamic tunnels ride SessionRecord.tunnels instead"
    )


def _link_snapshot(link: Link) -> LinkSnapshot:
    """Map a runtime :class:`~otto.link.model.Link` into a :class:`LinkSnapshot`.

    Field-for-field mirror of ``scripts/gen_monitor_fixtures.py``'s fixture
    link construction: id, both endpoints (host/interface/ip/port), protocol,
    the provenance's string value, name, and the passthrough ``impair``
    middlebox reference.
    """
    return LinkSnapshot(
        id=link.id,
        endpoints=[
            LinkEndpointSnapshot(
                host=link.a.host, interface=link.a.interface, ip=link.a.ip, port=link.a.port
            ),
            LinkEndpointSnapshot(
                host=link.b.host, interface=link.b.interface, ip=link.b.ip, port=link.b.port
            ),
        ],
        protocol=link.protocol,
        provenance=_link_provenance(link.provenance),
        name=link.name,
        impair=link.impair,
    )


def snapshot_lab(hosts: Sequence[RemoteHost], declared: list[Link]) -> LabSnapshot:
    """Freeze the view-relevant lab config into a session snapshot.

    Static links only (implicit hop edges + declared routes; contract spec
    2026-07-10 §2) — dynamic/tunnel links are runtime state and never enter
    a snapshot. Credentials never leave the host object. ``elements`` stays
    empty: real labs declare membership per-host (``element`` field) and the
    frontend derives the grouping, exactly as with generator fixtures.

    A link is exported only when **both** endpoints resolve to a host in this
    snapshot, mirroring the fixture generator's ``_implicit_links``. That
    drops the ``local`` edge ``implicit_links`` gives every hop-less host: the
    local root is a node the frontend's topology view synthesizes for itself,
    never a ``RemoteHost``, so a ``local`` endpoint in the document would be a
    phantom. The same filter drops a declared link naming an unknown host.

    Args:
        hosts: Hosts to include, in the caller's order.
        declared: Already-resolved declared links (see
            ``otto.link.derive.resolve_declared_links``) — resolution against
            the active lab config is the CLI's job, not this pure module's.

    Returns:
        The frozen :class:`~otto.models.monitor.LabSnapshot`.
    """
    host_snaps = [_host_snapshot(h) for h in hosts]
    known = {snap.id for snap in host_snaps}
    links = [
        _link_snapshot(link)
        for link in [*implicit_links({h.id: h for h in hosts}), *declared]
        if link.a.host in known and link.b.host in known
    ]
    return LabSnapshot(hosts=host_snaps, elements=[], links=links)


def snapshot_lab_json(hosts: Sequence[RemoteHost], declared: list[Link]) -> str:
    """Build the same snapshot as :func:`snapshot_lab`, serialized to JSON.

    Args:
        hosts: Hosts to include, in the caller's order.
        declared: Already-resolved declared links.

    Returns:
        The JSON-encoded snapshot, ready to hand to the DB (``lab_json``) or
        the server.
    """
    return snapshot_lab(hosts, declared).model_dump_json()


# Imports of collector/factory/export live inside the methods: otto.monitor.db
# and otto.monitor.export import SessionFrame from this module.
class MonitorSession:
    """One live monitor session: its identity, lab snapshot, collector and archive.

    The single owner of what every producer (``otto monitor --live``,
    ``otto test --monitor``, the ``monitor`` fixture) used to hand-maintain:
    build the frame, snapshot and collector once; open the archive BEFORE
    any collection task exists; at the end, stamp the end, write the export,
    finalize the archive and close. Who drives
    :meth:`~otto.monitor.collector.MetricCollector.run` stays the caller's
    choice: :meth:`spawn` for a task beside a server, or the caller's own
    loop (``otto test`` drives it per class).

    Build with :meth:`build`; ``async with session:`` is :meth:`open` /
    :meth:`finish`.
    """

    def __init__(
        self,
        *,
        frame: SessionFrame,
        lab: "LabSnapshot",
        collector: "MetricCollector",
        interval: timedelta,
        db: "MetricDB | None",
        db_path: "Path | None",
        export_path: "Path | None",
        owns_hosts: bool,
    ) -> None:
        self.frame = frame
        self.lab = lab
        self.collector = collector
        self.interval = interval
        self.db_path = db_path
        self.export_path = export_path
        self.owns_hosts = owns_hosts
        self._db = db
        self._opened = False
        self._finished = False

    @classmethod
    def build(  # noqa: PLR0913 — one keyword per session input
        cls,
        hosts: "Sequence[RemoteHost] | None" = None,
        *,
        targets: "list[MonitorTarget] | None" = None,
        parsers: "list[MetricParser] | None" = None,
        interval: "timedelta | float",
        db_path: "Path | str | None" = None,
        export_path: "Path | None" = None,
        label: "str | None" = None,
        note: "str | None" = None,
        declared: "Sequence[Link]" = (),
        tunnel_source: "Callable[[], Awaitable[list[TunnelRecord]]] | None" = None,
        owns_hosts: bool,
    ) -> "MonitorSession":
        """Validate the inputs, then build the session. No I/O: nothing is opened.

        Args:
            hosts: The hosts to sample. Exactly one of *hosts* and *targets*.
            targets: Per-host targets, for callers assigning their own parsers
                per host.
            parsers: Shell parsers for every shell host in *hosts* (default:
                each host's registered set). Ignored with *targets*.
            interval: Collection interval (seconds or a timedelta).
            db_path: SQLite archive written live; ``None`` keeps data in memory.
            export_path: format:1 JSON document written by :meth:`finish`.
            label: Human label stored with the session.
            note: Free-form note stored with the session.
            declared: The lab's resolved declared links; only links whose both
                endpoints are in this session are kept.
            tunnel_source: Full-lab tunnel discovery for the collector.
            owns_hosts: Whether :meth:`finish` closes the hosts' connections.
                ``False`` when the hosts belong to someone else (a test).

        Raises:
            MonitorInputError: neither or both of hosts/targets, no hosts, an
                interval below the floor, or a host otto cannot sample.
        """
        from ..utils import validate_interval
        from .collector import MetricCollector
        from .errors import MonitorInputError
        from .export import build_session_metric_db
        from .factory import build_monitor_collector, is_monitorable

        if (hosts is None) == (targets is None):
            raise MonitorInputError("pass exactly one of hosts or targets", field="hosts")
        seconds = interval.total_seconds() if isinstance(interval, timedelta) else float(interval)
        try:
            validate_interval(seconds)
        except ValueError as exc:
            raise MonitorInputError(str(exc), field="interval") from exc
        if hosts is not None:
            field, snapshot_hosts = "hosts", list(hosts)
            bad = [h.id for h in snapshot_hosts if not is_monitorable(h)]
        else:
            field, snapshot_hosts = "targets", [t.host for t in targets or []]
            bad = [t.host.id for t in targets or [] if t.snmp is None and not _is_unix(t.host)]
        if not snapshot_hosts:
            raise MonitorInputError("no hosts to monitor", field=field)
        if bad:
            raise MonitorInputError(
                f"cannot monitor {', '.join(sorted(bad))}: otto samples metrics over a "
                "shell (Unix hosts) or over SNMP (a host declaring an `snmp` block), "
                "and these offer neither",
                field=field,
            )

        def _collector(db: "MetricDB | None") -> "MetricCollector":
            if targets is not None:
                return MetricCollector(targets=targets, db=db, tunnel_source=tunnel_source)
            return build_monitor_collector(
                snapshot_hosts, parsers=parsers, db=db, tunnel_source=tunnel_source
            )

        frame = new_frame(label=label, note=note)
        lab = snapshot_lab(snapshot_hosts, list(declared))
        db = None
        if db_path is not None:
            # The throwaway collector only derives the parser-catalog meta that
            # MetricDB's constructor needs up front; see build_session_metric_db.
            db = build_session_metric_db(
                str(db_path), frame, lab, _collector(None), interval=seconds
            )
        return cls(
            frame=frame,
            lab=lab,
            collector=_collector(db),
            interval=timedelta(seconds=seconds),
            db=db,
            db_path=Path(db_path) if db_path is not None else None,
            export_path=export_path,
            owns_hosts=owns_hosts,
        )

    async def open(self) -> None:
        """Open the archive, before any collection task exists. Idempotent.

        The open must complete first: an in-task open can be cancelled
        mid-schema by a prompt stop and leave a partial archive (#136). A
        locked or unsupported archive raises here.
        """
        await self.collector.init_db()
        self._opened = True

    def spawn(self) -> "asyncio.Task[None]":
        """Start collection at the session interval; the caller owns the task.

        Cancel and gather it before :meth:`finish`.

        Raises:
            RuntimeError: :meth:`open` has not completed.
        """
        if not self._opened:
            raise RuntimeError("MonitorSession.spawn() before open(): open the archive first")
        return asyncio.create_task(self.collector.run(self.interval))

    async def finish(self) -> None:
        """Stamp the end, write the export, finalize the archive, close. Idempotent.

        Safe after a failed or skipped :meth:`open`. Closes the hosts only
        when the session owns them; otherwise only the archive.
        """
        if self._finished:
            return
        self._finished = True
        from .export import document_json

        end = datetime.now(tz=timezone.utc)
        self.frame.end = end
        try:
            if self.export_path is not None:
                self.export_path.parent.mkdir(parents=True, exist_ok=True)
                self.export_path.write_text(document_json(self.export()))
            if self._db is not None and self._opened:
                await self._db.finalize(end)
        finally:
            # Always reached: a failed export or finalize must not strand the
            # host connections, the aiosqlite connection or the archive flock.
            with teardown_step("monitor", "collector close"):
                if self.owns_hosts:
                    await self.collector.close()
                else:
                    await self.collector.close_db()

    def export(self) -> "MonitorExport":
        """Return the session as a single-session format:1 document."""
        from .export import build_live_export

        return build_live_export(self.frame, self.collector, self.lab)

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.finish()


def _is_unix(host: "RemoteHost") -> bool:
    from ..host.unix_host import UnixHost

    return isinstance(host, UnixHost)
