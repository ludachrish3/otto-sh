"""The exec frame: a shell-routed ``exec`` runs in a subshell of its own (#935).

What it promises, each against the real shells of the matrix on a pty
(:mod:`tests.unit.host._pty_session`): nothing the command changes reaches
the next command; an inherited ``set -e`` still stops it at its first
failure and its own status comes back; its stdin is at end of file; a
timed-out ``exec`` gives the shell back its ``set -e``. And the pieces that
make those hold: the end record and its parser, the status rule above 255,
the one body rule both frames apply, the isolation a frame's class carries,
the registration rule that keeps an exec frame readable, the measured
payloads, and the test side's one reader of both frame shapes.

No module-level asyncio mark: this module mixes sync and async tests, and a
module mark on a sync test is a warning, which is an error here.
"""

import asyncio
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from otto.host.command_frame import (
    AshFrame,
    BashFrame,
    ExecInput,
    ExecRestore,
    SessionMarkers,
    ZephyrFrame,
    ZephyrSerialFrame,
    _check_frame_class,
)
from otto.host.element import Element
from otto.host.embedded_host import EmbeddedHost
from otto.host.exit_status import subshell_status_of
from otto.host.output_codec import PatternReader
from otto.host.terminal_mapping import establish_exec_state
from otto.logger.mode import LogMode
from tests._fixtures import exec_frame_payloads
from tests._fixtures.fake_shell import end_record, parse_bash_frame
from tests._fixtures.paths import ensure_custom_hosts_on_path
from tests.conformance._vocabulary import POSIX
from tests.unit.host._pty_session import (
    PTY_SHELLS,
    PtySession,
    pty_session,
    reads_bang_and_hash_as_a_script_does,
)
from tests.unit.host._session_feed import scripted_shell, scripted_shell_unopened
from tests.unit.host.test_frame_shell_portability import FRAME_EDGE_CASES
from tests.unit.host.test_session_exec_pooled import _exec, _mgr

# As tests/unit/host/test_shell_transcripts.py imports it: the package's own
# name, which the registry already knows.
ensure_custom_hosts_on_path()
from custom_hosts.zephyr_inline import ZephyrInlineRetcodeFrame

ANY_VALUE = object()
READ = POSIX.state_read_command
M = SessionMarkers.for_session(session_id="0" * 8)


def embedded_host_over(session):
    """An :class:`EmbeddedHost` whose default session is *session* (alive)."""
    host = EmbeddedHost(
        ip="192.0.2.1",
        element=Element(name="zephyr37_fat"),
        log=LogMode.QUIET,
        command_frame=ZephyrFrame(),
    )
    host._live_session_mgr()._session = session
    return host


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.parametrize("setup", ["", "set -e", "set -eu"])
@pytest.mark.asyncio
async def test_state_set_in_one_exec_does_not_reach_the_next(name, setup):
    assert POSIX.exec_state_set_command is not None
    assert POSIX.exit_session_command is not None
    assert READ is not None
    async with pty_session(name, setup=setup) as s:
        before = (await s.exec_(READ)).value
        await s.exec_(POSIX.exec_state_set_command)
        await s.exec_(POSIX.exit_session_command)
        after = (await s.exec_(READ)).value
    assert after == before


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.parametrize("setup", ["", "set -e", "set -eu"])
@pytest.mark.parametrize(
    ("cmd", "value", "code"),
    [
        ("false; printf continued", None, None),  # value and code depend on setup (M2)
        ("exit 7", "", 7),
        ("sh -c 'kill -TERM $$'", ANY_VALUE, 143),  # a pty may show the shell's `Terminated`
        ("exec sh -c 'kill -TERM $$'", ANY_VALUE, 143),
        ("f() { return 271; }; f", "", 15),  # M1: truncated in the subshell
    ],
)
@pytest.mark.asyncio
async def test_the_end_marker_always_arrives_with_the_command_s_own_semantics(
    name, setup, cmd, value, code
):
    async with pty_session(name, setup=setup) as s:
        r = await s.exec_(cmd, timeout=5)
        if value is None:
            value, code = ("", 1) if "e" in setup else ("continued", 0)
        assert (r.timed_out, r.retcode) == (False, code)
        assert s.alive
        if value is not ANY_VALUE:
            assert r.value == value
        assert (await s.exec_("printf '%s\\n' ok")).value == "ok"


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.parametrize("setup", ["set -e", "set -eu"])
@pytest.mark.asyncio
async def test_a_timed_out_exec_gives_the_parent_back_its_errexit(name, setup):
    async with pty_session(name, setup=setup) as s:
        assert (await s.exec_("exec sleep 30", timeout=0.3)).timed_out
        assert not s._needs_recovery
        assert s.alive
        opts = (await s.run_cmd(cmd="echo $-")).value
        nxt = await s.exec_("false; printf continued", timeout=5)
    assert "e" in opts
    assert ("u" in opts) == ("u" in setup)
    assert (nxt.value, nxt.retcode) == ("", 1)


