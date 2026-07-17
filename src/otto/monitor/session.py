"""Session framing for live monitor runs (spec 2026-07-12).

The collector stays session-blind: one process run == one live session, and
the frame is stamped at the edges (CLI at launch, shutdown hook at exit).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from ..host.remote_host import RemoteHost
from ..link.derive import implicit_links
from ..link.model import Link, Provenance
from ..models import HostSnapshot, LabSnapshot, LinkEndpointSnapshot, LinkSnapshot


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
        element=host.element,
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
    local root is the frontend's own synthesized node (Plan 4 topology), never
    a ``RemoteHost``, so a ``local`` endpoint in the document would be a
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
