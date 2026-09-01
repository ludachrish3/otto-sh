"""Unit tests for ``ConnectionManager``'s credential-resolution surface.

``credentials`` / ``login_target`` / ``proxy_hops`` are the seam that lets a
proxied login (e.g. ``su``-only ``mysql``) authenticate the transport as its
directly-loginable ``via`` account while still tracking the *requested*
login. See :mod:`otto.host.login_proxy` for the chain-resolution semantics.

``ssh_as``/``sftp_as`` (spec 2026-09-01 §3) are a separate per-user
authentication surface: a directly-loginable cred opens its own cached
transport, keyed by login, distinct from the primary ``ssh()``/``sftp()``
slot that authenticates as ``login_target``.
"""

from unittest.mock import AsyncMock

import pytest

from otto.host.connections import ConnectionManager
from otto.host.login_proxy import Cred, LoginProxyError

ADMIN = Cred(login="admin", password="hunter2")
MYSQL = Cred(login="mysql", password="sqlpw", proxy="su", via="admin")


def _mgr(creds, user=None):
    return ConnectionManager(ip="10.0.0.1", creds=creds, user=user, term="ssh", name="h1")


def test_credentials_resolves_direct_cred_for_proxied_target():
    mgr = _mgr([MYSQL, ADMIN], user="mysql")
    assert mgr.credentials == ("admin", "hunter2")
    assert mgr.login_target == "mysql"
    assert [c.login for c in mgr.proxy_hops] == ["mysql"]


def test_credentials_plain_first_entry_default():
    mgr = _mgr([ADMIN, MYSQL])
    assert mgr.credentials == ("admin", "hunter2")
    assert mgr.login_target == "admin"
    assert mgr.proxy_hops == []


def test_credentials_empty_creds_loginless():
    mgr = _mgr([])
    assert mgr.credentials == ("", "")
    assert mgr.login_target == ""


@pytest.mark.asyncio
async def test_ssh_as_opens_and_caches_per_user(monkeypatch):
    calls: list[str] = []

    async def fake_connect(ip, username, password, tunnel=None, **kw):
        calls.append(username)
        return AsyncMock(name=f"conn-{username}")

    mgr = _mgr(creds=[Cred(login="vagrant", password="v"), Cred(login="postgres", password="p")])
    monkeypatch.setattr("otto.host.connections.ssh_connect", fake_connect)
    c1 = await mgr.ssh_as("postgres")
    c2 = await mgr.ssh_as("postgres")
    assert c1 is c2  # cached
    assert calls == ["postgres"]  # authenticated AS postgres, once
    assert (await mgr.ssh()) is not c1  # primary connection is separate


@pytest.mark.asyncio
async def test_ssh_as_refuses_proxied_user():
    creds = [
        Cred(login="vagrant", password="v"),
        Cred(login="admin", password=None, proxy="su", via="vagrant"),
    ]
    mgr = _mgr(creds=creds)
    with pytest.raises(LoginProxyError, match="has no directly-loginable cred"):
        await mgr.ssh_as("admin")


@pytest.mark.asyncio
async def test_ssh_as_unknown_login_uses_resolve_chains_error():
    mgr = _mgr(creds=[Cred(login="vagrant", password="v")])
    with pytest.raises(LoginProxyError, match="unknown login"):
        await mgr.ssh_as("ghost")


@pytest.mark.asyncio
async def test_sftp_as_opens_over_ssh_as_and_caches_per_user(monkeypatch):
    async def fake_connect(ip, username, password, tunnel=None, **kw):
        conn = AsyncMock(name=f"conn-{username}")
        conn.start_sftp_client = AsyncMock(return_value=AsyncMock(name=f"sftp-{username}"))
        return conn

    mgr = _mgr(creds=[Cred(login="vagrant", password="v"), Cred(login="postgres", password="p")])
    monkeypatch.setattr("otto.host.connections.ssh_connect", fake_connect)
    s1 = await mgr.sftp_as("postgres")
    s2 = await mgr.sftp_as("postgres")
    assert s1 is s2
    conn = await mgr.ssh_as("postgres")
    conn.start_sftp_client.assert_called_once()


def test_connected_false_with_no_open_connections():
    mgr = _mgr(creds=[Cred(login="vagrant", password="v")])
    assert mgr.connected is False


def test_connected_true_with_only_a_per_user_ssh_conn():
    """A host whose only open transport is an ``ssh_as`` connection must still
    report ``connected`` — otherwise ``context.py``'s scope-exit close sweep
    filters it OUT (``remaining = [h for h in hosts if getattr(h, "_connected",
    True)]``, remote_host.py) and its per-user transport leaks forever."""
    mgr = _mgr(creds=[Cred(login="vagrant", password="v")])
    mgr._user_ssh_conns["vagrant"] = AsyncMock(name="conn-vagrant")
    assert mgr.connected is True


def test_connected_true_with_only_a_per_user_sftp_conn():
    mgr = _mgr(creds=[Cred(login="vagrant", password="v")])
    mgr._user_sftp_conns["vagrant"] = AsyncMock(name="sftp-vagrant")
    assert mgr.connected is True
