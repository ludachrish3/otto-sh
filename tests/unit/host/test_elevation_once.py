"""The elevation password answers its own prompt, once, after the command starts.

Each case drives the real session read loop against
:class:`~tests._fixtures.elevation_shell.ElevationShell`, which prompts the
way sudo and su do, and plants text that looks like a prompt where the old
loop would have typed the password at it: the old fixed ``otto-sudo:`` in
output, the prompt echoed in the typed command line before BEGIN, a process
named like a prompt, a ``Password:`` printed under a root session, and the
prompt coming back after a wrong password.
"""

import asyncio
import logging
import re

import pytest
import pytest_asyncio
from pydantic import SecretStr

import otto.host.session as session_mod
from otto.host.element import Element
from otto.host.errors import PasswordRefusedError
from otto.host.host import CredentialExpect
from otto.host.options import UserlandOptions
from otto.host.unix_host import UnixHost
from otto.host.userland import Userland
from otto.logger.mode import LogMode
from otto.models.base import Cred
from otto.result import CommandResult
from otto.utils import Status, wait_for_async
from tests._fixtures.elevation_shell import ElevationSession, ElevationShell

PLANTED = "PLANTED-SECRET-7f3a"
_TIMEOUT = 5.0


async def _answers_no(cmd: str, **_kwargs: object) -> CommandResult:
    """A userland runner on which every probe fails, so only the declaration decides."""
    return CommandResult(status=Status.Error, value="", command=cmd, retcode=127)


def _host(creds: list[Cred], userland: Userland | None = None) -> UnixHost:
    class _Host(UnixHost):
        def _userland(self) -> Userland | None:
            return userland

    return _Host(ip="10.0.0.1", element=Element("box"), creds=creds, log=LogMode.QUIET)


async def _su_host(creds: list[Cred]) -> UnixHost:
    userland = Userland(UserlandOptions(elevation="su"), _answers_no)
    await userland.resolve()
    return _host(creds, userland)


@pytest.fixture
def host() -> UnixHost:
    """A sudo host (no resolver) whose login user admin has the planted password."""
    return _host([Cred(login="admin", password=PLANTED)])


@pytest_asyncio.fixture
async def su_host() -> UnixHost:
    """A su host whose root cred has the planted password; the login user is admin."""
    return await _su_host([Cred(login="admin"), Cred(login="root", password=PLANTED)])


async def _run_elevated(
    host: UnixHost, session: ElevationSession, cmd: str, *, redact: bool = False
) -> CommandResult:
    elevated = host._elevate(cmd)
    return await session.run_cmd(
        elevated.command,
        expects=elevated.expects,
        timeout=_TIMEOUT,
        redact=redact,
    )


# ---------------------------------------------------------------------------
# The command lines
# ---------------------------------------------------------------------------


def test_sudo_command_line_keeps_argv_and_uses_env_prompt(host):
    e = host._elevate("ls /root; echo done")
    assert re.fullmatch(
        r"SUDO_PROMPT='otto-sudo-[A-Za-z0-9]{16}:' sudo -S ls /root; echo done", e.command
    )
    assert "%" not in e.command
    (answer,) = e.expects
    assert isinstance(answer, CredentialExpect)
    prompt = e.command.split("'")[1]
    assert answer.pattern == re.escape(prompt)
    assert answer.response == SecretStr(PLANTED)


def test_every_elevation_gets_a_fresh_prompt(host):
    prompts = {host._elevate("id").command.split("'")[1] for _ in range(20)}
    assert len(prompts) == 20


@pytest.mark.asyncio
async def test_su_command_line_unchanged(su_host):
    assert su_host._elevate("id").command == "su -c id"


def test_the_prompt_is_in_no_argv(host):
    """The prompt is an assignment on the sudo invocation, never a word sudo or the command gets."""
    e = host._elevate("id")
    assignment, _, argv = e.command.partition(" ")
    assert assignment.startswith("SUDO_PROMPT='otto-sudo-")
    assert "otto-sudo" not in argv
    assert argv == "sudo -S id"


# ---------------------------------------------------------------------------
# Text that looks like a prompt gets no password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_lookalikes_after_begin_send_nothing(host):
    """Cached credentials: sudo never prompts, so nothing in the output is its prompt."""
    shell = ElevationShell(
        password=PLANTED,
        cached=True,
        outputs={"cat notes": "otto-sudo:\notto-sudo-AAAAAAAAAAAAAAAA:\nPassword:\nend\n"},
    )
    session = ElevationSession(shell)
    result = await _run_elevated(host, session, "cat notes")
    assert result.status == Status.Success, result
    assert "otto-sudo:" in result.value
    assert shell.writes_of(PLANTED) == 0


