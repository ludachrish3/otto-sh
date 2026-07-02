"""Unit tests for host privilege elevation (sudo / su / as_user)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status


def _mock_session_mgr():
    """AsyncMock session-mgr whose send/expect are awaitable but whose
    current_user bookkeeping is synchronous (no un-awaited coroutines)."""
    mgr = AsyncMock()
    mgr._set_current_user = MagicMock()
    mgr.current_user = ""
    return mgr


@pytest.mark.asyncio
async def test_perform_su_builds_command_and_returns_target():
    from otto.host.privilege import _perform_su

    sent = []

    async def send(text, log=LogMode.NORMAL):
        sent.append((text, log))

    async def expect(pat, timeout=10.0):
        return "Password:"

    target = await _perform_su(send, expect, "root", None, lambda u: "rootpw")
    assert target == "root"
    assert ("su root\n", LogMode.NORMAL) in sent
    assert ("rootpw\n", LogMode.NEVER) in sent


@pytest.mark.asyncio
async def test_perform_su_no_user_means_root_no_quote():
    from otto.host.privilege import _perform_su

    sent = []

    async def send(text, log=LogMode.NORMAL):
        sent.append(text)

    async def expect(pat, timeout=10.0):
        return "Password:"

    target = await _perform_su(send, expect, "", None, lambda u: None)
    assert target == "root"
    assert "su\n" in sent  # bare `su`, no username, no password sent


@pytest.mark.asyncio
async def test_switch_user_records_current_user():
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds={"admin": "secret", "root": "rootpw"},
        user="admin",
        log=LogMode.QUIET,
    )
    host._session_mgr = _mock_session_mgr()
    await host.switch_user("root")
    host._session_mgr._set_current_user.assert_called_once_with("root")


@pytest.mark.asyncio
async def test_as_user_restores_previous_user():
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds={"admin": "secret", "root": "rootpw"},
        user="admin",
        log=LogMode.QUIET,
    )
    mgr = _mock_session_mgr()
    mgr.current_user = "admin"
    host._session_mgr = mgr
    async with host.as_user("root"):
        pass
    calls = [c.args[0] for c in mgr._set_current_user.call_args_list]
    assert calls == ["root", "admin"]  # entered as root, restored to admin


@pytest.mark.asyncio
async def test_embedded_run_sudo_raises():
    from otto.host.embedded_host import ZephyrHost

    host = ZephyrHost(ip="192.0.2.1", element="sprout", log=LogMode.QUIET)
    with pytest.raises(NotImplementedError, match="sudo"):
        await host.run("ls", sudo=True)


@pytest.mark.asyncio
async def test_run_without_sudo_is_unchanged():
    from otto.host.local_host import LocalHost

    host = LocalHost()
    captured = {}

    async def fake_run_one(cmd, expects=None, timeout=None, log=LogMode.NORMAL):
        captured["cmd"] = cmd
        return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)

    with patch.object(host, "_run_one", new=fake_run_one):
        await host.run("id")
    assert captured["cmd"] == "id"  # no wrapping


def _capture_run_one(host):
    captured = {}

    async def fake_run_one(cmd, expects=None, timeout=None, log=LogMode.NORMAL):
        captured["cmd"] = cmd
        captured["expects"] = expects
        return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)

    return captured, fake_run_one


@pytest.mark.asyncio
async def test_unix_run_sudo_wraps_and_injects_password_expect():
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1", element="box", creds={"admin": "secret"}, user="admin", log=LogMode.QUIET
    )
    captured, fake = _capture_run_one(host)
    with patch.object(host, "_run_one", new=fake):
        await host.run("apt update", sudo=True)
    assert captured["cmd"] == "sudo -S -p 'otto-sudo:' apt update"
    assert ("otto-sudo:", "secret\n") in captured["expects"]


@pytest.mark.asyncio
async def test_localhost_sudo_wraps_without_password_expect():
    from otto.host.local_host import LocalHost

    host = LocalHost()
    captured, fake = _capture_run_one(host)
    with patch.object(host, "_run_one", new=fake):
        await host.run("id", sudo=True)
    assert captured["cmd"] == "sudo -S -p 'otto-sudo:' id"
    assert captured["expects"] == []  # passwordless: no injected expect


@pytest.mark.asyncio
async def test_sudo_preserves_caller_expects():
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1", element="box", creds={"admin": "secret"}, user="admin", log=LogMode.QUIET
    )
    captured, fake = _capture_run_one(host)
    with patch.object(host, "_run_one", new=fake):
        await host.run("rm -i x", expects=("remove.*\\?", "y\n"), sudo=True)
    # password expect first, caller's expect preserved after
    assert captured["expects"][0] == ("otto-sudo:", "secret\n")
    assert ("remove.*\\?", "y\n") in captured["expects"]


@pytest.mark.asyncio
async def test_switch_user_sends_su_and_password():
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds={"admin": "secret", "root": "rootpw"},
        user="admin",
        log=LogMode.NORMAL,  # NORMAL host so the su exchange's per-command modes pass through
    )
    host._session_mgr = _mock_session_mgr()
    await host.switch_user("root")
    host._session_mgr.send.assert_any_await("su root\n", log=LogMode.NORMAL)
    host._session_mgr.send.assert_any_await("rootpw\n", log=LogMode.NEVER)


@pytest.mark.asyncio
async def test_switch_user_default_is_root_no_user_arg():
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1", element="box", creds={"admin": "secret"}, user="admin", log=LogMode.NORMAL
    )
    host._session_mgr = _mock_session_mgr()
    host._session_mgr.expect.return_value = "Password:"
    await host.switch_user()  # default root, no creds entry for root → no password sent
    host._session_mgr.send.assert_any_await("su\n", log=LogMode.NORMAL)


@pytest.mark.asyncio
async def test_embedded_switch_user_raises():
    from otto.host.embedded_host import ZephyrHost

    host = ZephyrHost(ip="192.0.2.1", element="sprout", log=LogMode.QUIET)
    with pytest.raises(NotImplementedError, match="su"):
        await host.switch_user("root")


@pytest.mark.asyncio
async def test_as_user_switches_then_exits():
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds={"admin": "secret", "root": "rootpw"},
        user="admin",
        log=LogMode.QUIET,
    )
    host._session_mgr = _mock_session_mgr()
    async with host.as_user("root"):
        pass
    sent = [c.args[0] for c in host._session_mgr.send.await_args_list]
    assert "su root\n" in sent  # entered
    assert "exit\n" in sent  # returned
    assert sent.index("su root\n") < sent.index("exit\n")


@pytest.mark.asyncio
async def test_embedded_as_user_raises():
    from otto.host.embedded_host import ZephyrHost

    host = ZephyrHost(ip="192.0.2.1", element="sprout", log=LogMode.QUIET)
    with pytest.raises(NotImplementedError, match=r"as_user|su"):
        async with host.as_user("root"):
            pass


@pytest.mark.asyncio
async def test_switch_user_password_not_logged(caplog):
    """Regression: su password must NOT appear in logs (transport-level seam).

    Mocks at the ShellSession (transport) level so SessionManager._log_command
    executes normally — proves the QUIET guard in the actual code path, not
    a mock that skips the logging seam entirely.
    """
    import logging

    from otto.host.session import ShellSession
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds={"admin": "secret", "root": "rootpw"},
        user="admin",
        log=LogMode.NORMAL,
    )

    # Mock at the transport layer: give the SessionManager a live-looking
    # ShellSession so _ensure_session's fast-path fires and no real connect
    # is attempted. send/expect on the transport are AsyncMocks.
    mock_transport = MagicMock(spec=ShellSession)
    mock_transport.alive = True
    mock_transport.send = AsyncMock()
    mock_transport.expect = AsyncMock(return_value="Password:")
    host._session_mgr._session = mock_transport

    with caplog.at_level(logging.INFO, logger="otto"):
        await host.switch_user("root")

    # The su command line must be logged (proves suppression is surgical).
    assert "su root" in caplog.text
    # The password must NOT appear in the logs.
    assert "rootpw" not in caplog.text


@pytest.mark.asyncio
async def test_switch_user_quotes_special_char_username():
    """switch_user shlex-quotes usernames that contain shell-special characters."""
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds={"admin": "secret"},
        user="admin",
        log=LogMode.QUIET,
    )
    # Replace the session manager with a mock to capture what was sent.
    host._session_mgr = _mock_session_mgr()

    await host.switch_user("od d")  # space in username — must be shell-quoted

    # The first send must be the shlex-quoted su command.
    first_call = host._session_mgr.send.await_args_list[0]
    assert first_call.args[0] == "su 'od d'\n"


@pytest.mark.asyncio
async def test_embedded_current_user_is_empty_loginless():
    from otto.host.embedded_host import ZephyrHost

    host = ZephyrHost(ip="192.0.2.1", element="sprout", log=LogMode.QUIET)
    assert host.current_user == ""  # loginless embedded shell
