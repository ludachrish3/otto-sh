"""Unit tests for host privilege elevation (sudo / su / as_user)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host.login_proxy import Cred, register_login_proxy
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
async def test_perform_switch_builds_command_and_returns_target():
    from otto.host.login_proxy import perform_switch

    sent = []

    class _Io:
        async def send(self, text, log=LogMode.NORMAL):
            sent.append((text, log))

        async def expect(self, pat, timeout=10.0):
            return "Password:"

    applied = await perform_switch(
        _Io(), [Cred(login="root", password="rootpw")], "root", None, "", "h"
    )
    assert applied[-1].login == "root"
    assert ("su root\n", LogMode.NORMAL) in sent
    assert ("rootpw\n", LogMode.NEVER) in sent


@pytest.mark.asyncio
async def test_perform_switch_no_user_means_root_no_quote():
    from otto.host.login_proxy import perform_switch

    sent = []

    class _Io:
        async def send(self, text, log=LogMode.NORMAL):
            sent.append(text)

        async def expect(self, pat, timeout=10.0):
            return "Password:"

    applied = await perform_switch(_Io(), [], "", None, "", "h")
    assert (applied[-1].login or "root") == "root"
    assert "su\n" in sent  # bare `su`, no username, no password sent


@pytest.mark.asyncio
async def test_switch_user_records_current_user():
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds=[Cred(login="admin", password="secret"), Cred(login="root", password="rootpw")],
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
        creds=[Cred(login="admin", password="secret"), Cred(login="root", password="rootpw")],
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
        ip="10.0.0.1",
        element="box",
        creds=[Cred(login="admin", password="secret")],
        user="admin",
        log=LogMode.QUIET,
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
        ip="10.0.0.1",
        element="box",
        creds=[Cred(login="admin", password="secret")],
        user="admin",
        log=LogMode.QUIET,
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
        creds=[Cred(login="admin", password="secret"), Cred(login="root", password="rootpw")],
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
        ip="10.0.0.1",
        element="box",
        creds=[Cred(login="admin", password="secret")],
        user="admin",
        log=LogMode.NORMAL,
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
        creds=[Cred(login="admin", password="secret"), Cred(login="root", password="rootpw")],
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
        creds=[Cred(login="admin", password="secret"), Cred(login="root", password="rootpw")],
        user="admin",
        log=LogMode.NORMAL,
    )

    # Mock at the transport layer: give the SessionManager a live-looking
    # ShellSession so _ensure_session's fast-path fires and no real connect
    # is attempted. send/expect on the transport are AsyncMocks.
    mock_transport = MagicMock(spec=ShellSession)
    mock_transport.alive = True
    mock_transport.current_user = "admin"
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
        creds=[Cred(login="admin", password="secret")],
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


# ---------------------------------------------------------------------------
# Task 6: switch_user/as_user routed through the login-proxy engine
# ---------------------------------------------------------------------------

_MULTI_HOP_CREDS = [
    Cred(login="root", password="rootpw"),
    Cred(login="admin", password="adminpw", via="root"),
    Cred(login="mysql", password="mysqlpw", via="admin"),
]


@pytest.mark.asyncio
async def test_as_user_multi_hop_undoes_in_reverse():
    """as_user to a cred reached via a chain (root -> admin -> mysql) applies

    both hops on entry and undoes both (2 exits) on exit, in reverse order.
    """
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1", element="box", creds=_MULTI_HOP_CREDS, user="root", log=LogMode.QUIET
    )
    mgr = _mock_session_mgr()
    mgr.current_user = "root"
    host._session_mgr = mgr

    async with host.as_user("mysql"):
        sent_inside = [c.args[0] for c in mgr.send.await_args_list]
        assert "su admin\n" in sent_inside
        assert "su mysql\n" in sent_inside
        assert sent_inside.index("su admin\n") < sent_inside.index("su mysql\n")

    sent = [c.args[0] for c in mgr.send.await_args_list]
    assert sent.count("exit\n") == 2  # one exit per hop, undone in reverse

    set_user_calls = [c.args[0] for c in mgr._set_current_user.call_args_list]
    assert set_user_calls == ["mysql", "root"]  # entered as mysql, restored to root


@pytest.mark.asyncio
async def test_switch_user_from_via_user_runs_only_final_hop():
    """switch_user to a proxied cred, already logged in as its `via` user,

    applies only the final hop — no redundant re-switch to the via account.
    """
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1", element="box", creds=_MULTI_HOP_CREDS, user="root", log=LogMode.QUIET
    )
    mgr = _mock_session_mgr()
    mgr.current_user = "admin"  # already at mysql's `via` user
    host._session_mgr = mgr

    await host.switch_user("mysql")

    sent = [c.args[0] for c in mgr.send.await_args_list]
    assert sent == ["su mysql\n", "mysqlpw\n"]  # no "su admin" hop re-run
    mgr._set_current_user.assert_called_once_with("mysql")


@pytest.mark.asyncio
async def test_host_session_switch_user_on_proxied_cred_stamps_current_user():
    """HostSession.switch_user on a proxied cred (reached via another login)

    resolves the chain and stamps current_user with the final hop's login.
    """
    from unittest.mock import AsyncMock, MagicMock

    from otto.host.session import HostSession, ShellSession

    shell = MagicMock(spec=ShellSession)
    shell.current_user = "admin"  # already at mysql's `via` user
    shell.send = AsyncMock()
    shell.expect = AsyncMock(return_value="Password:")
    hs = HostSession(
        "n",
        shell,
        lambda *_: None,
        lambda *_: None,
        lambda _: None,
        creds=_MULTI_HOP_CREDS,
        host_id="n",
    )

    await hs.switch_user("mysql")

    assert hs.current_user == "mysql"
    sent = [c.args[0] for c in shell.send.await_args_list]
    assert sent == ["su mysql\n", "mysqlpw\n"]  # only the final hop ran


@pytest.mark.asyncio
async def test_sudo_password_reflects_current_user_after_switch():
    """Regression (Task 4 review fold-in): _sudo_password must key off the

    CURRENT user's password after switch_user, not the login user's — proven
    through the real SessionManager/switch_user path (not a stub that skips
    the current_user bookkeeping).
    """
    from unittest.mock import AsyncMock, MagicMock

    from otto.host.session import ShellSession
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds=[Cred(login="admin", password="adminpw"), Cred(login="root", password="rootpw")],
        user="admin",
        log=LogMode.NORMAL,
    )

    mock_transport = MagicMock(spec=ShellSession)
    mock_transport.alive = True
    mock_transport.current_user = "admin"
    mock_transport.send = AsyncMock()
    mock_transport.expect = AsyncMock(return_value="Password:")
    host._session_mgr._session = mock_transport

    # Before any switch: sudo uses the login user's (admin's) password.
    assert host._sudo_password() == "adminpw"

    await host.switch_user("root")

    # After switching: current_user is root, and sudo must use ROOT's
    # password — not admin's (the login user's) — for the *current* user.
    assert host.current_user == "root"
    assert host._sudo_password() == "rootpw"


# Chain used by the undo-observability tests: root (direct) -> admin -> mysql,
# where BOTH hops use a fake proxy WITH a custom undo, so run_undo drives the
# custom-undo branch (which reads ctx.via) instead of the default `exit` branch
# (which never reads via). This is what makes the reverse-undo `via` ordering
# — the trickiest line in the task — actually observable.
def _fake_undo_chain(proxy_name: str) -> list[Cred]:
    return [
        Cred(login="root", password="rootpw"),
        Cred(login="admin", password="adminpw", proxy=proxy_name, via="root"),
        Cred(login="mysql", password="mysqlpw", proxy=proxy_name, via="admin"),
    ]


@pytest.mark.asyncio
async def test_as_user_undo_via_ordering_observable_host():
    """Host path (PosixPrivilege.as_user): a proxy with a CUSTOM undo lets us

    observe the ``via`` the undo loop passes for each hop. Undo runs in reverse
    (innermost first), so undo #1 (mysql) must see via=admin and undo #2 (admin)
    must see via=root — with the FULL via cred (password intact), not a bare
    ``Cred(login=...)``. Guards the ``applied[-i-2]`` reverse index + the
    full-cred-lookup fix; with the wrong index or a bare cred this fails.
    """
    from otto.host.unix_host import UnixHost

    captured: list[tuple[str, str, str | None]] = []

    async def fake_fn(io, ctx):
        await io.send(f"become {ctx.target.login}\n")

    async def fake_undo(io, ctx):
        captured.append((ctx.target.login, ctx.via.login, ctx.via.password))
        await io.send("leave\n")

    register_login_proxy("task6-fake-undo-host", fake_fn, undo=fake_undo, overwrite=True)

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds=_fake_undo_chain("task6-fake-undo-host"),
        user="root",
        log=LogMode.QUIET,
    )
    mgr = _mock_session_mgr()
    mgr.current_user = "root"
    host._session_mgr = mgr

    async with host.as_user("mysql"):
        assert captured == []  # nothing undone until the block exits

    # (target, via.login, via.password), in the order run_undo fired them.
    assert captured == [
        ("mysql", "admin", "adminpw"),  # undo #1: reverse-innermost, via = admin cred
        ("admin", "root", "rootpw"),  # undo #2: via = root cred (the prior user)
    ]
