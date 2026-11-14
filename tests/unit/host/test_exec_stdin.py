"""What an ``exec``'s command reads on stdin, also after otto answers an elevation's password.

One decision (:func:`otto.host.host.exec_stdin_eof`): the command reads end of
file unless the caller passes pairs of its own. An elevation's password is
not the caller's: it is read from the caller's terminal on the frame's
credential descriptor (``EXEC_CREDENTIAL_FD``), so the elevated command
still reads end of file. The pty tests type the real elevation lines into
real shells, with the one stand-in ``sudo`` and ``su``
(:mod:`tests._fixtures.fake_elevation`) on ``PATH``.
"""

import dataclasses
import getpass

import pytest
from pydantic import SecretStr

from otto.host.command_frame import ExecInput
from otto.host.errors import PasswordRefusedError
from otto.host.host import CredentialExpect, exec_stdin_eof
from otto.host.login_proxy import Cred
from otto.host.userland import SUDO_FORM_SHELL, SUDO_FORM_VALIDATE
from tests._fixtures.fake_elevation import install_fake_elevation
from tests._fixtures.fake_shell import parse_bash_frame
from tests.unit.host._pty_session import PTY_SHELLS, pty_session
from tests.unit.host._session_feed import FD0_DUP_REFUSAL
from tests.unit.host.conftest import unix_host_over_scripted
from tests.unit.host.test_privilege import _exec_prompt, _host_with_userland

_ADMIN = [Cred(login="admin", password="secret")]
# The acting login is admin; su answers with root's password (privilege.py:
# root's cred arms su).
_SU_LOGIN = [Cred(login="admin", password="secret"), Cred(login="root", password="secret")]


@pytest.mark.parametrize(
    ("expects", "eof"),
    [
        (None, True),
        ([], True),
        ([CredentialExpect("p", SecretStr("x"), user="admin", host="box")], True),
        ([("x", "y\n")], False),
        ([CredentialExpect("p", SecretStr("x"), user="admin", host="box"), ("x", "y\n")], False),
    ],
)
def test_one_stdin_decision(expects, eof):
    assert exec_stdin_eof(expects) is eof


_FORMS = [SUDO_FORM_SHELL, SUDO_FORM_VALIDATE]


@pytest.mark.asyncio
# zsh is not claimed: its line editor hands the command the typed answer with
# its `\r`, so the password reaches sudo as `secret\r`.
@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize("name", [n for n in PTY_SHELLS if n != "zsh"])
@pytest.mark.parametrize(
    "cmd",
    [
        "cat; echo otto-cf-eof",
        "printf first; cat; echo otto-cf-eof",
        "cat >/dev/null; echo otto-cf-eof",
    ],
)
async def test_after_a_modeled_password_prompt_every_statement_reads_end_of_file(
    name, cmd, form, fake_sudo_on_path
):
    host = await _host_with_userland("sudo", _ADMIN, form)
    e = host._elevate(cmd, exec_input=ExecInput(stdin_eof=True))
    async with pty_session(name, env=fake_sudo_on_path) as s:
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
    # The prompt otto answered is the terminal's output, ahead of the command's.
    prompt = _exec_prompt(e.command)
    assert (r.timed_out, r.value.replace("first", "")) == (False, f"{prompt}otto-cf-eof")


@pytest.mark.asyncio
@pytest.mark.parametrize("form", _FORMS)
async def test_a_failed_authentication_ends_the_command_before_it_runs(
    form, fake_sudo_on_path, tmp_path
):
    ran = tmp_path / "ran"
    host = await _host_with_userland("sudo", [Cred(login="admin", password="wrong")], form)
    e = host._elevate(f"touch {ran}", exec_input=ExecInput(stdin_eof=True))
    async with pty_session("bash", env=fake_sudo_on_path) as s:
        with pytest.raises(PasswordRefusedError):
            await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
        after = await s.exec_("echo alive", timeout=5)
    assert not ran.exists()
    assert (after.timed_out, after.value) == (False, "alive")


