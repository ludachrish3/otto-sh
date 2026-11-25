"""The terminal a shell-routed ``exec`` leaves: its session's record, put back (#935).

A bash-family handshake keeps the terminal's settings, as otto's own ``stty``
left them, in the shell's ``__otto_base``. Every exec frame's tail restores
the record its route names (the session's baseline for a pooled session,
the terminal as the exec found it for the default session, so ``run``'s own
changes stay), and the post-recovery line restores it after a timed-out
exec. Each against the real shells of the matrix on a pty
(:mod:`tests.unit.host._pty_session`), zsh aside: its line editor resets
the terminal at each prompt, so it cannot tell a restore from none (M3).

The record is (re)established, without overwriting one already there, on
every managed shell transition: ``login_proxy._identity_probe`` carries
``establish_exec_state(replace=False)``.

No module-level asyncio mark: this module mixes sync and async tests.
"""

import logging
import re
from typing import Any

import pytest

from otto.host import login_proxy
from otto.host.command_frame import AshFrame, BashFrame, ExecRestore, SessionMarkers
from otto.host.login_proxy import LoginProxyError, _identity_pattern, _identity_probe, _resync_shell
from otto.host.session import typed_line_ceiling
from otto.host.terminal_mapping import START_VAR, establish_exec_state, saved_into
from otto.utils import wait_for
from tests._fixtures.interactive_shell import (
    READY_TIMEOUT,
    RESEND_MARKERS,
    TAIL_MARKERS,
    interactive_shell,
)
from tests.conformance._vocabulary import POSIX
from tests.unit.host._pty_session import PTY_SHELLS, pty_session

M = SessionMarkers.for_session(session_id="0" * 8)

TERMINAL_SHELLS = [n for n in PTY_SHELLS if n != "zsh"]
"""The shells a restore is observable on.

zsh's line editor resets the terminal at each prompt, so it cannot tell (M3)."""

LONG = "printf '%s\\n' " + "a" * 1400 + " >/dev/null; echo longok"
"""A 1,400-character exec: long enough that ash's line editor cuts it on an echoing terminal."""

STATE = "echo ${__otto_base+b}${__otto_e+e}${__otto_tty+t}:${__otto_base-}"
"""Which of the three exec variables are set, then the terminal record itself."""


_TERMINAL_ANSWER = re.compile(r"^(?:no)?echo (?:no)?icanon\r*$", re.MULTILINE)
"""A line :attr:`POSIX.terminal_read_command` answers with, as the terminal printed it."""


def _read() -> str:
    assert POSIX.terminal_read_command is not None
    return POSIX.terminal_read_command


def _change() -> str:
    assert POSIX.terminal_change_command is not None
    return POSIX.terminal_change_command


@pytest.fixture
def _fast_resync(monkeypatch: pytest.MonkeyPatch) -> None:
    """No settle before the resync's first probe: the pty has no transition to flush."""
    monkeypatch.setattr(login_proxy, "_RESYNC_SETTLE", 0.0)


@pytest.mark.parametrize("name", TERMINAL_SHELLS)
@pytest.mark.asyncio
async def test_the_handshake_baseline_is_what_the_next_exec_sees(name):
    async with pty_session(name) as s:
        assert (await s.exec_(_read())).value == POSIX.terminal_baseline_answer


@pytest.mark.parametrize("name", TERMINAL_SHELLS)
@pytest.mark.asyncio
async def test_a_terminal_change_does_not_reach_the_next_exec_even_a_long_one(name):
    async with pty_session(name) as s:
        await s.exec_(_change())
        long = await s.exec_(LONG, timeout=5)
        after = await s.exec_(_read())
    assert (long.value, after.value) == ("longok", POSIX.terminal_baseline_answer)  # M3


