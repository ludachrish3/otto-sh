"""The example reservation CLI: parse, build the gate via the library, render, translate."""

from typer.testing import CliRunner

from otto.config.lab import Lab
from otto.examples.reservations import ExampleReservationBackend
from otto.examples.reservations_cli import app, check_report, run_gate, translate
from otto.reservations import (
    Reservation,
    ReservationBackendBase,
    ReservationBackendError,
    ReservationGate,
    register_reservation_backend,
    resolve_username,
)
from otto.reservations.registry import RESERVATION_BACKENDS

runner = CliRunner()


class _UnbuildableBackend(ReservationBackendBase):
    """A backend whose constructor fails, as a scheduler that is down would."""

    def __init__(self, **kwargs):
        raise ReservationBackendError("scheduler down")

    def fetch_reservations(self, username, start=None, end=None) -> list[Reservation]:
        return []

    def backend_name(self) -> str:
        return "unbuildable"


class _BrokenBackend(ReservationBackendBase):
    """A backend whose queries always fail; no scheduler is contacted."""

    def fetch_reservations(self, username, start=None, end=None) -> list[Reservation]:
        raise ReservationBackendError("network down")

    def backend_name(self) -> str:
        return "broken"


def _gate(backend, user):
    return ReservationGate(backend=backend, identity=resolve_username(user))


def test_the_none_backend_passes_with_no_scheduler():
    result = runner.invoke(app, ["--resource", "rack1", "gate"])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_skip_prints_the_libraries_warning_and_passes():
    result = runner.invoke(app, ["--resource", "rack1", "-R", "gate"])
    assert result.exit_code == 0
    assert "SKIPPED" in result.output


def test_check_with_the_none_backend_reports_n_a_and_passes():
    result = runner.invoke(app, ["--resource", "rack1", "check"])
    assert result.exit_code == 0
    assert "rack1  lab example  n/a" in result.output


def test_a_missing_resource_exits_1_with_the_librarys_refusal(capsys):
    lab = Lab(name="demo", resources={"lab-a"})
    code = translate(
        lambda: run_gate(_gate(ExampleReservationBackend(username="carol"), "carol"), lab)
    )
    assert code == 1
    assert "does not hold all resources required by lab 'demo'" in capsys.readouterr().out


def test_an_unavailable_backend_exits_1_and_names_the_break_glass(capsys):
    lab = Lab(name="demo", resources={"lab-a"})
    code = translate(lambda: run_gate(_gate(_BrokenBackend(username="alice"), "alice"), lab))
    out = capsys.readouterr().out
    assert code == 1
    assert "reservation backend unavailable" in out
    assert "-R" in out


def test_check_renders_every_row_then_the_verdict(capsys):
    lab = Lab(name="demo", resources={"lab-a", "shared"})
    code = translate(
        lambda: check_report(_gate(ExampleReservationBackend(username="alice"), "alice"), lab)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "lab-a  lab demo  held" in out
    assert "shared  lab demo  held" in out
    assert out.rstrip().endswith("OK")


def test_check_names_the_missing_row_and_exits_1(capsys):
    lab = Lab(name="demo", resources={"lab-a"})
    code = translate(
        lambda: check_report(_gate(ExampleReservationBackend(username="carol"), "carol"), lab)
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "lab-a  lab demo  missing" in out
    assert "held by: alice" in out


def test_a_backend_that_cannot_be_built_exits_1_through_the_cli_and_names_the_break_glass():
    register_reservation_backend("example-unbuildable", _UnbuildableBackend)
    try:
        result = runner.invoke(app, ["--backend", "example-unbuildable", "--resource", "r", "gate"])
    finally:
        RESERVATION_BACKENDS.unregister("example-unbuildable")
    assert result.exit_code == 1
    assert "-R" in result.output
