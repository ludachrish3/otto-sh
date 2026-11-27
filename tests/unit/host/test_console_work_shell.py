"""On a console whose shell outlives the session, otto's session runs in a work shell (#1358).

A console client that owes no logout (``console_options.login`` or ``logout``
false) leaves its shell to the console's next user, a person. So the session
types line 1 into that shell with no shell prefix, starts a child of the same
kind there (the work shell), proves it through the resync probe with the
frame's work mark, and runs its own handshake in it. These cases drive the
product's open against every shell of the pty matrix
(:func:`~tests.unit.host._pty_session.work_shell_session`).
"""

import asyncio
import contextlib
import dataclasses
import os
import re
import shlex
import signal
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from otto.host import login_proxy
from otto.host.cancel_safe import result_within
from otto.host.command_frame import (
    FRAME_CLASSES,
    BashFrame,
    RawFrame,
    WorkShellLines,
    build_command_frame,
    handshake_prefix,
    register_command_frame,
)
from otto.host.errors import ConsoleError
from otto.host.login_proxy import _resync_shell
from otto.host.session import ShellSession, _WorkShell
from otto.host.terminal_mapping import IDENTITY, ONLCR, measure
from otto.registry import Ref
from tests._fixtures.controlling_pty import spawn_on_controlling_pty
from tests._fixtures.interactive_shell import SHELL_ARGV, SHELLS, shell_env
from tests.unit.host._pty_session import (
    PTY_SHELLS,
    PtySession,
    outliving_console,
    person_state,
    pty_session,
    reads_bang_and_hash_as_a_script_does,
    work_shell_session,
)
from tests.unit.host._sessions import proxy_io, session_manager

_HISTORY = pytest.mark.parametrize("history", [True, False], ids=["kept", "suppressed"])
_NONE_STARTED = outliving_console().work_shell_refused(started=False)
_NO_ANSWER = outliving_console().work_shell_refused(started=True)


def _why(refusal: ConsoleError) -> str:
    """The part of a refusal after the console's own words, which every console client shares."""
    return str(refusal).split("keeps its shell", 1)[1]


async def _value(s: PtySession, cmd: str) -> str:
    done = await s.run_cmd(cmd=cmd, timeout=5)
    assert (done.timed_out, done.retcode) == (False, 0), done
    return done.value


def _io(s: PtySession):
    return proxy_io(s, lambda *_: None, lambda *_: None)


def _inner(name: str) -> str:
    """The command that starts a non-interactive shell of *name*'s kind."""
    return shlex.join(dict(SHELLS)[name])


def _end(kept: list[PtySession]) -> None:
    """End the shells of sessions kept open on their line (``keep_shell``)."""
    for s in kept:
        s.end_shell()


_PERSON_RC = "PV=person; pfunc() { :; }; alias palias=:"
"""What a person's shell holds that otto's session must leave alone."""

_NEVER = re.compile(r"(?!x)x")


async def _read_to_the_end(s: PtySession) -> None:
    """Read the pty until the shell on it has exited and closed it."""
    with pytest.raises(asyncio.IncompleteReadError):
        await result_within(s.read_transport_until(pattern=_NEVER), 5)


async def _close(s: PtySession) -> None:
    """Close *s*, kept on its line, and read through its close lines to the next prompt.

    The close types the restore (where a record was saved) and the exit, and
    awaits neither. A line typed before the exit has run can be read by the
    work shell that is leaving (a line editor reads ahead once the restore
    has put the terminal back), so the test reads the BEGIN marker each
    framed close line prints, then the prompt of the shell that reads next.
    """
    typed = 1 + (s._saved_terminal is not None)
    await s.close()
    begin = re.compile(re.escape(s.markers.begin) + r"\r*\n")
    for _ in range(typed):
        await result_within(s.read_transport_until(pattern=begin), 5)
    await result_within(s.read_transport_until(pattern=re.compile(r"\$ ")), 5)


def _died(s: PtySession) -> None:
    """What an otto that died leaves: a session that types nothing more, not even at its close."""
    s._alive = False