@pytest.mark.asyncio
async def test_the_validate_form_needs_a_sudo_that_keeps_its_timestamp(
    fake_sudo_without_timestamp_on_path, tmp_path
):
    """The ``validate`` form's declared requirement: without a timestamp ``sudo -n`` is refused.

    The password validates (``sudo -S -v``) and the command's ``sudo -n``
    finds no timestamp on a sudo that keeps none (``timestamp_timeout=0``):
    nothing runs, status 1, nothing pending. The shell form is the answer.
    """
    ran = tmp_path / "ran"
    host = await _host_with_userland("sudo", _ADMIN, SUDO_FORM_VALIDATE)
    e = host._elevate(f"touch {ran}", exec_input=ExecInput(stdin_eof=True))
    async with pty_session("bash", env=fake_sudo_without_timestamp_on_path) as s:
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
        assert s.alive
    prompt = _exec_prompt(e.command)
    assert not ran.exists()
    assert (r.timed_out, r.retcode) == (False, 1)
    assert r.value == f"{prompt}sudo: a password is required"


@pytest.mark.asyncio
async def test_the_shell_form_runs_on_a_sudo_that_keeps_no_timestamp(
    fake_sudo_without_timestamp_on_path, tmp_path
):
    ran = tmp_path / "ran"
    host = await _host_with_userland("sudo", _ADMIN, SUDO_FORM_SHELL)
    e = host._elevate(f"touch {ran}", exec_input=ExecInput(stdin_eof=True))
    async with pty_session("bash", env=fake_sudo_without_timestamp_on_path) as s:
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
    assert (r.timed_out, r.retcode) == (False, 0), r
    assert ran.exists()


@pytest.mark.asyncio
async def test_the_shell_form_on_a_sudo_that_refuses_sh_is_an_ordinary_refusal(tmp_path):
    """The shell form's declared requirement: a sudoers without the login's shell refuses it."""
    env = install_fake_elevation(
        tmp_path / "elevation", password="secret", keeps_timestamp=True, refuses_shell=True
    )
    ran = tmp_path / "ran"
    host = await _host_with_userland("sudo", _ADMIN, SUDO_FORM_SHELL)
    e = host._elevate(f"touch {ran}", exec_input=ExecInput(stdin_eof=True))
    async with pty_session("bash", env=env) as s:
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
        after = await s.exec_("echo alive", timeout=5)
    assert not ran.exists()
    assert (r.timed_out, r.retcode) == (False, 1), r
    assert "is not allowed to execute" in r.value
    assert (after.timed_out, after.value) == (False, "alive")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("form", "second"), [(SUDO_FORM_SHELL, "root"), (SUDO_FORM_VALIDATE, getpass.getuser())]
)
@pytest.mark.parametrize("password", [True, False])
async def test_what_each_form_elevates_of_a_compound_command(
    form, second, password, fake_sudo_on_path
):
    """``shell`` elevates the whole string; ``validate`` only its first simple command.

    The same meaning with and without a password: a passwordless sudo is
    modelled by a timestamp already kept.
    """
    creds = _ADMIN if password else [Cred(login="admin")]
    host = await _host_with_userland("sudo", creds, form)
    e = host._elevate("id -un; id -un", exec_input=ExecInput(stdin_eof=True))
    async with pty_session("bash", env=fake_sudo_on_path) as s:
        if not password:
            warm = await s.run_cmd(cmd="echo secret | sudo -S -v", timeout=5)
            assert warm.retcode == 0
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
    answered = _exec_prompt(e.command) if password else ""
    lines = r.value.removeprefix(answered).splitlines()
    assert (r.timed_out, r.retcode, lines) == (False, 0, ["root", second]), r


