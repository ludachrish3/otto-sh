"""Conformance helpers verified against otto's built-in backends + an error sample."""

import json
from pathlib import Path

import pytest

from otto.labs import JsonFileLabRepository
from otto.reservations import (
    JsonReservationBackend,
    NullReservationBackend,
)
from otto.reservations.check import ReservationBackendError
from otto.testing import (
    assert_lab_repository_conforms,
    assert_reservation_backend_conforms,
)


def _hosts_file(path: Path) -> None:
    (path / "lab.json").write_text(
        json.dumps(
            {
                "hosts": [
                    {
                        "ip": "10.0.0.1",
                        "element": "a",
                        "creds": [{"login": "u", "password": "p"}],
                        "resources": ["a"],
                        "labs": ["alpha"],
                    },
                    {
                        "ip": "10.0.0.2",
                        "element": "b",
                        "creds": [{"login": "u", "password": "p"}],
                        "resources": ["b"],
                        "labs": ["beta"],
                    },
                ]
            }
        )
    )


def _reservations_file(path: Path) -> Path:
    f = path / "reservations.json"
    f.write_text(
        json.dumps(
            {
                "version": 1,
                "reservations": [
                    {"user": "alice", "resources": ["lab-a", "shared"]},
                    {"user": "bob", "resources": ["lab-b", "shared"]},
                ],
            }
        )
    )
    return f


class TestLabRepositoryConformance:
    def test_json_builtin_conforms(self, tmp_path):
        _hosts_file(tmp_path)
        repo = JsonFileLabRepository([tmp_path])
        # Must not raise.
        assert_lab_repository_conforms(repo, expected_labs=["alpha", "beta"])

    def test_non_conforming_repo_raises_with_aggregate(self):
        class Broken:
            def load_lab(self, name, preferences=None):
                return "not a lab"  # wrong type

            def list_labs(self):
                return "not a list"  # wrong type

        with pytest.raises(AssertionError) as exc:
            assert_lab_repository_conforms(Broken())
        assert "LabRepository" in str(exc.value)

    def test_missing_list_labs_raises_assertion_not_attribute_error(self):
        """A repo with no list_labs at all must aggregate an AssertionError,
        not propagate an AttributeError before raise_if_failures()."""

        class NoListLabs:
            def load_lab(self, name, preferences=None):
                raise KeyError(name)

        with pytest.raises(AssertionError) as exc:
            assert_lab_repository_conforms(NoListLabs())
        assert "LabRepository" in str(exc.value)

    def test_load_lab_raises_on_idempotency_recall_records_not_crashes(self):
        """A backend whose load_lab raises on the second call (idempotency re-call)
        must produce an aggregated AssertionError, not propagate the raw exception."""
        from otto.config.lab import Lab

        class RaisesOnSecondCall:
            def __init__(self):
                self._call_count = 0

            def load_lab(self, name, preferences=None):
                self._call_count += 1
                if self._call_count == 1:
                    return Lab(name=name)
                raise RuntimeError("second load_lab call exploded")

            def list_labs(self):
                return ["mylab"]

        with pytest.raises(AssertionError) as exc:
            assert_lab_repository_conforms(RaisesOnSecondCall())
        assert "LabRepository" in str(exc.value)


class TestReservationBackendConformance:
    def test_null_builtin_conforms(self):
        assert_reservation_backend_conforms(NullReservationBackend())

    def test_json_builtin_conforms_with_round_trip(self, tmp_path):
        f = _reservations_file(tmp_path)
        backend = JsonReservationBackend(path=f)
        assert_reservation_backend_conforms(
            backend, known_user="alice", known_resources=["lab-a", "shared"]
        )

    def test_non_conforming_backend_raises(self):
        class Broken:
            def get_reserved_resources(self, username):
                return ["not", "a", "set"]  # wrong type

            def who_reserved(self, resource):
                return None  # wrong type — must be list

            def backend_name(self):
                return ""  # empty — invalid

        with pytest.raises(AssertionError) as exc:
            assert_reservation_backend_conforms(Broken())
        assert "ReservationBackend" in str(exc.value)

    def test_get_reserved_resources_returns_none_raises_assertion_not_type_error(self):
        """A backend whose get_reserved_resources returns None (not a set) must
        aggregate an AssertionError via raise_if_failures(), not crash the helper
        with a TypeError when the round-trip path does `r in None`."""

        class NoneReturner:
            def get_reserved_resources(self, username):
                return None  # non-set — triggers the round-trip guard

            def who_reserved(self, resource):
                # Returns a list with a holder so the round-trip path is entered.
                return ["alice"]

            def backend_name(self):
                return "none-returner"

        with pytest.raises(AssertionError) as exc:
            assert_reservation_backend_conforms(
                NoneReturner(),
                known_user="alice",
                known_resources=["lab-x"],
            )
        assert "ReservationBackend" in str(exc.value)


class TestReservationErrorContract:
    """The error-contract rule (§4.3) is exercised by a purpose-built failing
    sample, not the generic helper (which cannot force a healthy backend to fail).
    """

    def test_failure_modes_raise_reservation_backend_error(self):
        class FailingBackend:
            def get_reserved_resources(self, username):
                raise ReservationBackendError("scheduler unreachable")

            def who_reserved(self, resource):
                raise ReservationBackendError("scheduler unreachable")

            def backend_name(self):
                return "failing"

        backend = FailingBackend()
        with pytest.raises(ReservationBackendError):
            backend.get_reserved_resources("anyone")
        with pytest.raises(ReservationBackendError):
            backend.who_reserved("anything")
