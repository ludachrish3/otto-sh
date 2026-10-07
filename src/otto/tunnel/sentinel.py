"""Sentinel v2 wire format: the argv marker every tunnel process carries (spec §5).

The running processes on the hosts ARE the tunnel record — zero persisted
state. Each process is launched with argv[0]::

    otto-tunnel:v1:<id>:<proto>:<svc-port>:<carrier-port>:<direction>:<role>:<hop-index>:<dest>:<path>

Prefix + version + 9 payload segments (framing: otto.host.daemon), each
percent-encoded; empty segment = None. The ``path`` segment carries the
full fwd-ordered chain (entries ``host@iface``
or bare ``host``, each percent-encoded, joined with ``,``, the joined string
percent-encoded once more), so any single surviving process reconstructs the
entire intended tunnel — the record survives every other host being down.

Owner-agnostic (no username). Unknown versions/prefixes parse to ``None``,
never an error. STABILITY CONTRACT restarts at ``otto-tunnel:v1``: from the
first release with users, evolve only by adding versions and keeping old
ones parseable. (The ``otto-link:v1`` era predates users and is deleted.)
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote

from ..host.daemon import dec, enc, encode_token, split_token
from .model import (
    TUNNEL_SENTINEL_READ_VERSIONS,
    TUNNEL_SENTINEL_WRITE_VERSIONS,
    Direction,
    Role,
    Tunnel,
    TunnelHop,
)

if TYPE_CHECKING:
    from collections.abc import Callable

SENTINEL_PREFIX = "otto-tunnel"
[SENTINEL_VERSION] = TUNNEL_SENTINEL_WRITE_VERSIONS
"""The one declared write version, which :func:`encode_sentinel` writes.

Unpacked from ``TUNNEL_SENTINEL_WRITE_VERSIONS`` (``otto.tunnel.model``), so a
second write version fails at import until the writer chooses."""
_PAYLOAD_SEGMENTS_V1 = 9


@dataclass(frozen=True, slots=True)
class ParsedSentinel:
    """One decoded tunnel process: the whole tunnel + this process's slot."""

    tunnel: Tunnel
    direction: Direction
    role: Role
    hop_index: int
    carrier_port: int


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
    payload = (
        enc(tunnel.id),
        enc(tunnel.protocol),
        enc(tunnel.service_port),
        enc(carrier_port),
        direction.value,
        role.value,
        str(hop_index),
        enc(tunnel.dest),
        _encode_path(tunnel.path),
    )
    return encode_token(SENTINEL_PREFIX, SENTINEL_VERSION, payload)


def _decode_v1(payload: list[str]) -> "ParsedSentinel | None":
    tunnel_id, proto = dec(payload[0]), dec(payload[1])
    if not tunnel_id or not proto:
        return None
    try:
        service_port = int(dec(payload[2]))
        carrier_port = int(dec(payload[3]))
        direction = Direction(payload[4])
        role = Role(payload[5])
        hop_index = int(payload[6])
    except ValueError:
        return None
    dest = dec(payload[7]) or None
    path = _decode_path(payload[8])
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


_DECODERS: "dict[str, tuple[int, Callable[[list[str]], ParsedSentinel | None]]]" = {
    "v1": (_PAYLOAD_SEGMENTS_V1, _decode_v1),
}
"""Each readable version's payload segment count and decoder; keys equal the declared reads."""


def parse_sentinel(token: str) -> ParsedSentinel | None:
    """Parse one wire token; ``None`` for non-otto / undeclared-version / malformed."""
    for version in TUNNEL_SENTINEL_READ_VERSIONS:
        segments, decode = _DECODERS[version]
        payload = split_token(token, SENTINEL_PREFIX, version, segments)
        if payload is not None:
            return decode(payload)
    return None
