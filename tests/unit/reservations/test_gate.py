"""Unit tests for ``ReservationGate.evaluate()`` — the typer-free library gate.

Outcome matrix (per user, per backend state):

* ``skip_check=True``            -> skipped=True,  warning contains "SKIPPED"
* ``backend=None`` (no skip)     -> checked=False, skipped=False, warning=None
* backend configured, missing    -> raises MissingReservationError
* backend configured, fully held -> checked=True,  skipped=False, warning=None

Plus an import-hygiene guard: importing ``otto.reservations`` must never pull
in ``typer`` — that is the whole point of extracting the gate out of the CLI.
"""

from unittest.mock import patch

import pytest

from otto.config.lab import Lab
from otto.reservations import (
    MissingReservationError,
    ReservationGate,
    ReservationGateOutcome,
    ResolvedIdentity,
)
from tests.conftest import make_host


class _FakeBackend:
    """Minimal in-memory ReservationBackend for testing the gate."""

    def __init__(self, owners: dict[str, str]) -> None:
        self.owners = owners  # resource -> username

    def get_reserved_resources(self, username: str) -> set[str]:
        return {r for r, u in self.owners.items() if u == username}

    def who_reserved(self, resource: str) -> list[str]:
        u = self.owners.get(resource)
        return [u] if u is not None else []

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


class TestReservationGateOutcomeMatrix:
    def test_skip_check_returns_skipped_outcome_with_warning(self, caplog):
        import logging

        lab = _lab_with_resources()
        backend = _FakeBackend(owners={})  # would fail the check if it ran
        identity = ResolvedIdentity(username="alice", source="$USER")
        gate = ReservationGate(backend=backend, identity=identity, skip_check=True)

        with (
            caplog.at_level(logging.WARNING, logger="otto"),
            patch("otto.config.get_lab", return_value=lab),
        ):
            outcome = gate.evaluate()

        assert outcome.checked is False
        assert outcome.skipped is True
        assert outcome.warning is not None
        assert "SKIPPED" in outcome.warning
        assert "alice" in outcome.warning
        assert "test_lab" in outcome.warning
        # No rich markup — that is the CLI adapter's job, not the library's.
        assert "[bold red]" not in outcome.warning
        assert any("skipped" in rec.message.lower() for rec in caplog.records)

    def test_skip_check_warns_even_when_backend_none(self, caplog):
        import logging

        lab = _lab_with_resources()
        identity = ResolvedIdentity(username="alice", source="$USER")
        # backend=None models the -R break-glass path: construction skipped.
        gate = ReservationGate(backend=None, identity=identity, skip_check=True)

        with (
            caplog.at_level(logging.WARNING, logger="otto"),
            patch("otto.config.get_lab", return_value=lab),
        ):
            outcome = gate.evaluate()

        assert outcome.skipped is True
        assert outcome.checked is False
        assert outcome.warning is not None
        assert "SKIPPED" in outcome.warning

    def test_no_backend_is_all_false_none(self):
        gate = ReservationGate(backend=None, identity=None, skip_check=False)
        outcome = gate.evaluate()
        assert outcome == ReservationGateOutcome(checked=False, skipped=False, warning=None)

    def test_backend_missing_resource_raises(self):
        lab = _lab_with_resources()
        backend = _FakeBackend(owners={})  # no one has anything
        identity = ResolvedIdentity(username="alice", source="$USER")
        gate = ReservationGate(backend=backend, identity=identity, skip_check=False)

        with (
            patch("otto.config.get_lab", return_value=lab),
            pytest.raises(MissingReservationError),
        ):
            gate.evaluate()

    def test_backend_fully_held_returns_checked(self):
        lab = _lab_with_resources()
        backend = _FakeBackend(
            owners={
                "rack1": "alice",
                "carrot": "alice",
                "tomato": "alice",
            }
        )
        identity = ResolvedIdentity(username="alice", source="$USER")
        gate = ReservationGate(backend=backend, identity=identity, skip_check=False)

        with patch("otto.config.get_lab", return_value=lab):
            outcome = gate.evaluate()

        assert outcome == ReservationGateOutcome(checked=True, skipped=False, warning=None)

    def test_backend_configured_but_identity_none_raises_runtime_error(self):
        lab = _lab_with_resources()
        backend = _FakeBackend(owners={})
        gate = ReservationGate(backend=backend, identity=None, skip_check=False)

        with (
            patch("otto.config.get_lab", return_value=lab),
            pytest.raises(RuntimeError, match="identity must be resolved"),
        ):
            gate.evaluate()


def test_reservations_import_is_typer_free():
    import subprocess
    import sys

    code = "import sys, otto.reservations; sys.exit(1 if 'typer' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
