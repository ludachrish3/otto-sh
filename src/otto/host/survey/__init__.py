"""The protocol survey behind ``otto host <id> probe``.

Spec: ``docs/superpowers/specs/2026-09-14-host-probe-protocol-survey-design.md``.
Imported only inside the verb, so the ``host`` import surface stays flat.
"""

from .engine import Survey, run_survey
from .report import survey_report
from .verdict import ProtocolVerdict

__all__ = ["ProtocolVerdict", "Survey", "run_survey", "survey_report"]
