"""Sentinel v2 wire format: the argv marker every tunnel process carries (spec §5).

The running processes on the hosts ARE the tunnel record — zero persisted
state. Each process is launched with argv[0]::

    otto-tunnel:v1:<id>:<proto>:<svc-port>:<carrier-port>:<direction>:<role>:<hop-index>:<dest>:<path>

11 colon-joined segments, each percent-encoded; empty segment = None. The
``path`` segment carries the full fwd-ordered chain (entries ``host@iface``
or bare ``host``, each percent-encoded, joined with ``,``, the joined string
percent-encoded once more), so any single surviving process reconstructs the
entire intended tunnel — the record survives every other host being down.

Owner-agnostic (no username). Unknown versions/prefixes parse to ``None``,
never an error. STABILITY CONTRACT restarts at ``otto-tunnel:v1``: from the
first release with users, evolve only by adding versions and keeping old
ones parseable. (The ``otto-link:v1`` era predates users and is deleted.)
"""

from dataclasses import dataclass
from urllib.parse import quote, unquote

from .model import Direction, Role, Tunnel, TunnelHop

SENTINEL_PREFIX = "otto-tunnel"
SENTINEL_VERSION = "v1"
_SEGMENT_COUNT = 11


@dataclass(frozen=True, slots=True)
class ParsedSentinel:
    """One decoded tunnel process: the whole tunnel + this process's slot."""

    tunnel: Tunnel
    direction: Direction
    role: Role
    hop_index: int
    carrier_port: int


def _enc(value: str | int | None) -> str:
    return quote(str(value), safe="") if value is not None else ""


def _encode_path(path: tuple[TunnelHop, ...]) -> str:
    entries = ",".join(
        quote(f"{h.host}@{h.interface}" if h.interface else h.host, safe="") for h in path
    )
    return quote(entries, safe="")


def _decode_path(segment: str) -> tuple[TunnelHop, ...] | None:
    hops: list[TunnelHop] = []
    for raw in unquote(segment).split(","):
        entry = unquote(raw)
        if not entry:
            return None
        host, sep, iface = entry.partition("@")
        if not host:
            return None
        hops.append(TunnelHop(host=host, interface=iface if sep and iface else None))
    return tuple(hops)


def encode_sentinel(
    tunnel: Tunnel, *, direction: Direction, role: Role, hop_index: int, carrier_port: int
) -> str:
    """Return the wire token for one process of *tunnel*."""
    segments = (
        SENTINEL_PREFIX,
        SENTINEL_VERSION,
        _enc(tunnel.id),
        _enc(tunnel.protocol),
        _enc(tunnel.service_port),
        _enc(carrier_port),
        direction.value,
        role.value,
        str(hop_index),
        _enc(tunnel.dest) if tunnel.dest is not None else "",
        _encode_path(tunnel.path),
    )
    return ":".join(segments)


def parse_sentinel(token: str) -> ParsedSentinel | None:
    """Parse one wire token; ``None`` for non-otto / other-version / malformed."""
    parts = token.split(":")
    if len(parts) != _SEGMENT_COUNT or parts[0] != SENTINEL_PREFIX:
        return None
    if parts[1] != SENTINEL_VERSION:
        return None
    tunnel_id, proto = unquote(parts[2]), unquote(parts[3])
    if not tunnel_id or not proto:
        return None
    try:
        service_port = int(unquote(parts[4]))
        carrier_port = int(unquote(parts[5]))
        direction = Direction(parts[6])
        role = Role(parts[7])
        hop_index = int(parts[8])
    except ValueError:
        return None
    dest = unquote(parts[9]) or None
    path = _decode_path(parts[10])
    if path is None:
        return None
    try:
        tunnel = Tunnel(
            protocol=proto, service_port=service_port, path=path, dest=dest, id=tunnel_id
        )
    except ValueError:
        return None
    return ParsedSentinel(
        tunnel=tunnel,
        direction=direction,
        role=role,
        hop_index=hop_index,
        carrier_port=carrier_port,
    )