@pytest.mark.asyncio
@pytest.mark.parametrize("form", _FORMS)
async def test_each_form_returns_the_command_s_status(form, fake_sudo_on_path):
    host = await _host_with_userland("sudo", _ADMIN, form)
    e = host._elevate("sh -c 'exit 7'", exec_input=ExecInput(stdin_eof=True))
    async with pty_session("bash", env=fake_sudo_on_path) as s:
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
    assert (r.timed_out, r.retcode) == (False, 7), r


@pytest.mark.asyncio
async def test_a_password_run_on_a_sudo_without_timestamp_still_runs_the_command(
    fake_sudo_without_timestamp_on_path, tmp_path
):
    ran = tmp_path / "ran"
    host = await _host_with_userland("sudo", _ADMIN)
    e = host._elevate(f"touch {ran}")
    async with pty_session("bash", env=fake_sudo_without_timestamp_on_path) as s:
        r = await s.run_cmd(cmd=e.command, timeout=5, expects=e.expects)
    assert (r.timed_out, r.retcode) == (False, 0)
    assert ran.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("form", _FORMS)
async def test_a_refused_password_under_inherited_errexit_and_nounset_keeps_the_shell(
    form, fake_sudo_on_path
):
    host = await _host_with_userland("sudo", [Cred(login="admin", password="wrong")], form)
    e = host._elevate("true", exec_input=ExecInput(stdin_eof=True))
    async with pty_session("bash", env=fake_sudo_on_path) as s:
        armed = await s.run_cmd(cmd="set -eu", timeout=5)
        assert (armed.timed_out, armed.retcode) == (False, 0)
        with pytest.raises(PasswordRefusedError):
            await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
        after = await s.exec_("echo alive", timeout=5)
    assert (after.timed_out, after.value) == (False, "alive")


@pytest.mark.asyncio
@pytest.mark.parametrize("form", _FORMS)
async def test_each_form_runs_under_inherited_errexit_and_nounset(form, fake_sudo_on_path):
    host = await _host_with_userland("sudo", _ADMIN, form)
    e = host._elevate("id -un", exec_input=ExecInput(stdin_eof=True))
    async with pty_session("bash", env=fake_sudo_on_path) as s:
        armed = await s.run_cmd(cmd="set -eu", timeout=5)
        assert (armed.timed_out, armed.retcode) == (False, 0)
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
        after = await s.exec_("echo alive", timeout=5)
    assert (r.timed_out, r.retcode, r.value.endswith("root")) == (False, 0, True), r
    assert (after.timed_out, after.value) == (False, "alive")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mechanism", "form"), [("sudo", SUDO_FORM_SHELL), ("sudo", SUDO_FORM_VALIDATE), ("su", None)]
)
async def test_a_caller_prompt_after_the_password_keeps_the_terminal(
    mechanism, form, fake_sudo_on_path, fake_su_on_path
):
    creds = _ADMIN if mechanism == "sudo" else _SU_LOGIN
    host = await _host_with_userland(mechanism, creds, form)
    caller = ("name\\?", "otto\n")
    x = ExecInput(stdin_eof=exec_stdin_eof([caller]))
    e = host._elevate("printf 'name?'; read n; echo got-$n", exec_input=x)
    x = dataclasses.replace(x, credential=e.reads_credential_fd)
    async with pty_session("bash", env=fake_sudo_on_path | fake_su_on_path) as s:
        r = await s.run_cmd(cmd=e.command, timeout=5, expects=[*e.expects, caller], exec_input=x)
    assert (r.timed_out, r.value.endswith("got-otto")) == (False, True), r


@pytest.mark.asyncio
async def test_after_a_modeled_su_password_the_command_reads_end_of_file(fake_su_on_path):
    host = await _host_with_userland("su", _SU_LOGIN)
    e = host._elevate("cat; echo otto-cf-eof", exec_input=ExecInput(stdin_eof=True))
    async with pty_session("bash", env=fake_su_on_path) as s:
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
    assert (r.timed_out, r.value.endswith("otto-cf-eof")) == (False, True), r


