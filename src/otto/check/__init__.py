"""Shared core of otto's setup-time checks (``otto link check``, ``otto tunnel check``).

Verdicts, the host fingerprint, the proven-range comparison, the stdout
renderer, the ``--report`` JSON writer and the leftover sweep's age rule
(:mod:`otto.check.sweep`) live here so both checks speak one vocabulary.
This package imports neither ``otto.link`` nor ``otto.tunnel``: those two
stay decoupled from each other, and each builds on this core.
"""

from .errors import CheckCommandFailedError, CheckHostUnreachableError
from .fingerprint import (
    CHECK_HOST_TIMEOUT,
    LINK_TOOLS,
    LINK_VERSIONS,
    TUNNEL_TOOLS,
    TUNNEL_VERSIONS,
    HostFingerprint,
    check_exec,
    fingerprint_command,
    parse_fingerprint,
    probe_fingerprint,
)
from .proven import (
    ProvenEntry,
    ProvenRange,
    RangeLabel,
    label_against_range,
    load_proven_range,
    range_labels,
)
from .render import CheckRow, CheckSection, render_sections, section_counts
from .report import REPORT_SCHEMA, report_to_json
from .sweep import SWEEP_MIN_AGE_S
from .verdict import FeatureResult, ReportVerdicts, UnmeasuredReason, Verdict, count_verdicts

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
