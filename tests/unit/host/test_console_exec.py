"""A console's ``exec`` runs on the shell ``run`` uses, in an isolating subshell (#935, #1261).

A console has one shell for ``run`` and ``exec``. Its ``exec`` frame
(:meth:`~otto.host.command_frame.BashFrame.frame_exec` with
:attr:`~otto.host.command_frame.ExecRestore.START`, the restore a console's
default session is given) is the pooled frame plus a snapshot of the
terminal at its start: the command starts from ``run``'s directory,
variables and options, and nothing it changes reaches a later command.
Every other promise of the exec frame holds as it does on a pooled shell.
Each is proved here on the real shells of the matrix on a pty
(:mod:`tests.unit.host._pty_session`), through a :class:`SessionManager`
configured as a console where the entry matters.
"""

import pytest

from otto.host.command_frame import ExecInput, ExecRestore
from otto.host.errors import PasswordRefusedError
from otto.host.session import NEVER_RAN
from otto.logger.mode import LogMode
from otto.utils import Status
from tests.unit.host._pty_session import PTY_SHELLS, console_session, pty_session
from tests.unit.host.test_exec_frame import _ZSH_INTERACTIVE
from tests.unit.host.test_exec_stdin import _ADMIN
from tests.unit.host.test_frame_shell_portability import FRAME_EDGE_CASES
from tests.unit.host.test_privilege import _host_with_userland
from tests.unit.host.test_session_exec_pooled import _mgr

_BANG = "echo a !b c"
"""An unquoted ``!`` before a word: a history event to an interactive bash or zsh."""

_WORDS = [
    ("printf '%s|' 'single' \"double\" '$x' 'a\\b'", "single|double|$x|a\\b|", 0),
    ("printf '%s\\n' \"it's\" 'say \"hi\"'\nprintf done", 'it\'s\nsay "hi"\ndone', 0),
    ("x='a'\"'\"'b'; echo \"$x\" # it's a comment", "a'b", 0),
    ("printf '%s\\n' a\\\\b", "a\\b", 0),
    ("exit 7", "", 7),
    ("sh -c 'kill -TERM $$'", None, 143),
    (_BANG, "a !b c", 0),
]
"""Commands whose quotes, ``$``, backslashes and ends must reach the shell byte-exact."""


def test_shells_are_installed():
    assert PTY_SHELLS, "no shell of the matrix is installed: the console exec is proved on none"


@pytest.mark.parametrize("case", FRAME_EDGE_CASES)
@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_exec_keeps_every_frame_edge_case(case, name):
    expected = _ZSH_INTERACTIVE.get(case.text, case.expected) if name == "zsh" else case.expected
    async with console_session(name) as s:
        r = await s.exec_(case.text, timeout=5)
    assert (r.timed_out, r.value, r.retcode) == (False, expected, case.code)


_INTERACTIVE_WORDS = {
    ("zsh", "x='a'\"'\"'b'; echo \"$x\" # it's a comment"): (True, -1),
    ("zsh", _BANG): (True, -1),
    ("bash", _BANG): (True, -1),
}
"""What an interactive shell does where another shell differs, as ``(timed_out, retcode)``.

Without ``interactivecomments`` (_ZSH_INTERACTIVE) zsh reads the ``#`` as an
ordinary word, so the apostrophe after it opens a quote that never closes.
Interactive bash and zsh expand history, so the unquoted ``!b`` is an event
that is not found and the shell drops the whole typed line. Either way the
frame never ends and the call times out, as the same command from ``run``
does, and the next call finds the console working."""


@pytest.mark.parametrize(("cmd", "value", "code"), _WORDS)
@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_exec_runs_the_command_with_its_own_status(name, cmd, value, code):
    differs = _INTERACTIVE_WORDS.get((name, cmd))
    async with console_session(name) as s:
        r = await s.exec_(cmd, timeout=5)
        if differs is not None:
            assert (r.timed_out, r.retcode) == differs
        else:
            assert (r.timed_out, r.retcode) == (False, code)
            if value is not None:
                assert r.value == value
        assert (await s.exec_("echo ok", timeout=5)).value == "ok"


