"""Unit tests for the reservation backend registry."""

import pytest

from otto.reservations import (
    JsonReservationBackend,
    NullReservationBackend,
    register_reservation_backend,
)
from otto.reservations.registry import (
    _RESERVATION_BACKENDS,
    get_reservation_backend_class,
)


def test_builtins_registered():
    assert get_reservation_backend_class("none") is NullReservationBackend
    assert get_reservation_backend_class("json") is JsonReservationBackend


def test_register_and_lookup():
    class MyBackend:
        def get_reserved_resources(self, username):
            return set()

        def who_reserved(self, resource):
            return []

        def backend_name(self):
            return "mine"

    register_reservation_backend("mine-test", MyBackend)
    try:
        assert get_reservation_backend_class("mine-test") is MyBackend
    finally:
        _RESERVATION_BACKENDS.pop("mine-test", None)


def test_unknown_name_lists_registered():
    with pytest.raises(ValueError, match="Unknown reservation backend"):
        get_reservation_backend_class("does-not-exist")
