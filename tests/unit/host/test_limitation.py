"""A difference from otto's behaviour promises is declared as a :class:`Limitation`."""

import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from otto.host.command_frame import (
    BUILTIN_FRAMES,
    BashFrame,
    build_command_frame,
    register_command_frame,
)
from otto.host.exec_route import ExecRoute
from otto.host.limitation import Limitation
from otto.host.session import EXEC_ROUTES, SessionManager, typed_line_budget
from otto.registry import Ref

if TYPE_CHECKING:
    from otto.host.connections import ConnectionManager


class _RefParent(BashFrame):
    type_name = "ref-parent"
    exact_output = Limitation(affects="a promise", kind="device", reason="r", measured_on=["x"])


class RefChild(_RefParent):
    """Inherits a limitation it never declared; named by a ``Ref`` below."""

    type_name = "ref-child"


def test_a_limitation_names_its_bad_field():
    for kwargs, field in [
        ({"kind": "device", "affects": "a", "reason": "", "measured_on": ["z"]}, "reason"),
        ({"kind": "device", "affects": " ", "reason": "r", "measured_on": ["z"]}, "affects"),
        ({"kind": "device", "affects": "a", "reason": "r", "measured_on": []}, "measured_on"),
        ({"kind": "cost", "affects": "a", "reason": "r", "measured_on": ["x"]}, "kind"),
    ]:
        with pytest.raises(ValueError, match=field):
            Limitation(**kwargs)


def test_an_inherited_limitation_is_refused_by_name():
    class Parent(BashFrame):
        type_name = "p"
        exact_output = Limitation(affects="a promise", kind="device", reason="r", measured_on=["x"])

    class Child(Parent):
        type_name = "child"

    with pytest.raises(ValueError, match=r"Child must declare exact_output on its own class body"):
        register_command_frame("child", Child)


def test_a_ref_registered_frame_is_checked_when_resolved():
    register_command_frame("ref-child", Ref(target=f"{__name__}:RefChild"))
    with pytest.raises(
        ValueError, match=r"RefChild must declare exact_output on its own class body"
    ):
        build_command_frame(type_name="ref-child")


def test_exact_output_must_be_a_limitation_or_none():
    class Trimmed(BashFrame):
        type_name = "trimmed"
        exact_output = "trimmed"  # ty: ignore[invalid-assignment]

    with pytest.raises(TypeError, match="exact_output"):
        register_command_frame("trimmed", Trimmed)


def test_a_frame_declaring_its_own_limitation_registers():
    class Declared(BashFrame):
        type_name = "declared"
        exact_output = Limitation(affects="a promise", kind="device", reason="r", measured_on=["x"])

    register_command_frame("declared", Declared)
    assert type(build_command_frame(type_name="declared")).exact_output is Declared.exact_output


def test_code_otto_implements_declares_no_library_limitation():
    for route, record in EXEC_ROUTES.items():
        if record.implemented_by == "otto":
            assert all(lim.kind == "device" for lim in record.limitations.values()), route
    for frame in BUILTIN_FRAMES:
        lim = type(build_command_frame(type_name=frame.name)).exact_output
        assert lim is None or lim.kind == "device", frame.name


def test_every_route_has_a_record_and_the_shell_routes_declare_the_line_budget():
    assert set(EXEC_ROUTES) == set(ExecRoute) - {ExecRoute.UNSUPPORTED}
    assert {r for r, rec in EXEC_ROUTES.items() if "line_budget" in rec.limitations} == {
        ExecRoute.POOLED_SHELL,
        ExecRoute.DEFAULT_SESSION,
    }


def test_the_line_budget_is_read_from_the_route_record():
    manager = SessionManager(
        connections=cast("ConnectionManager", SimpleNamespace(term="telnet", proxy_hops=[])),
        name="h",
        command_frame=BashFrame(),
    )
    assert manager.line_budget(route=ExecRoute.SSH_CHANNEL) is None
    assert manager.line_budget(route=ExecRoute.UNSUPPORTED) is None
    assert manager.line_budget(route=ExecRoute.POOLED_SHELL) == typed_line_budget(
        BashFrame(), echoes=True
    )
    assert manager.line_budget(route=ExecRoute.DEFAULT_SESSION) == typed_line_budget(
        BashFrame(), echoes=True
    )


def test_the_line_budget_reads_its_measurements_from_their_one_home():
    from otto.host.session import (
        _LINE_EDITOR_RECORD,
        _SSH_CHANNEL_COMMAND_MEASUREMENT,
        _TYPED_LINE_MEASUREMENTS,
    )
    from otto.host.userland import gap_for

    gap = gap_for(_LINE_EDITOR_RECORD)
    assert gap is not None, _LINE_EDITOR_RECORD
    measured = EXEC_ROUTES[ExecRoute.POOLED_SHELL].limitations["line_budget"].measured_on
    assert measured == _TYPED_LINE_MEASUREMENTS
    # The gap record's measured_on is prose, so the short entries cannot be
    # derived from it; instead EVERY number each entry states (dates, versions,
    # character counts, read off the entry itself rather than listed here) must
    # be one the record states.
    entries = [*_TYPED_LINE_MEASUREMENTS, _SSH_CHANNEL_COMMAND_MEASUREMENT]
    for entry in entries:
        numbers = re.findall(r"\d+(?:[.-]\d+)*", entry)
        assert numbers, entry
        for number in numbers:
            assert number in gap.measured_on, (number, entry)


def test_the_ssh_channel_declares_its_measured_command_length_and_is_not_budgeted():
    from otto.host.session import _SSH_CHANNEL_COMMAND_MEASUREMENT, SSH_CHANNEL_COMMAND_MAX

    record = EXEC_ROUTES[ExecRoute.SSH_CHANNEL]
    assert set(record.limitations) == {"command_length"}
    lim = record.limitations["command_length"]
    assert lim.kind == "device"
    assert lim.measured_on == [_SSH_CHANNEL_COMMAND_MEASUREMENT]
    assert "dropbear 2022.83" in lim.reason
    assert f"{SSH_CHANNEL_COMMAND_MAX + 1}" in lim.reason
    assert "OpenSSH" not in lim.reason


def test_the_line_budget_reason_states_the_ceiling_from_its_constant():
    from otto.host.session import SSH_CHANNEL_COMMAND_MAX
    from otto.host.userland import PTY_TYPED_LINE_MAX

    reason = EXEC_ROUTES[ExecRoute.POOLED_SHELL].limitations["line_budget"].reason
    assert str(PTY_TYPED_LINE_MAX) in reason
    # The ssh channel's figure is that route's own declaration, not restated here.
    assert str(SSH_CHANNEL_COMMAND_MAX) not in reason


def test_a_local_shell_reads_a_pipe_so_no_line_budget_bounds_it():
    from otto.host import LocalHost

    mgr = LocalHost()._session_mgr
    assert mgr.line_budget(route=ExecRoute.DEFAULT_SESSION) is None
    assert mgr.line_budget(route=ExecRoute.POOLED_SHELL) is None


def test_a_missing_line_editor_record_breaks_the_build(monkeypatch):
    from otto.host import session

    monkeypatch.setattr(session, "gap_for", lambda surface: None)
    with pytest.raises(LookupError, match="run-command-line-length"):
        session._line_budget_limitation()
