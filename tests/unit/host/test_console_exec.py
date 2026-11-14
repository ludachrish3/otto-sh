"""A console's ``exec`` runs in a clean login shell started from the console's shell (#935).

A console has one shell for ``run`` and ``exec``. Its ``exec`` frame
(:meth:`~otto.host.command_frame.BashFrame.frame_exec` with
:attr:`~otto.host.command_frame.ExecRestore.START`, the restore a console's
default session is given) replaces its subshell with ``env -i ... "$SHELL" -l
-c``: the command starts in the login's home directory, environment and
options, never from what ``run`` left, and nothing it changes reaches a later
command. Every other promise of the exec frame holds as it does on a pooled
shell. Each is proved here on the real shells of the matrix on a pty
(:mod:`tests.unit.host._pty_session`), through a :class:`SessionManager`
configured as a console where the route matters.
"""

import shlex

import pytest

from otto.host.command_frame import BashFrame, ExecInput, ExecRestore, SessionMarkers
from otto.host.errors import PasswordRefusedError
from otto.host.exec_route import ExecRoute
from otto.host.session import NEVER_RAN
from otto.logger.mode import LogMode
from otto.utils import Status
from tests.unit.host._pty_session import PTY_SHELLS, console_session, pty_session
from tests.unit.host.test_exec_stdin import _ADMIN
from tests.unit.host.test_frame_shell_portability import FRAME_EDGE_CASES
from tests.unit.host.test_privilege import _host_with_userland
from tests.unit.host.test_session_exec_pooled import _mgr

M = SessionMarkers.for_session(session_id="0" * 8)

_STATE = 'pwd; echo "${X-unset}"; case $- in *f*) echo f;; *) echo nof;; esac'
"""Reads what ``run`` can leave: the directory, an exported variable, an option."""

_PROFILE = 'printf \'%s\\n\' {line} >"$HOME/.profile"; cp "$HOME/.profile" "$HOME/.zprofile"'
"""Writes a login profile every matrix shell reads (zsh reads ``.zprofile``)."""

_WORDS = [
    ("printf '%s|' 'single' \"double\" '$x' 'a\\b'", "single|double|$x|a\\b|", 0),
    ("printf '%s\\n' \"it's\" 'say \"hi\"'\nprintf done", 'it\'s\nsay "hi"\ndone', 0),
    ("x='a'\"'\"'b'; echo \"$x\" # it's a comment", "a'b", 0),
    ("printf '%s\\n' a\\\\b", "a\\b", 0),
    ("exit 7", "", 7),
    ("sh -c 'kill -TERM $$'", None, 143),
    ("kill -TERM $$", None, 143),
]
"""Commands whose quotes, ``$``, backslashes and ends must reach the login shell byte-exact."""


def test_shells_are_installed():
    assert PTY_SHELLS, "no shell of the matrix is installed: the console exec is proved on none"


def test_only_a_console_s_exec_starts_a_clean_login_shell():
    f = BashFrame()
    assert "env -i" in f.frame_exec("c", m=M, stdin_eof=True, restore=ExecRestore.START)
    assert "env -i" not in f.frame_exec("c", m=M, stdin_eof=True, restore=ExecRestore.BASELINE)


@pytest.mark.parametrize("case", FRAME_EDGE_CASES)
@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_exec_keeps_every_frame_edge_case(case, name):
    async with console_session(name) as s:
        r = await s.exec_(case.text, timeout=5)
    assert (r.timed_out, r.value, r.retcode) == (False, case.expected, case.code)


@pytest.mark.parametrize(("cmd", "value", "code"), _WORDS)
@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_exec_runs_the_command_as_one_word_with_its_own_status(
    name, cmd, value, code
):
    async with console_session(name) as s:
        r = await s.exec_(cmd, timeout=5)
        assert (r.timed_out, r.retcode) == (False, code)
        if value is not None:
            assert r.value == value
        assert (await s.exec_("echo ok", timeout=5)).value == "ok"


async def _console_mgr(session):
    """A console's :class:`SessionManager` whose default session is *session* (open)."""
    mgr = _mgr(term="console")
    mgr._session = session
    return mgr