@pytest.mark.parametrize("name", PTY_SHELLS)
@_HISTORY
@pytest.mark.asyncio
async def test_a_console_session_runs_in_a_child_of_the_persons_shell(name, history):
    async with work_shell_session(
        name, rc="PV=person; pfunc() { :; }; alias palias=:", shell_history=history
    ) as s:
        person = s.person_before.shell
        assert s._work_shell is _WorkShell.ENTERED
        assert await _value(s, "echo $PPID") == str(person.pid)
        assert await _value(s, "echo $$") != str(person.pid)
        assert await _value(s, "readlink /proc/$$/exe") == person.program
        await reads_bang_and_hash_as_a_script_does(s)
        await s.write_transport(data="exit\n")
        after = await person_state(s)
    assert after.shell == person
    assert (person.pv, person.pfunc, person.palias, person.work_mark) == (
        "person",
        True,
        True,
        False,
    )


_EXPORT_CASES = ["set-a-in-startup", "pre-exported-name"]


@pytest.mark.parametrize("before", _EXPORT_CASES)
@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_the_work_mark_is_never_exported(name, before, monkeypatch):
    monkeypatch.setattr(login_proxy, "_RESYNC_SETTLE", 0.0)
    startup = "set -a" if before == "set-a-in-startup" else ""
    rc = "export __otto_as_w=stale" if before == "pre-exported-name" else ""
    query = "alias __otto_as_w >/dev/null 2>&1 && echo has || echo none"
    async with work_shell_session(name, rc=rc, startup=startup) as s:
        assert await _value(s, query) == "has"
        assert await _value(s, f"{_inner(name)} -c {shlex.quote(query)}") == "none"
        lines = BashFrame().work_shell(prefix="OTTO_SENTINEL=ran; ")
        await s.send(" ".join(SHELL_ARGV[name]) + "\n")  # a shell started inside the work shell
        inner = await _resync_shell(_io(s), "h", "u", work=lines)
        assert inner.in_work_shell is False
        assert await _value(s, "echo ${OTTO_SENTINEL-unset}") == "unset"
        await s.send("exit\n")
        back = await _resync_shell(_io(s), "h", "u", work=lines)
        assert back.in_work_shell is True
        assert await _value(s, "echo ${OTTO_SENTINEL-unset}") == "ran"


def _refusal_cases() -> list:
    cases = [("restricted-bash", "bash", ["bash", "--norc", "--noprofile", "-r", "-i"])]
    if "dash" in PTY_SHELLS:
        cases += [
            ("dash-as-su", "dash", ["bash", "-c", "exec -a -su dash -i"]),
            ("dash-not-named-sh", "dash", ["bash", "-c", "exec -a -notashell dash -i"]),
        ]
    return [pytest.param(name, argv, id=case) for case, name, argv in cases]


async def _refused_and_left_as_found(
    kept: list[PtySession], refused: ConsoleError, *, restricted: bool = False
) -> None:
    """The open was refused as none started; the person's shell and terminal are as they were.

    The person's shell holds :data:`_PERSON_RC`, so its variable, function and
    alias are there to compare. A restricted shell refuses the state
    reading's output redirection, so there it reads no function or alias,
    and only the variable is proved there.
    """
    (s,) = kept
    try:
        assert _why(refused) == _why(_NONE_STARTED)
        before = s.person_before.shell
        assert before.pv == "person"
        if not restricted:
            assert (before.pfunc, before.palias) == (True, True)
        after = await person_state(s)
        assert after.shell == s.person_before.shell
        assert after.terminal == s.person_before.terminal
    finally:
        s.end_shell()


@pytest.mark.parametrize(("name", "argv"), _refusal_cases())
@pytest.mark.asyncio
async def test_a_shell_that_cannot_start_a_work_shell_is_refused(name, argv):
    with (
        tempfile.TemporaryDirectory(prefix="otto-ws-home-") as home,
        tempfile.TemporaryDirectory(prefix="otto-ws-fakes-") as fakes,
    ):
        pty = spawn_on_controlling_pty(argv, env=shell_env(name, home, fakes), onlcr=True)
        kept: list[PtySession] = []
        try:
            with pytest.raises(ConsoleError) as refused:
                async with work_shell_session(
                    name, pty=pty, rc=_PERSON_RC, keep_shell=True, kept=kept
                ):
                    pass
            restricted = "-r" in argv
            await _refused_and_left_as_found(kept, refused.value, restricted=restricted)
        finally:
            if not kept:
                pty.close()


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_lost_start_line_is_refused(name, monkeypatch):
    """The start line arrives without its first 20 bytes: no child, and no mark to find."""
    lines = BashFrame().work_shell(prefix="")
    start = lines.lead + lines.start
    original = PtySession.write_transport

    async def cut(self, *, data):
        await original(self, data=data[20:] if data == start else data)

    monkeypatch.setattr(PtySession, "write_transport", cut)
    kept: list[PtySession] = []
    with pytest.raises(ConsoleError) as refused:
        async with work_shell_session(name, rc=_PERSON_RC, keep_shell=True, kept=kept):
            pass
    await _refused_and_left_as_found(kept, refused.value)


