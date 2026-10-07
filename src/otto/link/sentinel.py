"""otto-impair argv sentinel + expire-timer discovery (spec §7; v2 spec 2026-07-11 §1).

Wire formats, percent-encoded segments, framing via :mod:`otto.host.daemon`:

- v1 (whole-link timers): ``otto-impair:v1:<link-id>:<netdev>``
- v2 (decode only; per-selector timers from older otto):
  ``otto-impair:v2:<link-id>:<netdev>:<port>:<proto-or-empty>``
- v3 (per-selector timers):
  ``otto-impair:v3:<link-id>:<netdev>:<port>:<end-or-empty>:<proto-or-empty>:<side-or-empty>``

v1 stays parseable forever so repair cancels timers launched by older otto.
The timer process's argv IS the state — discoverable via ``ps``, unambiguously
otto's, owner-agnostic.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..host.daemon import dec, enc, encode_token, parse_ps_output, ps_scan_command, split_token
from .params import IMPAIR_SENTINEL_READ_VERSIONS, Selector

if TYPE_CHECKING:
    from collections.abc import Callable

    _Decoded = tuple[str, str, Selector | None] | None

IMPAIR_SENTINEL_PREFIX = "otto-impair"
IMPAIR_SENTINEL_VERSION = "v1"
"""The whole-link writer's version (:func:`encode_impair_sentinel`)."""
IMPAIR_SENTINEL_VERSION_V3 = "v3"
"""The per-selector writer's version (:func:`encode_impair_sentinel_v3`).

Each writer names its own version; a test holds the two equal to
``IMPAIR_SENTINEL_WRITE_VERSIONS`` (``otto.link.params``), so neither can emit
an undeclared one."""

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


def encode_impair_sentinel_v3(link_id: str, netdev: str, selector: Selector) -> str:
    """v3 sentinel token tagging one selector's expire timer on one placement."""
    return encode_token(
        IMPAIR_SENTINEL_PREFIX,
        IMPAIR_SENTINEL_VERSION_V3,
        (
            enc(link_id),
            enc(netdev),
            enc(selector.port),
            enc(selector.end),
            enc(selector.proto),
            enc(selector.side),
        ),
    )


def _selector_or_none(port_text: str, proto_text: str, side_text: str) -> Selector | None:
    """Decode a sentinel's selector segments; ``None`` = not a selector otto writes."""
    try:
        return Selector.parse(port_text, proto_text or None, side_text or None)
    except ValueError:
        return None


def _decode_v1(payload: list[str]) -> "_Decoded":
    return dec(payload[0]), dec(payload[1]), None


def _decode_v2(payload: list[str]) -> "_Decoded":
    selector = _selector_or_none(dec(payload[2]), dec(payload[3]), "")
    return None if selector is None else (dec(payload[0]), dec(payload[1]), selector)


def _decode_v3(payload: list[str]) -> "_Decoded":
    port, end = dec(payload[2]), dec(payload[3])
    selector = _selector_or_none(f"{port}:{end}" if end else port, dec(payload[4]), dec(payload[5]))
    return None if selector is None else (dec(payload[0]), dec(payload[1]), selector)


_DECODERS: "dict[str, tuple[int, Callable[[list[str]], _Decoded]]]" = {
    "v1": (2, _decode_v1),
    "v2": (4, _decode_v2),
    "v3": (6, _decode_v3),
}
"""Each readable version's payload segment count and decoder; keys equal the declared reads.

:func:`parse_impair_sentinel` walks ``IMPAIR_SENTINEL_READ_VERSIONS``, never
this table's keys, so a decoder whose version is not declared reads nothing."""


# DEBT(no-tuple-return): three parsed fields; callers index into it.
# ast-grep-ignore: no-tuple-return
def parse_impair_sentinel(token: str) -> tuple[str, str, Selector | None] | None:
    """Decode a token to ``(link_id, netdev, selector)``; ``None`` if not ours or undeclared.

    v1 decodes with ``selector=None``; v2 (older otto) decodes to a
    single-port, either-side selector. Versions outside
    ``IMPAIR_SENTINEL_READ_VERSIONS`` and malformed payloads
    (non-numeric/out-of-range port, unknown proto/side) parse to ``None``,
    never an error — the framing stability contract.
    """
    for version in IMPAIR_SENTINEL_READ_VERSIONS:
        segments, decode = _DECODERS[version]
        payload = split_token(token, IMPAIR_SENTINEL_PREFIX, version, segments)
        if payload is not None:
            return decode(payload)
    return None


def parse_impair_ps(output: str) -> list[ImpairTimer]:
    """Reconstruct live timers from :data:`IMPAIR_PS_COMMAND` output (v1, v2, and v3)."""
    out: list[ImpairTimer] = []
    for proc in parse_ps_output(output, IMPAIR_SENTINEL_PREFIX):
        parsed = parse_impair_sentinel(proc.token)
        if parsed is None:
            continue
        out.append(ImpairTimer(proc.pid, parsed[0], parsed[1], parsed[2]))
    return out
