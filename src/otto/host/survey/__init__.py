"""The protocol survey behind ``otto host <id> probe``.

Spec: ``docs/superpowers/specs/2026-09-14-host-probe-protocol-survey-design.md``.
Imported only inside the verb, so the ``host`` import surface stays flat.

Every name is exported lazily (PEP 562), the shape every otto package shares:
the verb's ``--scan-ports`` parser, ``otto.host.survey.sweep``, loads without
the survey engine. The resolver does not write a resolved name back into the
module dict; see ``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Survey as Survey
    from .engine import run_survey as run_survey
    from .report import survey_report as survey_report
    from .verdict import ProtocolVerdict as ProtocolVerdict

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "Survey": "otto.host.survey.engine",
    "run_survey": "otto.host.survey.engine",
    "survey_report": "otto.host.survey.report",
    "ProtocolVerdict": "otto.host.survey.verdict",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.host.survey's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "ProtocolVerdict",
    "Survey",
    "run_survey",
    "survey_report",
]
