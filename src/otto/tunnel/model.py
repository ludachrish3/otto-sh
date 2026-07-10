"""Runtime ``Tunnel`` model — one end-to-end forwarding path (#2b spec §4).

A tunnel is what one ``otto tunnel add`` builds: service port + protocol +
ordered host path (+ optional far-end delivery override). Its per-hop
segments ride links (``otto.link`` edges); ``otto.tunnel`` imports from
``otto.link``, never the reverse.
"""

import enum
import hashlib
from dataclasses import dataclass


class Direction(enum.Enum):
    """Which mirrored chain a process belongs to (spec §6.1)."""

    FWD = "fwd"
    """First-listed host toward last-listed host."""

    REV = "rev"
    """Last-listed host toward first-listed host."""


class Role(enum.Enum):
    """What a tagged process does within its direction's chain."""

    INGRESS = "ingress"
    """Binds the service port on its endpoint's data-plane ip."""

    RELAY = "relay"
    """TCP carrier pass-through on an intermediate hop."""

    EGRESS = "egress"
    """Delivers carrier traffic to the local service (or ``--dest``)."""


ProcKey = tuple[str, Direction, Role]
"""One expected/observed tunnel process: ``(host_id, direction, role)``."""


@dataclass(frozen=True, slots=True)
class TunnelHop:
    """One position in a tunnel's ordered path."""

    host: str
    """Host id (containers use their dotted ``parent.project.service`` id)."""

    interface: str | None = None
    """Netdev key on the host; ``None`` = single/assumed interface
    (containers: always ``None`` — they have no modeled interfaces)."""


def make_tunnel_id(path: tuple[TunnelHop, ...], protocol: str, service_port: int) -> str:
    """Deterministic id: ``tun-<12 hex>-<service_port>`` (spec §4).

    The hash covers the **ordered** chain + protocol; the service port is a
    readable suffix, not hashed (same scheme as the retired
    ``lnk-<hex>-<port>``). ``a,c,b`` != ``b,c,a`` — a reversed duplicate is
    rejected by the endpoint-bind conflict rule instead (spec §7).
    """
    canon = protocol.lower() + "|" + ",".join(f"{h.host}@{h.interface or ''}" for h in path)
    return "tun-" + hashlib.sha256(canon.encode()).hexdigest()[:12] + f"-{service_port}"


@dataclass(frozen=True, slots=True)
class Tunnel:
    """One end-to-end forwarding path (one ``add`` = one tunnel, spec D4)."""

    protocol: str
    """``"udp"`` or ``"tcp"`` (validated at the manage layer)."""

    service_port: int
    """The port that binds on both endpoint hosts."""

    path: tuple[TunnelHop, ...]
    """Ordered chain, first = one endpoint, last = the other; len >= 2."""

    dest: str | None = None
    """Far-end delivery override host id (``--dest``), or ``None`` for
    loopback delivery on the last-listed host (spec §6.3)."""

    id: str = ""
    """Auto-computed via :func:`make_tunnel_id` when empty. A sentinel-parsed
    tunnel passes its wire id through verbatim (never recomputed)."""

    def __post_init__(self) -> None:
        min_hosts = 2
        if len(self.path) < min_hosts:
            raise ValueError(f"a tunnel path needs at least {min_hosts} hosts")
        if not self.id:
            object.__setattr__(
                self, "id", make_tunnel_id(self.path, self.protocol, self.service_port)
            )

    def expected_processes(self) -> set[ProcKey]:
        """Return the ``2 * len(path)`` processes a healthy tunnel runs (spec §6.1).

        Every host carries exactly two: its FWD-chain process and its
        REV-chain process, with the role determined by position (ingress at
        the direction's origin, egress at its far end, relay between).
        """
        last = len(self.path) - 1
        out: set[ProcKey] = set()
        for i, hop in enumerate(self.path):
            for direction in Direction:
                origin = 0 if direction is Direction.FWD else last
                far = last if direction is Direction.FWD else 0
                role = Role.INGRESS if i == origin else Role.EGRESS if i == far else Role.RELAY
                out.add((hop.host, direction, role))
        return out
