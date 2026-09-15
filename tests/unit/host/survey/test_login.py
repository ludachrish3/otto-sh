"""The login tier: one real login per (protocol, port) on a throwaway copy of the host.

The copy is the sanctioned `dataclasses.replace` seam: it re-runs
`__post_init__`, so it owns a fresh ConnectionManager and the ORIGINAL host's
sessions are never touched. Wire failures are classified, never guessed.
"""

import asyncio

import asyncssh
import pytest

from otto.host.element import Element
from otto.host.errors import SessionSetupError
from otto.host.login_proxy import Cred, LoginProxyError
from otto.host.options import FtpOptions, SshOptions, TelnetOptions
from otto.host.survey.login import (
    LoginOutcome,
    attempt_ftp_login,
    attempt_term_login,
    classify_login_error,
    host_copy_on_port,
)
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode

_CREDS = [
    Cred(login="admin", password="pw"),
    Cred(login="ftpuser", password="pw2", protocols=["ftp"]),
]


def _host(**extra):
    """A host whose declared ports differ from every port the tests attempt on."""
    extra.setdefault("ssh_options", SshOptions(port=22))
    extra.setdefault("telnet_options", TelnetOptions(port=23))
    extra.setdefault("ftp_options", FtpOptions(port=21))
    extra.setdefault("creds", list(_CREDS))
    return UnixHost(
        ip="127.0.0.1",
        element=Element("u1"),
        name="u1",
        valid_terms=["ssh", "telnet"],
        valid_transfers=["scp", "ftp"],
        log=LogMode.QUIET,
        **extra,
    )


def test_the_copy_changes_only_the_named_port_and_leaves_the_original_alone():
    """Mutation: mutate the host in place and the original's port moves."""
    host = _host()
    copy = host_copy_on_port(host, "ssh", 2222, login=None)
    assert copy.term == "ssh"
    assert copy.valid_terms == ["ssh"]
    assert copy.ssh_options.port == 2222
    assert host.ssh_options.port == 22
    assert copy.connections is not host.connections
    assert copy.creds == host.creds


def test_a_telnet_copy_switches_the_term():
    copy = host_copy_on_port(_host(), "telnet", 2323, login=None)
    assert copy.term == "telnet"
    assert copy.telnet_options.port == 2323
    assert copy.valid_terms == ["telnet"]


def test_an_ftp_copy_keeps_the_term_and_pins_the_transfer():
    host = _host()
    copy = host_copy_on_port(host, "ftp", 2121, login=None)
    assert copy.term == host.term
    assert copy.transfer == "ftp"
    assert copy.valid_transfers == ["ftp"]
    assert copy.ftp_options.port == 2121


def test_a_named_login_moves_its_cred_to_the_front():
    """Mutation: append instead of prepend and default_login keeps picking admin."""
    copy = host_copy_on_port(_host(), "ftp", 21, login="ftpuser")
    assert [c.login for c in copy.creds] == ["ftpuser", "admin"]
    assert copy.connections.login_target_for("ftp") == "ftpuser"


def test_an_unknown_login_is_refused_by_name():
    with pytest.raises(LoginProxyError, match="no cred for login 'ghost'"):
        host_copy_on_port(_host(), "ssh", 22, login="ghost")


@pytest.mark.parametrize(
    ("exc", "state", "needle"),
    [
        (asyncssh.PermissionDenied("Permission denied"), "login-failed", "permission denied"),
        (asyncssh.HostKeyNotVerifiable("Host key is not trusted"), "login-failed", "host key"),
        (
            asyncssh.KeyExchangeFailed("No matching key exchange algorithm"),
            "login-failed",
            "host key",
        ),
        (LoginProxyError("u1: ftp login refused for 'admin' (530)"), "login-failed", "530"),
        (ConnectionRefusedError(111, "Connection refused"), "closed", "refused"),
        (asyncio.TimeoutError(), "timeout", ""),
        (OSError(113, "No route to host"), "not-checkable", "No route to host"),
        # The exact exception ShellSession._fail_init raises when a transport
        # connects but no prompt ever arrives -- a bare builtin ConnectionError,
        # which is an OSError: the login tier must NOT read it as unreachable.
        (
            ConnectionError(
                "shell never became ready after open -- the transport connected but "
                "the shell never reached a prompt; the shell may never have started, "
                "or the login never completed (e.g. bad credentials)"
            ),
            "login-failed",
            "shell never became ready after open",
        ),
        (ValueError("no login tier for 'http'"), "login-failed", "ValueError"),
    ],
)
def test_wire_failures_classify_without_guessing(exc, state, needle):
    """Mutation: map OSError to closed and an unroutable host reads as dead."""
    out = classify_login_error(exc, who="login 'admin'")
    assert out.state == state
    assert needle in out.detail


