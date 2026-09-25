"""The ``--report`` JSON a user attaches to an issue (spec 2026-09-24 §3.6)."""

import dataclasses
import enum
import json

from ..version import get_version
from .proven import load_proven_range

REPORT_SCHEMA = "otto-check/1"


def _default(value: object) -> object:
    if isinstance(value, enum.Enum):
        return value.value
    return str(value)


def report_to_json(result: object, *, kind: str) -> str:
    """Serialise one check result (a dataclass instance) with the schema envelope."""
    if not dataclasses.is_dataclass(result) or isinstance(result, type):
        raise TypeError("report_to_json needs a dataclass instance")
    doc = {
        "schema": REPORT_SCHEMA,
        "kind": kind,
        "otto_version": get_version(),
        "proven_revision": load_proven_range().revision,
        "result": dataclasses.asdict(result),
    }
    return json.dumps(doc, indent=2, default=_default) + "\n"