@pytest.mark.parametrize("name", TERMINAL_SHELLS)
@pytest.mark.asyncio
async def test_a_timed_out_exec_is_restored_before_the_next_one(name):
    async with pty_session(name) as s:
        r = await s.exec_(f"{_change()}; exec sleep 30", timeout=0.5)
        assert r.timed_out
        assert not s._needs_recovery
        assert s.alive
        long = await s.exec_(LONG, timeout=5)
        after = await s.exec_(_read())
    assert (long.value, after.value) == ("longok", POSIX.terminal_baseline_answer)  # M4


@pytest.mark.parametrize("name", TERMINAL_SHELLS)
@pytest.mark.asyncio
async def test_a_console_exec_restores_to_its_own_start_so_run_keeps_its_terminal(name):
    async with pty_session(name, restore=ExecRestore.START) as s:
        await s.run_cmd(cmd="stty -icanon </dev/tty")
        await s.exec_("stty echo </dev/tty")
        after = await s.run_cmd(cmd=_read())
    assert after.value == "noecho noicanon"  # M5


@pytest.mark.parametrize("name", TERMINAL_SHELLS)
@pytest.mark.asyncio
async def test_an_exec_with_no_time_left_types_nothing(name, monkeypatch, tmp_path):
    """``timeout=0`` times out before the command's frame is written.

    The session's own stop after a timeout may still type (an interrupt and
    its resync probe); the command never is."""
    marker = tmp_path / "ran"
    async with pty_session(name) as s:
        writes: list[str] = []
        real = type(s).write_transport

        async def recording(self: Any, *, data: Any) -> None:
            writes.append(str(data))
            await real(self, data=data)

        monkeypatch.setattr(type(s), "write_transport", recording)
        assert (await s.exec_(f"touch {marker}", timeout=0)).timed_out
        assert [w for w in writes if str(marker) in w] == [], "the command is never typed"
        monkeypatch.undo()
        assert s.alive
        assert (await s.exec_("echo next")).value == "next"
    assert not marker.exists()


@pytest.mark.parametrize("name", TERMINAL_SHELLS)
@pytest.mark.asyncio
async def test_an_exec_that_never_starts_restores_no_earlier_exec_s_terminal(name):
    async with pty_session(name, restore=ExecRestore.START) as s:
        await s.exec_("true")
        await s.run_cmd(cmd=_change())
        assert (await s.exec_("true", timeout=0)).timed_out
        after = await s.run_cmd(cmd=_read())
        assert s.alive
    assert after.value == "echo noicanon"


@pytest.mark.parametrize("name", TERMINAL_SHELLS)
@pytest.mark.parametrize("setup", ["set -e", "set -eu"])
@pytest.mark.parametrize("restore", list(ExecRestore))
@pytest.mark.asyncio
async def test_a_failed_snapshot_is_no_restore_and_the_session_survives(
    name, setup, restore, failing_stty_on_path
):  # M16
    async with pty_session(name, setup=setup, restore=restore, env=failing_stty_on_path) as s:
        # Which stty the shell resolved, named in any failure. Not via
        # ``record_property``: it warns (an error here) under ``--junitxml``
        # in a single-process run, which every xdist lane hides.
        stty = (await s.run_cmd(cmd="type stty")).value
        r = await s.exec_("printf ok", timeout=5)
        t = await s.exec_("exec sleep 30", timeout=0.3)
        got = (r.value, r.retcode, t.timed_out, s.alive)
        assert got == ("ok", 0, True, True), f"stty resolved to: {stty}"
        assert not s.exec_baseline_lost, f"stty resolved to: {stty}"


_CONTROL_SHELLS = [n for n in ("dash", "busybox-sh") if n in PTY_SHELLS]
"""The shells the control runs on: bash and ksh repair their own line editor (M4)."""


def test_the_control_has_a_shell_to_run_on():
    assert _CONTROL_SHELLS, "neither dash nor busybox sh is installed: the control runs nowhere"


