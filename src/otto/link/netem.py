"""NetEm — the first-party ``LinkImpairer`` (tc qdisc netem on unix hosts).

argv builders ALWAYS emit explicit units (spec §3.1) — tc's bare-number
semantics vary by parameter and iproute2 version. The parser reads
``tc qdisc show dev X`` back into :class:`~otto.link.params.ImpairmentParams`
(kernel qdisc config is the only state — spec §6) and tolerates both modern
and old iproute2 formatting (``50ms`` vs ``50.0ms``).
"""

import re
from dataclasses import dataclass
from typing import ClassVar

from typing_extensions import override

from .impairer import (
    FIRST_SELECTOR_BAND,
    MAX_SELECTORS,
    LinkImpairer,
    ScopedState,
    register_impairer,
)
from .params import ImpairmentParams, Selector, collides

_TIME_TOKEN = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>us|usec|ms|msec|s|sec)$")
_PERCENT_TOKEN = re.compile(r"^(?P<num>\d+(?:\.\d+)?)%$")
_TIME_TO_MS = {"us": 0.001, "usec": 0.001, "ms": 1.0, "msec": 1.0, "s": 1000.0, "sec": 1000.0}
_PERCENT_KEYWORDS = {
    "loss": "loss_pct",
    "corrupt": "corrupt_pct",
    "duplicate": "duplicate_pct",
    "reorder": "reorder_pct",
}
_MIN_DELAY_TOKENS = 4
"""``qdisc netem <handle>: root ...`` — the shortest possible root-netem line."""

_SCOPED_BANDS = 11
"""Fixed prio band count: 3 kernel-default bands + the 8-selector cap. The
root is created ONCE per clean->scoped transition and never re-tuned while
scoped — re-`replace`-ing a live prio root risks the kernel re-initializing
bands and destroying sibling selectors' netem leaves."""

_KERNEL_DEFAULT_PRIOMAP = "1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1"
"""The kernel's default prio priomap: every TOS value maps to bands 1-3, so
unmatched traffic behaves exactly as with no qdisc (pfifo_fast equivalence)."""

_PROTO_NUM = {"tcp": 6, "udp": 17}

_SIDES_ORDER = ("dst", "src")
_PROTOS_ORDER = ("tcp", "udp")
_SIDE_MATCH = {"dst": "dport", "src": "sport"}
_PREF_SIDE_STRIDE = 1000
_PREF_TIER_STRIDE = 200
_PREF_BAND_STRIDE = 10
_MAX_TIER = 2


@dataclass(frozen=True, slots=True)
class _Slot:
    """One (side, proto) pair a selector covers — one pref, one or more u32 entries."""

    side: str
    proto: str


def _selector_slots(selector: Selector) -> list[_Slot]:
    """Return the slots *selector* occupies: dst before src, tcp before udp."""
    return [
        _Slot(side, proto)
        for side in _SIDES_ORDER
        if side in selector.sides
        for proto in _PROTOS_ORDER
        if proto in selector.protos
    ]


def _filter_pref(selector: Selector, band: int, slot: _Slot) -> int:
    """``1000*side + 200*tier + 10*band + proto`` — the pref order IS the precedence.

    The kernel walks filters in ascending pref and stops at the first match:
    every dport slot sorts before every sport slot (destination first), and
    within a side a strictly narrower selector has a strictly lower tier. Two
    selectors sharing a (side, tier, proto) cannot both match one packet on
    that side — that pair collides and is refused before any filter is
    written — so the band term only keeps prefs distinct.
    """
    return (
        _PREF_SIDE_STRIDE * _SIDES_ORDER.index(slot.side)
        + _PREF_TIER_STRIDE * selector.tier
        + _PREF_BAND_STRIDE * band
        + _PROTOS_ORDER.index(slot.proto)
    )


_PORT_SPACE = 0x10000


@dataclass(frozen=True, slots=True)
class PortPrefix:
    """One u32 port match: ports ``p`` with ``p & mask == value`` (16-bit)."""

    value: int
    mask: int