def test_a_wrapped_permission_denial_is_still_login_failed():
    """Mutation: classify only the outermost and a wrapped denial reads not-checkable.

    ``SessionSetupError`` is an ``OSError`` subclass (``OttoError,
    ConnectionError``), so an outermost-only classifier folds a refused
    password into the unroutable-host arm.
    """
    wrapped = SessionSetupError("u1: shell never came up")
    wrapped.__cause__ = asyncssh.PermissionDenied("Permission denied")
    out = classify_login_error(wrapped, who="login 'admin'")
    assert out == LoginOutcome(state="login-failed", detail="login 'admin': permission denied")


@pytest.mark.parametrize(
    ("code", "reason", "state", "detail"),
    [
        (
            asyncssh.OPEN_CONNECT_FAILED,
            "Connect failed",
            "closed",
            "connection refused (via hop)",
        ),
        (
            asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED,
            "open failed",
            "not-checkable",
            "hop forbids port forwarding: open failed",
        ),
        (
            asyncssh.OPEN_RESOURCE_SHORTAGE,
            "out of channels",
            "not-checkable",
            "out of channels",
        ),
    ],
)
def test_a_hop_forward_failure_is_read_from_its_open_code(code, reason, state, detail):
    """On a hopped host a dead port never reaches a socket: the hop refuses the forward.

    Mutation: leave ChannelOpenError to the catch-all and every closed port on
    a hopped host reads login-failed -- a fabricated verdict for a port that
    nothing is listening on.
    """
    out = classify_login_error(asyncssh.ChannelOpenError(code, reason), who="login 'admin'")
    assert out == LoginOutcome(state=state, detail=detail)


def test_a_reasonless_hop_failure_still_states_a_reason():
    """asyncssh's ``reason`` IS its ``str``, so a reason-less error carries no text at all.

    A not-checkable is specified as "no honest check exists; reason stated"
    (spec 2026-09-14 section 3), so the code has to become the reason when the
    hop supplies none -- an empty detail states nothing. What matters besides
    is that it is still read as a hop failure: the catch-all would put
    ``login 'admin': ChannelOpenError:`` in the row and call it login-failed.
    """
    exc = asyncssh.ChannelOpenError(asyncssh.OPEN_UNKNOWN_CHANNEL_TYPE, "")
    assert str(exc) == ""
    out = classify_login_error(exc, who="login 'admin'")
    assert out == LoginOutcome(
        state="not-checkable",
        detail=f"channel open failed (code {asyncssh.OPEN_UNKNOWN_CHANNEL_TYPE})",
    )


def test_a_wrapped_hop_refusal_is_still_closed():
    """The session layer re-raises the forward failure; the chain walk must reach it."""
    wrapped = SessionSetupError("u1: could not open a session")
    wrapped.__cause__ = asyncssh.ChannelOpenError(asyncssh.OPEN_CONNECT_FAILED, "Connect failed")
    out = classify_login_error(wrapped, who="login 'admin'")
    assert out == LoginOutcome(state="closed", detail="connection refused (via hop)")


def test_a_suppressed_context_is_not_read_as_the_failure():
    """``raise X from None`` says the context is irrelevant; the walk must honour it.

    Mutation: walk ``__context__`` unconditionally and an unrelated earlier
    denial captured by the interpreter renames the failure.
    """
    suppressed = ConnectionError("shell never became ready after open -- no prompt")
    suppressed.__context__ = asyncssh.PermissionDenied("Permission denied")
    suppressed.__suppress_context__ = True
    out = classify_login_error(suppressed, who="login 'admin'")
    assert out.state == "login-failed"
    assert out.detail == "login 'admin': shell never became ready after open -- no prompt"


@pytest.mark.asyncio
async def test_a_refused_port_is_closed_and_the_copy_is_closed(monkeypatch):
    """Loopback:1 refuses in milliseconds (the repo's own no-real-transport pattern)."""
    closed = []
    orig_close = UnixHost.close

    async def spy_close(self):
        closed.append(self.ssh_options.port)
        await orig_close(self)

    monkeypatch.setattr(UnixHost, "close", spy_close)
    host = _host()
    out = await attempt_term_login(host, "ssh", 1, login=None, timeout=5.0)
    assert out.state == "closed"
    assert closed == [1]  # the COPY's port, not the original's 22
    assert host.ssh_options.port == 22


@pytest.mark.asyncio
async def test_permission_denied_is_login_failed_naming_the_cred(monkeypatch):
    async def deny(*_a, **_k):
        raise asyncssh.PermissionDenied("Permission denied")

    monkeypatch.setattr("otto.host.connections.ssh_connect", deny)
    out = await attempt_term_login(_host(), "ssh", 2222, login=None, timeout=5.0)
    assert out == LoginOutcome(state="login-failed", detail="login 'admin': permission denied")