_STATE = 'pwd; echo "${OTTO_T2-unset}"; case $- in *C*) echo C;; esac'
"""Reads what ``run`` can leave: the directory, an exported variable, an option.

The option is ``noclobber`` (``set -C``): every matrix shell shows it in ``$-``,
and none of them starts with it on. ``set -f`` would not do: the matrix's zsh
runs as ``zsh -f``, where ``f`` is already in ``$-``."""


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_exec_starts_from_run_s_directory_variables_and_options(name):
    async with console_session(name) as s:
        await s.run_cmd(cmd="cd /tmp; export OTTO_T2=1; set -C", timeout=5)
        r = await s.exec_(_STATE, timeout=5)
    assert (r.retcode, r.value) == (0, "/tmp\n1\nC")


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_what_a_console_exec_changes_does_not_reach_run(name):
    async with console_session(name, setup="cd /tmp") as s:
        await s.exec_("cd /; export OTTO_T2=2; set -C", timeout=5)
        r = await s.run_cmd(cmd=_STATE, timeout=5)
    assert r.value.splitlines()[0] != "/"
    assert "unset" in r.value
    assert "C" not in r.value.splitlines()
    assert r.value == "/tmp\nunset"


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_exec_inherits_run_s_errexit(name):
    async with console_session(name) as s:
        await s.run_cmd(cmd="set -e", timeout=5)
        r = await s.exec_("false; echo continued", timeout=5)
        off = await s.exec_("set +e; false; echo continued", timeout=5)
        opts = (await s.run_cmd(cmd="echo $-", timeout=5)).value
    assert "continued" not in r.value
    assert r.retcode != 0
    assert (off.retcode, off.value) == (0, "continued")  # the exec's own `set +e` ...
    assert "e" in opts  # ... leaves run's `set -e` on


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_exec_runs_when_run_left_a_directory_that_is_gone(name):
    async with console_session(name) as s:
        await s.run_cmd(cmd='cd "$(mktemp -d)"; rmdir "$PWD"', timeout=5)
        r = await s.exec_("echo ok", timeout=5)
        still = await s.run_cmd(cmd="echo still", timeout=5)
    assert (r.value, r.retcode) == ("ok", 0)
    assert still.value == "still"


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_exec_under_run_s_nounset_fails_the_command_not_the_console(name):
    async with console_session(name) as s:
        await s.run_cmd(cmd="set -u", timeout=5)
        r = await s.exec_('echo "$OTTO_T2_NEVER_SET"; echo after', timeout=5)
        opts = await s.run_cmd(cmd="case $- in *u*) echo u;; esac", timeout=5)
    assert r.retcode != 0
    assert opts.value == "u"


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_file_operation_reads_a_relative_path_from_run_s_directory(name, tmp_path):
    """``exists`` runs through ``exec``, so on a console a relative path is ``run``'s ``cd``'s."""
    from tests.unit.host.test_unix_host import TestConsoleTermTransfers

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "here").touch()
    async with console_session(name) as s:
        host = TestConsoleTermTransfers()._console_host()
        host._live_session_mgr()._session = s
        await host.run(f"cd {tmp_path / 'sub'}")
        assert (await host.exists("here"), await host.exists("sub")) == (True, False)


async def _console_mgr(session):
    """A console's :class:`SessionManager` whose default session is *session* (open)."""
    mgr = _mgr(term="console")
    mgr._session = session
    return mgr


async def _mgr_exec(mgr, cmd, **kw):
    return await mgr.exec_on_default_session(
        cmd=cmd,
        timeout=5,
        log=LogMode.QUIET,
        exec_input=ExecInput(stdin_eof=kw.pop("stdin_eof", True)),
        **kw,
    )


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_manager_s_exec_sees_run_and_keeps_its_own_changes(name):
    # pty_session's own restore is the pooled one: the manager gives the console's.
    async with console_session(name) as s:
        s._test_restore = ExecRestore.BASELINE
        mgr = await _console_mgr(s)
        left = await mgr.run_cmd(cmd="cd /tmp; export X=1; set -C", timeout=5)
        assert left.retcode == 0
        seen = await _mgr_exec(mgr, 'pwd; echo "${X-unset}"; case $- in *C*) echo C;; esac')
        own = await _mgr_exec(mgr, "cd /; export Y=2; X=3; set +C; exit 4")
        kept = await mgr.run_cmd(
            cmd='pwd; echo "${X-unset}"; case $- in *C*) echo C;; esac; echo "${Y-unset}"',
            timeout=5,
        )
    assert (seen.retcode, seen.value) == (0, "/tmp\n1\nC")
    assert own.retcode == 4
    assert kept.value == "/tmp\n1\nC\nunset"