def port_prefixes(lo: int, hi: int) -> list[PortPrefix]:
    """Minimal mask-aligned blocks covering exactly ``lo..hi``, ascending.

    Repeatedly takes the largest power-of-two block aligned at ``lo`` that
    still fits. A single port is one ``0xffff`` prefix — the same filter
    shape a one-port selector always had. At most 30 blocks for 16 bits.
    """
    if not 0 <= lo <= hi < _PORT_SPACE:
        raise ValueError(f"port range {lo}:{hi} must satisfy 0 <= START <= END <= 65535")
    out: list[PortPrefix] = []
    while lo <= hi:
        size = lo & -lo if lo else _PORT_SPACE
        while size > hi - lo + 1:
            size //= 2
        out.append(PortPrefix(lo, (_PORT_SPACE - 1) & ~(size - 1)))
        lo += size
    return out


def netem_args(params: ImpairmentParams) -> str:
    """Render *params* as netem qdisc arguments with explicit units."""
    return params.describe()


def _parse_time(token: str) -> float | None:
    m = _TIME_TOKEN.match(token)
    return float(m.group("num")) * _TIME_TO_MS[m.group("unit")] if m else None


def _parse_percent_token(token: str) -> float | None:
    m = _PERCENT_TOKEN.match(token)
    return float(m.group("num")) if m else None


def parse_qdisc_show(output: str) -> ImpairmentParams | None:
    """Parse ``tc qdisc show dev X`` output; ``None`` = no root netem qdisc."""
    for line in output.splitlines():
        tokens = line.split()
        if (
            len(tokens) >= _MIN_DELAY_TOKENS
            and tokens[0] == "qdisc"
            and tokens[1] == "netem"
            and "root" in tokens
        ):
            return _parse_netem_tokens(tokens)
    return None


def _parse_netem_tokens(tokens: list[str]) -> ImpairmentParams:
    kw: dict[str, float | str | None] = {}
    i = 0
    while i < len(tokens):
        word = tokens[i]
        if word == "delay" and i + 1 < len(tokens):
            kw["delay_ms"] = _parse_time(tokens[i + 1])
            if i + 2 < len(tokens):
                jitter = _parse_time(tokens[i + 2])
                if jitter is not None:
                    kw["jitter_ms"] = jitter
                    i += 1
            i += 2
            continue
        if word in _PERCENT_KEYWORDS and i + 1 < len(tokens):
            kw[_PERCENT_KEYWORDS[word]] = _parse_percent_token(tokens[i + 1])
            i += 2
            continue
        if word == "rate" and i + 1 < len(tokens):
            kw["rate"] = tokens[i + 1].lower()
            i += 2
            continue
        i += 1
    return ImpairmentParams(**{k: v for k, v in kw.items() if v is not None})  # ty: ignore[invalid-argument-type]


_ROOT_MIN_TOKENS = 3
_PRIO_MINOR_MIN = FIRST_SELECTOR_BAND
_PRIO_MINOR_MAX = FIRST_SELECTOR_BAND + MAX_SELECTORS - 1  # 11


def _root_tokens(output: str) -> list[str] | None:
    """Tokens of the root-qdisc line in ``tc qdisc show`` output; ``None`` = no root line."""
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) >= _ROOT_MIN_TOKENS and tokens[0] == "qdisc" and "root" in tokens:
            return tokens
    return None


def _is_our_prio_root(tokens: list[str]) -> bool:
    """Exactly our generated root: ``prio 1:`` with 11 bands and the kernel-default priomap."""
    if tokens[1] != "prio" or tokens[2] != "1:":
        return False
    try:
        bands_i = tokens.index("bands")
        priomap_i = tokens.index("priomap")
        if tokens[bands_i + 1] != str(_SCOPED_BANDS):
            return False
    except (ValueError, IndexError):
        # truncated output (e.g. a line ending in "... bands") is foreign,
        # same as any other malformed shape — never a crash (spec §9).
        return False
    priomap = " ".join(tokens[priomap_i + 1 : priomap_i + 17])
    return priomap == _KERNEL_DEFAULT_PRIOMAP


def _parse_band_leaves(output: str) -> dict[int, ImpairmentParams] | None:
    """Netem leaves under our root: ``{band: params}``; ``None`` = foreign artifact."""
    leaves: dict[int, ImpairmentParams] = {}
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < _ROOT_MIN_TOKENS or tokens[0] != "qdisc" or "root" in tokens:
            continue
        try:
            parent = tokens[tokens.index("parent") + 1]
        except (ValueError, IndexError):
            return None
        major, _, minor = parent.partition(":")
        if major != "1" or not minor:
            return None
        try:
            band = int(minor, 16)
        except ValueError:
            return None
        handle = tokens[2]
        if (
            tokens[1] != "netem"
            or not _PRIO_MINOR_MIN <= band <= _PRIO_MINOR_MAX
            or handle != f"{band:x}0:"
        ):
            return None
        leaves[band] = _parse_netem_tokens(tokens)
    return leaves


