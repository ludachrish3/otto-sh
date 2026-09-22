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


def _mgr(creds):
    return ConnectionManager(ip="10.0.0.1", creds=creds, term="ssh", name="h1")


def test_credentials_resolves_direct_cred_for_proxied_target():
    mgr = _mgr([MYSQL, ADMIN])
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
    assert mgr.proxy_hops == []


def test_has_direct_cred_is_the_question_direct_cred_for_answers_by_raising():
    creds = [Cred(login="alice", password="pw"), Cred(login="root", proxy="su", via="alice")]
    mgr = _mgr(creds)
    assert mgr.has_direct_cred("alice") is True
    assert mgr.has_direct_cred("root") is False, "reachable only through a hop"
    with pytest.raises(LoginProxyError):
        mgr._direct_cred_for("root")


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


# ---------------------------------------------------------------------------
# Per-protocol picks (spec 2026-09-13 cred-scope §4)
# ---------------------------------------------------------------------------

FTP_USER = Cred(login="ftpuser", password="ftp-only", protocols=["ftp"])
TELNET_ROOT = Cred(login="root", password="tn", protocols=["telnet"])


def test_transport_cred_for_ftp_is_the_scoped_cred_and_the_term_pick_is_untouched():
    # The ftp-scoped entry leads, so it is ftp's default and nothing else's:
    # over the ssh term it does not apply and the proxied `mysql` is the pick.
    mgr = _mgr([FTP_USER, MYSQL, ADMIN])
    assert mgr.transport_cred_for("ftp") == FTP_USER
    assert mgr.login_target_for("ftp") == "ftpuser"
    assert mgr.credentials == ("admin", "hunter2")
    assert mgr.login_target == "mysql"


def test_transport_cred_for_a_protocol_with_no_scoped_cred_is_the_direct_end_of_the_term_pick():
    mgr = _mgr([MYSQL, ADMIN])
    assert mgr.transport_cred_for("ftp") == ADMIN


def test_transport_cred_for_is_none_when_no_cred_applies():
    assert _mgr([]).transport_cred_for("ftp") is None
    assert _mgr([FTP_USER]).transport_cred_for("ssh") is None


def test_a_term_every_cred_is_scoped_away_from_is_loginless():
    """No raise and no substitution: the term's pick is the loginless ``""``.

    A ``--term`` override can select a term no entry applies to; the transport
    then fails at the far end naming the host (spec 2026-09-13 cred-scope §3.2).
    """
    mgr = _mgr([FTP_USER, TELNET_ROOT])
    assert mgr.login_target == ""
    assert mgr.credentials == ("", "")
    assert mgr.proxy_hops == []


def test_login_target_for_a_non_term_protocol_is_its_own_pick():
    mgr = _mgr([FTP_USER, ADMIN])
    assert mgr.login_target_for("ftp") == "ftpuser"
    assert mgr.login_target == "admin"


@pytest.mark.asyncio
async def test_ftp_authenticates_as_the_ftp_pick(monkeypatch):
    import aioftp

    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def connect(self, host, port):
            seen["connect"] = (host, port)

        async def login(self, user, password):
            seen["login"] = (user, password)

    monkeypatch.setattr(aioftp, "Client", FakeClient)
    mgr = _mgr([FTP_USER, ADMIN])
    await mgr.ftp()
    assert seen["login"] == ("ftpuser", "ftp-only")


@pytest.mark.asyncio
async def test_ftp_closes_the_client_when_login_is_refused(monkeypatch):
    """A rejected login must not leak the just-connected client.

    ``self._ftp_conn`` is only assigned after ``login()`` succeeds, so a
    client that raises on login is otherwise referenced by nothing and never
    closed — see ``ConnectionManager.ftp()``'s try/except around ``login()``.
    """
    import aioftp

    closed = {"count": 0}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def connect(self, host, port):
            pass

        async def login(self, user, password):
            raise RuntimeError("530 Login incorrect")

        def close(self):
            closed["count"] += 1

    monkeypatch.setattr(aioftp, "Client", FakeClient)
    mgr = _mgr([FTP_USER, ADMIN])
    with pytest.raises(RuntimeError, match="530 Login incorrect"):
        await mgr.ftp()
    assert closed["count"] == 1
    assert mgr._ftp_conn is None


@pytest.mark.asyncio
async def test_a_refused_ftp_login_raises_an_otto_error_naming_the_login(monkeypatch):
    """aioftp's own StatusCodeError names no host, no login and not the protocol.

    Which is exactly what a reader needs on the failure scope makes likely:
    ftp picks its OWN cred, so the account that was refused is not necessarily
    the one the lab file reads as the host's login. The received status code
    stays in the message (530 is a wrong cred, 421 a wrong server); the
    password never appears in it.
    """
    import aioftp

    closed = {"count": 0}
    refusal = aioftp.errors.StatusCodeError(
        aioftp.Code("230"), aioftp.Code("530"), ["Login incorrect"]
    )

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def connect(self, host, port):
            pass

        async def login(self, user, password):
            raise refusal

        def close(self):
            closed["count"] += 1

    monkeypatch.setattr(aioftp, "Client", FakeClient)
    mgr = _mgr([FTP_USER, ADMIN])
    with pytest.raises(LoginProxyError, match=r"h1: ftp login refused for 'ftpuser' \(530\)"):
        await mgr.ftp()
    assert closed["count"] == 1
    assert mgr._ftp_conn is None

    with pytest.raises(LoginProxyError) as caught:
        await mgr.ftp()
    assert caught.value.__cause__ is refusal, "the aioftp refusal must stay the cause"
    assert FTP_USER.password not in str(caught.value), "the password must never be in the message"


@pytest.mark.asyncio
async def test_ssh_under_a_telnet_term_authenticates_as_the_ssh_pick(monkeypatch):
    seen: dict[str, object] = {}

    async def fake_ssh_connect(ip, username, password, tunnel=None, **kw):
        seen["auth"] = (username, password)
        return AsyncMock()

    monkeypatch.setattr("otto.host.connections.ssh_connect", fake_ssh_connect)
    ssh_ops = Cred(login="ops", password="ops-pw", protocols=["ssh"])
    mgr = ConnectionManager(ip="10.0.0.1", creds=[TELNET_ROOT, ssh_ops], term="telnet", name="h1")
    await mgr.ssh()
    assert seen["auth"] == ("ops", "ops-pw")
    assert mgr.login_target == "root"


@pytest.mark.asyncio
async def test_ssh_as_resolves_against_ssh(monkeypatch):
    seen: dict[str, object] = {}

    async def fake_ssh_connect(ip, username, password, tunnel=None, **kw):
        seen["auth"] = (username, password)
        return AsyncMock()

    monkeypatch.setattr("otto.host.connections.ssh_connect", fake_ssh_connect)
    ssh_admin = Cred(login="admin", password="ssh-pw", protocols=["ssh"])
    mgr = _mgr([ADMIN, ssh_admin])
    await mgr.ssh_as("admin")
    assert seen["auth"] == ("admin", "ssh-pw")
