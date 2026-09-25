"""Shared core of otto's setup-time checks (``otto link check``, ``otto tunnel check``).

Verdicts, the host fingerprint, the proven-range comparison, the stdout
renderer and the ``--report`` JSON writer live here so both checks speak one
vocabulary. This package imports neither ``otto.link`` nor ``otto.tunnel``:
those two stay decoupled from each other, and each builds on this core.
"""

from .errors import CheckCommandFailedError, CheckHostUnreachableError
from .fingerprint import (
    CHECK_HOST_TIMEOUT,
    LINK_TOOLS,
    LINK_VERSIONS,
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
)
from .render import CheckRow, CheckSection, render_sections, section_counts
from .report import REPORT_SCHEMA, report_to_json
from .verdict import FeatureResult, UnmeasuredReason, Verdict, count_verdicts

__all__ = [
    "CHECK_HOST_TIMEOUT",
    "LINK_TOOLS",
    "LINK_VERSIONS",
    "REPORT_SCHEMA",
    "CheckCommandFailedError",
    "CheckHostUnreachableError",
    "CheckRow",
    "CheckSection",
    "FeatureResult",
    "HostFingerprint",
    "ProvenEntry",
    "ProvenRange",
    "RangeLabel",
    "UnmeasuredReason",
    "Verdict",
    "check_exec",
    "count_verdicts",
    "fingerprint_command",
    "label_against_range",
    "load_proven_range",
    "parse_fingerprint",
    "probe_fingerprint",
    "render_sections",
    "report_to_json",
    "section_counts",
]
