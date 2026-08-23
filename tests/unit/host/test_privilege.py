"""Unit tests for host privilege elevation (sudo / su / as_user)."""

import ast
import asyncio
import inspect
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host import host as host_module
from otto.host.errors import UnsupportedOnUserlandError
from otto.host.login_proxy import Cred, register_login_proxy
from otto.host.options import UserlandOptions
from otto.host.userland import Userland
from otto.logger.mode import LogMode
from otto.models.options import UserlandOptionsSpec
from otto.result import CommandResult
from otto.utils import Status


def _without_resync(sent: list[str]) -> list[str]:
    """Drop the login-proxy engine's post-transition echo-proof `$?`-digit
    resync probes.

    ``run_proxy``/``run_undo`` now end every hop with a resync (see
    ``otto.host.login_proxy._resync_shell``) — filter its noise out before
    asserting on the exact send sequence a test cares about.

    Substring rather than prefix match: on a host with history suppression on
    (the ``UnixHost`` default) the probe line is preceded by a ``HISTFILE=…``
    statement, so the echo is no longer first on it.
    """
    return [s for s in sent if 'echo "__OTTO_' not in s]


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

    host = ZephyrHost(ip="192.0.2.1", element="zephyr37_fat", log=LogMode.QUIET)
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


async def _every_probe_succeeds(cmd: str, **_kwargs: object) -> CommandResult:
    """An ``exec`` stand-in on which every capability probe answers yes.

    A bare :class:`~otto.host.unix_host.UnixHost` declares nothing, so its
    first ``run(sudo=True)`` genuinely probes — over ``exec``, which is a
    different channel from the persistent session ``run`` uses, so patching
    ``_run_one`` alone leaves the probes reaching for a real connection.

    Answering yes to everything rather than to a named command: which
    spelling the elevation probe issues belongs to
    ``tests/unit/host/test_userland.py``, and a test that repeated it here
    would break on a rewording that changed nothing. Yes-to-everything lands
    on ``sudo`` because that is the first mechanism tried, which is the same
    property those tests pin directly.
    """
    return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)


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
    with (
        patch.object(host, "_run_one", new=fake),
        patch.object(host, "exec", new=_every_probe_succeeds),
    ):
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
    with (
        patch.object(host, "_run_one", new=fake),
        patch.object(host, "exec", new=_every_probe_succeeds),
    ):
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

    host = ZephyrHost(ip="192.0.2.1", element="zephyr37_fat", log=LogMode.QUIET)
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

    host = ZephyrHost(ip="192.0.2.1", element="zephyr37_fat", log=LogMode.QUIET)
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

    host = ZephyrHost(ip="192.0.2.1", element="zephyr37_fat", log=LogMode.QUIET)
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
    assert _without_resync(sent) == ["su mysql\n", "mysqlpw\n"]  # no "su admin" hop re-run
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
    assert _without_resync(sent) == ["su mysql\n", "mysqlpw\n"]  # only the final hop ran


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


@pytest.mark.asyncio
async def test_as_user_undo_survives_cancellation():
    """A cancellation landing while the undo chain runs must not strand the
    session as the switched user: every hop still unwinds and current_user
    is restored (the undo is a shielded compensating action)."""
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1", element="box", creds=_MULTI_HOP_CREDS, user="root", log=LogMode.QUIET
    )
    mgr = _mock_session_mgr()
    mgr.current_user = "root"

    async def _yielding_send(*_a, **_k) -> None:
        await asyncio.sleep(0)  # a real suspension per send, so a cancel CAN land mid-undo

    mgr.send.side_effect = _yielding_send
    host._session_mgr = mgr

    inside = asyncio.Event()
    release = asyncio.Event()

    async def body() -> None:
        async with host.as_user("mysql"):
            inside.set()
            await release.wait()

    task = asyncio.ensure_future(body())
    await inside.wait()
    task.cancel()  # lands at release.wait(); the finally-undo starts
    await asyncio.sleep(0)
    task.cancel()  # second cancel, mid-undo: must be held by compensate
    with pytest.raises(asyncio.CancelledError):
        await task

    sent = [c.args[0] for c in mgr.send.await_args_list]
    assert sent.count("exit\n") == 2, "the undo chain was torn mid-unwind"
    set_user_calls = [c.args[0] for c in mgr._set_current_user.call_args_list]
    assert set_user_calls == ["mysql", "root"]  # entered as mysql, restored to root


