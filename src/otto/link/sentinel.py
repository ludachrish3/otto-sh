"""otto-impair argv sentinel + expire-timer discovery (spec §7).

Wire format: ``otto-impair:v1:<link-id>:<netdev>`` with percent-encoded
segments. Same philosophy as the tunnel sentinel: the timer process's argv IS
the state — discoverable via ``ps``, unambiguously otto's, owner-agnostic.
Framing, ps scanning, and percent-encoding ride :mod:`otto.host.daemon`.
"""

from ..host.daemon import dec, enc, encode_token, parse_ps_output, ps_scan_command, split_token

IMPAIR_SENTINEL_PREFIX = "otto-impair"
IMPAIR_SENTINEL_VERSION = "v1"
_PAYLOAD_SEGMENTS = 2

IMPAIR_PS_COMMAND: str = ps_scan_command(IMPAIR_SENTINEL_PREFIX)
"""The per-host expire-timer scan. Built by
:func:`otto.host.daemon.ps_scan_command` — see it for the procps
portability story; bytes pinned by ``TestWireGolden``."""


def encode_impair_sentinel(link_id: str, netdev: str) -> str:
    """Sentinel token tagging one placement's expire timer."""
    return encode_token(
        IMPAIR_SENTINEL_PREFIX, IMPAIR_SENTINEL_VERSION, (enc(link_id), enc(netdev))
    )


def parse_impair_sentinel(token: str) -> tuple[str, str] | None:
    """Decode a sentinel token to ``(link_id, netdev)``; ``None`` if not ours."""
    payload = split_token(token, IMPAIR_SENTINEL_PREFIX, IMPAIR_SENTINEL_VERSION, _PAYLOAD_SEGMENTS)
    if payload is None:
        return None
    return dec(payload[0]), dec(payload[1])


def parse_impair_ps(output: str) -> list[tuple[int, str, str]]:
    """Reconstruct ``(pid, link_id, netdev)`` from :data:`IMPAIR_PS_COMMAND` output."""
    out: list[tuple[int, str, str]] = []
    for proc in parse_ps_output(output, IMPAIR_SENTINEL_PREFIX):
        parsed = parse_impair_sentinel(proc.token)
        if parsed is None:
            continue
        out.append((proc.pid, parsed[0], parsed[1]))
    return out
