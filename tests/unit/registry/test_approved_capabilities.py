"""The shrink-only approved-capabilities list equals the live declarations."""

from tests.unit.registry.approved_capabilities import APPROVED
from tests.unit.test_registry_loading import (
    _import_every_module_that_builds_a_registry,
    _otto_registries,
)


def live_capabilities() -> dict[str, list[tuple[str, str]]]:
    """``"module:ATTR" -> [(capability type name, reason)]`` for every otto table with any."""
    bound = {
        (row.module, row.kind): row.bound for row in _import_every_module_that_builds_a_registry()
    }
    live: dict[str, list[tuple[str, str]]] = {}
    for table in _otto_registries():
        declared = [
            (type(j.capability).__name__, j.reason) for j in getattr(table, "capabilities", [])
        ]
        if declared:
            live[f"{table.defined_in}:{bound[(table.defined_in, table.kind)]}"] = declared
    return live


def test_the_approved_list_equals_the_live_capabilities():
    """Red: add a fake entry to APPROVED."""
    assert live_capabilities() == APPROVED


def test_no_approved_reason_is_blank():
    blank = [key for key, pairs in APPROVED.items() for _, reason in pairs if not reason.strip()]
    assert blank == []