def _shell_side_open_budget() -> float:
    """``SessionManager.open_budget`` of a console that keeps its shell, less its line's waits.

    A pty session dials nothing and drains nothing, so the connection's
    allowances are zero, and it is not retried: what is left is the waits the
    shell side of the open is given, the work shell's included.
    """
    conn = MagicMock()
    conn.connect_allowance.return_value = 0.0
    conn.release_allowance.return_value = 0.0
    conn.retries_failed_open.return_value = False
    conn.keeps_console_shell.return_value = True
    return session_manager(connections=conn, name="h").open_budget(term="console")


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_child_that_never_answers_is_refused_within_the_budget(name, monkeypatch):
    monkeypatch.setattr(login_proxy, "RESYNC_DEADLINE", 0.5)
    opening = work_shell_session(name, startup="cat >/dev/null")
    with pytest.raises(ConsoleError) as refused:
        await result_within(opening.__aenter__(), _shell_side_open_budget())
    assert _why(refused.value) == _why(_NO_ANSWER)


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_work_shell_whose_startup_file_changes_the_terminal_is_measured_there(
    name, monkeypatch
):
    """The person's terminal maps ``onlcr``; the work shell's startup file turns it off."""
    replies: list[tuple[str, str]] = []
    original = ShellSession._measure_terminal_mapping

    def spy(self, reply):
        replies.append((reply or "", self._markers.ready))
        original(self, reply)

    monkeypatch.setattr(ShellSession, "_measure_terminal_mapping", spy)
    async with work_shell_session(name, startup="stty echo -onlcr") as s:
        assert len(replies) == 2
        (first, first_ready), (second, second_ready) = replies
        assert measure(first, first_ready).mapping == ONLCR
        assert second_ready == s.markers.ready
        assert measure(second, second_ready).mapping == IDENTITY
        assert s.terminal_mapping.startswith(IDENTITY)
        assert s.echoes is False


@pytest.mark.asyncio
async def test_a_console_with_no_terminal_record_still_enters_its_work_shell(tmp_path):
    fails = tmp_path / "stty"
    fails.write_text("#!/bin/sh\nexit 1\n")
    fails.chmod(0o755)
    async with work_shell_session("bash", path_first=[str(tmp_path)]) as s:
        assert s._saved_terminal is None
        assert s._work_shell is _WorkShell.ENTERED
        assert await _value(s, "echo $PPID") == str(s.person_before.shell.pid)


@pytest.mark.asyncio
async def test_a_repeated_frame_entry_starts_no_second_work_shell(monkeypatch):
    written: list[str] = []
    original = PtySession.write_transport

    async def record(self, *, data):
        written.append(str(data))
        await original(self, data=data)

    monkeypatch.setattr(PtySession, "write_transport", record)
    lines = BashFrame().work_shell(prefix="")
    async with work_shell_session("bash") as s:
        pid = await _value(s, "echo $$")
        await s.enter_frame(frame=BashFrame())
        assert await _value(s, "echo $$") == pid
    assert sum(lines.start in w for w in written) == 1


@pytest.mark.asyncio
async def test_a_raw_landing_decides_at_frame_entry(monkeypatch):
    """The raw landing's handshake decides nothing; the frame entry after it does."""
    async with work_shell_session("bash", frame=RawFrame()) as s:
        assert s._work_shell is _WorkShell.UNDECIDED
        await s.enter_frame(frame=BashFrame())
        assert s._work_shell is _WorkShell.ENTERED
        assert await _value(s, "echo $PPID") == str(s.person_before.shell.pid)

    written: list[str] = []
    original = PtySession.write_transport

    async def record(self, *, data):
        written.append(str(data))
        await original(self, data=data)

    client = outliving_console()
    async with work_shell_session("bash", frame=RawFrame(), client=client) as s:
        client._logged_in = True  # what a hook's console_login() leaves
        monkeypatch.setattr(PtySession, "write_transport", record)
        await s.enter_frame(frame=BashFrame())
        monkeypatch.undo()
        assert s._work_shell is _WorkShell.NOT_USED
        assert await _value(s, "echo $$") == str(s.person_before.shell.pid)
        m = s.markers
        frame = BashFrame()
        prefix = handshake_prefix(frame, m, shell_history=True, save_terminal=True)
        assert written == [frame.handshake(m, prefix=prefix)]