_MATCH_RE = re.compile(r"^match (?P<val>[0-9a-f]{8})/(?P<mask>[0-9a-f]{8}) at (?P<off>\d+)$")
_MATCHES_PER_BLOCK = 2
"""Exactly two u32 matches (proto @8, port @20) per selector filter block."""
_PROTO_MATCH_OFFSET = 8
_PORT_MATCH_OFFSET = 20


_FilterBlock = tuple[int, int, list[tuple[str, str, int]]]


def _parse_filter_blocks(filter_output: str) -> list[_FilterBlock] | None:
    """``(pref, flowid_band, [(val, mask, off), ...])`` per u32 block; ``None`` = foreign.

    Only ``filter ... u32`` headers that carry a ``flowid`` open a block (the
    bare and ``ht divisor`` headers carry no matches). Any non-empty line
    that fits neither shape is foreign.

    Captured live on the unix bed, iproute2 6.1.0, 2026-07-11: modern
    ``tc filter show`` prints NO ``parent 1:`` token on filter lines at all
    (only ``tc qdisc show`` echoes the parent), and prefixes the flowid with
    a bare ``*`` (``*flowid 1:4``) whenever the classid resolves into a
    ``prio`` qdisc band — those bands are implicit (never registered via
    ``tc class add``), so tc's classid lookup can't verify them and marks
    the flowid unverified. Every real scoped-tree capture hits this, so the
    ``*`` is normalized away here rather than treated as a foreign marker.

    Old-userland posture (spec §6 dual-format requirement) is also
    live-verified, not just modeled: test3's oldos image (centos:7,
    iproute2-ss170501, 2026-07-11) produces the SAME ``filter ...`` shape
    with NO ``parent 1:`` token and, notably, NO ``*`` before ``flowid`` —
    the asterisk-for-unverified-prio-class marker is a modern-iproute2-only
    addition, so the old-format hand-modeled fixtures in this test module
    (plain ``flowid``, no leading ``*``) already match live old-userland
    bytes with zero drift.
    """
    blocks: list[_FilterBlock] = []
    current: list[tuple[str, str, int]] | None = None
    for raw in filter_output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("match "):
            m = _MATCH_RE.match(line)
            if m is None or current is None:
                return None
            current.append((m.group("val"), m.group("mask"), int(m.group("off"))))
            continue
        tokens = ["flowid" if t == "*flowid" else t for t in line.split()]
        if tokens[0] != "filter" or "u32" not in tokens or "pref" not in tokens:
            return None
        current = None
        if "flowid" not in tokens:
            continue
        try:
            pref = int(tokens[tokens.index("pref") + 1])
            flowid = tokens[tokens.index("flowid") + 1]
        except (ValueError, IndexError):
            return None
        major, _, minor = flowid.partition(":")
        if major != "1" or not minor:
            return None
        try:
            band = int(minor, 16)
        except ValueError:
            return None
        current = []
        blocks.append((pref, band, current))
    return blocks


@dataclass(frozen=True, slots=True)
class _FilterEntry:
    """One decoded u32 entry of our layout."""

    slot: _Slot
    tier: int
    prefix: PortPrefix


def _port_half(side: str, word_val: int, word_mask: int) -> PortPrefix | None:
    """Return the prefix in *side*'s half of the u32 port word (at 20); ``None`` = not ours."""
    if side == "dst":
        if word_val >> 16 or word_mask >> 16:
            return None
        value, mask = word_val & 0xFFFF, word_mask & 0xFFFF
    else:
        if word_val & 0xFFFF or word_mask & 0xFFFF:
            return None
        value, mask = word_val >> 16, word_mask >> 16
    inverse = ~mask & 0xFFFF
    if mask == 0 or inverse & (inverse + 1) or value & inverse:
        return None
    return PortPrefix(value, mask)