@pytest.mark.parametrize("name", _CONTROL_SHELLS)
@pytest.mark.asyncio
async def test_control_without_the_tail_and_the_post_recovery_line_the_change_is_seen(
    name, monkeypatch
):  # M4: bash and ksh repair their own line editor, so they cannot discriminate
    monkeypatch.setattr(BashFrame, "terminal_restore", lambda self, restore: ":")
    async with pty_session(name) as s:
        await s.exec_(_change())  # arm (tail)
        tail = (await s.exec_(_read())).value
        await s.exec_("stty -echo icanon </dev/tty")  # back to the baseline by hand
        await s.exec_(f"{_change()}; exec sleep 30", timeout=0.5)  # arm (after recovery)
        recovered = (await s.exec_(_read())).value
    assert tail == "echo noicanon"
    assert recovered.startswith("echo")


def test_each_restore_half_is_in_both_compositions():  # DEF-TAIL
    f = BashFrame()
    for restore in ExecRestore:
        line = f.restore_line(restore, M)
        assert line is not None
        for half in (f.terminal_restore(restore), f.errexit_restore()):
            assert half in f.frame_exec("c", m=M, stdin_eof=True, restore=restore)
            assert half in line
    assert establish_exec_state(replace=True) in f.handshake(m=M)
    assert saved_into(START_VAR) in f.frame_exec(
        "c", m=M, stdin_eof=True, restore=ExecRestore.START
    )


def test_the_post_recovery_line_fits_an_echoing_terminal():
    line = BashFrame().restore_line(ExecRestore.START, M)  # what the stop actually writes
    assert line is not None
    assert max(len(x) for x in line.split("\n")) < typed_line_ceiling(AshFrame(), echoes=True)


@pytest.mark.parametrize("name", PTY_SHELLS)
def test_exec_restore_line_with_lost_state_answers_and_restores_nothing(name):
    """With no exec variable set, under ``set -eu``, the whole restore line still answers."""
    line = BashFrame().restore_line(ExecRestore.BASELINE, TAIL_MARKERS)
    pattern = BashFrame().restored_pattern(TAIL_MARKERS)
    assert line is not None
    assert pattern is not None
    with interactive_shell(name, setup="set -u") as shell:
        before = _TERMINAL_ANSWER.findall(shell.run(_read()))
        shell.type_line(line, pattern, READY_TIMEOUT)
        out = shell.run("echo probe=${__otto_probe-unset} e=${__otto_e-unset}; " + _read())
    assert "probe=unset e=unset" in out
    assert _TERMINAL_ANSWER.findall(out) == before
    assert len(before) == 1


def test_establishing_on_replace_snapshots_the_terminal():
    assert saved_into("__otto_base") in establish_exec_state(replace=True)
    assert "__otto_e=" in establish_exec_state(replace=True)
    assert "__otto_tty=" in establish_exec_state(replace=True)


# --- DEF-EST: the record across shell transitions -----------------------------------------


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.parametrize("options", ["set -u", "set -e", "set -eu"])
def test_a_fresh_nounset_shell_establishes_and_a_cut_resend_does_not_confirm(name, options):
    history = BashFrame().quiet_history()
    with interactive_shell(name, setup=options) as shell:
        shell.type_line(
            _identity_probe(TAIL_MARKERS, history), _identity_pattern(TAIL_MARKERS), READY_TIMEOUT
        )
        state = shell.run("printf probe=${__otto_probe-unset}' '; " + STATE)
        assert re.search(r"probe=unset bet:\S+", state), state
        # The resend cut just before its final echo: the anchor is not set, so
        # it must not confirm. Under `set -eu` the unset anchor may end the
        # shell (bash, mksh, zsh), which does not confirm either.
        cut = _identity_probe(RESEND_MARKERS, history)
        whole = SessionMarkers.for_session(session_id="feedc0de")
        shell.send(cut[cut.rindex("echo ") :])
        shell.send(_identity_probe(whole, history))
        wait_for(
            lambda: (
                shell.terminal.proc.poll() is not None
                or _identity_pattern(whole).search(shell.output) is not None
            ),
            READY_TIMEOUT,
            interval=0.01,
            on_timeout=f"{name}: neither answered the whole probe nor exited",
        )
        assert _identity_pattern(RESEND_MARKERS).search(shell.output) is None, shell.output
        if options != "set -eu":  # only there may the unset anchor end the shell
            assert shell.terminal.proc.poll() is None, shell.output
            assert _identity_pattern(whole).search(shell.output) is not None, shell.output


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.parametrize("options", ["", "set -u"])
def test_the_whole_resync_keeps_a_seeded_outer_record(name, options):
    """The resync probe, typed whole into a shell that has a record, leaves the record alone."""
    seed = "__otto_base=OUTER; __otto_e=e; __otto_tty=T"
    with interactive_shell(name, setup="; ".join(p for p in (options, seed) if p)) as shell:
        shell.type_line(
            _identity_probe(TAIL_MARKERS, BashFrame().quiet_history()),
            _identity_pattern(TAIL_MARKERS),
            READY_TIMEOUT,
        )
        out = shell.run("printf state=; " + STATE)
    assert "state=bet:OUTER" in out


