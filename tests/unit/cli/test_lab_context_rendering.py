"""The lab-context refusals render exactly as they did before the library owned them."""

import pytest
import typer

from otto.cli.invoke import report_lab_context_error
from otto.session import LabBuildError


def test_no_labs_is_the_plain_missing_option_line(capsys):
    with pytest.raises(typer.Exit) as exc:
        report_lab_context_error(LabBuildError("x", field="labs", kind="no_labs"))
    assert exc.value.exit_code == 2
    assert (
        capsys.readouterr().err == "Error: Missing option '--lab' / '-l' (env var: 'OTTO_LAB').\n"
    )


@pytest.mark.parametrize(
    ("kind", "label"),
    [("sources", "Host source unavailable:"), ("inventory", "Inventory unavailable:")],
)
def test_a_build_failure_is_the_bold_red_frame_with_the_detail_escaped(capsys, kind, label):
    with pytest.raises(typer.Exit) as exc:
        report_lab_context_error(
            LabBuildError("x", field=None, kind=kind, detail="bad [inventory] x")
        )
    assert exc.value.exit_code == 1
    assert f"{label} bad [inventory] x" in capsys.readouterr().out


def test_an_unknown_lab_reaches_the_boundary_frame_unchanged():
    err = LabBuildError("Lab 'nope' not found", field="labs", kind="unknown_lab", detail="d")
    with pytest.raises(LabBuildError) as exc:
        report_lab_context_error(err)
    assert exc.value is err


def test_a_reservation_backend_failure_keeps_its_hint(capsys):
    from otto.reservations import ReservationBackendError

    with pytest.raises(typer.Exit) as exc:
        report_lab_context_error(ReservationBackendError("down"))
    out = capsys.readouterr().out
    assert exc.value.exit_code == 1
    assert "Reservation backend unavailable: down" in out
    assert "--skip-reservation-check" in out
    assert "-R" in out


def test_the_context_manager_renders_a_refusal_raised_inside(capsys):
    from otto.cli.invoke import lab_context_refusals

    with pytest.raises(typer.Exit) as exc, lab_context_refusals():
        raise LabBuildError("x", field="labs", kind="no_labs")
    assert exc.value.exit_code == 2
    assert "Missing option '--lab'" in capsys.readouterr().err


def test_the_context_manager_lets_anything_else_through():
    from otto.cli.invoke import lab_context_refusals

    with pytest.raises(KeyError), lab_context_refusals():
        raise KeyError("not a lab-context refusal")
