"""Login proxies: registered multi-step user-switch sequences.

A cred entry may declare that its login cannot be reached by direct
authentication: to *become* it, otto authenticates (or starts) as another
account (``via``) and replays the named proxy's send/expect steps. Proxies
are async callables registered by libraries from ``init`` modules via
:func:`register_login_proxy`, mirroring the term/transfer registries. The
built-in ``"su"`` proxy is the default user-switch mechanism (it replaces
the old hardcoded su-switch helper that ``switch_user``/``as_user`` used to
call directly).
"""

import asyncio
import contextlib
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from ..logger.mode import LogMode
from ..registry import Registry, caller_module


@dataclass(frozen=True)
class Cred:
    """One credential entry: a login plus how to become it."""

    login: str
    """The account name."""

    password: str | None = None
    """Password, or None (key/agent auth on SSH; empty line on telnet; no
    password exchange in the built-in su proxy)."""

    proxy: str | None = None
    """Login-proxy registry key; None means directly loginable (switch via
    the built-in ``"su"``)."""

    via: str | None = None
    """Login of the account the proxy starts from. None defaults to the
    first proxy-less cred entry."""

    params: dict[str, Any] = field(default_factory=dict)
    """Free-form data handed to the proxy callable (host-specific knobs)."""


@runtime_checkable
class ProxyIO(Protocol):
    """Minimal I/O handle a proxy drives.

    Satisfied by hosts, ``HostSession`` instances, the raw-session adapter
    used at session establishment, and the interact bridge adapter.
    """

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        """Send text to the proxy IO."""
        ...

    async def expect(self, pattern: str | re.Pattern[str], timeout: float = 10.0) -> str:
        """Expect a pattern and return the matched output."""
        ...


@dataclass(frozen=True)
class ProxyContext:
    """Everything a proxy step may need.

    Deliberately NOT the host object — running commands mid-proxy on the
    session being established deadlocks.
    """

    target: Cred
    via: Cred
    host_id: str


LoginProxyFn = Callable[[ProxyIO, ProxyContext], Awaitable[None]]
"""An async callable that drives one proxy's steps: ``async def proxy(io, ctx)``."""


@dataclass(frozen=True)
class LoginProxy:
    """A registered proxy: the steps plus an optional reversal."""

    fn: LoginProxyFn
    undo: LoginProxyFn | None = None


class LoginProxyError(ConnectionError):
    """A proxy step failed or a chain could not be resolved."""


LOGIN_PROXIES: Registry[LoginProxy] = Registry(
    "login proxy", register_hint="otto.register_login_proxy()"
)


def register_login_proxy(
    name: str,
    fn: LoginProxyFn,
    *,
    undo: LoginProxyFn | None = None,
    overwrite: bool = False,
) -> None:
    """Register a login proxy under *name* (see :data:`LoginProxyFn`).

    *undo* reverses the steps for ``as_user`` restore; None means the
    default reversal (send ``exit``), correct for any su/sudo-style nested
    shell.
    """
    LOGIN_PROXIES.register(name, LoginProxy(fn, undo), overwrite=overwrite, origin=caller_module())


async def _su_proxy(io: ProxyIO, ctx: ProxyContext) -> None:
    """Built-in single-step ``su`` exchange (the pre-proxy default)."""
    login = ctx.target.login
    cmd = "su" if not login else f"su {shlex.quote(login)}"
    await io.send(cmd + "\n")
    if ctx.target.password is not None:
        await io.expect(r"[Pp]assword:")
        await io.send(ctx.target.password + "\n", log=LogMode.NEVER)


register_login_proxy("su", _su_proxy)


def _default_direct(creds: list[Cred]) -> Cred | None:
    return next((c for c in creds if c.proxy is None), None)


def cred_for(creds: list[Cred], login: str) -> Cred | None:
    """Look up a cred entry by login (None when absent)."""
    return next((c for c in creds if c.login == login), None)


