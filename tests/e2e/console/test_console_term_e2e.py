"""The ``console`` term against test2's serial getty, served by ser2net on test1.

Login and run over both dial modes, the ``shell`` transfer on the one
session, logout on close, and the failures a console must raise loudly
(already logged in, a refused login, a busy line), ``exec`` on the one
session with a named session still refused, a reboot that never takes
(otto logs the line out), and a console otto does not log in on, whose
shell otto leaves as it found it.

Every test releases the line and leaves it at its login prompt; the
``test2_console`` fixture (``conftest.py``) checks that after each test and,
when a test broke it, restores the prompt and fails the test that broke it.
"""

import os
from dataclasses import replace
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock

import pytest

from otto.host.errors import ConsoleError
from otto.host.login_proxy import Cred
from otto.result import Result
from otto.utils import Status
from tests._fixtures.interactive_shell import PersonState, parse_person_state, person_state_line
from tests._fixtures.labdata import host_data, host_data_console, make_console_host
from tests.e2e.console._raw import (
    collect,
    console_answer,
    ends_at_login,
    login_and_abandon,
    raw_console,
    server_cred,
    strip_ansi_bytes,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.timeout(180),
    # One worker for the module: every test drives the same single-client line.
    pytest.mark.xdist_group("console_e2e"),
    pytest.mark.usefixtures("test2_console"),
]

_OPTS = host_data_console("test2")
_SERVER = host_data(_OPTS.server)
_CREDS = [Cred(**c) for c in host_data("test2")["creds"]]
_CONSOLE_LOGIN = _CREDS[0]
"""The cred the console logs in as: the first that applies to the term."""
_OTHER = next(c for c in _CREDS if c.login != _CONSOLE_LOGIN.login)
"""A second account, for the shell someone else left logged in."""


@pytest.mark.parametrize("host1", ["console"], indirect=True)
async def test_login_and_run(host1):
    res = (await host1.run("id -un")).only
    assert res.retcode == 0, res
    assert res.value == _CONSOLE_LOGIN.login


async def test_direct_dial():
    h = make_console_host("test2", console_options=replace(_OPTS, dial="direct"))
    try:
        assert (await h.run("hostname")).only.value == "test2"
    finally:
        await h.close()


@pytest.mark.parametrize("host1", ["console"], indirect=True)
async def test_shell_transfer_roundtrip(host1, tmp_path):
    src = tmp_path / "otto_console_blob.bin"
    src.write_bytes(os.urandom(20_000))
    remote = PurePosixPath("/tmp") / src.name
    back = tmp_path / "back"
    back.mkdir()
    try:
        put = await host1.put(src, Path(remote.parent))
        assert put.is_ok, put
        got = await host1.get(Path(remote), back)
        assert got.is_ok, got
        assert (back / src.name).read_bytes() == src.read_bytes()
    finally:
        await host1.run(f"rm -f {remote}")


async def test_already_logged_in_fails_loud():
    await login_and_abandon(_OPTS.server, _OPTS.port, _OTHER.login, _OTHER.password)
    h = make_console_host("test2")
    try:
        with pytest.raises(ConsoleError, match=rf"already logged in.*{_OTHER.login}@test2") as ei:
            await h.run("true")
        assert _OTHER.password is not None
        secret = _OTHER.password.get_secret_value()
        assert secret not in str(ei.value), "the refusal quotes the password"
    finally:
        await h.logout()  # the abandoned shell back to login:
        await h.close()


async def _person_state(*typed_first: str) -> PersonState:
    """Type each of *typed_first* into the shell on test2's line, then read its state.

    Over a raw line, so the reading reaches the shell as it is, by the one
    reading line (:func:`~tests._fixtures.interactive_shell.person_state_line`).
    """
    typed = person_state_line()
    cred = server_cred(_OPTS.server)
    async with raw_console(_SERVER["ip"], cred.login, cred.password, _OPTS.port) as line:
        for text in (*typed_first, typed.line.rstrip("\n")):
            line.writer.write(text.encode() + b"\r")
        raw = await collect(
            line, 20.0, lambda b: typed.done.search(b.decode(errors="replace")) is not None
        )
    out = strip_ansi_bytes(raw).decode(errors="replace")
    assert typed.done.search(out), f"the shell on test2's line printed no state reading: {out!r}"
    return parse_person_state(out, typed)


