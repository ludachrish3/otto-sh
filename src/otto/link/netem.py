"""NetEm — the first-party ``LinkImpairer`` (tc qdisc netem on unix hosts).

argv builders ALWAYS emit explicit units (spec §3.1) — tc's bare-number
semantics vary by parameter and iproute2 version. The parser reads
``tc qdisc show dev X`` back into :class:`~otto.link.params.ImpairmentParams`
(kernel qdisc config is the only state — spec §6) and tolerates both modern
and old iproute2 formatting (``50ms`` vs ``50.0ms``).
"""

import re
from typing import ClassVar

from typing_extensions import override

from .impairer import (
    FIRST_SELECTOR_BAND,
    MAX_SELECTORS,
    LinkImpairer,
    ScopedState,
    register_impairer,
)
from .params import ImpairmentParams, Selector

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

_SLOTS: tuple[tuple[str, str], ...] = (
    ("dport", "tcp"),
    ("sport", "tcp"),
    ("dport", "udp"),
    ("sport", "udp"),
)
"""Fixed per-selector pref-slot order (spec §2): pref = band*10 + slot index."""

_PROTO_NUM = {"tcp": 6, "udp": 17}


def _selector_slots(selector: Selector) -> list[int]:
    """Return the pref-slot indices *selector* occupies (2 for one proto, 4 for both)."""
    return [i for i, (_side, proto) in enumerate(_SLOTS) if selector.proto in (None, proto)]


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
_SLOT_COUNT = len(_SLOTS)
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

    Captured live on the veggies bed, iproute2 6.1.0, 2026-07-11: modern
    ``tc filter show`` prints NO ``parent 1:`` token on filter lines at all
    (only ``tc qdisc show`` echoes the parent), and prefixes the flowid with
    a bare ``*`` (``*flowid 1:4``) whenever the classid resolves into a
    ``prio`` qdisc band — those bands are implicit (never registered via
    ``tc class add``), so tc's classid lookup can't verify them and marks
    the flowid unverified. Every real scoped-tree capture hits this, so the
    ``*`` is normalized away here rather than treated as a foreign marker.

    Old-userland posture (spec §6 dual-format requirement) is also
    live-verified, not just modeled: pepper_seed's oldos image (centos:7,
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


def _selector_from_slots(slots: dict[int, int]) -> Selector | None:
    """Rebuild the band's Selector from ``{slot: port}``; ``None`` = not our shape."""
    if len(set(slots.values())) != 1:
        return None
    port = next(iter(slots.values()))
    present = frozenset(slots)
    proto_by_slots = {
        frozenset({0, 1, 2, 3}): None,
        frozenset({0, 1}): "tcp",
        frozenset({2, 3}): "udp",
    }
    if present not in proto_by_slots:
        return None
    try:
        return Selector(port, proto_by_slots[present])
    except ValueError:
        return None


def _decode_block(
    pref: int, band: int, matches: list[tuple[str, str, int]]
) -> tuple[int, int] | None:
    """Validate one u32 block against our conventions; return ``(slot, port)``."""
    slot = pref - band * 10
    if not 0 <= slot < _SLOT_COUNT or len(matches) != _MATCHES_PER_BLOCK:
        return None
    side, proto = _SLOTS[slot]
    proto_match = next((m for m in matches if m[2] == _PROTO_MATCH_OFFSET), None)
    port_match = next((m for m in matches if m[2] == _PORT_MATCH_OFFSET), None)
    if proto_match is None or port_match is None:
        return None
    val, mask, _ = proto_match
    if mask != "00ff0000" or (int(val, 16) >> 16) & 0xFF != _PROTO_NUM[proto]:
        return None
    val, mask, _ = port_match
    if side == "dport" and mask == "0000ffff":
        return slot, int(val, 16) & 0xFFFF
    if side == "sport" and mask == "ffff0000":
        return slot, int(val, 16) >> 16
    return None


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
    slots_by_band: dict[int, dict[int, int]] = {}
    for pref, band, matches in blocks:
        decoded = _decode_block(pref, band, matches)
        if decoded is None or (band in slots_by_band and decoded[0] in slots_by_band[band]):
            return ScopedState.foreign()
        slots_by_band.setdefault(band, {})[decoded[0]] = decoded[1]
    if set(slots_by_band) != set(leaves):
        return ScopedState.foreign()
    selectors: dict[Selector, tuple[int, ImpairmentParams]] = {}
    for band, slots in slots_by_band.items():
        selector = _selector_from_slots(slots)
        if selector is None or selector in selectors:
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
        """u32 filters steering *selector* into band *band*, fixed pref-slot order."""
        cmds: list[str] = []
        for slot in _selector_slots(selector):
            side, proto = _SLOTS[slot]
            cmds.append(
                f"tc filter add dev {netdev} parent 1: pref {band * 10 + slot} "
                f"protocol ip u32 match ip protocol {_PROTO_NUM[proto]} 0xff "
                f"match ip {side} {selector.port} 0xffff flowid 1:{band:x}"
            )
        return cmds

    @override
    def scoped_clear_selector_commands(
        self, netdev: str, band: int, selector: Selector
    ) -> list[str]:
        """Delete *selector*'s filters (by pref) then its band's netem leaf."""
        cmds = [
            f"tc filter del dev {netdev} parent 1: pref {band * 10 + slot} protocol ip u32"
            for slot in _selector_slots(selector)
        ]
        cmds.append(f"tc qdisc del dev {netdev} parent 1:{band:x} handle {band:x}0:")
        return cmds

    @override
    def scoped_read_commands(self, netdev: str) -> list[str]:
        """Qdisc + filter reads for :meth:`parse_scoped`.

        The filter read is guarded (``2>/dev/null || true``) belt-and-braces:
        captured live on the veggies bed, iproute2 6.1.0, 2026-07-11,
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
