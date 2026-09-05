"""Unit tests for ReservationBackendBase — the official base class for backends.

The Protocol in ``otto.reservations.protocol`` stays the contract otto is
written against; the base class is the recommended way to satisfy it. These
tests pin what inheriting buys an implementer: a missing method fails at
instantiation naming that method, a complete subclass satisfies the runtime
Protocol and the conformance helper, the constructor keeps what the factory
passes, and the optional capabilities stay structural.
"""

from pathlib import Path

import pytest

from otto.reservations import (
    ReservationBackend,
    ReservationBackendBase,
    SupportsReservationWindows,
    SupportsUsernameCompletion,
)
from otto.testing import assert_reservation_backend_conforms


class _Complete(ReservationBackendBase):
    def __init__(self, *, url=None, repo_dir=None, holdings=None):
        super().__init__(url=url, repo_dir=repo_dir)
        self._holdings = holdings or {}

    def get_reserved_resources(self, username: str) -> set[str]:
        return set(self._holdings.get(username, ()))

    def who_reserved(self, resource: str) -> list[str]:
        return sorted(u for u, rs in self._holdings.items() if resource in rs)

    def backend_name(self) -> str:
        return "complete"


class TestAbstractContract:
    def test_missing_method_fails_at_instantiation_naming_it(self):
        class _Incomplete(ReservationBackendBase):
            def get_reserved_resources(self, username):
                return set()

            def backend_name(self):
                return "incomplete"

        with pytest.raises(TypeError, match="who_reserved"):
            _Incomplete()

    def test_complete_subclass_satisfies_the_protocol(self):
        assert isinstance(_Complete(), ReservationBackend)

    def test_complete_subclass_passes_conformance(self):
        backend = _Complete(holdings={"alice": ["rack1"]})
        assert_reservation_backend_conforms(backend, known_user="alice", known_resources=["rack1"])


class TestConstructor:
    def test_keeps_what_the_factory_passes(self, tmp_path):
        backend = _Complete(url="https://sched.example", repo_dir=tmp_path)
        assert backend.url == "https://sched.example"
        assert backend.repo_dir == tmp_path

    def test_defaults_are_none(self):
        backend = _Complete()
        assert backend.url is None
        assert backend.repo_dir is None

    def test_repo_dir_is_kept_as_a_path(self, tmp_path):
        backend = _Complete(repo_dir=str(tmp_path))
        assert isinstance(backend.repo_dir, Path)


class TestOptionalCapabilitiesStayStructural:
    def test_base_alone_claims_no_capability(self):
        backend = _Complete()
        assert not isinstance(backend, SupportsUsernameCompletion)
        assert not isinstance(backend, SupportsReservationWindows)

    def test_adding_the_method_is_the_signal(self):
        class _WithUsers(_Complete):
            def list_usernames(self) -> list[str]:
                return sorted(self._holdings)

        assert isinstance(_WithUsers(), SupportsUsernameCompletion)
        assert not isinstance(_WithUsers(), SupportsReservationWindows)


class TestShippedBackendsInherit:
    """The built-ins and the docs' worked example use the base they recommend."""

    def test_null_backend(self):
        from otto.reservations import NullReservationBackend

        assert isinstance(NullReservationBackend(), ReservationBackendBase)

    def test_json_backend(self, tmp_path):
        from otto.reservations import JsonReservationBackend

        assert isinstance(JsonReservationBackend(path=tmp_path / "r.json"), ReservationBackendBase)

    def test_example_backend(self):
        from otto.examples.reservations import ExampleReservationBackend

        assert isinstance(ExampleReservationBackend(), ReservationBackendBase)
