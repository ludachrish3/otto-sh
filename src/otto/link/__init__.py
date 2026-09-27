"""The link subsystem: the unified ``Link`` edge model and its derivations.

Model, static derivation, and id computation for the static link layer
(implicit hop edges, declared routes). Live tunnel creation/discovery lives
in ``otto.tunnel``.

Every name is exported lazily (PEP 562). ``otto.models.host`` imports
``IMPAIRERS`` from here at module scope (a host spec's impairer name is checked
against it), so every command that loads the host models imports this package
and ``.impairer`` with the ``.params`` it builds on. That import alone loads
nothing else: not the edge model, the placement layer or the netem builders
(a command that loads a lab does load ``.model`` and ``.derive``, for the
lab's links), and never ``.manage`` (``otto.host.daemon``,
``otto.link.sentinel``) or ``.check`` (``otto.check``'s probe machinery), which
only ``otto link`` itself calls. The built-in netem impairer is registered by
reference in ``.impairer``, so no import here exists for its side effect. The
resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .check import LinkCheckReport as LinkCheckReport
    from .check import check_link as check_link
    from .impairer import IMPAIRERS as IMPAIRERS
    from .impairer import LinkImpairer as LinkImpairer
    from .impairer import ScopedState as ScopedState
    from .impairer import build_impairer as build_impairer
    from .impairer import register_impairer as register_impairer
    from .manage import AppliedPlacement as AppliedPlacement
    from .manage import DirectionState as DirectionState
    from .manage import DryRunPlan as DryRunPlan
    from .manage import ImpairReport as ImpairReport
    from .manage import LinkCommandFailedError as LinkCommandFailedError
    from .manage import LinkHostUnreachableError as LinkHostUnreachableError
    from .manage import LinkNotMeasuredError as LinkNotMeasuredError
    from .manage import LinkState as LinkState
    from .manage import RepairAllReport as RepairAllReport
    from .manage import RepairReport as RepairReport
    from .manage import find_link as find_link
    from .manage import impair_link as impair_link
    from .manage import read_link_states as read_link_states
    from .manage import repair_all as repair_all
    from .manage import repair_link as repair_link
    from .model import Link as Link
    from .model import LinkEndpoint as LinkEndpoint
    from .model import Provenance as Provenance
    from .model import make_link_id as make_link_id
    from .model import make_static_link_id as make_static_link_id
    from .netem import NetEmImpairer as NetEmImpairer
    from .params import ImpairmentParams as ImpairmentParams
    from .params import Selector as Selector
    from .params import canonical_key as canonical_key
    from .params import equivalent as equivalent
    from .params import parse_percent as parse_percent
    from .params import parse_rate as parse_rate
    from .params import parse_time_ms as parse_time_ms
    from .placement import BOTH_DIRECTIONS as BOTH_DIRECTIONS
    from .placement import FlowDirection as FlowDirection
    from .placement import Placement as Placement
    from .placement import impairment_refusal as impairment_refusal

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "LinkCheckReport": "otto.link.check",
    "check_link": "otto.link.check",
    "IMPAIRERS": "otto.link.impairer",
    "LinkImpairer": "otto.link.impairer",
    "ScopedState": "otto.link.impairer",
    "build_impairer": "otto.link.impairer",
    "register_impairer": "otto.link.impairer",
    "AppliedPlacement": "otto.link.manage",
    "DirectionState": "otto.link.manage",
    "DryRunPlan": "otto.link.manage",
    "ImpairReport": "otto.link.manage",
    "LinkCommandFailedError": "otto.link.manage",
    "LinkHostUnreachableError": "otto.link.manage",
    "LinkNotMeasuredError": "otto.link.manage",
    "LinkState": "otto.link.manage",
    "RepairAllReport": "otto.link.manage",
    "RepairReport": "otto.link.manage",
    "find_link": "otto.link.manage",
    "impair_link": "otto.link.manage",
    "read_link_states": "otto.link.manage",
    "repair_all": "otto.link.manage",
    "repair_link": "otto.link.manage",
    "Link": "otto.link.model",
    "LinkEndpoint": "otto.link.model",
    "Provenance": "otto.link.model",
    "make_link_id": "otto.link.model",
    "make_static_link_id": "otto.link.model",
    "NetEmImpairer": "otto.link.netem",
    "ImpairmentParams": "otto.link.params",
    "Selector": "otto.link.params",
    "canonical_key": "otto.link.params",
    "equivalent": "otto.link.params",
    "parse_percent": "otto.link.params",
    "parse_rate": "otto.link.params",
    "parse_time_ms": "otto.link.params",
    "BOTH_DIRECTIONS": "otto.link.placement",
    "FlowDirection": "otto.link.placement",
    "Placement": "otto.link.placement",
    "impairment_refusal": "otto.link.placement",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.link's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "BOTH_DIRECTIONS",
    "IMPAIRERS",
    "AppliedPlacement",
    "DirectionState",
    "DryRunPlan",
    "FlowDirection",
    "ImpairReport",
    "ImpairmentParams",
    "Link",
    "LinkCheckReport",
    "LinkCommandFailedError",
    "LinkEndpoint",
    "LinkHostUnreachableError",
    "LinkImpairer",
    "LinkNotMeasuredError",
    "LinkState",
    "NetEmImpairer",
    "Placement",
    "Provenance",
    "RepairAllReport",
    "RepairReport",
    "ScopedState",
    "Selector",
    "build_impairer",
    "canonical_key",
    "check_link",
    "equivalent",
    "find_link",
    "impair_link",
    "impairment_refusal",
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