class _MarkedX(BashFrame):
    """A project's bash frame whose work shell is marked ``x``."""

    type_name = "work-shell-mark-x-test"

    def work_shell(self, *, prefix: str) -> WorkShellLines:
        lines = super().work_shell(prefix=prefix)
        return dataclasses.replace(
            lines,
            **{
                f.name: getattr(lines, f.name).replace("__otto_as_w", "__otto_as_x")
                for f in dataclasses.fields(lines)
            },
        )


class _OwnSwitchMark(BashFrame):
    """A project's bash frame that marks a switched shell its own way, inheriting the work shell."""

    type_name = "work-shell-own-switch-mark-test"

    def identity_mark_statement(self, *, mark: str) -> str:
        return f"alias __proj_as_{mark}=:"

    def identity_guard(self, *, payload, mark, m):
        guard = super().identity_guard(payload=payload, mark=mark, m=m)
        return guard.replace(f"__otto_as_{mark}", f"__proj_as_{mark}")


_REGISTRATIONS = ["class", "ref"]


@contextlib.contextmanager
def _registered(cls: "type[BashFrame]", how: str):
    """Register *cls* eagerly (``class``) or by ``Ref``, and yield the frame lab data builds."""
    entry = cls if how == "class" else Ref(target=f"{__name__}:{cls.__name__}")
    register_command_frame(cls.type_name, entry, overwrite=True)
    try:
        yield build_command_frame(type_name=cls.type_name)
    finally:
        FRAME_CLASSES.unregister(name=cls.type_name)


@pytest.fixture(params=_REGISTRATIONS)
def marked_x(request):
    with _registered(_MarkedX, request.param) as frame:
        yield frame


@pytest.fixture(params=_REGISTRATIONS)
def own_switch_mark(request):
    with _registered(_OwnSwitchMark, request.param) as frame:
        yield frame


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_frame_that_marks_switched_shells_its_own_way_still_enters_and_leaves(
    name, own_switch_mark
):
    """The work-shell lines set and query their mark themselves, not through the identity hooks."""
    assert "__otto_as_w" in own_switch_mark.work_shell(prefix="").proof
    kept: list[PtySession] = []
    try:
        async with work_shell_session(
            name, frame=own_switch_mark, rc=_PERSON_RC, keep_shell=True, kept=kept
        ) as s:
            assert s._work_shell is _WorkShell.ENTERED
            assert await _value(s, "echo $PPID") == str(s.person_before.shell.pid)
            has = "alias {} >/dev/null 2>&1 && echo has || echo none"
            assert await _value(s, has.format("__otto_as_w")) == "has"
            assert await _value(s, has.format("__proj_as_w")) == "none"
            await _close(s)
        after = await person_state(s)
    finally:
        _end(kept)
    assert after == s.person_before


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_custom_frames_lines_drive_the_open(name, marked_x):
    """The frame's own lines enter its work shell, and its own exit leaves it at the close."""
    assert "__otto_as_x" in marked_x.work_shell(prefix="").exit
    kept: list[PtySession] = []
    try:
        async with work_shell_session(name, frame=marked_x, keep_shell=True, kept=kept) as s:
            assert s._work_shell is _WorkShell.ENTERED
            assert await _value(s, "echo $PPID") == str(s.person_before.shell.pid)
            has = "alias {} >/dev/null 2>&1 && echo has || echo none"
            assert await _value(s, has.format("__otto_as_x")) == "has"
            assert await _value(s, has.format("__otto_as_w")) == "none"
            await _close(s)
        after = await person_state(s, mark="__otto_as_x")
    finally:
        _end(kept)
    assert after.shell.pid == s.person_before.shell.pid
    assert after.shell.work_mark is False


