"""Sentinel wire format: the argv marker every otto tunnel process carries.

The running processes on the hosts ARE the dynamic-link record — zero
persisted state. Each process is launched (sub-project #2) with an argv[0]
of the form::

    otto-link:v1:<id>:<proto>:<a-host>:<a-iface>:<a-port>:<b-host>:<b-iface>:<b-port>

so ``pgrep -af '^otto-link:'`` on a host returns exactly otto's tunnels, and
parsing the marker reconstructs the full ``Link`` with no ledger lookup.

STABILITY CONTRACT — live tunnels outlive otto processes, so a v1 marker
must parse forever; evolve the format only by adding a new version segment
and keeping v1 parsing intact:

- 10 colon-joined segments, each percent-encoded (a literal ``:`` inside a
  segment — e.g. the netdev alias ``eth0:1`` — survives);
- ``None`` interface/port encode as empty segments;
- deliberately **no username segment**: markers are owner-agnostic so any
  user can discover and reap any otto tunnel;
- unknown versions parse to ``None`` (skipped), never an error.
"""

from urllib.parse import quote, unquote

from .model import Link, LinkEndpoint, Provenance

SENTINEL_PREFIX = "otto-link"
SENTINEL_VERSION = "v1"
_SEGMENT_COUNT = 10


def _enc(value: str | int | None) -> str:
    return quote(str(value), safe="") if value is not None else ""


def encode_sentinel(link: Link) -> str:
    """Return the wire token for *link* (ports read from the endpoints)."""
    segments = (
        SENTINEL_PREFIX,
        SENTINEL_VERSION,
        _enc(link.id),
        _enc(link.protocol),
        _enc(link.a.host),
        _enc(link.a.interface),
        _enc(link.a.port),
        _enc(link.b.host),
        _enc(link.b.interface),
        _enc(link.b.port),
    )
    return ":".join(segments)


def parse_sentinel(token: str) -> Link | None:
    """Parse one wire token; ``None`` for non-otto / other-version / malformed input."""
    parts = token.split(":")
    prefix_ok = len(parts) == _SEGMENT_COUNT and parts[0] == SENTINEL_PREFIX
    if not prefix_ok or parts[1] != SENTINEL_VERSION:
        return None
    link_id, proto = unquote(parts[2]), unquote(parts[3])
    if not link_id or not proto:
        return None

    def endpoint(host_seg: str, iface_seg: str, port_seg: str) -> LinkEndpoint | None:
        host = unquote(host_seg)
        if not host:
            return None
        port: int | None = None
        if port_seg:
            try:
                port = int(unquote(port_seg))
            except ValueError:
                return None
        return LinkEndpoint(host=host, interface=unquote(iface_seg) or None, port=port)

    a = endpoint(parts[4], parts[5], parts[6])
    b = endpoint(parts[7], parts[8], parts[9])
    if a is None or b is None:
        return None
    return Link(a=a, b=b, protocol=proto, provenance=Provenance.DYNAMIC, id=link_id)


def parse_discovery(ps_output: str) -> list[Link]:
    """Reconstruct links from ``pgrep -af``-style output (one process per line).

    One link is usually several tagged processes (a socat per end, a forward
    on the hop) sharing the same id: group by id, first non-``None`` port per
    end wins. Non-otto lines are ignored — discovery must never misattribute
    a stranger's socat.
    """
    by_id: dict[str, Link] = {}
    for line in ps_output.splitlines():
        token = next((w for w in line.split() if w.startswith(f"{SENTINEL_PREFIX}:")), None)
        if token is None:
            continue
        parsed = parse_sentinel(token)
        if parsed is None:
            continue
        seen = by_id.get(parsed.id)
        if seen is None:
            by_id[parsed.id] = parsed
            continue
        # Merge: keep the first non-None port per end.
        merged_a = seen.a if seen.a.port is not None else parsed.a
        merged_b = seen.b if seen.b.port is not None else parsed.b
        by_id[parsed.id] = Link(
            a=merged_a,
            b=merged_b,
            protocol=seen.protocol,
            provenance=Provenance.DYNAMIC,
            id=seen.id,
            name=seen.name,
        )
    return list(by_id.values())