# ---------------------------------------------------------------------------
# Elevation is a resolved strategy, not an assumption
#
# `_elevate` used to build `sudo -S -p ...` for every posix host. BusyBox
# systems frequently ship `su` and no sudo at all, so the mechanism now comes
# from `Userland.elevation` — see
# `docs/superpowers/specs/2026-08-11-busybox-host-support-design.md`.
#
# THE BLAST RADIUS IS EVERY UNIX HOST, not only the BusyBox ones, which is why
# the sudo arm is pinned as an exact string-and-expects tuple rather than by
# "contains sudo": the whole lab elevates through this one function.
# ---------------------------------------------------------------------------

# Today's construction, captured by running the PRE-CHANGE code before any of
# this was written, so these are a RECORD of what shipped rather than a
# restatement of what the new branch happens to build. Elevating "apt update"
# on a UnixHost with creds admin/secret and user admin produced exactly the
# command and expects below; elevating "id" on a passwordless, credless
# LocalHost produced "sudo -S -p 'otto-sudo:' id" with an empty expects list.
_TODAYS_SUDO_CMD = "sudo -S -p 'otto-sudo:' apt update"
_TODAYS_SUDO_EXPECTS = [("otto-sudo:", "secret\n")]

# Two DIFFERENT passwords on purpose. sudo authenticates as the current user
# (admin) and `su` authenticates as the target (root), so a `su` arm that
# reused `_sudo_password()` would send "secret" and these tests would catch it;
# with one shared password they could not tell the two sources apart.
_ADMIN_AND_ROOT = [Cred(login="admin", password="secret"), Cred(login="root", password="rootpw")]


async def _answers_no(cmd: str, **_kwargs: object) -> CommandResult:
    """A ``Userland`` runner on which every probe fails.

    Only ``elevation`` is declared by the helpers below, so the other four
    capabilities really are probed and really do reach this. Answering "no" to
    everything is what makes the declared value the ONLY thing that can decide
    what ``_elevate`` builds: a ``Userland`` that ignored the declaration and
    probed anyway would find neither sudo nor su here and resolve ``"none"``,
    so the sudo and su tests would fail rather than agree with the declaration
    by accident.
    """
    return CommandResult(status=Status.Error, value="", command=cmd, retcode=127)


def _declared_userland(elevation: str) -> Userland:
    """A ``Userland`` declaring *elevation*, deliberately left unresolved."""
    return Userland(UserlandOptions(elevation=elevation), _answers_no)


def _host_wired_to(userland: Userland | None, creds: list[Cred] | None = None):
    """A ``UnixHost`` whose ``_userland()`` hook returns *userland*.

    Production hosts now build their own from ``userland_options``, but that
    route can only produce a resolver in one state — declared, and resolved
    when someone resolves it. These tests need the states production cannot
    hand them directly: ``None``, and a resolver deliberately left unresolved.
    Overriding the hook is how they get one. It has to be a subclass rather
    than an assignment: the hosts are ``@dataclass(slots=True)``, so there is
    nowhere to hang one on an instance.
    """
    from otto.host.unix_host import UnixHost

    class _Host(UnixHost):
        def _userland(self) -> Userland | None:
            return userland

    return _Host(
        ip="10.0.0.1",
        element="box",
        creds=_ADMIN_AND_ROOT if creds is None else creds,
        user="admin",
        log=LogMode.QUIET,
    )


async def _host_with_userland(elevation: str, creds: list[Cred] | None = None):
    """A host carrying a RESOLVED userland that declares *elevation*."""
    userland = _declared_userland(elevation)
    await userland.resolve()
    return _host_wired_to(userland, creds)