def resolve_chain(creds: list[Cred], target_login: str) -> tuple[Cred, list[Cred]]:
    """Resolve the direct-auth cred and the hop list for *target_login*.

    Returns ``(direct, hops)`` where *direct* is the cred to authenticate
    the transport as and *hops* are the proxied creds to apply afterwards,
    outermost (first to run) first. Spec validation guarantees termination;
    the ``seen`` set is a runtime backstop against hand-built cred lists.
    """
    cred = cred_for(creds, target_login)
    if cred is None:
        known = ", ".join(c.login for c in creds) or "<none>"
        raise LoginProxyError(f"unknown login {target_login!r}; creds define: {known}")
    hops: list[Cred] = []
    seen = {cred.login}
    while cred.proxy is not None:
        hops.append(cred)
        nxt = cred_for(creds, cred.via) if cred.via is not None else _default_direct(creds)
        if nxt is None or nxt.login in seen:
            raise LoginProxyError(
                f"cred {cred.login!r}: cannot resolve a directly-loginable "
                f"via-chain (missing or cyclic 'via')"
            )
        seen.add(nxt.login)
        cred = nxt
    return cred, list(reversed(hops))


def _get_proxy(hop: Cred) -> LoginProxy:
    return LOGIN_PROXIES.get(hop.proxy or "su")


# Ceiling on one resync attempt and the number of attempts _resync_shell makes
# before giving up. A su/sudo/exit transition is a foreground-process handoff
# on the pty: su/login/sudo traditionally flush pending terminal input across
# a privilege boundary (a typeahead-attack defense), so bytes written the
# instant control changes hands can be silently dropped. Confirmed on the live
# bed with otto's built-in "su" proxy: a fire-and-forget send after the
# transition reproduced a 100%-reliable hang on the very next sentinel-wrapped
# command. Resending a fresh, unique marker (retried a bounded number of times)
# closes the race — mirrors the connection-level READY handshake in
# otto.host.session._ensure_initialized.
#
# INTERIM HARDENING (issue: `make release` 3.13 flake on the mysql bed): the
# very first probe after the transition is *deterministically* eaten by the
# flush (verified 40/40 on the live bed — the `exit` and the `echo <marker>`
# are written back-to-back, so the marker lands in the flush window). Recovery
# then depends on the remaining attempts each completing a round-trip within
# _RESYNC_TIMEOUT; under heavy `make release` load (nox x3 + subprocess-coverage
# saturating the client VM) those round-trips slow down and all attempts are
# exhausted. Two cheap knobs close the gap until the unified redesign lands
# (spec: docs/superpowers/specs/2026-07-05-shell-liveness-probe-unification-design.md):
#   * _RESYNC_SETTLE — a short settle so the first probe is NOT written into
#     the flush window (live-bed: a 0.3 s settle makes attempt 1 land ~7 ms,
#     eliminating the always-wasted first attempt);
#   * a larger _RESYNC_TIMEOUT — tolerate a slow probe round-trip under load
#     (only paid when a probe is genuinely slow, which the settle makes rare).
_RESYNC_ATTEMPTS = 5
_RESYNC_TIMEOUT = 6.0
_RESYNC_SETTLE = 0.3


async def _resync_shell(io: ProxyIO, host_id: str, hop_login: str) -> None:
    r"""Resync with the shell after a su/sudo/exit transition.

    Sends a fresh, unique marker via ``echo <marker>`` and waits for it to
    come back, retrying up to :data:`_RESYNC_ATTEMPTS` times.

    The wait matches the marker **only when it is NOT immediately preceded by
    this function's own ``"echo "`` probe prefix** (a negative lookbehind).
    That discriminator is what makes the resync sound across both echo modes
    a login proxy can run in:

    - **echo-ON** (the ``interact --as-user`` bridge replays hops over
      ``_BridgeProxyIO`` on a PTY that still echoes input): the outgoing
      ``echo <marker>`` probe is echoed back on the same read stream, and
      ``_BridgeProxyIO.expect()`` does an unanchored ``regex.search``. A bare
      marker would match inside that echoed *command* — before the shell ran
      anything — and vacuously "succeed" without confirming a round-trip. The
      lookbehind rejects the echoed occurrence (preceded by ``"echo "``) and
      matches only the shell's real output line (preceded by a newline).
    - **echo-OFF** (framed ``switch_user``/``as_user``/session establishment,
      which run ``stty -echo`` — and ``stty`` state persists across ``su``/
      ``sudo`` since it lives on the PTY): the probe command is NOT echoed,
      so the shell's marker output glues directly onto the prior prompt with
      no leading newline (e.g. ``test@host:~$ <marker>``). A pure line-anchor
      (``(?:^|\r|\n)``) would *reject* that — verified on the live bed to hang
      the framed path — whereas the lookbehind matches it (preceded by the
      prompt, not ``"echo "``).

    In both modes the only occurrence the resync must ignore is the marker
    inside its own ``echo <marker>`` probe; the lookbehind targets exactly
    that and nothing else, so it is correct regardless of echo state or
    whether a prompt precedes the output.

    Raises :class:`LoginProxyError` if the shell never resyncs — the caller
    (:func:`run_proxy`/:func:`run_undo`) wraps this with hop context like any
    other proxy-step failure.
    """
    # Let the su/sudo/exit foreground handoff settle before probing so the first
    # marker is not written into the transition's tty-flush window and silently
    # dropped (see the _RESYNC_* constants' note). Interim hardening for the
    # `make release` 3.13 flake, superseded by the unified liveness-probe redesign.
    await asyncio.sleep(_RESYNC_SETTLE)
    for _ in range(_RESYNC_ATTEMPTS):
        marker = f"__OTTO_LP_SYNC_{uuid.uuid4().hex}__"
        await io.send(f"echo {marker}\n")
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            # Negative lookbehind for our own "echo " probe prefix: match the
            # shell's real echo of the marker, never the marker inside the
            # echoed command on an echo-ON pty. See docstring.
            await io.expect(rf"(?<!echo ){re.escape(marker)}", timeout=_RESYNC_TIMEOUT)
            return
    raise LoginProxyError(
        f"{host_id}: shell did not resync after a login-proxy transition "
        f"({hop_login!r}) — su/sudo/exit flushed the next command"
    )


