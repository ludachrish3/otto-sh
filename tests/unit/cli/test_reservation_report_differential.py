"""``otto reservation check`` renders ``gate.report()``: rows and verdict agree, case by case."""

import re
from datetime import datetime, timedelta, timezone

import click
import pytest
import typer

from otto.cli.reservation import check
from otto.config.fleet import get_hosts_in_play
from otto.config.lab import Lab
from otto.reservations import (
    NullReservationBackend,
    Reservation,
    ReservationBackendBase,
    ReservationGate,
    ResolvedIdentity,
)
from tests._fixtures.fleet import install_scoped_context
from tests.unit.cli.test_reservation import _rig_lab, _slot_host


class _Backend(ReservationBackendBase):
    def __init__(self, rows, username="alice"):
        super().__init__(username=username)
        self.rows = rows

    def fetch_reservations(self, username, start=None, end=None):
        return list(self.rows)

    def backend_name(self):
        return "diff"


def _held(*resources, user="alice", **kw):
    return _Backend([Reservation(user=user, resource=r, **kw) for r in resources])


def _lab_level(resources):
    return lambda: Lab(name="lab1", resources=resources)


def _slot_lab():
    return _rig_lab(_slot_host("test1", "chassis1", "slot-1"))


def _shared_lab():
    """``slot-1`` is declared by the lab AND by a host: two origins, one resource."""
    lab = _slot_lab()
    lab.resources = {"slot-1"}
    return lab


def _expiring_backend():
    return _held("r1", end=datetime.now(tz=timezone.utc) + timedelta(minutes=2))


# case -> (lab builder, backend builder)
CASES = {
    "held": (_lab_level({"r1"}), lambda: _held("r1")),
    "missing": (_lab_level({"r1", "r2"}), lambda: _held("r1")),
    "foreign": (_lab_level({"r1"}), lambda: _held("r1", user="bob")),
    "none": (_lab_level({"r1"}), NullReservationBackend),
    "nothing": (_lab_level(set()), _held),
    # An element slug and a host id in the owner column.
    "element-and-host-held": (_slot_lab, lambda: _held("chassis-1", "slot-1")),
    "element-and-host-missing": (_slot_lab, lambda: _held("slot-1")),
    "element-and-host-none": (_slot_lab, NullReservationBackend),
    "multi-origin-held": (_shared_lab, lambda: _held("slot-1", "chassis-1")),
    "multi-origin-missing": (_shared_lab, lambda: _held("chassis-1")),
    "expiring": (_lab_level({"r1"}), _expiring_backend),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_check_command_shows_exactly_the_report(case, capsys, caplog):
    make_lab, make_backend = CASES[case]
    lab = make_lab()
    install_scoped_context(lab, [])
    gate = ReservationGate(
        backend=make_backend(), identity=ResolvedIdentity(username="alice", source="$USER")
    )
    report = gate.report(lab, get_hosts_in_play())

    ctx = click.Context(click.Command("reservation"))
    ctx.meta["otto_reservation"] = gate
    exit_code = 0
    with caplog.at_level("WARNING"):
        try:
            check(ctx)  # type: ignore[arg-type]
        except typer.Exit as e:
            exit_code = e.exit_code
    out = capsys.readouterr().out

    for row in report.rows:
        held = "n/a" if row.held is None else ("yes" if row.held else "no")
        cells = [re.escape(c) for c in (row.resource, row.level, row.owner)] + [held]
        pattern = r"\s+│\s+".join(cells) + r"\b"
        assert re.search(pattern, out), (case, row, out)
    # Exactly one table line per row: nothing deduplicated or invented.
    assert sum(line.startswith("│") for line in out.splitlines()) == (
        len(report.rows) + 1 if report.rows else 0
    ), (case, out)
    if not report.rows:
        assert "requires no reservation" in out
    assert (exit_code == 0) is report.covered
    assert ("OK — all required resources are reserved." in out) is report.covered
    assert ("expires in" in caplog.text) is bool(report.expiring)
    if report.rows and report.covered:
        assert out.index("╭") < out.index("OK — all required")


def test_the_expiring_case_really_has_an_expiring_booking():
    make_lab, make_backend = CASES["expiring"]
    lab = make_lab()
    install_scoped_context(lab, [])
    gate = ReservationGate(
        backend=make_backend(), identity=ResolvedIdentity(username="alice", source="$USER")
    )
    assert [r.resource for r in gate.report(lab, get_hosts_in_play()).expiring] == ["r1"]