async def _state(s) -> str:
    return (await s.run_cmd(cmd=STATE)).value


@pytest.mark.parametrize("name", TERMINAL_SHELLS)
@pytest.mark.usefixtures("_fast_resync")
@pytest.mark.asyncio
async def test_a_rejected_switch_after_a_terminal_change_keeps_the_outer_record(name):
    async with pty_session(name) as s:
        before = await _state(s)
        await s.run_cmd(cmd="stty -icanon </dev/tty")
        changed = (await s.run_cmd(cmd="2>/dev/null stty -g")).value
        # su rejected the password and exited: the shell answering is the outer one.
        with pytest.raises(LoginProxyError, match="answered as"):
            await _resync_shell(s, "h", "nobody", expect_user="otto-no-such-user")
        after = await _state(s)
    assert after == before
    assert after != f"bet:{changed}"


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.usefixtures("_fast_resync")
@pytest.mark.asyncio
async def test_a_switch_into_a_fresh_shell_establishes_and_its_undo_keeps_the_outer(name):
    async with pty_session(name) as s:
        outer = await _state(s)
        await s.send("sh\n")  # the switch: a nested shell, none of the variables set
        await _resync_shell(s, "h", "nested")
        nested = await _state(s)
        await s.send("exit\n")  # the undo: back to the outer shell
        await _resync_shell(s, "h", "outer")
        back = await _state(s)
    assert nested.startswith("bet:")
    assert back == outer


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.usefixtures("_fast_resync")
@pytest.mark.asyncio
async def test_a_custom_undo_landing_in_a_fresh_shell_establishes(name):
    async with pty_session(name) as s:
        await s.send("exec sh\n")  # a custom undo that replaces the shell
        await _resync_shell(s, "h", "fresh")
        assert (await _state(s)).startswith("bet:")


# --- A lost record ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", TERMINAL_SHELLS)
@pytest.mark.asyncio
async def test_a_lost_record_is_reported_once_and_never_re_snapshotted(name, caplog):
    async with pty_session(name) as s:
        await s.run_cmd(cmd="unset __otto_base")
        with caplog.at_level(logging.WARNING, logger="otto.host.session"):
            first = await s.exec_("printf one")
            second = await s.exec_("printf two")
        assert (first.value, second.value) == ("one", "two")
        assert s.exec_baseline_lost
        assert (await s.run_cmd(cmd="echo ${__otto_base+set}")).value == ""
    lost = [r for r in caplog.records if "baseline lost" in r.getMessage()]
    assert len(lost) == 1


class _SinkError(Exception):
    """The host log's sink failed on a command's output."""


@pytest.mark.parametrize("name", TERMINAL_SHELLS)
@pytest.mark.asyncio
async def test_a_lost_record_is_noted_before_a_failing_sink_sees_the_output(name):
    """A sink that fails on the unfinished tail costs log lines, never the session's health."""

    def failing(line: str) -> None:
        raise _SinkError(line)

    async with pty_session(name) as s:
        await s.run_cmd(cmd="unset __otto_base")
        with pytest.raises(_SinkError):
            await s.exec_("printf one", on_output=failing)
        assert s.exec_baseline_lost
