"""otto-impair argv sentinel + expire-timer discovery (spec §7).

Wire format: ``otto-impair:v1:<link-id>:<netdev>`` with percent-encoded
segments. Same philosophy as the tunnel sentinel: the timer process's argv IS
the state — discoverable via ``ps``, unambiguously otto's, owner-agnostic.
"""

from urllib.parse import quote, unquote

IMPAIR_SENTINEL_PREFIX = "otto-impair"
IMPAIR_SENTINEL_VERSION = "v1"
_SEGMENT_COUNT = 4
_MIN_PS_FIELDS = 3

#: Separate -eo flags, NOT comma-joined — procps-ng 3.3.10 mis-parses the
#: combined form. `|| true` so a no-match grep isn't a command failure.
IMPAIR_PS_COMMAND: str = (
    "ps -eo pid= -eo etime= -eo args= 2>/dev/null | grep -a ' otto-impair:' || true"
)


def encode_impair_sentinel(link_id: str, netdev: str) -> str:
    """Sentinel token tagging one placement's expire timer."""
    return ":".join(
        (
            IMPAIR_SENTINEL_PREFIX,
            IMPAIR_SENTINEL_VERSION,
            quote(link_id, safe=""),
            quote(netdev, safe=""),
        )
    )


def parse_impair_sentinel(token: str) -> tuple[str, str] | None:
    """Decode a sentinel token to ``(link_id, netdev)``; ``None`` if not ours."""
    parts = token.split(":")
    if (
        len(parts) != _SEGMENT_COUNT
        or parts[0] != IMPAIR_SENTINEL_PREFIX
        or parts[1] != IMPAIR_SENTINEL_VERSION
    ):
        return None
    return unquote(parts[2]), unquote(parts[3])


def parse_impair_ps(output: str) -> list[tuple[int, str, str]]:
    """Reconstruct ``(pid, link_id, netdev)`` from :data:`IMPAIR_PS_COMMAND` output."""
    out: list[tuple[int, str, str]] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < _MIN_PS_FIELDS or not fields[0].isdigit():
            continue
        token = next((w for w in fields[2:] if w.startswith(f"{IMPAIR_SENTINEL_PREFIX}:")), None)
        if token is None:
            continue
        parsed = parse_impair_sentinel(token)
        if parsed is None:
            continue
        out.append((int(fields[0]), parsed[0], parsed[1]))
    return out
