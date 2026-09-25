"""Render NetEm builder output as ``tc … show`` text — the shape read-back parses.

Tests use this to stage a fake host's reads for any selector mapping without
hand-writing u32 hex. It renders the no-``parent``-token, plain-``flowid``
form the old-userland capture showed; the modern ``*flowid`` form is pinned by
the live captures in ``test_netem.py``.
"""

import re
from dataclasses import dataclass

from otto.link.netem import NetEmImpairer, netem_args
from otto.link.params import ImpairmentParams, Selector

ROOT_LINE = "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"

_ADD_RE = re.compile(
    r"pref (?P<pref>\d+) protocol ip u32 match ip protocol (?P<proto>\d+) 0xff "
    r"match ip (?P<field>dport|sport) (?P<val>\d+) 0x(?P<mask>[0-9a-f]{4}) "
    r"flowid 1:(?P<band>[0-9a-f]+)$"
)


@dataclass(frozen=True)
class ScopedTree:
    """The two read-back outputs for one netdev."""

    qdisc: str
    filters: str


def render_scoped_tree(mapping: dict[Selector, tuple[int, ImpairmentParams]]) -> ScopedTree:
    """``tc qdisc show`` + ``tc filter show`` text for *mapping* (selector -> band, params)."""
    imp = NetEmImpairer()
    leaves = sorted(mapping.values(), key=lambda band_params: band_params[0])
    qdisc = ROOT_LINE + "".join(
        f"qdisc netem {band:x}0: parent 1:{band:x} limit 1000 {netem_args(params)}\n"
        for band, params in leaves
    )
    blocks: list[str] = []
    for selector, (band, _params) in mapping.items():
        for cmd in imp.scoped_filter_commands("eth1.100", band, selector):
            m = _ADD_RE.search(cmd)
            assert m is not None, cmd
            val, mask = int(m["val"]), int(m["mask"], 16)
            port = (
                f"0000{val:04x}/0000{mask:04x}"
                if m["field"] == "dport"
                else f"{val:04x}0000/{mask:04x}0000"
            )
            blocks.append(
                f"filter protocol ip pref {m['pref']} u32 "
                f"fh 800::{0x800 + len(blocks):x} flowid 1:{m['band']}\n"
                f"  match {int(m['proto']):04x}0000/00ff0000 at 8\n"
                f"  match {port} at 20\n"
            )
    return ScopedTree(qdisc, "".join(blocks))