@pytest.mark.asyncio
async def test_control_without_the_post_recovery_line_the_parent_keeps_set_plus_e(monkeypatch):
    async def _no_restore(self, restore):  # restores nothing, reports success
        return None

    monkeypatch.setattr(PtySession, "_restore_after_interrupt", _no_restore)
    async with pty_session("dash" if "dash" in PTY_SHELLS else "sh", setup="set -e") as s:
        await s.exec_("exec sleep 30", timeout=0.3)
        assert s.alive
        assert "e" not in (await s.run_cmd(cmd="echo $-")).value


_CANCEL_SHELLS = [n for n in ("bash", "dash", "busybox-sh") if n in PTY_SHELLS]
"""The shells a cancelled exec's recovery is pinned on: each skips the frame's tail on ``^C``."""

ARMED = "armed"
"""What the cancelled exec prints once its terminal change is made."""


def test_the_cancel_pins_have_a_shell_to_run_on():
    assert _CANCEL_SHELLS, "none of bash, dash, busybox sh is installed: the pins run nowhere"


async def _cancel_once_armed(run_one, cmd):
    """Start *run_one(cmd, on_output)*, cancel it once the command printed :data:`ARMED`."""
    armed = asyncio.Event()

    def sink(line: str) -> None:
        if ARMED in line:
            armed.set()

    task = asyncio.ensure_future(run_one(cmd, sink))
    await asyncio.wait_for(armed.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


_DAMAGE = f"{POSIX.terminal_change_command}; echo arm''ed; sleep 30"
"""An exec that changes the terminal, prints :data:`ARMED` (a word its own text does not
carry, so an echo of the typed line cannot match), then blocks until it is interrupted."""


class _WatchedPty(PtySession):
    """A :class:`PtySession` that sets :attr:`armed` once a read returns :data:`ARMED`."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.armed = asyncio.Event()

    async def read_transport_until(self, *, pattern: re.Pattern[str]) -> str:
        data = await super().read_transport_until(pattern=pattern)
        if ARMED in data:
            self.armed.set()
        return data


@pytest.mark.parametrize("name", _CANCEL_SHELLS)
@pytest.mark.asyncio
async def test_a_cancelled_exec_gives_the_parent_back_its_errexit_and_terminal(name):
    async with pty_session(name, setup="set -e") as s:
        await _cancel_once_armed(
            lambda cmd, sink: s.exec_(cmd, timeout=40, on_output=sink), _DAMAGE
        )
        opts = (await s.run_cmd(cmd="echo $-", timeout=10)).value
        terminal = (await s.run_cmd(cmd=POSIX.terminal_read_command, timeout=10)).value
        nxt = await s.exec_("false; echo reached", timeout=10)
        assert s.alive
    assert "e" in opts
    assert terminal == POSIX.terminal_baseline_answer
    assert (nxt.value, nxt.retcode) == ("", 1)


@pytest.mark.parametrize("name", _CANCEL_SHELLS)
@pytest.mark.asyncio
async def test_a_pooled_session_cancelled_mid_exec_is_closed_not_handed_out(name):
    """A cancelled pooled exec closes its session: the next exec is on a fresh one."""
    mgr = _mgr()
    made: list[_WatchedPty] = []

    def factory():
        made.append(_WatchedPty(name))
        return made[-1]

    mgr._session_factory = factory
    exec_input = ExecInput(stdin_eof=True, restore=ExecRestore.BASELINE)

    def pooled(cmd):
        return _exec(mgr, cmd=cmd, timeout=40, exec_input=exec_input)

    try:
        assert (await pooled("true")).retcode == 0
        (session,) = made
        assert (await session.run_cmd(cmd="set -e", timeout=5)).retcode == 0
        task = asyncio.ensure_future(pooled(_DAMAGE))
        await asyncio.wait_for(session.armed.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not session.alive
        terminal = await pooled(POSIX.terminal_read_command)
        nxt = await pooled("false; echo reached")
        assert len(made) == 2
        assert made[0] is session
    finally:
        for s in made:
            await s.close()
    assert terminal.value == POSIX.terminal_baseline_answer
    assert (nxt.value, nxt.retcode) == ("reached", 0), "a fresh session has no set -e"


class _RecordingPty(_WatchedPty):
    """A :class:`_WatchedPty` that records what is written to it."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.writes: list[str] = []

    async def write_transport(self, *, data) -> None:
        self.writes.append(str(data))
        await super().write_transport(data=data)


def _pooled_mgr(name):
    mgr = _mgr()
    made: list[_RecordingPty] = []

    def factory():
        made.append(_RecordingPty(name))
        return made[-1]

    mgr._session_factory = factory
    return mgr, made


def _idle_names(mgr) -> list[str]:
    return [s.name for idle in mgr._exec_pools.values() for s in idle]


_PARTIAL = "otto-part''ial"
"""What the stopped exec prints before it blocks (its typed text does not carry the word)."""


async def _stop_pooled(mgr, made, how):
    """Run one exec on the pooled session, stop the next by *how*, and return what is known."""
    exec_input = ExecInput(stdin_eof=True, restore=ExecRestore.BASELINE)
    first = await _exec(mgr, cmd="true", exec_input=exec_input)
    assert first.retcode == 0
    (kept,) = _idle_names(mgr)
    (session,) = made
    cmd = f"echo {_PARTIAL}; echo arm''ed; sleep 30"
    written = len(session.writes)
    result = None
    if how == "timeout":
        result = await _exec(mgr, cmd=cmd, timeout=3, exec_input=exec_input)
        assert session.armed.is_set(), "the command never started within its budget"
    else:
        task = asyncio.ensure_future(_exec(mgr, cmd=cmd, timeout=40, exec_input=exec_input))
        await asyncio.wait_for(session.armed.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    after = session.writes[written:]
    return kept, session, result, after


@pytest.mark.parametrize("how", ["timeout", "cancel"])
@pytest.mark.parametrize("name", _CANCEL_SHELLS)
@pytest.mark.asyncio
async def test_a_stopped_pooled_exec_closes_its_terminal_session_probed_not_restored(name, how):
    """Interrupted and probed, never restored: the session is closed, the next exec gets another."""
    mgr, made = _pooled_mgr(name)
    try:
        kept, session, result, after = await _stop_pooled(mgr, made, how)
        # The command's own line, the interrupt, then the recovery probe that
        # confirms the command is gone; no restore line.
        assert after[1] == "\x03", after
        assert len(after) > 2, "no recovery probe after the ^C"
        restore_line = session._frame.restore_line(ExecRestore.BASELINE, session._markers)
        assert restore_line is not None
        assert restore_line not in after, after
        assert not session.alive
        assert kept not in _idle_names(mgr)
        nxt = await _exec(
            mgr,
            cmd="echo again",
            exec_input=ExecInput(stdin_eof=True),
        )
        assert nxt.value == "again"
        (fresh,) = _idle_names(mgr)
        assert fresh != kept
        assert len(made) == 2
    finally:
        for s in made:
            await s.close()
    if result is not None:
        assert result.timed_out
        assert "otto-partial" in result.value


def _running(pid: int) -> bool:
    """Whether *pid* is a process that has not exited (a zombie has)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


class _ClosingWitness(_RecordingPty):
    """A :class:`_RecordingPty` that notes, as its close starts, whether *pid_file*'s process runs.

    This double's close kills the pty's foreground group, which a real
    transport's close (an ssh channel's) does not: the witness is taken before
    it, so only the stop that ran before the close can have ended the command.
    """

    def __init__(self, name: str, pid_file) -> None:
        super().__init__(name)
        self.pid_file = pid_file
        self.running_at_close: bool | None = None

    async def close(self) -> None:
        if self.running_at_close is None and self.pid_file.exists():
            self.running_at_close = _running(int(self.pid_file.read_text()))
        await super().close()


@pytest.mark.parametrize("name", _CANCEL_SHELLS)
@pytest.mark.asyncio
async def test_a_timed_out_pooled_exec_returns_once_its_command_is_gone(name, tmp_path):
    """By the time the pool closes the stopped session, the process its command started has exited.

    The command's process takes a moment to die on the interrupt (its INT trap
    sleeps first), so only a stop that waits for the shell to come back sees
    it gone.
    """
    start = tmp_path / "pid"
    cmd = (
        'sh -c \'trap "sleep 1; exit 0" INT; '
        f'echo $$ > {start}; echo arm""ed; while :; do sleep 0.1; done\''
    )
    mgr = _mgr()
    made: list[_ClosingWitness] = []

    def factory():
        made.append(_ClosingWitness(name, start))
        return made[-1]

    mgr._session_factory = factory
    try:
        result = await _exec(
            mgr,
            cmd=cmd,
            timeout=3,
            exec_input=ExecInput(stdin_eof=True, restore=ExecRestore.BASELINE),
        )
        assert result.timed_out
        (session,) = made
        assert session.armed.is_set(), "the command never started within its budget"
        assert not session.alive
        assert session.running_at_close is False, (
            f"the session was closed while its command still ran: {session.running_at_close}"
        )
    finally:
        for s in made:
            await s.close()


@pytest.mark.parametrize("how", ["timeout", "cancel"])
@pytest.mark.asyncio
async def test_a_stopped_exec_on_a_session_of_its_own_is_still_recovered(how):
    """Off the pool (a default session's exec), the stop still probes and restores."""
    name = _CANCEL_SHELLS[0]
    s = _RecordingPty(name)
    try:
        await s._ensure_initialized()
        written = len(s.writes)
        if how == "timeout":
            r = await s.exec_("echo arm''ed; sleep 30", timeout=3)
            assert r.timed_out
        else:
            await _cancel_once_armed(
                lambda cmd, sink: s.exec_(cmd, timeout=40, on_output=sink),
                "echo arm''ed; sleep 30",
            )
        assert len(s.writes[written:]) > 2, "recovered: a probe and a restore after the ^C"
        assert s.alive
        assert (await s.exec_("echo again", timeout=10)).value == "again"
    finally:
        await s.close()


class _UnconfirmedRestore(_WatchedPty):
    """A :class:`_WatchedPty` whose restore line gets no confirmation, and that records writes."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.writes: list[str] = []
        self.refuse_restore = False

    async def write_transport(self, *, data) -> None:
        self.writes.append(str(data))
        await super().write_transport(data=data)

    async def read_transport_until(self, *, pattern: re.Pattern[str]) -> str:
        # The restore line is typed, and its confirmation is waited for under the
        # product's own bound on text the shell never prints: a missing reply.
        if self.refuse_restore and pattern == self._frame.restored_pattern(self._markers):
            pattern = re.compile("otto-never-said")
        return await super().read_transport_until(pattern=pattern)


@pytest.mark.parametrize("name", _CANCEL_SHELLS)
@pytest.mark.asyncio
async def test_a_session_whose_restore_is_not_confirmed_gets_no_next_command(name):
    s = _UnconfirmedRestore(name)
    try:
        await s._ensure_initialized()
        task = asyncio.ensure_future(s.exec_(_DAMAGE, timeout=40))
        await asyncio.wait_for(s.armed.wait(), 5)
        s.refuse_restore = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        before = len(s.writes)
        with pytest.raises(RuntimeError, match="session is not alive"):
            await s.exec_("echo otto-next-command", timeout=5)
        assert not s.alive
        assert not any("otto-next-command" in w for w in s.writes[before:]), s.writes[before:]
    finally:
        await s.close()


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.parametrize("restore", list(ExecRestore))
@pytest.mark.asyncio
async def test_an_exec_that_never_starts_restores_no_earlier_exec_s_errexit(name, restore):
    async with pty_session(name, restore=restore) as s:
        await s.run_cmd(cmd="set -e")
        assert (await s.exec_("true")).retcode == 0
        await s.run_cmd(cmd="set +e")
        assert (await s.exec_("true", timeout=0)).timed_out
        opts = (await s.run_cmd(cmd="echo $-")).value
        nxt = await s.exec_("false; printf continued", timeout=5)
        assert s.alive
    assert "e" not in opts
    assert (nxt.value, nxt.retcode) == ("continued", 0)


@pytest.mark.parametrize(
    ("reported", "expected"), [(0, 0), (7, 7), (143, 143), (255, 255), (271, 143), (265, 137)]
)
def test_the_exec_status_canonicalises_only_above_255(reported, expected):
    assert subshell_status_of(reported) == expected
    assert BashFrame().extract_exec_retcode(end_record(M, reported, flag="B"), m=M) == expected


def test_a_missing_end_is_still_minus_one():
    assert BashFrame().extract_exec_retcode("no marker", m=M) == -1


@pytest.mark.skipif(
    shutil.which("ksh") is None, reason="ksh is optional, as in test_frame_shell_portability"
)
def test_a_ksh_signal_death_in_the_exec_frame_parses_as_143():
    payload = BashFrame().frame_exec(
        "exec sh -c 'kill -TERM $$'", m=M, stdin_eof=True, restore=ExecRestore.BASELINE
    )
    out = subprocess.run(["ksh"], input=payload, capture_output=True, text=True, check=False).stdout
    assert BashFrame().extract_exec_retcode(out, m=M) == 143
    assert BashFrame().extract_retcode(out, m=M) == 271


@pytest.mark.parametrize("case", FRAME_EDGE_CASES)
@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_frame_exec_keeps_every_frame_edge_case(case, name):
    async with pty_session(name) as s:
        r = await s.exec_(case.text, timeout=5)
    assert (r.timed_out, r.value, r.retcode) == (False, case.expected, case.code)


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_an_exec_reads_end_of_file_and_a_run_keeps_the_terminal(name):
    assert POSIX.stdin_eof_command is not None
    async with pty_session(name) as s:
        r = await s.exec_(POSIX.stdin_eof_command, timeout=5)
        assert (r.timed_out, r.value) == (False, "otto-cf-eof")
        t = await s.run_cmd(cmd="[ -t 0 ] && echo tty", timeout=5)
        assert t.value == "tty"


def test_frame_and_frame_exec_share_one_body_rule():
    for cmd in ["", "# only a comment", "x \\", "a\nb"]:
        body = BashFrame._frame_body(cmd)
        assert body in BashFrame().frame(cmd, m=M)
        assert body in BashFrame().frame_exec(
            cmd, m=M, stdin_eof=True, restore=ExecRestore.BASELINE
        )


def test_isolation_is_derived_from_the_frame_exec_a_frame_has():
    assert BashFrame().isolates_exec
    assert AshFrame().isolates_exec
    for frame in (ZephyrFrame(), ZephyrSerialFrame(), ZephyrInlineRetcodeFrame()):
        assert not frame.isolates_exec
        assert frame.frame_exec(
            "c", m=M, stdin_eof=True, restore=ExecRestore.BASELINE
        ) == frame.frame("c", m=M)

    class DirectBash(BashFrame):
        type_name = "direct-bash"

    class BelowAsh(AshFrame):
        type_name = "below-ash"

    class OwnZephyr(ZephyrFrame):
        type_name = "own-zephyr"

    class Delegating(ZephyrFrame):
        """Overrides frame_exec only to delegate to frame: the author's error the docs name."""

        type_name = "delegating"

        def frame_exec(self, cmd, *, m, stdin_eof, restore):
            return self.frame(cmd, m=m)

    assert DirectBash().isolates_exec
    assert BelowAsh().isolates_exec
    assert not OwnZephyr().isolates_exec
    assert Delegating().isolates_exec  # the derivation reads the override, not the payload


def test_a_frame_that_re_spells_frame_below_frame_exec_is_refused():
    class Own(BashFrame):
        type_name = "own-bash"

        def frame(self, cmd, *, m):
            return f"{m.begin}\n{cmd}\n{m.end_prefix}\n"

    with pytest.raises(TypeError, match="frame_exec"):
        _check_frame_class("own-bash", Own)


def test_a_frame_that_re_spells_end_pattern_below_frame_exec_is_refused():
    class Own(AshFrame):
        type_name = "own-ash"

        def end_pattern(self, m):
            return re.compile(re.escape(m.end_prefix))

    with pytest.raises(TypeError, match="frame_exec"):
        _check_frame_class("own-ash", Own)


def test_an_isolating_frame_without_the_restore_protocol_is_refused():
    class NoRestore(ZephyrFrame):
        type_name = "no-restore"

        def frame_exec(self, cmd, *, m, stdin_eof, restore):
            return self.frame(cmd, m=m)

    with pytest.raises(TypeError, match="restore_line"):
        _check_frame_class("no-restore", NoRestore)


def test_every_frame_otto_and_its_examples_register_passes_the_exec_frame_check():
    for frame in (BashFrame, AshFrame, ZephyrFrame, ZephyrSerialFrame, ZephyrInlineRetcodeFrame):
        _check_frame_class(frame.type_name, frame)


def test_the_base_exec_status_is_the_run_status():
    out = f"{M.begin}\r\nx\r\nretval\r\n0\r\n{M.end_prefix}"
    assert ZephyrFrame().extract_exec_retcode(out, m=M) == ZephyrFrame().extract_retcode(out, m=M)


@pytest.mark.asyncio
async def test_an_embedded_exec_renders_run_s_frame_and_parses_its_status():
    s = await scripted_shell(frame=ZephyrFrame(), replies={"kernel version": ("Zephyr 3.7", 0)})
    host = embedded_host_over(s)
    r = await host.exec("kernel version")
    assert s.written_frames[-1] == ZephyrFrame().frame("kernel version", m=s.markers)
    assert (r.value, r.retcode) == ("Zephyr 3.7", 0)


@pytest.mark.asyncio
async def test_a_pooled_exec_reaches_the_session_with_one_exec_input():
    mgr = _mgr()
    made = []

    def factory():
        made.append(scripted_shell_unopened(replies={"true": ("", 0)}))
        return made[-1]

    mgr._session_factory = factory
    r = await _exec(mgr, cmd="true", exec_input=ExecInput(stdin_eof=True))
    assert (r.timed_out, r.retcode) == (False, 0)
    framed = next(
        f for f in map(parse_bash_frame, made[-1].written_frames) if f and f.cmd == "true"
    )
    assert framed.isolated
    assert framed.stdin_eof
    assert framed.cmd == "true"


_MEASURED_COMMANDS = [
    "c",
    "echo 'it'\\''s' \"$HOME\" '$x'",
    "printf a\nprintf b",
    "cat <<'EOF'\n$x 'q'\nEOF",
    "# only a comment",
    "",
    "echo a \\",
]
"""Commands whose quotes, newlines, ``$``, here-doc and ends both renderers must agree on."""


@pytest.mark.parametrize("cmd", _MEASURED_COMMANDS)
def test_the_measured_payloads_are_the_product_frames(cmd):
    assert M.restored == exec_frame_payloads.restored_marker(M)
    for restore, var in ((ExecRestore.BASELINE, "__otto_base"), (ExecRestore.START, "__otto_tty")):
        assert BashFrame().terminal_restore(restore) == exec_frame_payloads.terminal_restore(var)
        assert BashFrame().errexit_restore() == exec_frame_payloads.errexit_restore()
        assert BashFrame().restore_line(restore, M) == exec_frame_payloads.restore_line(var, M)
        for eof in (True, False):
            for credential in (True, False):
                expected = exec_frame_payloads.shape_c(
                    cmd,
                    m=M,
                    stdin_eof=eof,
                    start_var=var if restore is ExecRestore.START else None,
                    credential=credential,
                )
                assert (
                    BashFrame().frame_exec(
                        cmd, m=M, stdin_eof=eof, restore=restore, credential=credential
                    )
                    == expected
                )


def test_each_restore_half_appears_verbatim_in_both_compositions():
    frame = BashFrame()
    for restore in ExecRestore:
        line = frame.restore_line(restore, M)
        payload = frame.frame_exec("c", m=M, stdin_eof=True, restore=restore)
        assert line is not None
        for half in (frame.terminal_restore(restore), frame.errexit_restore()):
            assert half in line
            assert half in payload


def test_the_test_side_frame_reader_reads_both_shapes():
    shapes = [(BashFrame().frame("c", m=M), False, False, False)]
    shapes += [
        (
            BashFrame().frame_exec("c", m=M, stdin_eof=e, restore=r, credential=k),
            True,
            e,
            k,
        )
        for e in (True, False)
        for r in ExecRestore
        for k in (True, False)
    ]
    for payload, isolated, eof, credential in shapes:
        f = parse_bash_frame(payload)
        assert f is not None
        assert (f.cmd, f.isolated, f.stdin_eof, f.credential) == ("c", isolated, eof, credential)


@pytest.mark.parametrize("flag", ["", "B", "N"])
@pytest.mark.asyncio
async def test_each_end_record_completes_one_read_split_at_every_offset(flag):
    """A record split anywhere ends the read once, with the exact output, status and flag."""
    frame = BashFrame()
    pattern = frame.end_pattern(M)
    reply = f"{M.begin}\nout\n{end_record(M, 42, flag=flag)}\nafter"
    for offset in range(1, len(reply)):
        chunks = [reply[:offset].encode(), reply[offset:].encode(), b""]

        async def read(chunks=chunks):
            return chunks.pop(0)

        reader = PatternReader(read)
        got = await reader.read_transport_until(pattern)
        assert got == reply[: reply.index("__\nafter") + 2], offset
        assert frame.parse_output(got, cmd="", m=M) == "out"
        assert frame.extract_retcode(got, m=M) == 42
        match = pattern.search(got)
        assert match is not None
        assert match.group(BashFrame.END_FLAG_GROUP) == flag
        assert frame.exec_baseline_present(got, m=M) is (flag != "N")


@pytest.mark.asyncio
async def test_a_caller_group_named_like_the_record_does_not_collide():
    s = await scripted_shell(hang=frozenset(), replies={"ask": ("name?\nq\ndone", 3)})
    r = await s.run_cmd(cmd="ask", expects=[("(?P<code>name)\\?", "x\n"), ("(?P<flag>q)", "y\n")])
    assert (r.timed_out, r.retcode) == (False, 3)
    assert "x\n" in s.written
    assert "y\n" in s.written


@pytest.mark.parametrize("options", ["", "set -u", "set -e", "set -eu"])
def test_establishing_the_exec_state_never_aborts(options):
    """Both modes reach the line's last statement under any options, and keep what they should."""
    kept = f"__otto_base=OUTER; {establish_exec_state(replace=False)}; echo kept=$__otto_base"
    fresh = f"{establish_exec_state(replace=True)}; echo fresh=$__otto_base$__otto_e$__otto_tty."
    unset = f"{establish_exec_state(replace=False)}; echo set=${{__otto_e+x}}"
    script = "\n".join(f"{options}; {line}" if options else line for line in (kept, fresh, unset))
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False).stdout
    assert out.splitlines() == ["kept=OUTER", "fresh=.", "set=x"]


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_typed_run_reads_bang_and_hash_as_a_script_does(name):
    """``!word`` is literal and ``#`` starts a comment on every shell, as in a script (#1346)."""
    async with pty_session(name) as s:
        await reads_bang_and_hash_as_a_script_does(s)