def test_a_host_with_no_userland_builds_todays_exact_sudo_command():
    """The no-resolver default, chosen rather than inherited.

    Not the state of the whole lab any more —
    :class:`~otto.host.unix_host.UnixHost` builds a resolver per host, so its
    answer is measured. ``None`` is now the state of the families that reach
    otto's own machine, :class:`~otto.host.local_host.LocalHost` and
    :class:`~otto.host.docker_host.DockerContainerHost`, which declare no
    ``userland_options`` and do not override the hook. It has to be
    indistinguishable from the pre-change behaviour there, and the direction
    is not symmetric: refusing (or picking ``su``) would break privileged
    operations on those hosts in exchange for nothing, because a host with no
    resolver has told us nothing that contradicts sudo.

    So the answer is sudo, byte for byte. The literals are the pre-change
    output captured by running the old code, which is what makes this a
    regression pin rather than a mirror of the new implementation. The
    password-carrying half needs ``creds``, which the two credless families do
    not have, so it runs on a host wired explicitly to ``None`` — that is the
    same branch of ``_elevate``, reached the only way a creds-carrying host
    can still reach it.
    """
    from otto.host.docker_host import DockerContainerHost
    from otto.host.local_host import LocalHost
    from otto.host.privilege import PosixPrivilege
    from otto.host.unix_host import UnixHost

    unix = _host_wired_to(None, creds=[Cred(login="admin", password="secret")])
    # The premise: this really is the no-resolver path. Without it the tuple
    # below would still pass on a host that had somehow acquired one.
    assert unix._userland() is None
    assert unix._elevate("apt update") == (_TODAYS_SUDO_CMD, _TODAYS_SUDO_EXPECTS)

    # Passwordless, and a host family with no ``creds`` field at all —
    # a production instance of the state above.
    assert LocalHost()._userland() is None
    assert LocalHost()._elevate("id") == ("sudo -S -p 'otto-sudo:' id", [])

    # Docker's is a class-level check because the class needs a live parent
    # host to instantiate. Stated as a three-way split rather than two
    # separate facts: exactly one of the three posix-privilege families
    # overrides the hook, and a second one acquiring a resolver by accident
    # would change what elevation it reaches for without any test naming it.
    assert LocalHost._userland is PosixPrivilege._userland
    assert DockerContainerHost._userland is PosixPrivilege._userland
    assert UnixHost._userland is not PosixPrivilege._userland


@pytest.mark.asyncio
async def test_a_resolved_sudo_userland_builds_the_identical_command():
    """A host that resolved ``sudo`` gets exactly what it got before.

    Same literals as the un-wired test above, deliberately: consulting the
    resolver must not perturb the string by so much as a space, since the
    ``-p`` prompt is matched by the expect that types the password and the
    command is appended as sudo's trailing argument list (a shape
    ``otto.host.daemon`` explicitly depends on).
    """
    host = await _host_with_userland("sudo")
    assert host._elevate("apt update") == (_TODAYS_SUDO_CMD, _TODAYS_SUDO_EXPECTS)


@pytest.mark.asyncio
async def test_a_host_without_sudo_elevates_with_su():
    """BusyBox has no sudo at all; ``sudo: not found`` is not an answer."""
    host = await _host_with_userland("su")
    assert host._elevate("id -u") == ("su -c 'id -u'", [("[Pp]assword:", "rootpw\n")])


@pytest.mark.asyncio
async def test_su_quotes_the_command_into_one_word():
    """``su -c`` takes ONE argument, so the command has to survive as one word.

    Unlike sudo, which takes an argv tail. A multi-word command spliced in
    unquoted hands ``su`` its first word and leaves the rest to the CALLING
    shell — for a redirect or a ``;`` that is a different command running at a
    different privilege, not an error anyone would notice.
    """
    host = await _host_with_userland("su")
    wrapped, _expects = host._elevate("sh -c 'echo hi' > /root/out; id")
    assert wrapped == """su -c 'sh -c '"'"'echo hi'"'"' > /root/out; id'"""


