"""otto-impair argv sentinel + expire-timer discovery (spec §7; v2 spec 2026-07-11 §1).

Wire formats, percent-encoded segments, framing via :mod:`otto.host.daemon`:

- v1 (whole-link timers): ``otto-impair:v1:<link-id>:<netdev>``
- v2 (per-selector timers): ``otto-impair:v2:<link-id>:<netdev>:<port>:<proto-or-empty>``

v1 stays parseable forever so repair cancels timers launched by older otto.
The timer process's argv IS the state — discoverable via ``ps``, unambiguously
otto's, owner-agnostic.
"""

from dataclasses import dataclass

from ..host.daemon import dec, enc, encode_token, parse_ps_output, ps_scan_command, split_token
from .params import Selector

IMPAIR_SENTINEL_PREFIX = "otto-impair"
IMPAIR_SENTINEL_VERSION = "v1"
IMPAIR_SENTINEL_VERSION_V2 = "v2"
_PAYLOAD_SEGMENTS_V1 = 2
_PAYLOAD_SEGMENTS_V2 = 4

IMPAIR_PS_COMMAND: str = ps_scan_command(IMPAIR_SENTINEL_PREFIX)
"""The per-host expire-timer scan. Built by
:func:`otto.host.daemon.ps_scan_command` — see it for the procps
portability story; bytes pinned by ``TestWireGolden``."""


@dataclass(frozen=True, slots=True)
class ImpairTimer:
    """One live expire-timer seen in a ps scan."""

    pid: int
    link_id: str
    netdev: str
    selector: Selector | None
    """``None`` = a v1 whole-link timer; set = a v2 per-selector timer."""


def encode_impair_sentinel(link_id: str, netdev: str) -> str:
    """v1 sentinel token tagging one placement's WHOLE-LINK expire timer.

    Whole-link timers deliberately stay on v1 — the whole-link path is
    byte-identical to pre-selector otto (spec 2026-07-11 hard constraint).
    """
    return encode_token(
        IMPAIR_SENTINEL_PREFIX, IMPAIR_SENTINEL_VERSION, (enc(link_id), enc(netdev))
    )


def encode_impair_sentinel_v2(link_id: str, netdev: str, selector: Selector) -> str:
    """v2 sentinel token tagging one selector's expire timer on one placement."""
    return encode_token(
        IMPAIR_SENTINEL_PREFIX,
        IMPAIR_SENTINEL_VERSION_V2,
        (enc(link_id), enc(netdev), enc(selector.port), enc(selector.proto or "")),
    )


def parse_impair_sentinel(token: str) -> tuple[str, str, Selector | None] | None:
    """Decode a v1 OR v2 token to ``(link_id, netdev, selector)``; ``None`` if not ours.

    v1 tokens decode with ``selector=None``. Unknown versions and malformed
    v2 payloads (non-numeric/out-of-range port, unknown proto) parse to
    ``None``, never an error — the framing stability contract.
    """
    v1 = split_token(token, IMPAIR_SENTINEL_PREFIX, IMPAIR_SENTINEL_VERSION, _PAYLOAD_SEGMENTS_V1)
    if v1 is not None:
        return dec(v1[0]), dec(v1[1]), None
    v2 = split_token(
        token, IMPAIR_SENTINEL_PREFIX, IMPAIR_SENTINEL_VERSION_V2, _PAYLOAD_SEGMENTS_V2
    )
    if v2 is None:
        return None
    port_text, proto_text = dec(v2[2]), dec(v2[3])
    if not port_text.isdigit():
        return None
    try:
        selector = Selector(int(port_text), proto_text or None)
    except ValueError:
        return None
    return dec(v2[0]), dec(v2[1]), selector


def parse_impair_ps(output: str) -> list[ImpairTimer]:
    """Reconstruct live timers from :data:`IMPAIR_PS_COMMAND` output (v1 AND v2)."""
    out: list[ImpairTimer] = []
    for proc in parse_ps_output(output, IMPAIR_SENTINEL_PREFIX):
        parsed = parse_impair_sentinel(proc.token)
        if parsed is None:
            continue
        out.append(ImpairTimer(proc.pid, parsed[0], parsed[1], parsed[2]))
    return out
