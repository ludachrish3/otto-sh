"""The example CLI and ``otto reservation check`` reach the same verdict on the same input."""

import json

import click
import pytest
import typer

from otto.cli.reservation import check
from otto.config.lab import Lab
from otto.examples.reservations_cli import check_report, translate
from otto.reservations import gate_from_settings
from tests._fixtures.fleet import install_scoped_context
from tests.conftest import make_host


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "reservations.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "reservations": [
                    {"user": "alice", "resources": ["r1"]},
                    {"user": "bob", "resources": ["r1", "r2"]},
                ],
            }
        )
    )
    return {"backend": "json", "json": {"path": str(path)}}


def _example_state(out, resource):
    for state in ("held", "missing"):
        if f"{resource}  lab lab1  {state}" in out:
            return state
    return "absent"


def _otto_state(out, resource):
    for line in out.splitlines():
        cells = [c.strip() for c in line.split("│")]
        if len(cells) >= 6 and cells[1] == resource:
            return "held" if cells[4] == "yes" else "missing"
    return "absent"


EXPECTED = {
    "alice": {"r1": "held", "r2": "missing"},
    "bob": {"r1": "held", "r2": "held"},
    "carol": {"r1": "missing", "r2": "missing"},
}
"""What each user holds of the lab's two resources, per the reservations file in ``settings``."""


@pytest.mark.parametrize("user", ["alice", "bob", "carol"])
def test_the_example_and_otto_agree(user, settings, tmp_path, monkeypatch, capsys):
    lab = Lab(name="lab1", resources={"r1", "r2"}, hosts={"test1": make_host("test1")})
    gate = gate_from_settings(settings, tmp_path, holder=user, skip_reservation_check=False)

    example_code = translate(lambda: check_report(gate, lab))
    example_out = capsys.readouterr().out

    install_scoped_context(monkeypatch, lab, [])
    ctx = click.Context(click.Command("reservation"))
    ctx.meta["otto_reservation"] = gate
    otto_code = 0
    try:
        check(ctx)  # type: ignore[arg-type]
    except typer.Exit as e:
        otto_code = e.exit_code
    otto_out = capsys.readouterr().out

    assert example_code == otto_code
    for resource in ("r1", "r2"):
        assert _example_state(example_out, resource) == EXPECTED[user][resource], (
            user,
            resource,
            example_out,
        )
        assert _example_state(example_out, resource) == _otto_state(otto_out, resource), (
            user,
            resource,
            example_out,
            otto_out,
        )