@pytest.mark.asyncio
@pytest.mark.parametrize(("caller", "eof"), [(None, True), ([("name\\?", "otto\n")], False)])
async def test_one_exec_input_reaches_the_elevation_and_the_frame_through_the_real_path(
    caller, eof
):
    host, sessions = await unix_host_over_scripted("telnet", elevation="su", creds=_SU_LOGIN)
    try:
        r = await host.exec("true", sudo=True, expects=caller)
        assert (r.timed_out, r.retcode) == (False, 0)
        framed = parse_bash_frame(sessions[-1].written_frames[-1])
        assert framed is not None
        assert framed.isolated
        assert framed.stdin_eof is eof
        assert framed.credential
        assert ("exec </dev/null; " in framed.cmd) is eof
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("sudo", [False, True])
async def test_only_an_exec_whose_password_otto_answers_opens_the_credential_descriptor(sudo):
    """``su`` as root's cred arms reads fd 3; root itself arms nothing, and neither does no sudo."""
    host, sessions = await unix_host_over_scripted("telnet", elevation="su", creds=_SU_LOGIN)
    try:
        r = await host.exec("true", sudo=sudo)
        assert (r.timed_out, r.retcode) == (False, 0)
        typed = sessions[-1].written_frames[-1]
        framed = parse_bash_frame(typed)
        assert framed is not None
        assert framed.credential is sudo
        assert ("3<" in typed) is sudo
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_a_plain_exec_runs_on_a_shell_that_refuses_to_duplicate_fd_0():
    """BusyBox 1.16.1's ash answers ``3<&0`` with "Bad file descriptor": no plain exec types it."""
    host, _ = await unix_host_over_scripted(
        "telnet", elevation="su", creds=_SU_LOGIN, refuses_fd0_dup=True
    )
    try:
        r = await host.exec("true")
        assert (r.timed_out, r.retcode) == (False, 0), r
        # The declared device limit: a password otto answers still needs fd 3.
        elevated = await host.exec("true", sudo=True)
        assert (elevated.retcode, elevated.value) == (FD0_DUP_REFUSAL[1], FD0_DUP_REFUSAL[0])
    finally:
        await host.close()


_FD3_SHELLS = [n for n in PTY_SHELLS if n in ("bash", "dash", "busybox-sh")]


def test_the_credential_descriptor_shells_are_installed():
    assert _FD3_SHELLS, "no bash, dash or BusyBox sh installed: the fd 3 tests run on none"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _FD3_SHELLS)
async def test_an_exec_that_arms_no_password_runs_with_fd_3_closed(name):
    async with pty_session(name) as s:
        r = await s.exec_("if ( : <&3 ) 2>/dev/null; then echo fd3-open; else echo fd3-closed; fi")
    assert (r.timed_out, r.value) == (False, "fd3-closed"), r


@pytest.mark.asyncio
@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize("name", _FD3_SHELLS)
async def test_an_exec_that_arms_a_password_still_reads_it_on_fd_3(name, form, fake_sudo_on_path):
    host = await _host_with_userland("sudo", _ADMIN, form)
    e = host._elevate("cat; echo otto-cf-eof", exec_input=ExecInput(stdin_eof=True))
    assert e.reads_credential_fd
    async with pty_session(name, env=fake_sudo_on_path) as s:
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=True)
    assert (r.timed_out, r.value.endswith("otto-cf-eof")) == (False, True), r


