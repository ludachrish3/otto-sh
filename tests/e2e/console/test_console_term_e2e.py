"""The ``console`` term against test2's serial getty, served by ser2net on test1.

Login and run over both dial modes, the ``shell`` transfer on the one
session, logout on close, and the failures a console must raise loudly
(already logged in, a refused login, a busy line), and ``exec`` on the
one session with a named session still refused.

Every test releases the line and leaves it at its login prompt; the
``test2_console`` fixture (``conftest.py``) checks that after each test and,
when a test broke it, restores the prompt and fails the test that broke it.
"""

import os
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from otto.host.errors import ConsoleError
from otto.host.login_proxy import Cred
from tests._fixtures.labdata import host_data, host_data_console, make_console_host
from tests.e2e.console._raw import (
    console_answer,
    ends_at_login,
    login_and_abandon,
    raw_console,
    server_cred,
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
    assert res.value.strip() == _CONSOLE_LOGIN.login


async def test_direct_dial():
    h = make_console_host("test2", console_options=replace(_OPTS, dial="direct"))
    try:
        assert (await h.run("hostname")).only.value.strip() == "test2"
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
        assert _OTHER.password not in str(ei.value), "the refusal quotes the password"
    finally:
        await h.logout()  # the abandoned shell back to login:
        await h.close()


@pytest.mark.parametrize("host1", ["console"], indirect=True)
async def test_logout_on_close_restores_the_login_prompt(host1):
    assert (await host1.run("true")).only.retcode == 0
    await host1.close()
    answer = await console_answer(_OPTS.server, _OPTS.port)
    assert ends_at_login(answer), answer


async def test_wrong_password_fails_loud():
    h = make_console_host("test2", creds=[Cred(_OTHER.login, "wrong")])
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
    assert (first.retcode, first.value.strip()) == (0, _CONSOLE_LOGIN.login), first
    second = await host1.exec("sh -c 'exit 3'")
    assert second.retcode == 3, second
    assert (await host1.run("echo still-here")).only.value.strip() == "still-here"
    with pytest.raises(ConsoleError, match=r"single-client.*named sessions"):
        await host1.open_session("aux")
