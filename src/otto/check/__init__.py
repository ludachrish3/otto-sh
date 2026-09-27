"""Shared core of otto's setup-time checks (``otto link check``, ``otto tunnel check``).

Verdicts, the host fingerprint, the proven-range comparison, the stdout
renderer, the ``--report`` JSON writer and the leftover sweep's age rule
(:mod:`otto.check.sweep`) live here so both checks speak one vocabulary.
This package imports neither ``otto.link`` nor ``otto.tunnel``: those two
stay decoupled from each other, and each builds on this core.

Every name is exported lazily (PEP 562), the shape every otto package shares:
a check that takes only the verdict vocabulary does not load the renderer,
the report writer or the proven-range reader. The resolver does not write a
resolved name back into the module dict; see ``otto.config``'s ``__dir__``
for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .errors import CheckCommandFailedError as CheckCommandFailedError
    from .errors import CheckHostUnreachableError as CheckHostUnreachableError
    from .fingerprint import CHECK_HOST_TIMEOUT as CHECK_HOST_TIMEOUT
    from .fingerprint import LINK_TOOLS as LINK_TOOLS
    from .fingerprint import LINK_VERSIONS as LINK_VERSIONS
    from .fingerprint import TUNNEL_TOOLS as TUNNEL_TOOLS
    from .fingerprint import TUNNEL_VERSIONS as TUNNEL_VERSIONS
    from .fingerprint import HostFingerprint as HostFingerprint
    from .fingerprint import check_exec as check_exec
    from .fingerprint import fingerprint_command as fingerprint_command
    from .fingerprint import parse_fingerprint as parse_fingerprint
    from .fingerprint import probe_fingerprint as probe_fingerprint
    from .proven import ProvenEntry as ProvenEntry
    from .proven import ProvenRange as ProvenRange
    from .proven import RangeLabel as RangeLabel
    from .proven import label_against_range as label_against_range
    from .proven import load_proven_range as load_proven_range
    from .proven import range_labels as range_labels
    from .render import CheckRow as CheckRow
    from .render import CheckSection as CheckSection
    from .render import render_sections as render_sections
    from .render import section_counts as section_counts
    from .report import REPORT_SCHEMA as REPORT_SCHEMA
    from .report import report_to_json as report_to_json
    from .sweep import SWEEP_MIN_AGE_S as SWEEP_MIN_AGE_S
    from .verdict import FeatureResult as FeatureResult
    from .verdict import ReportVerdicts as ReportVerdicts
    from .verdict import UnmeasuredReason as UnmeasuredReason
    from .verdict import Verdict as Verdict
    from .verdict import count_verdicts as count_verdicts

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "CheckCommandFailedError": "otto.check.errors",
    "CheckHostUnreachableError": "otto.check.errors",
    "CHECK_HOST_TIMEOUT": "otto.check.fingerprint",
    "HostFingerprint": "otto.check.fingerprint",
    "LINK_TOOLS": "otto.check.fingerprint",
    "LINK_VERSIONS": "otto.check.fingerprint",
    "TUNNEL_TOOLS": "otto.check.fingerprint",
    "TUNNEL_VERSIONS": "otto.check.fingerprint",
    "check_exec": "otto.check.fingerprint",
    "fingerprint_command": "otto.check.fingerprint",
    "parse_fingerprint": "otto.check.fingerprint",
    "probe_fingerprint": "otto.check.fingerprint",
    "ProvenEntry": "otto.check.proven",
    "ProvenRange": "otto.check.proven",
    "RangeLabel": "otto.check.proven",
    "label_against_range": "otto.check.proven",
    "load_proven_range": "otto.check.proven",
    "range_labels": "otto.check.proven",
    "CheckRow": "otto.check.render",
    "CheckSection": "otto.check.render",
    "render_sections": "otto.check.render",
    "section_counts": "otto.check.render",
    "REPORT_SCHEMA": "otto.check.report",
    "report_to_json": "otto.check.report",
    "SWEEP_MIN_AGE_S": "otto.check.sweep",
    "FeatureResult": "otto.check.verdict",
    "ReportVerdicts": "otto.check.verdict",
    "UnmeasuredReason": "otto.check.verdict",
    "Verdict": "otto.check.verdict",
    "count_verdicts": "otto.check.verdict",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.check's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "CHECK_HOST_TIMEOUT",
    "LINK_TOOLS",
    "LINK_VERSIONS",
    "REPORT_SCHEMA",
    "SWEEP_MIN_AGE_S",
    "TUNNEL_TOOLS",
    "TUNNEL_VERSIONS",
    "CheckCommandFailedError",
    "CheckHostUnreachableError",
    "CheckRow",
    "CheckSection",
    "FeatureResult",
    "HostFingerprint",
    "ProvenEntry",
    "ProvenRange",
    "RangeLabel",
    "ReportVerdicts",
    "UnmeasuredReason",
    "Verdict",
    "check_exec",
    "count_verdicts",
    "fingerprint_command",
    "label_against_range",
    "load_proven_range",
    "parse_fingerprint",
    "probe_fingerprint",
    "range_labels",
    "render_sections",
    "report_to_json",
    "section_counts",
]