def _decode_entry(pref: int, band: int, matches: list[tuple[str, str, int]]) -> _FilterEntry | None:
    """Validate one u32 block against the pref layout; ``None`` = not ours."""
    side_i, rest = divmod(pref, _PREF_SIDE_STRIDE)
    tier, rest = divmod(rest, _PREF_TIER_STRIDE)
    pref_band, proto_i = divmod(rest, _PREF_BAND_STRIDE)
    if (
        side_i >= len(_SIDES_ORDER)
        or tier > _MAX_TIER
        or proto_i >= len(_PROTOS_ORDER)
        or pref_band != band
        or len(matches) != _MATCHES_PER_BLOCK
    ):
        return None
    slot = _Slot(_SIDES_ORDER[side_i], _PROTOS_ORDER[proto_i])
    proto_match = next((m for m in matches if m[2] == _PROTO_MATCH_OFFSET), None)
    port_match = next((m for m in matches if m[2] == _PORT_MATCH_OFFSET), None)
    if proto_match is None or port_match is None:
        return None
    val, mask, _ = proto_match
    if mask != "00ff0000" or (int(val, 16) >> 16) & 0xFF != _PROTO_NUM[slot.proto]:
        return None
    prefix = _port_half(slot.side, int(port_match[0], 16), int(port_match[1], 16))
    return None if prefix is None else _FilterEntry(slot, tier, prefix)


def _port_span(prefixes: set[PortPrefix]) -> range | None:
    """Return the one interval *prefixes* minimally tile; ``None`` = gap, overlap or non-minimal."""
    ordered = sorted(prefixes, key=lambda p: p.value)
    lo = cursor = ordered[0].value
    for p in ordered:
        if p.value != cursor:
            return None
        cursor += (~p.mask & 0xFFFF) + 1
    if cursor > _PORT_SPACE or port_prefixes(lo, cursor - 1) != ordered:
        return None
    return range(lo, cursor)


def _selector_from_band(slots: dict[_Slot, set[PortPrefix]], tiers: set[int]) -> Selector | None:
    """Rebuild one band's Selector; ``None`` = not a shape our builders emit."""
    sides = {s.side for s in slots}
    protos = {s.proto for s in slots}
    if set(slots) != {_Slot(side, proto) for side in sides for proto in protos}:
        return None
    spans = {_port_span(prefixes) for prefixes in slots.values()}
    span = spans.pop()
    if spans or span is None:
        return None
    try:
        selector = Selector(
            span.start,
            protos.pop() if len(protos) == 1 else None,
            end=span.stop - 1,
            side=sides.pop() if len(sides) == 1 else None,
        )
    except ValueError:
        return None
    return selector if tiers == {selector.tier} else None


def parse_scoped_outputs(qdisc_output: str, filter_output: str) -> ScopedState:
    """Parse the two :meth:`NetEmImpairer.scoped_read_commands` outputs.

    Only trees otto generated parse as ``scoped``; kernel-default roots
    (handle ``0:`` / ``noqueue``) are ``clean``; a root netem is ``whole``
    (the byte-identical v1 read-back); everything else is ``foreign``.
    An otherwise-ours root with zero leaves and zero filters is ``clean`` —
    a timer race that empties the tree must not wedge exclusivity.
    """
    root = _root_tokens(qdisc_output)
    if root is None or root[1] == "noqueue" or root[2] == "0:":
        return ScopedState.clean()
    if root[1] == "netem":
        params = parse_qdisc_show(qdisc_output)
        return ScopedState.whole_link(params) if params is not None else ScopedState.foreign()
    if not _is_our_prio_root(root):
        return ScopedState.foreign()
    leaves = _parse_band_leaves(qdisc_output)
    blocks = _parse_filter_blocks(filter_output)
    if leaves is None or blocks is None:
        return ScopedState.foreign()
    if not leaves and not blocks:
        return ScopedState.clean()
    slots_by_band: dict[int, dict[_Slot, set[PortPrefix]]] = {}
    tiers_by_band: dict[int, set[int]] = {}
    for pref, band, matches in blocks:
        entry = _decode_entry(pref, band, matches)
        if entry is None:
            return ScopedState.foreign()
        prefixes = slots_by_band.setdefault(band, {}).setdefault(entry.slot, set())
        if entry.prefix in prefixes:
            return ScopedState.foreign()
        prefixes.add(entry.prefix)
        tiers_by_band.setdefault(band, set()).add(entry.tier)
    if set(slots_by_band) != set(leaves):
        return ScopedState.foreign()
    selectors: dict[Selector, tuple[int, ImpairmentParams]] = {}
    for band, slots in slots_by_band.items():
        selector = _selector_from_band(slots, tiers_by_band[band])
        # A selector colliding with an earlier one (every pair is checked once)
        # is foreign: otto refuses such a pair before writing it, and in that
        # tree band order, not scope, would pick the winner.
        if (
            selector is None
            or selector in selectors
            or any(collides(selector, other) for other in selectors)
        ):
            return ScopedState.foreign()
        selectors[selector] = (band, leaves[band])
    return ScopedState.from_selectors(selectors)