_KILLED = (Status.Error, -1, "Session died unexpectedly")
"""What a command that kills its session's shell ends as, from ``run`` and from ``exec``."""


@pytest.mark.parametrize("entry", ["run", "exec"])
@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_command_that_kills_its_session_s_shell_ends_alike_from_run_and_exec(
    name, entry
):
    # `$$` is the console's one shell from either entry (test_exit_status.py states
    # the rule for a pooled session), so killing it ends the session both ways.
    async with console_session(name) as s:
        s._test_restore = ExecRestore.BASELINE
        mgr = await _console_mgr(s)
        if entry == "run":
            r = await mgr.run_cmd(cmd="kill -KILL $$", timeout=5)
        else:
            r = await _mgr_exec(mgr, "kill -KILL $$")
    assert (r.status, r.retcode, r.value[: len(_KILLED[2])]) == _KILLED, r


# zsh is not claimed, as on the other routes: its line editor hands the
# command the typed answer with its `\r` (test_exec_stdin.py).
@pytest.mark.parametrize("name", [n for n in PTY_SHELLS if n != "zsh"])
@pytest.mark.asyncio
async def test_a_console_exec_reads_end_of_file_or_the_caller_s_terminal(name):
    async with console_session(name) as s:
        eof = await s.exec_("cat; echo otto-cf-eof", timeout=5)
        typed = await s.exec_(
            "printf 'name?'; read n; echo got-$n", timeout=5, expects=[("name\\?", "otto\n")]
        )
    assert (eof.timed_out, eof.value) == (False, "otto-cf-eof")
    assert (typed.timed_out, typed.value.endswith("got-otto")) == (False, True), typed


_FD3_SHELLS = [n for n in PTY_SHELLS if n in ("bash", "dash", "busybox-sh")]


def test_the_credential_descriptor_shells_are_installed():
    assert _FD3_SHELLS, "no bash, dash or BusyBox sh installed: the fd 3 tests run on none"


@pytest.mark.parametrize("form", ["shell", "validate"])
@pytest.mark.parametrize("name", _FD3_SHELLS)
@pytest.mark.asyncio
async def test_a_console_exec_s_password_is_read_on_fd_3(name, form, fake_sudo_on_path):
    host = await _host_with_userland("sudo", _ADMIN, sudo_form=form)
    e = host._elevate("cat; echo otto-cf-eof", exec_input=ExecInput(stdin_eof=True))
    assert e.reads_credential_fd
    async with console_session(name, env=fake_sudo_on_path) as s:
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=True)
    assert (r.timed_out, r.value.endswith("otto-cf-eof")) == (False, True), r


@pytest.mark.parametrize("form", ["shell", "validate"])
@pytest.mark.parametrize("name", _FD3_SHELLS)
@pytest.mark.asyncio
async def test_a_refused_password_on_a_console_keeps_the_console(name, form, fake_sudo_on_path):
    from otto.host.login_proxy import Cred

    host = await _host_with_userland("sudo", [Cred(login="admin", password="wrong")], form)
    e = host._elevate("true", exec_input=ExecInput(stdin_eof=True))
    async with console_session(name, env=fake_sudo_on_path, setup="set -e") as s:
        with pytest.raises(PasswordRefusedError):
            await s.exec_(e.command, timeout=5, expects=e.expects, credential=True)
        after = await s.exec_("echo alive", timeout=5)
        opts = (await s.run_cmd(cmd="echo $-", timeout=5)).value
    assert (after.timed_out, after.value) == (False, "alive")
    assert "e" in opts


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_timed_out_console_exec_recovers_and_restores(name):
    async with console_session(name, setup="cd /tmp; set -e") as s:
        before = (await s.run_cmd(cmd="stty -g", timeout=5)).value
        assert (await s.exec_("exec sleep 30", timeout=0.3)).timed_out
        assert s.alive
        after = (await s.run_cmd(cmd="stty -g; pwd; echo $-", timeout=5)).value.split("\n")
        nxt = await s.exec_("echo next", timeout=5)
    assert after[:2] == [before, "/tmp"]
    assert "e" in after[2]
    assert (nxt.retcode, nxt.value) == (0, "next")


@pytest.mark.asyncio
async def test_control_a_pooled_exec_still_inherits_what_its_own_shell_has():
    async with pty_session("bash", setup="cd /tmp") as s:
        r = await s.exec_("pwd", timeout=5)
    assert r.value == "/tmp"