async def run_proxy(io: ProxyIO, hop: Cred, via: Cred, host_id: str) -> None:
    """Run *hop*'s proxy steps over *io*, wrapping failures with context.

    Ends with a post-transition shell resync (``_resync_shell``) so the
    next sentinel-wrapped command otto writes can't land in the transition's
    tty-flush window and be silently discarded (see that function's
    docstring). A resync failure surfaces through the same wrapping as any
    other proxy-step failure below.
    """
    name = hop.proxy or "su"
    try:
        proxy = _get_proxy(hop)
        await proxy.fn(io, ProxyContext(target=hop, via=via, host_id=host_id))
        await _resync_shell(io, host_id, hop.login)
    except LoginProxyError:
        raise
    except Exception as e:
        raise LoginProxyError(
            f"{host_id}: login proxy failed becoming {hop.login!r} via proxy {name!r}: {e}"
        ) from e


async def run_undo(io: ProxyIO, hop: Cred, via: Cred, host_id: str) -> None:
    """Reverse *hop*: the registered undo, or the default ``exit``.

    Also ends with a post-transition shell resync, like :func:`run_proxy` —
    the ``exit`` back to the prior shell is the same kind of foreground
    handoff a su/sudo switch is, and races the next command the same way.
    Failures are wrapped in :class:`LoginProxyError` with context, like
    :func:`run_proxy`.
    """
    name = hop.proxy or "su"
    try:
        proxy = _get_proxy(hop)
        if proxy.undo is None:
            await io.send("exit\n")
        else:
            await proxy.undo(io, ProxyContext(target=hop, via=via, host_id=host_id))
        await _resync_shell(io, host_id, hop.login)
    except LoginProxyError:
        raise
    except Exception as e:
        raise LoginProxyError(
            f"{host_id}: login-proxy undo failed leaving {hop.login!r} via proxy {name!r}: {e}"
        ) from e


async def perform_switch(
    io: ProxyIO,
    creds: list[Cred],
    user: str,
    password: str | None,
    current_user: str,
    host_id: str,
) -> list[Cred]:
    """Become *user* from *current_user*; return the hops applied, in order.

    Semantics preserved from the pre-proxy ``switch_user``: ``user=""``
    targets root via bare ``su``; an explicit *password* overrides the
    creds entry; a user with no creds entry is an ad-hoc ``su`` target.
    A cred whose ``via`` differs from *current_user* first switches to the
    via account (recursively), so ``as_user`` can undo hop-by-hop.
    """
    cred = cred_for(creds, user) if user else None
    if cred is None:
        cred = Cred(login=user)
    if password is not None:
        cred = replace(cred, password=password)

    applied: list[Cred] = []
    if cred.via is not None and cred.via != current_user:
        applied += await perform_switch(io, creds, cred.via, None, current_user, host_id)
        current_user = applied[-1].login
    via = cred_for(creds, current_user) or Cred(login=current_user)
    await run_proxy(io, cred, via=via, host_id=host_id)
    applied.append(cred)
    return applied