@pytest.mark.asyncio
async def test_su_sends_no_password_when_no_root_cred_is_declared():
    """Passwordless ``su`` is a real configuration and must not invent a prompt.

    Also the discriminator for WHICH password ``su`` uses: admin's is right
    here in the cred list, so an arm that reused ``_sudo_password()`` would
    produce an expect instead of an empty list.
    """
    host = await _host_with_userland("su", [Cred(login="admin", password="secret")])
    assert host._elevate("id -u") == ("su -c 'id -u'", [])


@pytest.mark.asyncio
async def test_su_answers_the_prompt_with_the_target_creds_password_not_the_chains():
    """A chain on the root cred changes the HOPS, never whose password su asks for.

    ``perform_switch`` resolves a chain by running each hop and then answers the
    FINAL hop's prompt with that hop's own password (``_su_proxy`` reads
    ``ctx.target.password``). ``_su_password`` matches on that point and only
    that point: root's password, not the directly-loginable account's.

    ``proxy="su"`` IS THE DISCRIMINATOR, not ``via``, and the distinction is the
    whole test. ``resolve_chain``'s hop loop is ``while cred.proxy is not
    None``, so a cred carrying ``via`` alone collapses to a plain ``cred_for``
    lookup and returns root either way — this test carried exactly that fixture
    at first and could not fail under the swap it is named for. With ``proxy``
    set, ``resolve_chain`` walks to ``admin`` and a swapped implementation
    returns ``"secret"``.

    What the one-shot form cannot replay is a chain of MORE than one hop from
    the current user; this one-hop ``proxy="su"`` shape is precisely what
    ``su -c`` already does. See ``_su_password`` for what is and is not served.
    """
    host = await _host_with_userland(
        "su",
        [
            Cred(login="admin", password="secret"),
            Cred(login="root", password="rootpw", proxy="su", via="admin"),
        ],
    )
    assert host._elevate("id -u") == ("su -c 'id -u'", [("[Pp]assword:", "rootpw\n")])


@pytest.mark.asyncio
async def test_a_host_with_neither_refuses_rather_than_guessing():
    """Emitting a command that cannot work produces a confusing failure far
    from its cause; refusing names the cause at the call site."""
    host = await _host_with_userland("none")
    with pytest.raises(UnsupportedOnUserlandError, match="elevation") as excinfo:
        host._elevate("id -u")
    message = str(excinfo.value)
    assert "id -u" in message, f"the refusal does not say what was refused: {message}"
    assert host.name in message, f"the refusal does not say which host: {message}"


def _declared_elevations() -> set[str]:
    """The ``elevation`` vocabulary, read from the boundary spec that owns it.

    Derived rather than hand-listed so a fourth spelling accepted in lab data
    cannot reach ``_elevate`` without a branch — that failure would otherwise
    be a live host silently taking whichever arm the fallthrough happens to be.
    """
    annotation = UserlandOptionsSpec.model_fields["elevation"].annotation
    return {
        member
        for arg in get_args(annotation)
        for member in get_args(arg)
        if isinstance(member, str)
    }


@pytest.mark.asyncio
async def test_every_declared_elevation_is_handled_explicitly():
    """Each member of the vocabulary either builds its own command or refuses."""
    vocabulary = _declared_elevations()
    assert vocabulary == {"sudo", "su", "none"}, (
        f"the elevation vocabulary moved to {sorted(vocabulary)} — enrol the new "
        "member in `_elevate` and in the expectations below"
    )

    built: dict[str, str] = {}
    refused: set[str] = set()
    for member in sorted(vocabulary):
        host = await _host_with_userland(member)
        try:
            wrapped, _expects = host._elevate("id -u")
        except UnsupportedOnUserlandError:
            refused.add(member)
        else:
            built[member] = wrapped

    # `built` is not asserted against a list of its own: every member either
    # builds or refuses, so `built == vocabulary - refused` is already fixed by
    # the two assertions above and such a line could never fail. What is NOT
    # fixed by them is whether each member builds ITS OWN mechanism.
    assert refused == {"none"}, f"refusals are {sorted(refused)}, expected just 'none'"
    mismatched = {m: cmd for m, cmd in built.items() if cmd.split()[0] != m}
    assert not mismatched, (
        f"these elevations build a command that invokes something else: {mismatched}"
    )