@pytest.mark.asyncio
async def test_the_scoped_ftp_cred_is_named_in_its_identity_form(monkeypatch):
    async def refuse(*_a, **_k):
        raise LoginProxyError("u1: ftp login refused for 'ftpuser' (530)")

    monkeypatch.setattr("otto.host.connections.ConnectionManager.ftp", refuse)
    out = await attempt_ftp_login(_host(), 21, login="ftpuser", timeout=5.0)
    assert out.state == "login-failed"
    assert out.detail.startswith("login 'ftpuser [ftp]' (--user):")


@pytest.mark.asyncio
async def test_a_hung_login_is_timeout_under_the_injected_budget(monkeypatch):
    """Mutation: inherit the family timeout and this test runs for 10 s."""

    async def hang(*_a, **_k):
        await asyncio.sleep(30)

    monkeypatch.setattr("otto.host.connections.ssh_connect", hang)
    out = await attempt_term_login(_host(), "ssh", 2222, login=None, timeout=0.05)
    assert out.state == "timeout"


@pytest.mark.asyncio
async def test_a_user_override_names_the_login_asked_for_and_marks_it(monkeypatch):
    """Mutation: name transport_cred_for's direct end and a proxied --user reads 'admin'."""

    async def deny(*_a, **_k):
        raise asyncssh.PermissionDenied("Permission denied")

    monkeypatch.setattr("otto.host.connections.ssh_connect", deny)
    host = _host(
        creds=[
            Cred(login="admin", password="pw"),
            Cred(login="root", proxy="su", via="admin"),
        ]
    )
    out = await attempt_term_login(host, "ssh", 2222, login="root", timeout=5.0)
    assert out == LoginOutcome(
        state="login-failed", detail="login 'root' (--user): permission denied"
    )


@pytest.mark.asyncio
async def test_a_term_with_no_applicable_cred_attempts_nothing(monkeypatch):
    """Spec 3.2: no cred applies, so the row is not-checkable and no login is attempted."""
    called = []

    async def boom(*_a, **_k):
        called.append(1)
        raise AssertionError("no login may be attempted")

    monkeypatch.setattr(UnixHost, "run", boom)
    monkeypatch.setattr("otto.host.connections.ssh_connect", boom)
    host = _host(creds=[Cred(login="ftpuser", password="pw2", protocols=["ftp"])])
    out = await attempt_term_login(host, "telnet", 2323, login=None, timeout=5.0)
    assert out == LoginOutcome(state="not-checkable", detail="no cred applies to telnet")
    assert called == []


@pytest.mark.asyncio
async def test_an_ftp_candidate_with_no_applicable_cred_attempts_nothing(monkeypatch):
    called = []

    async def boom(*_a, **_k):
        called.append(1)
        raise AssertionError("no login may be attempted")

    monkeypatch.setattr("otto.host.connections.ConnectionManager.ftp", boom)
    host = _host(creds=[Cred(login="admin", password="pw", protocols=["ssh"])])
    out = await attempt_ftp_login(host, 21, login=None, timeout=5.0)
    assert out == LoginOutcome(state="not-checkable", detail="no cred applies to ftp")
    assert called == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempt", "seam"),
    [
        (
            lambda host: attempt_term_login(host, "ssh", 2222, login=None, timeout=10.0),
            "otto.host.connections.ssh_connect",
        ),
        (
            lambda host: attempt_ftp_login(host, 2121, login=None, timeout=10.0),
            "otto.host.connections.ConnectionManager.ftp",
        ),
    ],
    ids=["term", "ftp"],
)
async def test_an_attempt_cancelled_by_the_outer_budget_still_closes_its_copy(
    monkeypatch, attempt, seam
):
    """The survey budget cancels the attempt mid-login; the copy must not be dropped.

    `engine._bounded` grants each attempt `min(timeout, remaining budget)`, so
    a short remaining budget cancels `copy.run` / `copy.connections.ftp()`
    from OUTSIDE. `CancelledError` is not an `Exception`, so the ordinary
    close was skipped and the copy -- with a possibly-established transport to
    the target -- was dropped unreferenced: a half-logged-in session left on
    the device, and an unclosed-transport ResourceWarning that this repo turns
    into a hard error.

    Mutation: delete the `except asyncio.CancelledError` arm and `closed` is
    empty (the reviewer's reproduction read `copies closed: 0`).
    """
    closed: list[int] = []
    orig_close = UnixHost.close

    async def spy_close(self):
        closed.append(self.ssh_options.port)
        await orig_close(self)

    async def hang(*_a, **_k):
        await asyncio.sleep(30)

    monkeypatch.setattr(UnixHost, "close", spy_close)
    monkeypatch.setattr(seam, hang)
    host = _host()
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await asyncio.wait_for(attempt(host), 0.05)
    assert closed, "the cancelled attempt never closed its copy"
