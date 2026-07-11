"""Smoke tests for the third-party reservations_cli example (sample)."""

from typer.testing import CliRunner

from otto.config.lab import Lab
from otto.examples import reservations_cli
from otto.examples.reservations import ExampleReservationBackend
from otto.examples.reservations_cli import app, run_check
from otto.reservations import (
    NullReservationBackend,
    ReservationBackendError,
    resolve_username,
)

runner = CliRunner()


class _BrokenBackend:
    """A backend whose queries always fail — no scheduler is actually contacted."""

    def get_reserved_resources(self, username: str) -> set[str]:
        raise ReservationBackendError("network down")

    def who_reserved(self, resource: str) -> list[str]:
        raise ReservationBackendError("network down")

    def backend_name(self) -> str:
        return "broken"


def test_run_check_ok_with_null_backend_no_scheduler():
    # NullReservationBackend never touches a real scheduler; the gate is a no-op.
    lab = Lab(name="demo", resources={"lab-a"})
    assert run_check(lab, backend=NullReservationBackend(), identity=resolve_username(None)) == 0


def test_run_check_ok_when_identity_holds_resource():
    lab = Lab(name="demo", resources={"lab-a"})
    identity = resolve_username("alice")
    assert run_check(lab, backend=ExampleReservationBackend(), identity=identity) == 0


def test_run_check_returns_1_and_prints_on_missing_reservation(capsys):
    lab = Lab(name="demo", resources={"lab-a"})
    identity = resolve_username("carol")
    assert run_check(lab, backend=ExampleReservationBackend(), identity=identity) == 1
    out = capsys.readouterr().out
    assert "carol" in out
    assert "lab-a" in out


def test_run_check_returns_2_on_backend_query_failure():
    lab = Lab(name="demo", resources={"lab-a"})
    identity = resolve_username("alice")
    assert run_check(lab, backend=_BrokenBackend(), identity=identity) == 2


def test_cli_exits_0_with_default_null_backend():
    # No --backend given -> "none" -> NullReservationBackend fallback, no scheduler.
    result = runner.invoke(app, ["--resource", "rack1"])
    assert result.exit_code == 0, result.output


def test_cli_exits_1_when_identity_is_missing_a_resource(monkeypatch):
    monkeypatch.setattr(
        reservations_cli, "build_backend", lambda settings, repo_dir: ExampleReservationBackend()
    )
    result = runner.invoke(app, ["--resource", "lab-a", "--as-user", "carol"])
    assert result.exit_code == 1


def test_cli_exits_2_when_backend_construction_fails(monkeypatch):
    def _boom(settings, repo_dir):
        raise ReservationBackendError("scheduler unreachable")

    monkeypatch.setattr(reservations_cli, "build_backend", _boom)
    result = runner.invoke(app, [])
    assert result.exit_code == 2
