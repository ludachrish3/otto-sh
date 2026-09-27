"""Shared helpers for the ``check_tunnel`` test modules: the paths, ports and row readers."""

from otto.check import SWEEP_MIN_AGE_S, Verdict
from otto.tunnel.check import TunnelCheckColumn, TunnelCheckReport
from otto.tunnel.model import TunnelHop, make_tunnel_id

from ._check_fakes import Bed, bed

ABC = [("a", None), ("b", None), ("c", None)]
PORT = 8080
SCRATCH = 61000
AB = [("a", None), ("b", None)]
DEST = ("d", None)
EMBEDDED = {"has_bash": False, "serves": [PORT]}
"""A device that runs nothing of otto's: no shell, and its own service on --port."""
LAST_SEGMENT = "last segment → d"
HANDSHAKE_ONLY = "handshake only — the payload is not verified (#440)"
NO_ORACLE = "UDP to a device that runs nothing of otto's has no reply to check (#440)"
OLD_S = SWEEP_MIN_AGE_S + 60
"""A killed earlier run's leftover: older than the sweep's bound, so it is swept."""
OLD_NOTE = f"earlier or concurrent run, started {OLD_S // 60} min ago"
"""What a swept :data:`OLD_S` leftover's line says in its parenthesis."""


def _rows(report: TunnelCheckReport, protocol: str = "tcp") -> dict:
    [column] = [c for c in report.columns if c.protocol == protocol]
    return {r.feature: r for r in column.results}


def _column(report: TunnelCheckReport, protocol: str = "tcp") -> TunnelCheckColumn:
    [column] = [c for c in report.columns if c.protocol == protocol]
    return column


def _verdicts(report: TunnelCheckReport, protocol: str = "tcp") -> dict[str, Verdict]:
    return {name: r.verdict for name, r in _rows(report, protocol).items()}


def _throwaway_id(protocol: str, port: int = SCRATCH, hops: str = "abc") -> str:
    return make_tunnel_id(tuple(TunnelHop(h) for h in hops), protocol, port)


def _check_procs_left(the_bed: Bed) -> dict[str, list[str]]:
    return {h.id: [p.token for p in h.alive()] for h in the_bed.hosts.values() if h.alive()}


TCP_ROWS = [
    "service port",
    "segment a → b",
    "segment b → c",
    "build",
    "fwd 1 B",
    "fwd 1400 B",
    "fwd 64 KiB",
    "rev 1 B",
    "rev 1400 B",
    "rev 64 KiB",
    "rtt",
    "list",
    "teardown",
]


def _dest_bed(monkeypatch, **dest: object) -> Bed:
    """Hosts a, b (the path) and d (the dest, at 10.0.0.3), installed."""
    return bed("a", "b", "d", d=dict(dest)).install(monkeypatch)