@pytest.mark.asyncio
@pytest.mark.parametrize(("caller", "eof"), [(None, True), ([("name\\?", "otto\n")], False)])
async def test_a_container_s_exec_decides_stdin_once_on_its_parent(caller, eof, monkeypatch):
    """Docker's ``exec`` hands ``parent.exec`` the caller's pairs, and the parent decides."""
    from otto.host.docker_host import DockerContainerHost

    parent, sessions = await unix_host_over_scripted("telnet", elevation="sudo", creds=_ADMIN)

    async def _docker_exec(self, cmd, *, user=None):
        return f"docker exec -i c1 sh -c {cmd!r}"

    monkeypatch.setattr(DockerContainerHost, "_docker_exec", _docker_exec)
    container = DockerContainerHost(
        parent=parent,
        container_id="c1",
        project="p",
        service="api",
        compose_project="otto-p",
    )
    try:
        r = await container.exec("true", expects=caller)
        assert (r.timed_out, r.retcode) == (False, 0)
        framed = parse_bash_frame(sessions[-1].written_frames[-1])
        assert framed is not None
        assert framed.isolated
        assert framed.stdin_eof is eof
    finally:
        await parent.close()


@pytest.mark.asyncio
async def test_the_shell_form_runs_the_command_in_the_session_s_own_shell_language(
    fake_sudo_on_path,
):
    """Under a bash session the elevated command is bash: ``[[ ]]`` and brace expansion work."""
    host = await _host_with_userland("sudo", _ADMIN, SUDO_FORM_SHELL)
    e = host._elevate("[[ a == a ]] && echo {x,y}", exec_input=ExecInput(stdin_eof=True))
    env = fake_sudo_on_path | {"SHELL": "/bin/bash"}
    async with pty_session("bash", env=env) as s:
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
    assert (r.timed_out, r.retcode, r.value.removeprefix(_exec_prompt(e.command))) == (
        False,
        0,
        "x y",
    ), r


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [n for n in PTY_SHELLS if n in ("dash", "busybox-sh")])
async def test_the_shell_form_runs_under_a_posix_session_shell(name, fake_sudo_on_path):
    """A dash or BusyBox session whose ``$SHELL`` is itself: the form still runs."""
    import shutil

    host = await _host_with_userland("sudo", _ADMIN, SUDO_FORM_SHELL)
    e = host._elevate("id -un; id -un", exec_input=ExecInput(stdin_eof=True))
    program = shutil.which("dash" if name == "dash" else "busybox")
    shell = program if name == "dash" else "/bin/sh"
    async with pty_session(name, env=fake_sudo_on_path | {"SHELL": shell}) as s:
        r = await s.exec_(e.command, timeout=5, expects=e.expects, credential=e.reads_credential_fd)
    lines = r.value.removeprefix(_exec_prompt(e.command)).splitlines()
    assert (r.timed_out, r.retcode, lines) == (False, 0, ["root", "root"]), r


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [None, ("name\\?", "otto\n")])
async def test_a_nopasswd_sudo_gets_no_password_and_leaves_nothing_pending(caller, tmp_path):
    """A password configured, a sudo that never asks: nothing typed, nothing reaches the command."""
    env = install_fake_elevation(
        tmp_path / "e", password="secret", keeps_timestamp=False, nopasswd=True
    )
    host = await _host_with_userland("sudo", _ADMIN, SUDO_FORM_SHELL)
    pairs = [caller] if caller else []
    x = ExecInput(stdin_eof=exec_stdin_eof(pairs))
    cmd = "printf 'name?'; read n; echo got-$n" if caller else "cat; echo otto-cf-eof"
    e = host._elevate(cmd, exec_input=x)
    x = dataclasses.replace(x, credential=e.reads_credential_fd)
    async with pty_session("bash", env=env) as s:
        r = await s.run_cmd(cmd=e.command, timeout=5, expects=[*e.expects, *pairs], exec_input=x)
        after = await s.exec_("echo alive", timeout=5)
    assert r.value == ("name?got-otto" if caller else "otto-cf-eof"), r
    assert "secret" not in r.value
    assert not (tmp_path / "e" / "sudo" / "reads").exists()
    assert (after.timed_out, after.value) == (False, "alive")