async def _mgr_exec(mgr, cmd, **kw):
    return await mgr.exec_on_route(
        route=ExecRoute.DEFAULT_SESSION,
        cmd=cmd,
        timeout=5,
        log=LogMode.QUIET,
        exec_input=ExecInput(stdin_eof=kw.pop("stdin_eof", True)),
        **kw,
    )


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_console_manager_s_exec_starts_from_the_login_not_from_run(name):
    # pty_session's own restore is the pooled one: the manager gives the console's.
    async with console_session(name) as s:
        s._test_restore = ExecRestore.BASELINE
        mgr = await _console_mgr(s)
        home = (await mgr.run_cmd(cmd='echo "$HOME"', timeout=5)).value
        left = await mgr.run_cmd(cmd="cd /tmp; export X=1; set -f", timeout=5)
        assert left.retcode == 0
        clean = await _mgr_exec(mgr, _STATE)
        own = await _mgr_exec(mgr, "cd /; export Y=2; X=3; set -u; exit 4")
        kept = await mgr.run_cmd(cmd=_STATE + '; echo "${Y-unset}"', timeout=5)
        again = await _mgr_exec(mgr, _STATE)
    assert (clean.retcode, clean.value) == (0, f"{home}\nunset\nnof")
    assert own.retcode == 4
    assert kept.value == "/tmp\n1\nf\nunset"
    assert again.value == clean.value


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_the_console_s_errexit_is_neither_inherited_nor_changed(name):
    async with console_session(name, setup="set -e") as s:
        r = await s.exec_("false; echo continued", timeout=5)
        opts = (await s.run_cmd(cmd="echo $-", timeout=5)).value
    assert (r.retcode, r.value) == (0, "continued")
    assert "e" in opts


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_login_profile_s_output_does_not_reach_the_value(name):
    async with console_session(name) as s:
        await s.run_cmd(cmd=_PROFILE.format(line="otto-profile-says"), timeout=5)
        r = await s.exec_("echo value", timeout=5)
    assert (r.retcode, r.value) == (0, "value")


@pytest.mark.parametrize("profile", ["'exit 3'", "'exit 0'", "'exec true'"])
@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_login_shell_that_never_reaches_the_command_is_a_failure_never_a_success(
    name, profile
):
    async with console_session(name) as s:
        await s.run_cmd(cmd=_PROFILE.format(line=profile), timeout=5)
        r = await s.exec_("echo never", timeout=5)
        await s.run_cmd(cmd='rm "$HOME/.profile" "$HOME/.zprofile"', timeout=5)
        after = await s.exec_("echo alive", timeout=5)
    assert (r.timed_out, r.status, r.retcode, r.value) == (False, Status.Failed, -1, NEVER_RAN)
    assert (after.retcode, after.value) == (0, "alive")


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_login_shell_that_does_not_exist_is_the_command_s_failure(name):
    async with console_session(name, env={"SHELL": "/nonexistent/otto-sh"}) as s:
        r = await s.exec_("echo never", timeout=5)
        after = await s.run_cmd(cmd="echo alive", timeout=5)
    assert (r.timed_out, r.status, r.retcode, r.value) == (False, Status.Failed, -1, NEVER_RAN)
    assert after.value == "alive"


@pytest.mark.parametrize("home", ["unset HOME", "HOME="])
@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_login_with_no_home_starts_in_the_root_directory(name, home):
    async with console_session(name, setup=f"cd /tmp; {home}") as s:
        r = await s.exec_("pwd", timeout=5)
    assert (r.retcode, r.value) == (0, "/")


@pytest.mark.parametrize("name", PTY_SHELLS)
@pytest.mark.asyncio
async def test_a_home_with_spaces_and_quotes_reaches_the_login_shell(name, tmp_path):
    home = tmp_path / "a b'c\"d"
    home.mkdir()
    async with console_session(name) as s:
        await s.run_cmd(cmd=f"export HOME={shlex.quote(str(home))}", timeout=5)
        r = await s.exec_('pwd; printf "%s\\n" "$HOME"', timeout=5)
    assert (r.retcode, r.value) == (0, f"{home}\n{home}")


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
async def test_a_console_exec_s_password_is_read_on_fd_3_inside_the_login_shell(
    name, form, fake_sudo_on_path
):
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
async def test_a_console_with_shell_unset_passes_the_program_it_started(name):
    async with console_session(name, setup="unset SHELL") as s:
        r = await s.exec_('echo "$SHELL"', timeout=5)
    assert (r.retcode, r.value) == (0, "/bin/sh"), r


# zsh is left out as above: its line editor hands the command the typed answer with its `\r`.
@pytest.mark.parametrize("name", [n for n in PTY_SHELLS if n != "zsh"])
@pytest.mark.asyncio
async def test_a_caller_s_expect_is_never_answered_to_what_the_login_profile_prints(name):
    async with console_session(name) as s:
        await s.run_cmd(cmd=_PROFILE.format(line="'Continue? motd'"), timeout=5)
        typed: list[str] = []
        write = s.write_transport

        async def counting(*, data):
            if str(data) == "yes\n":
                typed.append(str(data))
            await write(data=data)

        s.write_transport = counting
        r = await s.exec_(
            "printf 'Continue? '; read a; echo got-$a",
            timeout=5,
            expects=[("Continue\\?", "yes\n")],
        )
        s.write_transport = write
        after = await s.exec_("echo alive", timeout=5)
    assert (r.timed_out, r.value.endswith("got-yes")) == (False, True), r
    assert typed == ["yes\n"]
    assert (after.timed_out, after.value) == (False, "alive")