def _capture_every_run_one():
    """Record EVERY command reaching ``_run_one``, not just the last one.

    ``_capture_run_one`` above keeps one command, which is enough for a single
    call and blind for a sequence: ``run()`` has two ``_apply_sudo`` sites and
    a test that only ever passes a string exercises one of them.
    """
    seen: list[str] = []

    async def fake_run_one(cmd, expects=None, timeout=None, log=LogMode.NORMAL):
        seen.append(cmd)
        return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)

    return seen, fake_run_one


@pytest.mark.parametrize(
    ("cmds", "expected"),
    [
        ("id -u", ["su -c 'id -u'"]),
        (["id -u", "id -g"], ["su -c 'id -u'", "su -c 'id -g'"]),
    ],
    ids=["single", "sequence"],
)
@pytest.mark.asyncio
async def test_running_with_sudo_resolves_the_userland_before_reading_it(cmds, expected):
    """Where resolution happens, made explicit — the answer is ``run()``.

    Every ``Userland`` capability raises when read before ``resolve()`` has
    been awaited, and ``_elevate`` is synchronous, so it cannot resolve on
    demand. Catching that and falling back to sudo would be the tempting fix
    and the worst available one: on a host with no sudo the fallback LOOKS like
    it worked, and the real failure surfaces later as ``sudo: not found``.

    So ``run()`` awaits ``_prepare_elevation()`` whenever ``sudo=True``, ahead
    of BOTH ``_apply_sudo`` call sites. The ``raises`` below states the
    premise: without it, a userland that arrived already resolved would green
    this test while proving nothing.

    BOTH SHAPES, and the sequence one is not padding. ``run()`` rewrites
    commands in two places — once for a single command and once for a list —
    and a resolution moved into the single-command arm satisfies a
    string-only test and every lexical source check, while
    ``run([...], sudo=True)`` on a wired host raises. Measured: that mutant
    was 32/32 green here before this parametrization existed.
    """
    host = _host_wired_to(_declared_userland("su"))
    with pytest.raises(RuntimeError, match="elevation read before resolve"):
        host._elevate("id -u")

    seen, fake = _capture_every_run_one()
    with patch.object(host, "_run_one", new=fake):
        await host.run(cmds, sudo=True)
    assert seen == expected


def test_every_elevation_rewrite_is_dominated_by_a_resolution():
    """...and the resolution reaches every rewrite, checked against the source.

    The behavioural guard above proves both call paths resolve; this proves no
    OTHER path skips it. A third ``_apply_sudo`` site added to a method that
    never awaits ``_prepare_elevation`` would pass that one and raise on a real
    host at the moment a privileged command was due to run.

    **Statement position, not line number.** An earlier version compared
    ``lineno``s, which made it branch-blind: an await inside the
    single-command arm is lexically above the sequence arm's rewrite, so the
    numbers checked out while the list path raised. What is required instead is
    that the await sit in a top-level statement of the enclosing function that
    comes STRICTLY BEFORE the top-level statement holding the rewrite — so a
    resolution buried in the same branch as one rewrite cannot vouch for the
    other. That is a structural proxy for dominance, not dominance itself: it
    cannot see the ``if sudo:`` condition, which is why the behavioural pair
    above exists.

    Awaits inside a nested function do not count either. A resolution defined
    early in a closure and called late is textually reassuring and executes
    whenever the closure does, which is not the same thing.
    """
    tree = ast.parse(inspect.getsource(host_module))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def _enclosing_function(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    def _statement_index(fn, node) -> int | None:
        """Index in *fn*'s own body of the top-level statement holding *node*.

        ``None`` for a node with no enclosing function at all. That case still
        fails the assertion below — a module-level rewrite has nothing that
        could resolve for it — but it must fail there and not as an
        ``AttributeError`` on ``None.body``, which reports a broken test rather
        than the real finding.
        """
        if fn is None:
            return None
        for i, stmt in enumerate(fn.body):
            if any(n is node for n in ast.walk(stmt)):
                return i
        return None

    def _calls_to(name: str, *, awaited: bool) -> list[ast.Call]:
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == name):
                continue
            if awaited and not isinstance(parents.get(node), ast.Await):
                continue
            found.append(node)
        return found

    rewrites = _calls_to("_apply_sudo", awaited=False)
    assert rewrites, (
        "no `_apply_sudo` call found in otto.host.host — this query has rotted "
        "and would pass on anything"
    )
    # ``awaited=True`` rather than mere presence: `self._prepare_elevation()`
    # without the await builds a coroutine, resolves nothing, and would satisfy
    # a check that only looked for the name — so dropping the await empties
    # this list and every rewrite lands in `undominated` below. No separate
    # "there is at least one resolution" assertion, deliberately: with
    # `rewrites` already proven non-empty, an empty `resolutions` cannot
    # produce an empty `undominated`, so such a line could never be the one
    # that fails.
    resolutions = _calls_to("_prepare_elevation", awaited=True)

    undominated = []
    for call in rewrites:
        fn = _enclosing_function(call)
        here = _statement_index(fn, call)
        dominated = False
        for res in resolutions:
            if here is None or _enclosing_function(res) is not fn:
                continue
            there = _statement_index(fn, res)
            if there is not None and there < here:
                dominated = True
                break
        if not dominated:
            undominated.append(f"{getattr(fn, 'name', '<module>')}:{call.lineno}")
    assert not undominated, (
        f"{undominated} rewrites a command through `_apply_sudo` with no awaited "
        "`_prepare_elevation` in a strictly earlier top-level statement of the "
        "same function. A resolution sharing a branch with one rewrite does not "
        "reach the other; without it the synchronous read in `_elevate` raises."
    )