async def test_a_login_false_console_leaves_the_persons_shell_as_it_found_it():
    """#1358: otto's session on a console it does not log in on leaves the person's shell as it was.

    No lab host declares ``login: false`` on a bash console, so test2's
    console is used with that option. A person's shell is left logged in on
    the line, with an unexported variable typed into it. otto's session runs
    in a child of that shell (its ``$PPID``), reads ``!`` and a TAB as a
    script does, and its close leaves the child: the person's shell keeps its
    variable, its TAB completion, its ``!`` history expansion and the rest of
    what the reading shows.
    """
    await login_and_abandon(_OPTS.server, _OPTS.port, _OTHER.login, _OTHER.password)
    try:
        before = await _person_state("PV=person")
        assert before.shell.pv == "person", before
        h = make_console_host("test2", console_options=replace(_OPTS, login=False))
        try:
            ppid = (await h.run("echo $PPID")).only
            assert (ppid.retcode, ppid.value) == (0, str(before.shell.pid)), ppid
            bang = (await h.run("echo a !b c")).only
            assert (bang.retcode, bang.value) == (0, "a !b c"), bang
            tab = (await h.run("echo 'a\tb'")).only
            assert (tab.retcode, tab.value) == (0, "a\tb"), tab
        finally:
            await h.close()
        after = await _person_state()
        assert after.shell.pv == "person", after
        assert '"\\C-i": complete' in after.shell.tab.splitlines(), after.shell.tab
        assert "set -o histexpand" in after.shell.options.splitlines(), after.shell.options
        assert after == before
    finally:
        h = make_console_host("test2")
        try:
            await h.logout()  # the person's shell back to login:
        finally:
            await h.close()


async def test_a_reboot_that_never_takes_says_so_and_logs_the_line_out(monkeypatch):
    """#451: a soft reboot that never takes effect, on a console host.

    NOTHING REBOOTS: the in-shell reboot is replaced by a no-op on this host
    object, and checked to be replaced before the call. The abandoned
    client's shell then answers every poll with output and no login prompt,
    which reads as not up, so the up phase runs out. Every poll met the same
    prompt, so the result says the reboot did not take, quoting it, and otto
    logs the line out through ``otto host <id> logout``'s path: the line is
    back at ``login:`` before the host is closed, and the message says so.
    """
    h = make_console_host("test2")
    try:
        assert (await h.run("true")).only.retcode == 0  # otto's login holds the line
        noop = AsyncMock(return_value=Result(status=Status.Success))
        monkeypatch.setattr(h, "_soft_reboot", noop)  # never the real reboot
        assert h._soft_reboot is noop
        result = await h.reboot(wait=True, timeout=30, down_timeout=15, poll_interval=1)
        noop.assert_awaited_once()
        assert result.status is Status.Failed, result
        msg = result.msg or ""
        assert "was still at a logged-in shell 30s after the reboot" in msg, msg
        assert "(last seen: '" in msg, msg
        # The quote is the shell's prompt (its login may read *** on a bed
        # whose password is the login name: the quote scrubs the password).
        assert "@test2:~$ ')" in msg, msg
        assert "the reboot likely did not take" in msg, msg
        # The logout's own outcome: it found the shell and restored the prompt.
        restored = f"console {_OPTS.server}:{_OPTS.port} reset: login prompt restored"
        assert msg.endswith(f"; otto logged the console line out (test2: {restored})"), msg
        answer = await console_answer(_OPTS.server, _OPTS.port)
        assert ends_at_login(answer), f"the reboot left the line logged in: {answer!r}"
    finally:
        await h.close()


@pytest.mark.parametrize("host1", ["console"], indirect=True)
async def test_logout_on_close_restores_the_login_prompt(host1):
    assert (await host1.run("true")).only.retcode == 0
    await host1.close()
    answer = await console_answer(_OPTS.server, _OPTS.port)
    assert ends_at_login(answer), answer


async def test_wrong_password_fails_loud():
    h = make_console_host("test2", creds=[Cred(login=_OTHER.login, password="wrong")])
    try:
        with pytest.raises(ConsoleError, match=f"login refused for '{_OTHER.login}'"):
            await h.run("true")
    finally:
        await h.close()


async def test_busy_console_fails_loud():
    cred = server_cred(_OPTS.server)
    h = make_console_host("test2")
    try:
        async with raw_console(_SERVER["ip"], cred.login, cred.password, _OPTS.port):
            with pytest.raises(ConsoleError, match="busy"):
                await h.run("true")
    finally:
        await h.close()


@pytest.mark.parametrize("host1", ["console"], indirect=True)
async def test_console_exec_runs_on_the_single_session(host1):
    """exec is a special case of run on a console: output and exit code, call after call."""
    first = await host1.exec("id -un")
    assert (first.retcode, first.value) == (0, _CONSOLE_LOGIN.login), first
    second = await host1.exec("sh -c 'exit 3'")
    assert second.retcode == 3, second
    assert (await host1.run("echo still-here")).only.value == "still-here"
    with pytest.raises(ConsoleError, match=r"single-client.*named sessions"):
        await host1.open_session("aux")
