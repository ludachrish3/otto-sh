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

import re
import shlex
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from ..logger.mode import LogMode
from ..registry import Registry, caller_module
from .command_frame import BashFrame, SessionMarkers
from .shell_liveness import confirm_live


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


# Post-transition resync. A su/sudo/exit hop is a foreground-process handoff on
# the pty: su/login/sudo flush pending terminal input across the privilege
# boundary (a typeahead-attack defense), so the first probe written back-to-back
# with the transition is silently dropped (verified 40/40 on the live bed).
# _RESYNC_SETTLE absorbs that flush; confirm_live then resends an echo-proof
# exit-code probe (BashFrame.recover) on a short interval until the shell answers
# with the digit form or _RESYNC_DEADLINE passes — decoupling per-probe wait from
# the overall budget so a slow round-trip under load no longer exhausts a fixed
# attempt count (the 3.13 flake). See otto.host.shell_liveness.confirm_live.
_RESYNC_SETTLE = 0.3
_RESYNC_PROBE_TIMEOUT = 0.5
_RESYNC_DEADLINE = 10.0
_RESYNC_FRAME = BashFrame()


async def _resync_shell(io: ProxyIO, host_id: str, hop_login: str) -> None:
    """Resync with the shell after a su/sudo/exit transition.

    Drives the shared :func:`~otto.host.shell_liveness.confirm_live` loop with
    a fresh :class:`~otto.host.command_frame.SessionMarkers` per probe and
    :class:`~otto.host.command_frame.BashFrame`'s echo-proof exit-code probe
    (``recover``/``recover_pattern``): the probe bakes ``$?`` into its own
    marker, so only a real shell — never an echo of the probe text itself —
    can produce the digit form the pattern requires. That holds across both
    echo modes a login proxy can run in (the echo-ON ``interact --as-user``
    bridge and the echo-OFF framed ``switch_user``/``as_user`` path), which is
    why no lookbehind or echo-mode discrimination is needed here.

    Raises :class:`LoginProxyError` if the shell never resyncs before
    :data:`_RESYNC_DEADLINE` — the caller (:func:`run_proxy`/:func:`run_undo`)
    wraps this with hop context like any other proxy-step failure.
    """
    confirmed = await confirm_live(
        io.send,
        io.expect,
        _RESYNC_FRAME.recover,
        _RESYNC_FRAME.recover_pattern,
        lambda: SessionMarkers.for_session(uuid.uuid4().hex[:12]),
        settle=_RESYNC_SETTLE,
        probe_timeout=_RESYNC_PROBE_TIMEOUT,
        deadline=_RESYNC_DEADLINE,
    )
    if not confirmed:
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
