"""Runtime ``Link`` model — the unified edge object across all provenances.

One type regardless of where the link came from, so the CLI, topology
derivation, and monitor GUI all speak the same object (foundation spec §6).
"""

import enum
import hashlib
from dataclasses import dataclass


class Provenance(enum.Enum):
    """Where a link came from."""

    IMPLICIT = "implicit"
    """Derived from a host's ``hop`` chain (the ssh/telnet management path)."""

    DECLARED = "declared"
    """Declared in ``lab.json``'s ``links`` section (a data-plane route)."""

    DYNAMIC = "dynamic"
    """An otto-created tunnel, observed live on the hosts."""


@dataclass(frozen=True, slots=True)
class LinkEndpoint:
    """One end of a link: a host, optionally pinned to a named interface."""

    host: str
    """Host id (see ``make_host_id``)."""

    interface: str | None = None
    """Netdev name (a key in the host's ``interfaces`` map); ``None`` = the
    management ``ip`` / the host's sole interface."""

    ip: str = ""
    """Resolved address of this end (empty when unresolvable, e.g. a sentinel
    parsed without lab context)."""

    port: int | None = None
    """Bound port on this end — dynamic links only (sub-project #2)."""


def _endpoint_key(e: LinkEndpoint) -> tuple[str, str]:
    return (e.host, e.interface or "")


def make_link_id(a: LinkEndpoint, b: LinkEndpoint, protocol: str) -> str:
    """Deterministic id for the *route* ``a <-> b`` over *protocol*.

    STABILITY CONTRACT — changing this algorithm invalidates every live
    tunnel's sentinel and every recorded id across otto versions:

    - endpoints are sorted by ``(host, interface or "")`` so a<->b == b<->a;
    - a ``None`` interface falls back to ``''`` in the canonical string;
    - *protocol* is lowercased, so a route declared ``"udp"`` and a tunnel
      added as ``"UDP"`` reconcile to the same id;
    - **ports and ips are excluded** — the id names the route, so a dynamic
      tunnel over a declared route reconciles to the same id;
    - format: ``"lnk-"`` + first 12 hex chars of sha256 over
      ``"{lo.host}|{lo.interface or ''}|{hi.host}|{hi.interface or ''}|{protocol.lower()}"``.
    """
    lo, hi = sorted((a, b), key=_endpoint_key)
    canon = f"{lo.host}|{lo.interface or ''}|{hi.host}|{hi.interface or ''}|{protocol.lower()}"
    return "lnk-" + hashlib.sha256(canon.encode()).hexdigest()[:12]


def make_dynamic_link_id(a: LinkEndpoint, b: LinkEndpoint, protocol: str, port: int) -> str:
    """Id for a dynamic tunnel: the route hash plus a readable ``-<port>`` suffix.

    The suffix keeps the port visible in the id, in ``otto link list``, in
    ``remove <id>``, and in every tagged process's ``argv[0]``. Distinct ports
    on the same route are therefore distinct tunnels.
    """
    return f"{make_link_id(a, b, protocol)}-{port}"


def make_static_link_id(a: LinkEndpoint, b: LinkEndpoint, name: str | None) -> str:
    """Readable handle for a static link — the declared ``name`` or ``a--b``.

    No hash: static links (implicit hop edges, declared routes) are described
    connectivity, not otto tunnels, so they never wear the ``lnk-<hex>`` form.
    Endpoints are sorted so ``a<->b`` and ``b<->a`` yield the same handle.
    """
    if name:
        return name
    lo, hi = sorted((a, b), key=_endpoint_key)
    return f"{lo.host}--{hi.host}"


@dataclass(frozen=True, slots=True)
class Link:
    """An edge between two endpoints, from any provenance."""

    a: LinkEndpoint
    b: LinkEndpoint
    protocol: str = "tcp"
    provenance: Provenance = Provenance.DECLARED
    id: str = ""
    """Provenance-aware id, auto-computed when empty.

    Uses ``make_dynamic_link_id`` (route hash + ``-<port>``) for DYNAMIC,
    ``make_static_link_id`` (readable ``name``/``a--b`` handle) otherwise."""
    name: str | None = None
    """Optional friendly handle from the lab data."""

    def __post_init__(self) -> None:
        if self.id:
            return
        if self.provenance is Provenance.DYNAMIC:
            port = self.a.port if self.a.port is not None else self.b.port
            new_id = make_dynamic_link_id(self.a, self.b, self.protocol, port or 0)
        else:
            new_id = make_static_link_id(self.a, self.b, self.name)
        object.__setattr__(self, "id", new_id)