@pytest.mark.asyncio
async def test_nonce_in_the_echoed_command_before_begin_sends_nothing(host):
    """The terminal echoes the typed line, prompt and all, before BEGIN: that is not sudo asking."""
    shell = ElevationShell(password=PLANTED, cached=True, echo=True, outputs={"id": "uid=0\n"})
    session = ElevationSession(shell)
    result = await _run_elevated(host, session, "id")
    assert result.status == Status.Success, result
    assert shell.writes_of(PLANTED) == 0


@pytest.mark.asyncio
async def test_planted_process_name_sends_nothing(host):
    """A process named like a prompt (the old fixed one, a guessed nonce) is not this prompt."""
    listing = (
        "  PID CMD\n"
        "  101 otto-sudo:\n"
        "  102 otto-sudo-0123456789abcdef:\n"
        "  103 [Pp]assword: Password:\n"
    )
    shell = ElevationShell(password=PLANTED, cached=True, outputs={"ps -eo pid,cmd": listing})
    session = ElevationSession(shell)
    result = await _run_elevated(host, session, "ps -eo pid,cmd")
    assert result.status == Status.Success, result
    assert shell.writes_of(PLANTED) == 0


@pytest.mark.asyncio
async def test_the_real_prompt_in_output_is_answered_at_most_once(host):
    """The documented residual: a command that prints the live prompt gets one answer, no more."""
    shell = ElevationShell(password=PLANTED, cached=True, outputs={"env-dump": "{prompt}\nok\n"})
    session = ElevationSession(shell)
    result = await _run_elevated(host, session, "env-dump")
    assert result.status == Status.Success, result
    assert shell.writes_of(PLANTED) == 1

    shell.outputs["env-dump-twice"] = "{prompt}\n{prompt}\n"
    with pytest.raises(PasswordRefusedError):
        await _run_elevated(host, session, "env-dump-twice")
    assert shell.writes_of(PLANTED) == 2  # one per command, never two in one


# ---------------------------------------------------------------------------
# The real prompt: answered once; refused fails fast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sudo_answers_its_prompt_once(host):
    shell = ElevationShell(password=PLANTED, outputs={"id -u": "0\n"})
    session = ElevationSession(shell)
    result = await _run_elevated(host, session, "id -u")
    assert result.status == Status.Success, result
    assert result.value.endswith("0")
    assert shell.writes_of(PLANTED) == 1


@pytest.mark.asyncio
async def test_wrong_password_fails_fast(host):
    """sudo prompts again after a wrong password: otto stops there, not at the timeout."""
    shell = ElevationShell(password="the-right-one", outputs={"echo ok": "ok\n"})
    session = ElevationSession(shell)
    with pytest.raises(PasswordRefusedError, match=r"password refused for admin on box") as ei:
        await _run_elevated(host, session, "id")
    assert PLANTED not in str(ei.value)
    assert shell.writes_of(PLANTED) == 1
    assert "\x03" in shell.writes, "the refused command was not interrupted"
    assert session.alive
    assert (await session.run_cmd("echo ok", timeout=_TIMEOUT)).value == "ok"


@pytest.mark.asyncio
async def test_a_refusal_whose_probe_fails_discards_the_session(host, monkeypatch):
    monkeypatch.setattr(session_mod, "_RECOVERY_TIMEOUT", 0.3)
    shell = ElevationShell(password="the-right-one", wedged=True)
    session = ElevationSession(shell)
    with pytest.raises(PasswordRefusedError):
        await _run_elevated(host, session, "id")
    assert not session.alive