@pytest.mark.parametrize("name", [name for name in PTY_SHELLS if name == "zsh"])
@pytest.mark.asyncio
async def test_a_zsh_with_no_startup_file_enters_its_work_shell_past_the_new_user_menu(
    name, monkeypatch
):
    """With no zsh startup file in HOME, the work shell starts zsh's new-user menu first.

    Measured: the menu reads one key, the entry proof's first byte, which no
    choice names, so it prints its screen and "Aborting." and writes no
    file; the rest of that line, and the next resend, reach zsh's line
    editor, and the resend answers with the mark. So the open enters, the
    menu's screen is printed on the console each time, and no ``~/.zshrc``
    appears.
    """
    reads: list[str] = []
    original = PtySession.read_transport_until

    async def spy(self, *, pattern):
        out = await original(self, pattern=pattern)
        reads.append(out)
        return out

    monkeypatch.setattr(PtySession, "read_transport_until", spy)
    async with work_shell_session(name, startup=None) as s:
        assert s._work_shell is _WorkShell.ENTERED
        assert await _value(s, "echo $PPID") == str(s.person_before.shell.pid)
        assert not Path(s.home, ".zshrc").exists()
    printed = "".join(reads)
    assert "zsh-newuser-install" in printed
    assert "Aborting." in printed


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_switch_and_the_entry_proof_answer_through_one_resync(name):
    lines = BashFrame().work_shell(prefix="")
    async with work_shell_session(name) as s:
        switch = await _resync_shell(_io(s), "h", "u", settle=0.0)
        entry = await _resync_shell(_io(s), "h", "u", settle=0.0, work=lines)
    assert switch.user == entry.user != ""
    assert (switch.in_work_shell, entry.in_work_shell) == (False, True)


# ---------------------------------------------------------------------------
# The close: the restore, then the work shell's exit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", PTY_SHELLS)
@_HISTORY
@pytest.mark.asyncio
async def test_a_console_session_leaves_the_persons_shell_as_it_found_it(name, history):
    """Whatever the session changed in its work shell, the close hands back the shell found."""
    kept: list[PtySession] = []
    try:
        async with work_shell_session(
            name, rc=_PERSON_RC, shell_history=history, keep_shell=True, kept=kept
        ) as s:
            await _value(s, "cd /tmp; export OTTO_T=1; set -C")
            assert await _value(s, "echo 'a\tb'") == "a\tb"
            await _close(s)
        after = await person_state(s)
    finally:
        _end(kept)
    assert after == s.person_before


_INC_APPEND = "setopt INC_APPEND_HISTORY"


def _history_file_cases() -> list:
    cases = [pytest.param(name, "", id=name) for name in PTY_SHELLS]
    if "zsh" in PTY_SHELLS:
        cases.append(pytest.param("zsh", _INC_APPEND, id="zsh-inc-append-in-startup"))
    return cases


_PERSON_LINES = ["echo person-line", "exit"]
"""What the person types into their own shell after otto's close."""


async def _persons_history(name: str, *, startup: str = "", ignores: str = "") -> str:
    """Open, run one command and close on a person's shell that keeps a history file; read it.

    The person's ``rc`` exports ``HISTFILE`` (zsh also saves each line as it
    is read), then runs *ignores*. *startup* is the work shell's startup
    file. After the close the person types :data:`_PERSON_LINES`, so every
    shell has written its file by the time it is read.
    """
    rc = "export HISTFILE=$HOME/.phist"
    if name == "zsh":  # zsh -f saves no history: the person's zsh saves each line as it is read
        rc += f" HISTSIZE=100 SAVEHIST=100; {_INC_APPEND}"
    if ignores:
        rc += f"; {ignores}"
    kept: list[PtySession] = []
    try:
        async with work_shell_session(
            name, rc=rc, startup=startup, shell_history=False, keep_shell=True, kept=kept
        ) as s:
            assert await _value(s, "echo otto-hist-probe") == "otto-hist-probe"
            await _close(s)
        for line in _PERSON_LINES:
            await s.write_transport(data=f"{line}\n")
        await _read_to_the_end(s)
        history = Path(s.home, ".phist")
        return history.read_text(errors="replace") if history.exists() else ""
    finally:
        _end(kept)


@pytest.mark.parametrize(("name", "startup"), _history_file_cases())
@pytest.mark.asyncio
async def test_no_command_otto_runs_reaches_the_persons_history_file(name, startup):
    """The person's shell keeps a history file the work shell inherits; otto's commands stay out.

    The entry proof itself may reach the file where the work shell records
    a line as it reads it: ksh and mksh, and a zsh whose startup file sets
    ``INC_APPEND_HISTORY`` (measured: the proof line is there, nothing after it).
    """
    written = await _persons_history(name, startup=startup)
    assert "otto-hist-probe" not in written
    if name in ("bash", "zsh"):
        assert "person-line" in written  # the file is the one the person's shell writes
        assert ("__otto_probe=RECOVER__" in written) is (startup == _INC_APPEND)


