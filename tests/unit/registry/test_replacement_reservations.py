"""Replacing a built-in reservation backend reaches the real product path.

``gate_from_settings`` is what every reservation gate is built through; a
backend registered over a built-in name with ``overwrite=True`` must be what
it builds for a table naming it (for ``none``, the absent table). The root
isolation fixture restores the table.
"""

import pytest

from otto.models.base import OttoModel
from otto.reservations.factory import gate_from_settings
from otto.reservations.registry import RESERVATION_BACKENDS, register_reservation_backend

REPLACES = [
    ("otto.reservations.registry:RESERVATION_BACKENDS", "none"),
    ("otto.reservations.registry:RESERVATION_BACKENDS", "json"),
]
"""Every ``(table, built-in name)`` pair this module replaces."""


class _Config(OttoModel, extra="allow", frozen=True):
    """Accepts whatever keys a built-in's table carries, so the spy can stand in for it."""


class _SpyBackend:
    def backend_name(self):
        return "spy"

    def fetch_reservations(self, username, start=None, end=None):
        return []


def test_replaces_names_every_built_in_reservation_backend():
    builtins = {
        ("otto.reservations.registry:RESERVATION_BACKENDS", name)
        for name in RESERVATION_BACKENDS.names()
        if RESERVATION_BACKENDS.origin(name).startswith("otto.")
    }
    assert set(REPLACES) == builtins


@pytest.mark.parametrize(
    ("name", "table"),
    [
        ("none", {}),
        ("json", {"backend": "json", "json": {"path": "reservations.json"}}),
    ],
)
def test_a_replaced_backend_is_what_the_gate_builds(tmp_path, name, table):
    spy = _SpyBackend()
    register_reservation_backend(name, config=_Config, factory=lambda c: spy, overwrite=True)
    gate = gate_from_settings(table, tmp_path, holder="me", skip_reservation_check=False)
    assert gate.backend is spy
