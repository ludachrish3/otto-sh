"""Unit tests for the reservation check and the gate helper."""

import types
from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch

import pytest

from otto.configmodule.lab import Lab
from otto.reservations import (
    MissingReservationError,
    NullReservationBackend,
    ReservationState,
    ResolvedIdentity,
    check_reservations,
    gate,
    required_resources,
)
from tests.conftest import make_host


def _fake_ctx(meta: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(meta=meta)


@dataclass
class _FakeBackend:
    """Minimal in-memory ReservationBackend for testing the check function."""

    owners: dict[str, str]  # resource -> username

    def get_reserved_resources(self, username: str) -> set[str]:
        return {r for r, u in self.owners.items() if u == username}

    def who_reserved(self, resource: str) -> Optional[str]:
        return self.owners.get(resource)

    def backend_name(self) -> str:
        return "fake"


def _lab_with_resources() -> Lab:
    """Build a lab whose total required resources are {rack1, carrot, tomato}."""
    return Lab(
        name="test_lab",
        resources={"rack1"},
        hosts={
            "carrot_seed": make_host("carrot", resources={"carrot"}),
            "tomato_seed": make_host("tomato", resources={"tomato"}),
        },
    )


class TestRequiredResources:

    def test_union_of_lab_and_hosts(self):
        lab = _lab_with_resources()
        assert required_resources(lab) == {"rack1", "carrot", "tomato"}

    def test_empty_lab(self):
        lab = Lab(name="empty")
        assert required_resources(lab) == set()


class TestCheckReservations:

    def test_full_coverage_returns_silently(self):
        lab = _lab_with_resources()
        backend = _FakeBackend(owners={
            "rack1": "alice", "carrot": "alice", "tomato": "alice",
        })
        check_reservations(lab, "alice", backend)  # must not raise

    def test_partial_coverage_raises_with_holders(self):
        lab = _lab_with_resources()
        backend = _FakeBackend(owners={
            "rack1":  "alice",
            "carrot": "bob",    # held by someone else
            "tomato": None,     # unreserved (not in dict, but model None explicitly)
        })
        # Remove tomato so it reads as unreserved
        del backend.owners["tomato"]
        with pytest.raises(MissingReservationError) as exc_info:
            check_reservations(lab, "alice", backend)
        msg = str(exc_info.value)
        assert "alice" in msg
        assert "test_lab" in msg
        assert "carrot" in msg
        assert "tomato" in msg
        assert "held by bob" in msg
        assert "unreserved" in msg

    def test_error_does_not_mention_skip_flag(self):
        """Regression guard — MissingReservationError must not advertise --skip-reservation-check."""
        lab = _lab_with_resources()
        backend = _FakeBackend(owners={})
        with pytest.raises(MissingReservationError) as exc_info:
            check_reservations(lab, "alice", backend)
        assert "--skip-reservation-check" not in str(exc_info.value)
        assert "-R" not in str(exc_info.value)

    def test_null_backend_is_noop(self):
        lab = _lab_with_resources()
        check_reservations(lab, "anyone", NullReservationBackend())  # must not raise

    def test_empty_lab_is_noop(self):
        lab = Lab(name="empty")
        # Empty required set — backend never queried
        backend = _FakeBackend(owners={})
        check_reservations(lab, "alice", backend)


class TestGate:

    def test_no_backend_is_noop(self):
        ctx = _fake_ctx({"otto_reservation": ReservationState(backend=None, identity=None, skip_check=False)})
        gate(ctx)  # must not raise

    def test_skip_flag_short_circuits(self, caplog):
        import logging

        lab = _lab_with_resources()
        backend = _FakeBackend(owners={})  # would fail the check if called
        identity = ResolvedIdentity(username="alice", source="$USER")
        res = ReservationState(backend=backend, identity=identity, skip_check=True)
        ctx = _fake_ctx({"otto_reservation": res})

        with (
            caplog.at_level(logging.WARNING, logger="otto"),
            patch("otto.configmodule.get_lab", return_value=lab),
        ):
            gate(ctx)  # must not raise

        assert any("skipped" in rec.message.lower() for rec in caplog.records)
        assert any("alice" in rec.message for rec in caplog.records)
        assert any("test_lab" in rec.message for rec in caplog.records)

    def test_normal_path_calls_check(self):
        lab = _lab_with_resources()
        backend = _FakeBackend(owners={
            "rack1": "alice", "carrot": "alice", "tomato": "alice",
        })
        identity = ResolvedIdentity(username="alice", source="$USER")
        res = ReservationState(backend=backend, identity=identity, skip_check=False)
        ctx = _fake_ctx({"otto_reservation": res})

        with patch("otto.configmodule.get_lab", return_value=lab):
            gate(ctx)  # must not raise — full coverage

    def test_failing_check_propagates(self):
        lab = _lab_with_resources()
        backend = _FakeBackend(owners={})  # no one has anything
        identity = ResolvedIdentity(username="alice", source="$USER")
        res = ReservationState(backend=backend, identity=identity, skip_check=False)
        ctx = _fake_ctx({"otto_reservation": res})

        with (
            patch("otto.configmodule.get_lab", return_value=lab),
            pytest.raises(MissingReservationError),
        ):
            gate(ctx)