def _ignores_space_cases() -> list:
    cases = [pytest.param("bash", "HISTCONTROL=ignorespace", id="bash-ignorespace")]
    if "zsh" in PTY_SHELLS:
        cases.append(pytest.param("zsh", "setopt HIST_IGNORE_SPACE", id="zsh-hist-ignore-space"))
    return cases


@pytest.mark.parametrize(("name", "ignores"), _ignores_space_cases())
@pytest.mark.asyncio
async def test_a_history_that_ignores_led_lines_holds_none_of_otto(name, ignores):
    """Where the person's shell drops a line led by a space, the lead keeps every otto line out.

    The test's own lines typed at the person's shell (its ``rc``, and the
    state reading before the open) are set aside; what is left is only the
    person's.
    """
    written = await _persons_history(name, ignores=ignores)
    theirs = [
        line
        for line in written.splitlines()
        if not line.startswith("export HISTFILE=") and "__PS_" not in line
    ]
    assert theirs == _PERSON_LINES


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_after_an_otto_that_died_one_exit_returns_the_persons_shell(name):
    kept: list[PtySession] = []
    try:
        async with work_shell_session(name, keep_shell=True, kept=kept) as s:
            assert s._work_shell is _WorkShell.ENTERED
            _died(s)
        await s.write_transport(data="exit\n")
        after = await person_state(s)
    finally:
        _end(kept)
    assert after.shell == s.person_before.shell


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_the_next_session_reuses_the_work_shell_an_otto_that_died_left(name):
    kept: list[PtySession] = []
    try:
        async with work_shell_session(name, keep_shell=True, kept=kept) as s:
            work = await _value(s, "echo $$")
            _died(s)
        async with work_shell_session(name, pty=s._pty, keep_shell=True) as again:
            assert again._work_shell is _WorkShell.ENTERED
            assert await _value(again, "echo $$") == work
            assert await _value(again, "echo $PPID") == str(s.person_before.shell.pid)
            await _close(again)
        after = await person_state(again)
    finally:
        _end(kept)
    assert after.shell == s.person_before.shell


def _checkjobs_cases() -> list:
    cases = [pytest.param(name, "", id=f"{name}-default") for name in PTY_SHELLS]
    if "zsh" in PTY_SHELLS:
        cases.append(pytest.param("zsh", "setopt NOCHECKJOBS", id="zsh-nocheckjobs"))
    return cases


async def _background_sleep(s: PtySession) -> int:
    """Start ``sleep 300`` as a job of the session's shell; return its pid."""
    return int((await _value(s, "sleep 300 & echo $!")).split()[-1])


_UNTIL_THE_SHELL_SEES_IT_STOPPED = (
    'until jobs -l >"$HOME/.jobs" 2>&1 && grep -qiE "stopped|suspended" "$HOME/.jobs"; do :; done'
)
"""Wait until the shell's own job table holds the stop, which ``kill`` returns before.

``jobs`` runs in the shell itself, so it reaps the stop. Without it, a shell
that has not reaped the stop by the time the exit runs takes the job for a
running one and exits: a dash work shell did once here, and dash was seen
reaping a stop one command late."""


def _kill(pid: "int | None") -> None:
    if pid is not None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize(("name", "startup"), _checkjobs_cases())
@pytest.mark.asyncio
async def test_a_stopped_job_keeps_the_work_shell_and_kills_nothing(name, startup):
    """The work shell refuses the exit, as with any stopped job; the next session reuses it."""
    kept: list[PtySession] = []
    pid = None
    try:
        async with work_shell_session(name, startup=startup, keep_shell=True, kept=kept) as s:
            work = await _value(s, "echo $$")
            pid = await _background_sleep(s)
            # By pid: zsh numbers the job [2], and its %1 names the work shell's own group.
            await _value(s, f"kill -STOP {pid}; {_UNTIL_THE_SHELL_SEES_IT_STOPPED}")
            await _close(s)
        after = await person_state(s)
        assert str(after.shell.pid) == work
        os.kill(pid, 0)  # alive
        async with work_shell_session(name, pty=s._pty, keep_shell=True) as again:
            assert await _value(again, "echo $$") == work
            await _close(again)
        assert (
            str((await person_state(again)).shell.pid) == work
        )  # refused again; still kills nothing
        os.kill(pid, 0)
    finally:
        _kill(pid)
        _end(kept)