class NetEmImpairer(LinkImpairer):
    """tc/netem on a unix host's interface."""

    host_families: ClassVar[frozenset[str]] = frozenset({"unix"})
    supports_selectors: ClassVar[bool] = True

    @override
    def apply_command(self, netdev: str, params: ImpairmentParams) -> str:
        """Idempotent ``tc qdisc replace`` command applying *params* to *netdev*."""
        return f"tc qdisc replace dev {netdev} root netem {netem_args(params)}"

    @override
    def read_command(self, netdev: str) -> str:
        """``tc qdisc show`` command; output is understood by :func:`parse_qdisc_show`."""
        return f"tc qdisc show dev {netdev}"

    @override
    def clear_command(self, netdev: str) -> str:
        """``tc qdisc del`` command removing the root netem qdisc from *netdev*."""
        return f"tc qdisc del dev {netdev} root"

    @override
    def parse_read(self, output: str) -> ImpairmentParams | None:
        """Parse :meth:`read_command` output via :func:`parse_qdisc_show`."""
        return parse_qdisc_show(output)

    @override
    def scoped_root_command(self, netdev: str) -> str:
        """Idempotent 11-band prio root; bands 1-3 keep kernel-default semantics."""
        return (
            f"tc qdisc replace dev {netdev} root handle 1: "
            f"prio bands {_SCOPED_BANDS} priomap {_KERNEL_DEFAULT_PRIOMAP}"
        )

    @override
    def scoped_band_command(self, netdev: str, band: int, params: ImpairmentParams) -> str:
        """Idempotent netem leaf for *band*. classid/handle minors are HEX."""
        return (
            f"tc qdisc replace dev {netdev} parent 1:{band:x} "
            f"handle {band:x}0: netem {netem_args(params)}"
        )

    @override
    def scoped_filter_commands(self, netdev: str, band: int, selector: Selector) -> list[str]:
        """u32 entries steering *selector* into *band*: one per port prefix per slot.

        Every entry of a slot shares that slot's single pref, so one
        ``tc filter del … pref P`` removes the whole slot.
        """
        prefixes = port_prefixes(selector.port, selector.last)
        return [
            f"tc filter add dev {netdev} parent 1: pref {_filter_pref(selector, band, slot)} "
            f"protocol ip u32 match ip protocol {_PROTO_NUM[slot.proto]} 0xff "
            f"match ip {_SIDE_MATCH[slot.side]} {prefix.value} 0x{prefix.mask:04x} "
            f"flowid 1:{band:x}"
            for slot in _selector_slots(selector)
            for prefix in prefixes
        ]

    @override
    def scoped_clear_selector_commands(
        self, netdev: str, band: int, selector: Selector
    ) -> list[str]:
        """Delete *selector*'s slots (one ``tc filter del`` per pref) then its band's leaf."""
        cmds = [
            f"tc filter del dev {netdev} parent 1: pref {_filter_pref(selector, band, slot)} "
            "protocol ip u32"
            for slot in _selector_slots(selector)
        ]
        cmds.append(f"tc qdisc del dev {netdev} parent 1:{band:x} handle {band:x}0:")
        return cmds

    @override
    def scoped_read_commands(self, netdev: str) -> list[str]:
        """Qdisc + filter reads for :meth:`parse_scoped`.

        The filter read is guarded (``2>/dev/null || true``) belt-and-braces:
        captured live on the unix bed, iproute2 6.1.0, 2026-07-11,
        ``tc filter show ... parent 1:`` on a netdev with no ``1:`` parent
        (every clean or whole-link netdev) does NOT error — it exits 0 with
        empty stdout. The guard is kept anyway for older/other iproute2
        builds where this call is documented to fail; either way the read
        path must treat empty/absent output as 'no filters', not a host
        error.
        """
        return [
            f"tc qdisc show dev {netdev}",
            f"tc filter show dev {netdev} parent 1: 2>/dev/null || true",
        ]

    @override
    def parse_scoped(self, qdisc_output: str, filter_output: str) -> ScopedState:
        """Parse :meth:`scoped_read_commands` outputs via :func:`parse_scoped_outputs`."""
        return parse_scoped_outputs(qdisc_output, filter_output)


register_impairer("netem", NetEmImpairer)
