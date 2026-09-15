"""The login tier: a real login per (protocol, port), on a copy that owns its transports.

``dataclasses.replace`` re-runs ``__post_init__``, so the copy has its own
ConnectionManager and SessionManager and the surveyed host's sessions are
untouched (the seam ``otto host --term`` already relies on). One attempt per
pair; the copy is closed afterwards whatever happened -- including when the
survey's budget CANCELS the attempt mid-login; every failure shape is
classified, and nothing is retried.
"""

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ...logger.mode import LogMode
from ..connections import teardown_step
from ..login_proxy import Cred, LoginProxyError, cred_for, cred_identity, default_login
from .verdict import State

if TYPE_CHECKING:
    from ..remote_host import RemoteHost
    from ..unix_host import UnixHost

_CAUSE_DEPTH = 4
"""How far down ``__cause__``/``__context__`` a classification looks.

A wire failure is often re-raised by the host layer -- and several of otto's
own errors are ``OSError`` subclasses (``SessionSetupError`` is ``OttoError,
ConnectionError``), so reading only the outermost exception would fold a
refused password into the unroutable-host arm.
"""


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """What one login attempt against one (protocol, port) established."""

    state: State
    detail: str


def _front_loaded(host: "UnixHost", protocol: str, login: str | None) -> list[Cred]:
    """*host*'s creds with *login*'s entry first, so it is the one the protocol picks."""
    if login is None:
        return list(host.creds)
    cred = cred_for(host.creds, login, protocol)
    if cred is None:
        msg = f"{host.name}: no cred for login {login!r}"
        raise LoginProxyError(msg)
    return [cred, *[c for c in host.creds if c is not cred]]


def host_copy_on_port(
    host: "UnixHost", protocol: str, port: int, *, login: str | None
) -> "UnixHost":
    """Build a copy of *host* that reaches *protocol* on *port*, with *login*'s cred first."""
    creds = _front_loaded(host, protocol, login)
    if protocol == "ssh":
        return replace(
            host,
            term="ssh",
            valid_terms=["ssh"],
            creds=creds,
            ssh_options=replace(host.ssh_options, port=port),
        )
    if protocol == "telnet":
        return replace(
            host,
            term="telnet",
            valid_terms=["telnet"],
            creds=creds,
            telnet_options=replace(host.telnet_options, port=port),
        )
    if protocol == "ftp":
        return replace(
            host,
            transfer="ftp",
            valid_transfers=["ftp"],
            creds=creds,
            ftp_options=replace(host.ftp_options, port=port),
        )
    msg = f"no login tier for {protocol!r}"
    raise ValueError(msg)


def _named_failure(exc: BaseException, *, who: str) -> "LoginOutcome | None":
    """Return the verdict for a failure shape otto recognises by type, else None."""
    import asyncssh

    if isinstance(exc, asyncssh.PermissionDenied):
        return LoginOutcome("login-failed", f"{who}: permission denied")
    if isinstance(exc, (asyncssh.HostKeyNotVerifiable, asyncssh.KeyExchangeFailed)):
        return LoginOutcome("login-failed", f"{who}: host key: {exc}")
    if isinstance(exc, asyncssh.ChannelOpenError):
        # A hopped candidate never reaches a socket of otto's: the connect is a
        # direct-tcpip forward the HOP performs, and a dead port comes back as
        # the hop refusing to open the channel. The open code carries what the
        # refused connect would have raised locally, so read it rather than
        # letting a closed port fall to the catch-all and read login-failed.
        if exc.code == asyncssh.OPEN_CONNECT_FAILED:
            return LoginOutcome("closed", "connection refused (via hop)")
        if exc.code == asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED:
            return LoginOutcome("not-checkable", f"hop forbids port forwarding: {exc.reason}")
        # asyncssh's ``reason`` IS its ``str``, so both can be empty; a
        # not-checkable with no reason states nothing.
        return LoginOutcome("not-checkable", exc.reason or f"channel open failed (code {exc.code})")
    if isinstance(exc, LoginProxyError):
        return LoginOutcome("login-failed", f"{who}: {exc}")
    if isinstance(exc, ConnectionRefusedError):
        return LoginOutcome("closed", "connection refused")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return LoginOutcome("timeout", "no session within the login budget")
    return None


def _chain(exc: BaseException) -> "list[BaseException]":
    """*exc* and up to :data:`_CAUSE_DEPTH` of its causes/contexts, outermost first."""
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(seen) < _CAUSE_DEPTH:
        if any(current is held for held in seen):
            break
        seen.append(current)
        context = None if current.__suppress_context__ else current.__context__
        current = current.__cause__ or context
    return seen


def classify_login_error(exc: BaseException, *, who: str) -> LoginOutcome:
    """Name what the wire said, without folding one failure into another.

    The recognised shapes are looked for down the cause/context chain, because
    the host layer re-raises them: a ``PermissionDenied`` wrapped in a
    ``SessionSetupError`` is still a refused password, not an unreachable
    host.

    Nothing recognised leaves the outermost exception to speak, and there the
    ``ConnectionError`` family is split off ahead of the generic ``OSError``
    arm. It has to be: a transport that connected and then never reached a
    prompt is raised as a bare builtin ``ConnectionError`` ("shell never
    became ready after open ... the login never completed (e.g. bad
    credentials)"), which is the single most common telnet credential
    failure, and an ``OSError`` arm that swallowed it would report the
    survey's most important row as ``not-checkable`` -- a state the drift
    table and the pin both discard. A ``ConnectionError`` means the service
    answered and the login did not complete; only an ``OSError`` outside that
    family is a wire condition otto cannot judge.
    """
    for link in _chain(exc):
        named = _named_failure(link, who=who)
        if named is not None:
            return named
    if isinstance(exc, ConnectionError):
        return LoginOutcome("login-failed", f"{who}: {exc}")
    if isinstance(exc, OSError):
        return LoginOutcome("not-checkable", f"{type(exc).__name__}: {exc}")
    return LoginOutcome("login-failed", f"{who}: {type(exc).__name__}: {exc}")