def _running_job_cases() -> list:
    cases = [pytest.param(name, "", id=f"{name}-default") for name in PTY_SHELLS]
    cases.append(pytest.param("bash", "shopt -s checkjobs", id="bash-checkjobs"))
    return cases


@pytest.mark.parametrize(("name", "startup"), _running_job_cases())
@pytest.mark.asyncio
async def test_a_running_job_neither_blocks_the_exit_nor_dies_with_it(name, startup):
    kept: list[PtySession] = []
    pid = None
    try:
        async with work_shell_session(name, startup=startup, keep_shell=True, kept=kept) as s:
            pid = await _background_sleep(s)
            await _close(s)
        after = await person_state(s)
        assert after.shell.pid == s.person_before.shell.pid
        os.kill(pid, 0)  # alive
    finally:
        _kill(pid)
        _end(kept)


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_close_skipped_while_a_program_owns_the_input_leaves_the_work_shell_for_the_next_session(  # noqa: E501 — the plan's test name
    name, monkeypatch
):
    """Nothing is typed into the program; the next session reuses the work shell and leaves it."""
    kept: list[PtySession] = []
    written: list[str] = []
    original = PtySession.write_transport

    async def record(self, *, data):
        written.append(str(data))
        await original(self, data=data)

    try:
        async with work_shell_session(name, keep_shell=True, kept=kept) as s:
            work = await _value(s, "echo $$")
            await s.send("head -n 1 >/dev/null; echo REL''EASED\n")
            with pytest.raises(asyncio.TimeoutError):
                await result_within(s.expect("never", timeout=30), 0.05)
            assert s._needs_recovery
            monkeypatch.setattr(PtySession, "write_transport", record)
        monkeypatch.undo()
        assert written == []  # the close typed nothing into `head`
        await s.write_transport(data="x\n")
        await s.read_transport_until(pattern=re.compile(r"RELEASED"))
        async with work_shell_session(name, pty=s._pty, keep_shell=True) as again:
            assert await _value(again, "echo $$") == work
            await _close(again)
        after = await person_state(again)
    finally:
        _end(kept)
    assert after.shell.pid == s.person_before.shell.pid


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_the_exit_does_nothing_in_a_shell_without_the_mark(name):
    """The framed exit leaves neither a shell started in the work shell nor the person's."""
    exit_line = BashFrame().work_shell(prefix="").exit
    kept: list[PtySession] = []
    try:
        async with work_shell_session(name, keep_shell=True, kept=kept) as s:
            work = int(await _value(s, "echo $$"))
            await s.send(" ".join(SHELL_ARGV[name]) + "\n")  # a shell started inside the work shell
            inner = (await person_state(s)).shell.pid
            assert inner not in (work, s.person_before.shell.pid)
            await s.write_transport(data=s._frame.frame(exit_line, m=s.markers))
            assert (await person_state(s)).shell.pid == inner  # that shell stays
            await s.write_transport(data="exit\n")
            assert (await person_state(s)).shell.pid == work
            await s.write_transport(data="exit\n")  # the work shell, left by hand
            assert (await person_state(s)).shell.pid == s.person_before.shell.pid
            await s.write_transport(data=s._frame.frame(exit_line, m=s.markers))
            after = await person_state(s)
    finally:
        _end(kept)
    assert after.shell == s.person_before.shell


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_session_opened_straight_after_a_close_opens(name):
    """Line 1 typed while the work shell is leaving is resent until the person's shell answers.

    On ksh and mksh the exiting work shell's line editor reads ahead, so the
    first copy of the next session's line 1 can go with it. The next session
    opens at once, with no read in between and no state reading of the
    harness's own (``pty_session``, not ``work_shell_session``).
    """
    kept: list[PtySession] = []
    try:
        async with work_shell_session(name, keep_shell=True, kept=kept) as s:
            assert s._work_shell is _WorkShell.ENTERED
        async with pty_session(
            name, pty=s._pty, console_client=outliving_console(), keep_shell=True
        ) as again:
            assert again._work_shell is _WorkShell.ENTERED
            assert await _value(again, "echo $PPID") == str(s.person_before.shell.pid)
            await _close(again)
        after = await person_state(again)
    finally:
        _end(kept)
    assert after.shell == s.person_before.shell
