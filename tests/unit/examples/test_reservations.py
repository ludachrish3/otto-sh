"""Behavior + conformance for the ExampleReservationBackend reference backend."""

from otto.examples.reservations import ExampleReservationBackend
from otto.reservations import SupportsUsernameCompletion, register_reservation_backend
from otto.reservations.registry import RESERVATION_BACKENDS
from otto.testing import assert_reservation_backend_conforms


def test_backend_name_stable():
    backend = ExampleReservationBackend()
    assert backend.backend_name() == "example"
    assert backend.backend_name() == backend.backend_name()


def test_get_reserved_resources_is_a_str_set():
    backend = ExampleReservationBackend()
    assert backend.get_reserved_resources("alice") == {"lab-a", "shared"}
    assert backend.get_reserved_resources("nobody") == set()


def test_who_reserved_multi_holder_sorted():
    backend = ExampleReservationBackend()
    # "shared" is held by both alice and bob — deterministic, deduped.
    assert backend.who_reserved("shared") == ["alice", "bob"]
    assert backend.who_reserved("lab-a") == ["alice"]
    assert backend.who_reserved("unheld") == []


def test_implements_username_completion():
    backend = ExampleReservationBackend()
    assert isinstance(backend, SupportsUsernameCompletion)
    assert backend.list_usernames() == ["alice", "bob"]


def test_custom_dataset_overrides_demo():
    backend = ExampleReservationBackend(reservations={"carol": ["x"]})
    assert backend.list_usernames() == ["carol"]
    assert backend.who_reserved("x") == ["carol"]


def test_accepts_url_for_factory_uniformity():
    # build_backend may call cls(url=url, **kwargs).
    backend = ExampleReservationBackend(url="https://example")
    assert backend.backend_name() == "example"


def test_sample_conforms_with_round_trip_and_capability():
    assert_reservation_backend_conforms(
        ExampleReservationBackend(),
        known_user="alice",
        known_resources=["lab-a", "shared"],
    )


def test_registrable_by_name():
    register_reservation_backend("example-reservations-test", ExampleReservationBackend)
    try:
        assert RESERVATION_BACKENDS.get("example-reservations-test") is ExampleReservationBackend
    finally:
        RESERVATION_BACKENDS.unregister("example-reservations-test")
