"""The link subsystem: the unified ``Link`` edge model and its derivations.

Model, static derivation, and id computation for the static link layer
(implicit hop edges, declared routes). Live tunnel creation/discovery lives
in ``otto.tunnel``.
"""

from typing import TYPE_CHECKING

from .impairer import IMPAIRERS, LinkImpairer, build_impairer, register_impairer
from .model import Link, LinkEndpoint, Provenance, make_link_id, make_static_link_id
from .netem import NetEmImpairer
from .params import (
    ImpairmentParams,
    canonical_key,
    equivalent,
    parse_percent,
    parse_rate,
    parse_time_ms,
)
from .placement import FlowDirection, Placement

# .manage pulls in otto.host.detached (+ otto.link.sentinel); only the future
# `otto link` CLI actually calls it. Every other importer of otto.link (8 of
# the 9 CLI surfaces, via otto.models.host -> otto.link.IMPAIRERS) never
# touches these names, so re-export them lazily to keep those surfaces out of
# manage's import weight (reviewer finding on Task 8, 2026-07-10).
if TYPE_CHECKING:
    from .manage import (
        AppliedPlacement,
        ImpairReport,
        LinkState,
        RepairReport,
        find_link,
        impair_link,
        read_link_states,
        repair_all,
        repair_link,
    )

__all__ = [
    "IMPAIRERS",
    "AppliedPlacement",
    "FlowDirection",
    "ImpairReport",
    "ImpairmentParams",
    "Link",
    "LinkEndpoint",
    "LinkImpairer",
    "LinkState",
    "NetEmImpairer",
    "Placement",
    "Provenance",
    "RepairReport",
    "build_impairer",
    "canonical_key",
    "equivalent",
    "find_link",
    "impair_link",
    "make_link_id",
    "make_static_link_id",
    "parse_percent",
    "parse_rate",
    "parse_time_ms",
    "read_link_states",
    "register_impairer",
    "repair_all",
    "repair_link",
]

_MANAGE_NAMES = frozenset(
    {
        "AppliedPlacement",
        "ImpairReport",
        "LinkState",
        "RepairReport",
        "find_link",
        "impair_link",
        "read_link_states",
        "repair_all",
        "repair_link",
    }
)


def __getattr__(name: str) -> object:
    """Lazily resolve the ``.manage`` orchestration API on first access.

    Keeps ``otto.host.detached``/``otto.link.sentinel`` off every otto.link
    importer that only wants the model/impairer/placement layer.
    """
    if name in _MANAGE_NAMES:
        from . import manage

        return getattr(manage, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
