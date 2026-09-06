"""The hooks and the frame the customizations page documents are the ones the labs load."""

import contextlib
import io
import re
import sys

from otto.host.command_frame import BashFrame, RawFrame, SessionMarkers, ZephyrFrame
from otto.host.session_setup import SESSION_SETUPS, SessionSetup
from tests._fixtures.gs_example import import_gs_example, load_example_lab

M = SessionMarkers.for_session("cafef00d")


def test_registrations():
    import_gs_example()
    for name in ("provision-app", "enter-python", "land-on-zephyr"):
        assert name in SESSION_SETUPS


def test_unix_test1_carries_provision_app():
    host = load_example_lab("unix").hosts["test1"]
    assert host.session_setup == SessionSetup(name="provision-app", params={"env": "lab"})
    assert host.landing_frame is None


def test_pyrepl_entry_is_two_dialect():
    host = load_example_lab("pyrepl").hosts["test1-py"]
    assert host.session_setup == SessionSetup(name="enter-python")
    assert isinstance(host.landing_frame, BashFrame)
    assert type(host.command_frame).type_name == "pyrepl"
    assert host.ip == load_example_lab("unix").hosts["test1"].ip


def test_zephyr37_lfs_lands_raw():
    # Host ids are `slug(element.name)`, so the `zephyr37_lfs` element keys as
    # `zephyr37-lfs` -- the same spelling the bed roster in
    # `test_getting_started_example.py` uses.
    host = load_example_lab("embedded").hosts["zephyr37-lfs"]
    assert host.session_setup == SessionSetup(name="land-on-zephyr")
    assert isinstance(host.landing_frame, RawFrame)
    assert isinstance(host.command_frame, ZephyrFrame)


class TestPyReplFrame:
    def _frame(self):
        import_gs_example()
        from gs_example.pyrepl_frame import PyReplFrame

        return PyReplFrame()

    def test_handshake_silences_prompts_and_prints_the_token(self):
        hs = self._frame().handshake(M)
        assert "sys.ps1 = ''" in hs
        assert hs.rstrip().endswith(f"print({M.ready!r})")

    def test_frame_is_one_repl_line(self):
        line = self._frame().frame("1 + 1", M)
        assert line.count("\n") == 1
        assert line.endswith("\n")
        assert M.begin in line
        assert M.end_prefix in line

    def test_parse_a_printing_line(self):
        f = self._frame()
        buf = f"{M.begin}\n2\n{M.end_prefix}0__\n"
        assert f.parse_output(buf, "1 + 1", M) == "2"
        assert f.extract_retcode(buf, M) == 0

    def test_parse_a_raising_line(self):
        f = self._frame()
        buf = f"{M.begin}\ndivision by zero\n{M.end_prefix}1__\n"
        assert f.parse_output(buf, "1 / 0", M) == "division by zero"
        assert f.extract_retcode(buf, M) == 1

    def test_echoed_frame_is_skipped(self):
        f = self._frame()
        echoed = f.frame("1 + 1", M)
        buf = f"{echoed}{M.begin}\n2\n{M.end_prefix}0__\n"
        assert f.parse_output(buf, "1 + 1", M) == "2"

    def test_recover_is_echo_proof(self):
        f = self._frame()
        probe = f.recover(M)
        assert f.recover_pattern(M).search(probe) is None  # the echo cannot match
        assert f.recover_pattern(M).search(f"{M.recover}0__\n") is not None

    def test_restore_puts_prompts_back(self):
        assert re.search(r"sys\.ps1 = '>>> '", self._frame().restore_interactive())

    @staticmethod
    def _run(payload: str, ns: dict) -> str:
        """Execute a rendered payload in *ns* the way the REPL would, capturing its output."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(payload, ns)  # noqa: S102 -- executing the render half IS the test
        return buf.getvalue()

    def test_round_trip_through_a_real_interpreter(self, monkeypatch):
        """Render, run in a real interpreter, parse back what it printed.

        The other tests feed the parse half hand-built buffers, so a render
        half that stops agreeing with them still passes. This one closes the
        loop: every buffer parsed here is output the frame's own payload
        produced.
        """
        # The handshake really does assign sys.ps1/sys.ps2 in this process, and
        # code elsewhere reads `hasattr(sys, "ps1")` as "interpreter is
        # interactive". Pre-setting them through monkeypatch means teardown
        # removes them again (they are absent in a non-interactive process).
        monkeypatch.setattr(sys, "ps1", "", raising=False)
        monkeypatch.setattr(sys, "ps2", "", raising=False)
        f = self._frame()
        ns: dict = {}

        # The handshake defines the helper and announces readiness, nothing else.
        assert self._run(f.handshake(M), ns).splitlines() == [M.ready]

        raised = self._run(f.frame("1 / 0", M), ns)
        assert f.parse_output(raised, "1 / 0", M) == "division by zero"
        assert f.extract_retcode(raised, M) == 1

        printed = self._run(f.frame("1 + 1", M), ns)
        assert f.parse_output(printed, "1 + 1", M) == "2"
        assert f.extract_retcode(printed, M) == 0

        # The probe must satisfy its own pattern once a REPL has EXECUTED it.
        assert f.recover_pattern(M).search(self._run(f.recover(M), ns)) is not None

    def test_extract_retcode_is_minus_one_when_the_line_never_closed(self):
        # A hung command must not read as success: `-1`, never `0`.
        assert self._frame().extract_retcode(f"{M.begin}\npartial\n", M) == -1

    def test_marks_begin_only_on_the_begin_chunk(self):
        f = self._frame()
        assert f.marks_begin(f"{M.begin}\n", M) is True
        assert f.marks_begin("noise\n", M) is False