# ---------------------------------------------------------------------------
# A refusal must stay a refusal
#
# `_elevate` raises rather than falling back BECAUSE a silent success on a
# privilege path looks like it worked. That argument is worth nothing if the
# caller then converts the loud failure into a quiet one, which is exactly what
# a broad `except Exception` around an elevated command does.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_is_not_swallowed_into_a_successful_reboot():
    """``reboot()`` on a host that cannot elevate must not report success.

    ``_soft_reboot`` wraps its ``run("reboot", sudo=True)`` in a broad handler
    on purpose — issuing a reboot races the connection teardown, and that
    disconnect is expected rather than a failure. But the same handler used to
    catch the elevation refusal and return ``Status.Success``, with the command
    never issued and ``wait=False`` by default meaning nothing downstream ever
    arbitrated. Measured before the fix: ``Status.Success ''``.

    A host otto never rebooted, reported as rebooted, is strictly worse than
    the ``sudo: not found`` this whole change exists to replace.
    """
    from otto.host.unix_host import UnixHost

    userland = _declared_userland("none")
    await userland.resolve()

    class _Host(UnixHost):
        def _userland(self) -> Userland | None:
            return userland

    host = _Host(
        ip="10.0.0.1",
        element="box",
        creds=[Cred(login="admin", password="secret")],
        user="admin",
        log=LogMode.QUIET,
    )
    issued: list[str] = []

    async def fake_run_one(cmd, expects=None, timeout=None, log=LogMode.NORMAL):
        issued.append(cmd)
        return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)

    with (
        patch.object(host, "_run_one", new=fake_run_one),
        pytest.raises(UnsupportedOnUserlandError, match="elevation"),
    ):
        await host.reboot()
    assert issued == [], f"a host that cannot elevate still issued {issued}"


_CATCHES_THE_REFUSAL = frozenset(
    {
        # `UnsupportedOnUserlandError` is `(OttoError, RuntimeError)`, so each of
        # these names a handler that would swallow it. `OSError` and
        # `ConnectionError` deliberately do not appear: `otto.link`'s `_root_run`
        # catches exactly those, which is why it is already correct.
        "BaseException",
        "Exception",
        "RuntimeError",
        "OttoError",
        "UnsupportedOnUserlandError",
    }
)


