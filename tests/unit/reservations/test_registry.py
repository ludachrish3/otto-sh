"""Unit tests for the reservation backend registry."""

import pytest

from otto.models.base import OttoModel
from otto.registry import DuplicateRegistration
from otto.reservations import (
    JsonReservationConfig,
    NoneReservationConfig,
    register_reservation_backend,
)
from otto.reservations.json_backend import _json_reservations
from otto.reservations.null_backend import _none_reservations
from otto.reservations.registry import RESERVATION_BACKENDS


class _Cfg(OttoModel, frozen=True):
    pass


class _Mine:
    def fetch_reservations(self, username, start=None, end=None):
        return []

    def backend_name(self):
        return "mine"


def _mine(c):
    return _Mine()


def test_the_builtins_resolve_to_their_config_models_and_factories():
    none, json = RESERVATION_BACKENDS.get("none"), RESERVATION_BACKENDS.get("json")
    assert (none.config, none.factory) == (NoneReservationConfig, _none_reservations)
    assert (json.config, json.factory) == (JsonReservationConfig, _json_reservations)


def test_register_and_lookup():
    register_reservation_backend("mine-test", config=_Cfg, factory=_mine)
    entry = RESERVATION_BACKENDS.get("mine-test")
    assert (entry.config, entry.factory) == (_Cfg, _mine)
    assert RESERVATION_BACKENDS.origin("mine-test") == __name__


def test_a_taken_name_needs_overwrite():
    with pytest.raises(DuplicateRegistration, match="json"):
        register_reservation_backend("json", config=_Cfg, factory=_mine)
    register_reservation_backend("json", config=_Cfg, factory=_mine, overwrite=True)
    assert RESERVATION_BACKENDS.get("json").factory is _mine


def test_builtins_are_named_and_peekable():
    assert "none" in RESERVATION_BACKENDS
    assert RESERVATION_BACKENDS.peek("json") is not None