@pytest.mark.parametrize("exec_input", [None, ExecInput(stdin_eof=True)])
@pytest.mark.parametrize("code", [0, 3])
@pytest.mark.asyncio
async def test_an_end_record_with_no_begin_before_it_is_a_failure_on_every_frame(exec_input, code):
    """One decision, in the session's read: a run's or a pooled exec's frame reaches it too."""
    import asyncio

    from tests._fixtures.fake_shell import end_record
    from tests.unit.host._session_feed import onlcr_session

    s = await onlcr_session()
    flag = "" if exec_input is None else "B"
    noise = f'$ > echo "{s.markers.begin}"; x\r\n'  # an echoed copy of the typed line
    feed = asyncio.create_task(
        s.feed_after_write(noise + end_record(s.markers, code, flag=flag) + "\r\n")
    )
    r = await s.run_cmd(cmd="x", timeout=5, exec_input=exec_input)
    await feed
    assert (r.status, r.retcode, r.value) == (Status.Failed, -1, NEVER_RAN)


_OWN_SYNTAX = {
    "bash": ("[[ a == a ]] && echo otto-own-syntax", "otto-own-syntax"),
    "ksh": ("print -r -- otto-own-syntax", "otto-own-syntax"),
    "mksh": ("print -r -- otto-own-syntax", "otto-own-syntax"),
    "zsh": ("print -r -- otto-own-syntax", "otto-own-syntax"),
}
"""A command only the named shell runs: the sudo ``shell`` form must start that shell."""


def test_the_shell_identity_pins_have_a_shell_to_run_on():
    assert [n for n in PTY_SHELLS if n in _OWN_SYNTAX], "no bash, ksh, mksh or zsh installed"


@pytest.mark.parametrize("name", [n for n in PTY_SHELLS if n in _OWN_SYNTAX])
@pytest.mark.asyncio
async def test_a_console_exec_under_sudo_starts_the_console_s_own_shell(name, tmp_path):
    from otto.host.login_proxy import Cred
    from otto.host.privilege import _SUDO_SHELL_PROBE
    from tests._fixtures.fake_elevation import install_fake_elevation

    env = install_fake_elevation(tmp_path / "e", password="x", keeps_timestamp=True, nopasswd=True)
    host = await _host_with_userland("sudo", [Cred(login="admin")], sudo_form="shell")
    cmd, expected = _OWN_SYNTAX[name]
    x = ExecInput(stdin_eof=True)
    elevated = host._elevate(cmd, exec_input=x)
    probe = host._sudo_command(_SUDO_SHELL_PROBE, None, exec_input=x)
    async with console_session(name, env=env) as s:
        shell = (await s.run_cmd(cmd='echo "$SHELL"', timeout=5)).value
        seen = await s.exec_('echo "$SHELL"', timeout=5)
        r = await s.exec_(elevated.command, timeout=5)
        asked = await s.exec_(probe.command, timeout=5)
    assert (seen.retcode, seen.value) == (0, shell), seen
    assert (r.retcode, r.value) == (0, expected), r
    assert (asked.retcode, asked.value) == (0, shell), asked


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_exec_under_sudo_with_shell_unset_runs_in_the_default_shell(name, tmp_path):
    from otto.host.login_proxy import Cred
    from otto.host.privilege import _SUDO_SHELL_PROBE
    from tests._fixtures.fake_elevation import install_fake_elevation

    env = install_fake_elevation(tmp_path / "e", password="x", keeps_timestamp=True, nopasswd=True)
    host = await _host_with_userland("sudo", [Cred(login="admin")], sudo_form="shell")
    x = ExecInput(stdin_eof=True)
    elevated = host._elevate("echo otto-posix", exec_input=x)
    probe = host._sudo_command(_SUDO_SHELL_PROBE, None, exec_input=x)
    async with console_session(name, env=env, setup="unset SHELL") as s:
        seen = await s.exec_('echo "${SHELL-unset}"', timeout=5)
        r = await s.exec_(elevated.command, timeout=5)
        asked = await s.exec_(probe.command, timeout=5)
    assert (seen.retcode, seen.value) == (0, "unset"), seen
    assert (r.retcode, r.value) == (0, "otto-posix"), r
    assert (asked.retcode, asked.value) == (0, "/bin/sh"), asked