@pytest.mark.asyncio
async def test_cancel_during_recovery_leaves_no_wedged_session(host):
    """A cancellation landing while the refused command is being interrupted marks recovery due."""
    shell = ElevationShell(password="the-right-one", wedged=True, outputs={"echo ok": "ok\n"})
    session = ElevationSession(shell)
    task = asyncio.create_task(_run_elevated(host, session, "id"))
    await wait_for_async(
        lambda: "\x03" in shell.writes,
        timeout=_TIMEOUT,
        on_timeout="the refused command was never interrupted",
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session._needs_recovery

    shell.wedged = False
    assert (await session.run_cmd("echo ok", timeout=_TIMEOUT)).value == "ok"
    assert shell.writes_of(PLANTED) == 1


@pytest.mark.asyncio
async def test_su_answers_once(su_host):
    shell = ElevationShell(password=PLANTED, outputs={"id -u": "0\n"})
    session = ElevationSession(shell)
    result = await _run_elevated(su_host, session, "id -u")
    assert result.status == Status.Success, result
    assert shell.writes_of(PLANTED) == 1


@pytest.mark.asyncio
async def test_su_with_a_wrong_password_fails_once_and_keeps_the_session(su_host):
    """su does not prompt twice: it reports and exits, so the result is the failure."""
    shell = ElevationShell(password="the-right-one", outputs={"echo ok": "ok\n"})
    session = ElevationSession(shell)
    result = await _run_elevated(su_host, session, "id -u")
    assert result.status == Status.Failed
    assert shell.writes_of(PLANTED) == 1
    assert (await session.run_cmd("echo ok", timeout=_TIMEOUT)).value == "ok"


@pytest.mark.asyncio
async def test_su_output_saying_password_after_the_answer_is_output(su_host):
    """su accepted the password; a ``Password:`` the command then prints is not su asking.

    su never prompts twice, so its answer is not refused on repeat: the later
    match gets no answer and raises nothing.
    """
    shell = ElevationShell(
        password=PLANTED, outputs={"cat app.yaml": "db:\n  Password: x\n  password: y\n"}
    )
    session = ElevationSession(shell)
    result = await _run_elevated(su_host, session, "cat app.yaml")
    assert result.status == Status.Success, result
    assert "Password: x" in result.value
    assert shell.writes_of(PLANTED) == 1
    assert "\x03" not in shell.writes


@pytest.mark.asyncio
async def test_begin_is_detected_on_a_chunk_an_expect_answered(host):
    """A caller pair matching the BEGIN line must not hide BEGIN from the credential gate.

    Mutation: skip BEGIN detection on a chunk where a pair matched and the
    sudo prompt is never answered, so the command times out.
    """
    shell = ElevationShell(password=PLANTED, outputs={"id -u": "0\n"})
    session = ElevationSession(shell)
    elevated = host._elevate("id -u")
    begin_pair = (re.escape(session._markers.begin), "")  # answers with nothing on the wire
    result = await session.run_cmd(
        elevated.command, expects=[*elevated.expects, begin_pair], timeout=2.0
    )
    assert result.status == Status.Success, result
    assert shell.writes_of(PLANTED) == 1


@pytest.mark.asyncio
async def test_a_refusal_on_a_host_with_no_name_still_reads_as_a_sentence():
    nameless = _host([Cred(login="admin", password=PLANTED)])
    object.__setattr__(nameless, "name", "")
    shell = ElevationShell(password="the-right-one")
    with pytest.raises(PasswordRefusedError, match=r"^password refused for admin on this host$"):
        await _run_elevated(nameless, ElevationSession(shell), "id")


@pytest.mark.asyncio
async def test_root_session_running_su_never_answers():
    """su never asks root, so a root session arms no answer and a ``Password:`` is output."""
    host = await _su_host([Cred(login="root", password=PLANTED)])
    assert host.current_user == "root"
    elevated = host._elevate("cat banner")
    assert elevated.expects == []

    shell = ElevationShell(user="root", password=PLANTED, outputs={"cat banner": "Password:\n"})
    session = ElevationSession(shell)
    result = await session.run_cmd(elevated.command, expects=elevated.expects, timeout=_TIMEOUT)
    assert result.status == Status.Success, result
    assert result.value == "Password:"
    assert shell.writes_of(PLANTED) == 0


@pytest.mark.asyncio
async def test_exec_as_root_with_su_arms_nothing(su_host):
    """``exec(user="root")`` runs su as root, which is never asked."""
    assert su_host._elevate("id", user="root").expects == []
    assert su_host._elevate("id").expects != []


# ---------------------------------------------------------------------------
# The DEBUG line names the pattern and the answer's length, never the answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("pair", ["elevation", "caller_plain", "caller_secret"])
async def test_expect_match_line_logs_only_the_length(pair, host, caplog):
    caplog.set_level(logging.DEBUG, logger="otto.host.session")
    shell = ElevationShell(password=PLANTED, outputs={"ask": "Password:\nok\n", "id": "0\n"})
    session = ElevationSession(shell)
    if pair == "elevation":
        response: str | SecretStr = SecretStr(PLANTED)
        result = await _run_elevated(host, session, "id")
    else:
        response = f"{PLANTED}\n" if pair == "caller_plain" else SecretStr(PLANTED)
        result = await session.run_cmd("ask", expects=[("Password:", response)], timeout=_TIMEOUT)
    assert result.status == Status.Success, result
    assert "expect matched" in caplog.text
    assert f"response=<{len(response)} chars>" in caplog.text
    assert PLANTED not in caplog.text


@pytest.mark.asyncio
async def test_under_log_never_no_response_is_logged_at_all(host, caplog):
    caplog.set_level(logging.DEBUG, logger="otto.host.session")
    shell = ElevationShell(password=PLANTED, outputs={"id": "0\n"})
    session = ElevationSession(shell)
    result = await _run_elevated(host, session, "id", redact=True)
    assert result.status == Status.Success, result
    assert shell.writes_of(PLANTED) == 1
    assert "expect matched" not in caplog.text
    assert "response=" not in caplog.text