def _no_cred(host: "UnixHost", protocol: str) -> bool:
    """Whether NO entry in *host*'s creds applies to *protocol*."""
    return not default_login(host.creds, protocol)


def _who(copy: "UnixHost", protocol: str, *, login: str | None) -> str:
    """Name the identity the attempt authenticates as, in the form errors print it.

    An operator override names the login that was ASKED for and says so. The
    two differ whenever the cred is proxied: ``transport_cred_for`` resolves
    the via-chain to its directly-loginable end, so ``--user root`` on a
    ``root``-via-``admin`` cred would otherwise put ``admin`` in the row --
    a login the operator never named. The marker is what tells a reader
    whether otto picked this login (the lab's cred order) or they forced it.
    """
    cred = (
        cred_for(copy.creds, login, protocol)
        if login is not None
        else copy.connections.transport_cred_for(protocol)
    )
    if cred is None:
        return "login ''"
    marker = " (--user)" if login is not None else ""
    return f"login {cred_identity(cred.login, cred.protocols)!r}{marker}"


async def _close_on_cancel(copy: "UnixHost | RemoteHost", name: str, step: str) -> None:
    """Close *copy* so the close survives the cancellation that is propagating.

    The survey's OUTER bound (`engine._bounded`) grants each attempt
    ``min(timeout, remaining budget)``, so whenever the remaining budget is
    shorter than the login timeout -- a slow inventory, several discovered
    ports -- the outer ``wait_for`` fires first and cancels the attempt mid
    ``copy.run`` / ``copy.connections.ftp()`` / ``copy.connections.telnet()``.
    ``CancelledError`` is not an ``Exception``, so the ordinary close below
    was never reached and the copy's fresh ConnectionManager -- holding a
    possibly-established ssh/telnet/ftp transport to the TARGET -- was
    dropped unreferenced: a half-logged-in session left hanging on the
    device (the spec's lockout hygiene), and an "unclosed transport"
    ResourceWarning that this repo's ``filterwarnings = error`` turns into a
    hard failure.

    :func:`otto.lifecycle.compensate` is the house answer (``teardown_step``'s
    own docstring names it): it HOLDS a further cancellation under a shield
    until the close finishes, then re-raises it. The awaited close therefore
    never sits in a bare ``finally`` -- the architecture gate's rule -- and
    the caller's cancellation still wins.

    Imported here, not at module scope: ``otto.lifecycle`` is only needed once
    a compensating action actually runs, and a top-level import drags it onto
    every CLI ``--help`` path (import-budget guard).
    """
    from ...lifecycle import compensate

    with teardown_step(name, step):
        await compensate(copy.close(), what=f"{name}: {step}")


async def attempt_term_login(
    host: "UnixHost", protocol: str, port: int, *, login: str | None, timeout: float
) -> LoginOutcome:
    """Open a session over *protocol* on *port* once; report what happened."""
    if _no_cred(host, protocol):
        return LoginOutcome("not-checkable", f"no cred applies to {protocol}")
    try:
        copy = host_copy_on_port(host, protocol, port, login=login)
    except LoginProxyError as exc:
        return LoginOutcome("not-checkable", str(exc))
    who = _who(copy, protocol, login=login)
    try:
        await asyncio.wait_for(copy.run("true", timeout=timeout, log=LogMode.NEVER), timeout)
        outcome = LoginOutcome("supported", f"{who}: session opened")
    except asyncio.CancelledError:
        await _close_on_cancel(copy, host.name, "survey login close")
        raise
    except Exception as exc:  # noqa: BLE001 — every wire failure becomes a classified verdict
        outcome = classify_login_error(exc, who=who)
    with teardown_step(host.name, "survey login close"):
        await copy.close()
    return outcome


async def attempt_ftp_login(
    host: "UnixHost", port: int, *, login: str | None, timeout: float
) -> LoginOutcome:
    """One ftp control-channel login on *port*; report what happened."""
    if _no_cred(host, "ftp"):
        return LoginOutcome("not-checkable", "no cred applies to ftp")
    try:
        copy = host_copy_on_port(host, "ftp", port, login=login)
    except LoginProxyError as exc:
        return LoginOutcome("not-checkable", str(exc))
    who = _who(copy, "ftp", login=login)
    try:
        await asyncio.wait_for(copy.connections.ftp(), timeout)
        outcome = LoginOutcome("supported", f"{who}: 230 logged in")
    except asyncio.CancelledError:
        await _close_on_cancel(copy, host.name, "survey login close")
        raise
    except Exception as exc:  # noqa: BLE001 — every wire failure becomes a classified verdict
        outcome = classify_login_error(exc, who=who)
    with teardown_step(host.name, "survey login close"):
        await copy.close()
    return outcome


async def attempt_console_open(host: "RemoteHost", port: int, *, timeout: float) -> LoginOutcome:
    """Open the embedded console on *port* once, on a copy; report what happened."""
    copy = replace(host, telnet_options=replace(host.telnet_options, port=port))
    try:
        await asyncio.wait_for(copy.connections.telnet(), timeout)
        outcome = LoginOutcome("supported", "console connected")
    except asyncio.CancelledError:
        await _close_on_cancel(copy, host.name, "survey console close")
        raise
    except Exception as exc:  # noqa: BLE001 — every wire failure becomes a classified verdict
        outcome = classify_login_error(exc, who="console")
    with teardown_step(host.name, "survey console close"):
        await copy.close()
    return outcome
