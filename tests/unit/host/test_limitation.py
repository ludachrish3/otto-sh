"""A difference from otto's behaviour promises is declared as a :class:`Limitation`."""

import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from otto.host.command_frame import (
    BUILTIN_FRAMES,
    BashFrame,
    ExecInput,
    build_command_frame,
    register_command_frame,
)
from otto.host.limitation import Limitation
from otto.host.session import (
    LINE_BUDGET_LIMITATION,
    SESSION_EXEC_LIMITATIONS,
    SessionManager,
    typed_line_budget,
)
from otto.registry import Ref
from tests.unit.host._sessions import session_manager

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
    # Every exec on a shell session is otto's own code.
    for name, lim in SESSION_EXEC_LIMITATIONS.items():
        assert lim.kind == "device", name
    for frame in BUILTIN_FRAMES:
        lim = type(build_command_frame(type_name=frame.name)).exact_output
        assert lim is None or lim.kind == "device", frame.name


def test_a_session_exec_declares_the_line_budget_in_one_place():
    assert SESSION_EXEC_LIMITATIONS["line_budget"] is LINE_BUDGET_LIMITATION


def test_each_session_is_budgeted_in_the_exec_frame_it_types():
    manager = session_manager(
        connections=cast("ConnectionManager", SimpleNamespace(term="telnet", proxy_hops=[])),
        name="h",
        command_frame=BashFrame(),
    )
    # A pooled session and the default one, each with its own restore.
    for shared in (False, True):
        typed = ExecInput(stdin_eof=True, restore=SessionManager._exec_restore_for(shared))
        assert manager.line_budget(shared=shared) == typed_line_budget(
            BashFrame(), echoes=True, exec_input=typed
        )


def test_the_line_budget_reads_its_measurements_from_their_one_home():
    from otto.host.session import (
        _LINE_EDITOR_RECORD,
        _TYPED_LINE_MEASUREMENTS,
    )
    from otto.host.userland import gap_for

    gap = gap_for(_LINE_EDITOR_RECORD)
    assert gap is not None, _LINE_EDITOR_RECORD
    measured = LINE_BUDGET_LIMITATION.measured_on
    assert measured == _TYPED_LINE_MEASUREMENTS
    # The gap record's measured_on is prose, so the short entries cannot be
    # derived from it; instead EVERY number each entry states (dates, versions,
    # character counts, read off the entry itself rather than listed here) must
    # be one the record states.
    for entry in _TYPED_LINE_MEASUREMENTS:
        numbers = re.findall(r"\d+(?:[.-]\d+)*", entry)
        assert numbers, entry
        for number in numbers:
            assert number in gap.measured_on, (number, entry)


def test_the_line_budget_reason_states_the_ceiling_from_its_constant():
    from otto.host.userland import PTY_TYPED_LINE_MAX

    reason = LINE_BUDGET_LIMITATION.reason
    assert str(PTY_TYPED_LINE_MAX) in reason


def test_a_local_shell_reads_a_pipe_so_no_line_budget_bounds_it():
    from otto.host import LocalHost

    mgr = LocalHost()._session_mgr
    assert mgr.line_budget(shared=True) is None
    assert mgr.line_budget(shared=False) is None


def test_a_missing_line_editor_record_breaks_the_build(monkeypatch):
    from otto.host import session

    monkeypatch.setattr(session, "gap_for", lambda surface: None)
    with pytest.raises(LookupError, match="run-command-line-length"):
        session._line_budget_limitation()