def _elevated_call_sites(tree: ast.Module) -> list[ast.Call]:
    """Every call passing a non-``False`` ``sudo=`` keyword."""
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "sudo":
                continue
            declined = isinstance(keyword.value, ast.Constant) and keyword.value.value in (
                False,
                None,
            )
            if not declined:
                sites.append(node)
    return sites


def test_no_elevated_command_sits_inside_a_handler_that_swallows_the_refusal():
    """Swept from source, so a new elevated call site is enrolled by existing.

    The behavioural test above pins the one site that was wrong. This is the
    completeness half: `_elevate` refuses loudly, and that is only worth
    anything while every caller lets the refusal past. A broad
    ``except Exception`` around ``run(..., sudo=True)`` that returns a Result
    turns "otto knew this could not work" into "it worked", which is the
    failure mode the refusal was chosen to avoid.

    A broad handler is still allowed — the reboot disconnect race needs one —
    provided an EARLIER handler on the same ``try`` re-raises the refusal.
    ``contextlib.suppress`` with a broad argument is checked too and has no
    such escape: there is nowhere in a ``suppress`` block to re-raise from, so
    the only fix there is to narrow the argument. It is worth checking because
    it is this repo's idiomatic swallow — 24 broad uses under ``src/otto`` —
    and it leaves no ``ast.Try`` node for a handler-only sweep to find.

    **WHAT THIS DOES NOT SEE, so that it is not read as complete.** The sweep
    is intraprocedural and keys on the ``sudo=`` literal. A helper that holds
    the literal and is called from elsewhere hides its callers' handlers from
    this query — ``otto.link.manage._root_run`` is exactly that shape, with 19
    call sites none of which are examined here. Those were checked by hand
    against this guard's own rule during review and none swallows the refusal;
    ``_root_run`` itself catches only ``(OSError, ConnectionError)``, which is
    why it does not appear below. Re-check by hand when a new indirection like
    it appears, or teach this sweep to follow one hop.
    """
    from tests._fixtures.paths import PROJECT_ROOT

    src = PROJECT_ROOT / "src" / "otto"
    offenders: list[str] = []
    files_with_sites: set[str] = set()

    def _broadly_suppresses(node: ast.AST) -> bool:
        """True for ``with suppress(Exception)``-style context managers."""
        items = getattr(node, "items", [])
        for item in items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "suppress":
                continue
            caught = {n.id for arg in call.args for n in ast.walk(arg) if isinstance(n, ast.Name)}
            if caught & _CATCHES_THE_REFUSAL:
                return True
        return False

    for py in sorted(src.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        sites = _elevated_call_sites(tree)
        if not sites:
            continue
        rel = str(py.relative_to(src))
        files_with_sites.add(rel)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for site in sites:
            node = site
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.With, ast.AsyncWith)) and _broadly_suppresses(node):
                    offenders.append(f"{rel}:{site.lineno} (suppressed at line {node.lineno})")
                    continue
                if not isinstance(node, ast.Try):
                    continue
                guarded = False
                for handler in node.handlers:
                    # A bare `except:` names nothing and catches everything.
                    names = (
                        {n.id for n in ast.walk(handler.type) if isinstance(n, ast.Name)}
                        if handler.type is not None
                        else {"BaseException"}
                    )
                    if not names & _CATCHES_THE_REFUSAL:
                        continue
                    if any(isinstance(n, ast.Raise) and n.exc is None for n in handler.body):
                        guarded = True
                    elif not guarded:
                        offenders.append(f"{rel}:{site.lineno} (handler at line {handler.lineno})")

    assert {"host/unix_host.py", "link/manage.py"} <= files_with_sites, (
        f"the elevated-call-site query found sites only in {sorted(files_with_sites)} — "
        "it has rotted and would pass on anything"
    )
    assert not offenders, (
        f"{offenders} runs an elevated command inside a handler (or a "
        "`contextlib.suppress`) that catches UnsupportedOnUserlandError without "
        "re-raising it. A host that cannot elevate would be reported as having "
        "succeeded. Add an explicit `except UnsupportedOnUserlandError: raise` "
        "above the broad handler, or narrow it to the exceptions it actually "
        "means — a `suppress` has only the second option."
    )
