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

from ..errors import OttoError
from ..logger.mode import LogMode
from ..registry import Registry, caller_module
from .command_frame import BashFrame, SessionMarkers
from .host import DEFAULT_COMMAND_TIMEOUT
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
    """Free-form data handed to the proxy callable (host-specific knobs).

    Opaque to otto except for the one key the built-in ``"su"`` proxy reads:
    ``login_shell`` (default True) chooses between ``su - <login>`` and the
    environment-inheriting ``su <login>``. A custom proxy is free to give that
    key its own meaning; nothing outside the proxy that receives these params
    interprets them.
    """


@runtime_checkable
class ProxyIO(Protocol):
    """Minimal I/O handle a proxy drives.

    Satisfied by hosts, ``HostSession`` instances, the raw-session adapter
    used at session establishment, and the interact bridge adapter.
    """

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        """Send text to the proxy IO."""
        ...

    async def expect(
        self, pattern: str | re.Pattern[str], timeout: float = DEFAULT_COMMAND_TIMEOUT
    ) -> str:
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
    """A registered proxy: the steps, an optional reversal, an optional prompt."""

    fn: LoginProxyFn
    undo: LoginProxyFn | None = None

    prompt: str | None = None
    """Credential prompt this proxy's mechanism may raise, or None.

    OPT-IN, and that is a safety property rather than a preference. Declaring
    a prompt authorizes the engine to type this cred's password at whatever
    matches it during the post-hop resync (``_resync_shell``). A proxy
    that answers its own prompt must NOT declare one: the prompt text it
    already consumed would still be sitting in the buffer, the engine would
    answer it a second time, and by then ``su`` has moved on — so the password
    would be typed into the new shell AS A COMMAND, landing in its history and
    in ``ps``. Silence is therefore the default, and the built-in ``"su"``
    proxy (which deliberately does not answer for itself) is what opts in.
    """


class LoginProxyError(OttoError, ConnectionError):
    """A proxy step failed or a chain could not be resolved."""


LOGIN_PROXIES: Registry[LoginProxy] = Registry(
    "login proxy", register_hint="otto.register_login_proxy()"
)


def register_login_proxy(
    name: str,
    fn: LoginProxyFn,
    *,
    undo: LoginProxyFn | None = None,
    prompt: str | None = None,
    overwrite: bool = False,
) -> None:
    """Register a login proxy under *name* (see :data:`LoginProxyFn`).

    *undo* reverses the steps for ``as_user`` restore; None means the
    default reversal (send ``exit``), correct for any su/sudo-style nested
    shell.

    *prompt* declares a credential prompt the engine may answer on this
    proxy's behalf, and defaults to None — see :attr:`LoginProxy.prompt` for
    why not declaring one is the safe default, and why a proxy that answers
    its own prompt must leave this unset.
    """
    LOGIN_PROXIES.register(
        name, LoginProxy(fn, undo, prompt), overwrite=overwrite, origin=caller_module()
    )


# How `su` spells the prompt it asks for credentials at. Not locale-
# independent, and there is no `su` flag that would make it so — sudo's `-p`
# has no counterpart — so both capitals are matched and nothing more. Shared
# with `otto.host.privilege.PosixPrivilege._elevate`, which auto-answers the
# same prompt through the expect channel when a host's resolved elevation is
# `su`: one fact, one spelling, so the interactive switch and the one-shot
# elevation cannot drift apart.
_SU_PROMPT = r"[Pp]assword:"

# The login a bare, argument-less `su` targets -- and so both the cred whose
# password answers its prompt and the identity its resync must see. Defined
# here rather than in `privilege.py` because it is a fact about `su`, and
# `privilege` imports this module (never the reverse).
_SU_TARGET = "root"


def _may_challenge(hop: Cred, via: Cred) -> bool:
    """Whether *hop* can raise a credential prompt when reached from *via*.

    Whether ``su`` challenges is a property of WHO IS ASKING, not of the cred:
    root is not authenticated (``pam_rootok`` on util-linux, an explicit
    ``getuid() != 0`` guard in BusyBox's applet), everyone else is. otto knows
    which case it is in without watching for anything, because the previous
    hop's resync ASSERTED the identity it left the shell in -- so the answer is
    already in hand and costs no clock -- when it is known at all: an
    unidentified caller is NOT assumed to be root (see the guard below).

    That matters because the watch is not free. Measured on the bed, a hop that
    waits out ``_PROMPT_WATCH`` for a prompt that never comes costs ~0.49s;
    skipping it where none can come takes a root-to-service-account switch from
    1.44s to 0.96s, while the challenged path is unchanged (0.63s either way,
    since a real prompt ends the watch in milliseconds).

    Returning False also DISARMS answering for that hop rather than merely
    skipping the wait, and that is the safety half. If this prediction is ever
    wrong -- a host whose PAM stack drops ``pam_rootok`` -- otto must not type a
    password at a shell whose state it has mispredicted: by then ``su`` may have
    flushed the probe and moved on, and the password would land in the parent
    shell as a command, in its history and in ``ps``. So a wrong prediction
    surfaces as the resync's identity error, which is loud, safe and carries the
    override. Set ``params={"expect_prompt": True}`` on the cred to force the
    watch back on for such a host.
    """
    forced = hop.params.get("expect_prompt")
    if forced is not None:
        return bool(forced)
    # POSITIVE identification of root only. An empty via login means otto does
    # not know who it is (a session whose identity was never established), and
    # `via.login or _SU_TARGET` would quietly read that as "we are root" -- the
    # unsafe direction, since it disarms answering for a hop that may well be
    # challenged. Unknown therefore falls through to True, which costs the
    # watch and nothing else.
    return via.login != _SU_TARGET


async def _su_proxy(io: ProxyIO, ctx: ProxyContext) -> None:
    """Built-in single-step ``su`` exchange (the pre-proxy default).

    Sends the LOGIN-SHELL form, ``su - <login>``. A proxy exists to *become*
    an account, and the accounts that need one are service accounts whose
    whole reason for existing is their own environment: ``su`` without ``-``
    hands them the caller's ``PATH``, ``HOME`` and ``USER`` and never sources
    their profile, so the target's own tooling is frequently not on ``PATH``
    at all.

    Spelled ``-``, never ``-l``, and that is a PORTABILITY choice rather than
    a stylistic one. The oldest BusyBox otto keeps a conformance artifact for
    (1.16.1) documents ``su [OPTIONS] [-] [USERNAME]`` and does not offer
    ``-l`` at all; every later BusyBox and util-linux accept both spellings,
    so ``-`` is the only one that reaches all of them. BusyBox is what makes
    that matter: such a userland frequently has no ``sudo`` for otto to fall
    back on (see :attr:`~otto.host.userland.Userland.elevation`).

    Two consequences worth naming, because they are real behavior and not
    side effects: the new shell starts in the TARGET's home directory rather
    than the caller's cwd, and the environment is reset rather than
    inherited. A cred that needs the old inheriting form sets
    ``params={"login_shell": False}`` (see :attr:`Cred.params`) rather than
    registering a whole custom proxy — the one knob this proxy interprets.

    Not changed by any of that: the password prompt (``su`` asks the same way
    either way, see :data:`_SU_PROMPT`), the target's shell (both forms run
    the account's passwd shell, so a restricted-shell target still needs a
    custom proxy), and the default ``exit`` undo, which leaves a login shell
    exactly as it leaves a non-login one.

    **This function does not wait for the password prompt, and that is the
    point.** Whether ``su`` challenges is a property of WHO IS ASKING, not of
    the cred: the same ``mysql`` entry prompts when reached from an
    unprivileged account and stays silent when reached from ``root``, and a
    cred cannot know which hop it is on. A proxy that waited whenever a
    password was configured would therefore stall every switch the host was
    willing to perform for free — which it did, for the whole of a command
    timeout, before this was moved.

    So the prompt is answered where otto is already listening:
    ``_resync_shell``, which every hop runs anyway, watches for it before
    its first probe and recognizes it in every probe after that. The password
    is sent only when it is wanted, sent exactly once when it is, and the cred
    that has none gets told which account asked instead of having otto's own
    probes spend authentication attempts against it.

    The watch is not free -- waiting one out costs ``_PROMPT_WATCH`` -- so it
    is armed only for hops that can actually be challenged, which
    ``_may_challenge`` decides from the caller's identity rather than by
    watching. Measured on the bed, that takes a root-to-service-account switch
    from 1.44s to 1.25s and leaves a challenged switch at 0.63s either way,
    since a real prompt ends the watch in milliseconds.

    What is deliberately NOT done is shortening the watch itself. A prompt
    arriving just after a too-short one would be met by otto's first probe, and
    a probe typed at a live password prompt IS an authentication attempt --
    exactly the lockout this design exists to avoid. The saving comes from not
    waiting where nothing can arrive, never from waiting too briefly where
    something can.
    """
    login = ctx.target.login
    dash = " -" if bool(ctx.target.params.get("login_shell", True)) else ""
    cmd = f"su{dash}" if not login else f"su{dash} {shlex.quote(login)}"
    await io.send(cmd + "\n")


register_login_proxy("su", _su_proxy, prompt=_SU_PROMPT)


def _default_direct(creds: list[Cred]) -> Cred | None:
    return next((c for c in creds if c.proxy is None), None)


def cred_for(creds: list[Cred], login: str) -> Cred | None:
    """Look up a cred entry by login (None when absent)."""
    return next((c for c in creds if c.login == login), None)


# DEBT(no-tuple-return): target credential plus hop chain.
# ast-grep-ignore: no-tuple-return
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

# When a proxy declares a credential prompt, the settle doubles as the watch for
# it — and THAT job wants a longer window than absorbing a tty flush does. A
# prompt that arrives after the watch closes is what the first probe gets typed
# into: one real failed authentication against the account, and then an identity
# mismatch that blames a rejected password for a password otto never sent.
#
# So the window is its own number rather than a second use of the settle: the
# two answer different questions, and tuning the flush window should not
# silently retune how long otto will wait for a credential prompt.
#
# The value is MEASURED, not guessed. Timing `su - test` to its prompt 40x on
# a live bed VM: min 2.2ms, median 2.7ms, p95 7.6ms, max 10.2ms — a prompt is
# effectively immediate, because PAM is local and nothing is on the wire. 0.5s
# is ~50x the slowest observed, which is headroom for a loaded host rather
# than an estimate of one.
#
# It is deliberately not larger. This is paid IN FULL by exactly the host that
# has a password configured and is not asked for it, on every hop, forever —
# so a generous-sounding multi-second window is a permanent tax levied to
# insure against something measured in single-digit milliseconds. It is paid
# only by a proxy that declares a prompt, and only until the prompt appears: a
# host that does challenge waits ~3ms here, not 500.
# How long to wait for a credential prompt that IS expected -- and it is only
# ever waited for when `_may_challenge` says one is coming, so this is time
# spent on the event otto is actually waiting for rather than a poll.
#
# Generous ON PURPOSE, and the reason is a race rather than politeness. Give up
# early and the resync's first probe goes out while `su` is still starting up;
# a slow host then raises its prompt underneath that probe, and whether the
# probe is DISCARDED (su flushes typeahead before reading, the usual case) or
# EATEN AS THE PASSWORD is a matter of microseconds. In the eaten case `su`
# reports an auth failure and EXITS, so the prompt otto reads next is stale and
# the shell behind it is the caller's -- and answering it would type the
# password into that shell as a command, into its history and into `ps`. The
# identity assertion catches the failed switch, but catching it does not
# un-send the password. Waiting for a prompt that is genuinely coming costs
# nothing when it arrives promptly, which is the normal case.
#
# A host where a NON-root caller is somehow not challenged (a `pam_wheel`
# trust, a NOPASSWD su rule) is the only one that pays this in full, once per
# hop, and it opts out with `params={"expect_prompt": False}`.
_PROMPT_WATCH = 5.0
_RESYNC_FRAME = BashFrame()


# The resync probe carries an IDENTITY assertion as well as a liveness one:
# `echo "<recover>$?__$(id -un)__"`. Liveness alone cannot tell "the switch
# worked" from "the switch failed and the shell I am talking to is the one I
# started in" -- a rejected `su` password prints `Authentication failure` and
# EXITS back to the via shell, which then answers the probe perfectly well. So
# every hop proves WHO it is talking to, not merely that someone is there.
# `id -un` is portable to every userland otto drives (measured on BusyBox
# 1.16.1/1.21.1/1.35.0 and util-linux), and `$(...)` keeps the probe
# echo-proof exactly as `$?` does: a literal echo cannot expand it.
#
# A userland where it is somehow absent expands `$(id -un)` to NOTHING, so the
# reply still matches (the capture is non-greedy) and arrives with an empty
# identity, which the check below skips. That degradation is deliberate and in
# this direction on purpose: a shell that cannot say who it is leaves otto
# exactly as well off as it was before identity was proven at all, whereas
# treating "no answer" as "wrong answer" would refuse every switch on a
# userland the probe simply cannot interrogate.
_IDENTITY_TAIL = r"(\d+)__(.*?)__"


def _identity_probe(m: SessionMarkers, history_prefix: str) -> str:
    """Render the liveness+identity probe for markers *m*."""
    return f'{history_prefix}echo "{m.recover}$?__$(id -un)__"\n'


def _identity_pattern(m: SessionMarkers) -> re.Pattern[str]:
    """Match the probe's reply, capturing the exit code and the reporting user.

    The user has to be INSIDE the match: a real ``expect`` returns text only
    up to the end of what it matched, so a pattern that stopped at the exit
    code would hand back a string with the identity still unread.
    """
    return re.compile(re.escape(m.recover) + _IDENTITY_TAIL)


async def _resync_shell(
    io: ProxyIO,
    host_id: str,
    hop_login: str,
    history_prefix: str = "",
    *,
    prompt: str | None = None,
    password: str | None = None,
    expect_user: str | None = None,
) -> None:
    """Resync with the shell after a su/sudo/exit transition, and prove who it is.

    *history_prefix* is the caller's already-resolved history-suppression
    payload (:func:`~otto.host.command_frame.history_prefix`), or ``""`` to
    leave the new shell's history alone. It rides the probe rather than
    following it: a ``su`` starts a fresh shell that re-reads rc files and so
    resets ``HISTFILE``, and anything sent after the resync would mean otto's
    own probes had already been recorded in the elevated user's history.

    Drives the shared :func:`~otto.host.shell_liveness.confirm_live` loop with
    a fresh :class:`~otto.host.command_frame.SessionMarkers` per probe. The
    probe bakes ``$?`` into its own marker, so only a real shell -- never an
    echo of the probe text itself -- can produce the digit form the pattern
    requires; that holds in both echo modes a login proxy runs in (the echo-ON
    ``login --user`` bridge and the echo-OFF ``switch_user``/``as_user``
    path), which is why no echo-mode discrimination is needed.

    **Everything the prompt needs is done in the `expect` ADAPTER below**
    rather than by widening ``confirm_live``, which the session handshake and
    the post-timeout recovery also use: none of this is their concern, and a
    shared loop that could type a credential is a worse object than a local
    wrapper that can. The adapter gets three jobs, and each exists because of
    a specific way this goes wrong:

    * *answer a prompt, once.* Only when *prompt* is declared by the proxy
      (:attr:`LoginProxy.prompt`) -- never for a proxy that answers its own.
    * *re-settle after answering.* The tty flush this settle exists to absorb
      happens when the privilege boundary is crossed, and with a password that
      is AFTER the password is accepted, not after the `su` line. Probing
      immediately would feed the first probe straight into the flush.
    * *check the identity.* See the note above :func:`_identity_probe`.

    Raises :class:`LoginProxyError` if the shell never resyncs before
    :data:`_RESYNC_DEADLINE`, if a declared prompt appears with no password to
    answer it, or if the shell that answers is not *expect_user*.
    """
    answered = False

    async def answer() -> None:
        nonlocal answered
        if password is None:
            raise LoginProxyError(
                f"{host_id}: becoming {hop_login!r} asked for a password and this cred "
                f"has none. Give the {hop_login!r} cred a 'password', or use a login "
                f"proxy that does not need one — otto will not answer a credential "
                f"prompt with its own probes."
            )
        if answered:
            raise LoginProxyError(
                f"{host_id}: {hop_login!r} asked for a password again after one was "
                f"sent — the configured password for {hop_login!r} was not accepted. "
                f"otto will not retry it: a second attempt cannot succeed where the "
                f"first failed, and would spend another authentication attempt."
            )
        answered = True
        await io.send(password + "\n", log=LogMode.NEVER)

    prompt_re = re.compile(prompt) if prompt else None

    async def expect(pattern: re.Pattern[str], timeout: float) -> str:
        wanted = (
            re.compile(
                f"(?:{pattern.pattern})|(?:{prompt_re.pattern})", pattern.flags | prompt_re.flags
            )
            if prompt_re
            else pattern
        )
        matched = await io.expect(wanted, timeout)
        hit = pattern.search(matched)
        if hit is None:
            # Identify the prompt POSITIVELY. "Did not match the probe" is not
            # the same as "is a credential prompt", and treating it as one
            # would type a password at whatever happened to be in the buffer.
            if prompt_re is None or not prompt_re.search(matched):
                raise asyncio.TimeoutError("neither the probe reply nor a prompt")
            # Answer it, let the transition settle, then report a timeout so
            # confirm_live retries with a FRESH marker rather than reusing one
            # this reply already consumed.
            await answer()
            await asyncio.sleep(_RESYNC_SETTLE)
            raise asyncio.TimeoutError("credential prompt answered; probe again")
        seen_user = hit.group(2)
        if expect_user and seen_user and seen_user != expect_user:
            raise LoginProxyError(
                f"{host_id}: after becoming {hop_login!r} the shell answered as "
                f"{seen_user!r}, not {expect_user!r} — the transition did not take. A "
                f"rejected password is the usual cause: `su` reports the failure and "
                f"exits back to the calling shell, which then answers otto's probe "
                f"perfectly well, so liveness alone cannot see it."
            )
        return matched

    # SPEND THE SETTLE WATCHING, not sleeping -- but only when this proxy
    # declared a prompt. The settle exists to let the transition's own output
    # land before otto writes anything, and a credential prompt IS that
    # output; waiting for it here is what stops the first probe being typed
    # into a live password prompt, where it would be consumed as a failed
    # authentication. When a prompt does come, the privilege boundary is
    # crossed AFTER the password is accepted, so the flush the settle exists
    # for has not happened yet -- hence the second settle before probing.
    settle = _RESYNC_SETTLE
    if prompt_re is not None:
        settle = 0.0
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            seen = await io.expect(prompt_re, _PROMPT_WATCH)
            if prompt_re.search(seen):
                await answer()
                await asyncio.sleep(_RESYNC_SETTLE)

    confirmed = await confirm_live(
        io.send,
        expect,
        lambda m: _identity_probe(m, history_prefix),
        _identity_pattern,
        lambda: SessionMarkers.for_session(uuid.uuid4().hex[:12]),
        settle=settle,
        probe_timeout=_RESYNC_PROBE_TIMEOUT,
        deadline=_RESYNC_DEADLINE,
    )
    if not confirmed:
        raise LoginProxyError(
            f"{host_id}: shell did not resync after a login-proxy transition "
            f"({hop_login!r}) — su/sudo/exit flushed the next command"
        )


async def run_proxy(
    io: ProxyIO, hop: Cred, via: Cred, host_id: str, history_prefix: str = ""
) -> None:
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
        await _resync_shell(
            io,
            host_id,
            hop.login,
            history_prefix,
            prompt=proxy.prompt if _may_challenge(hop, via) else None,
            password=hop.password,
            expect_user=hop.login or _SU_TARGET,
        )
    except LoginProxyError:
        raise
    except Exception as e:
        raise LoginProxyError(
            f"{host_id}: login proxy failed becoming {hop.login!r} via proxy {name!r}: {e}"
        ) from e


async def run_undo(
    io: ProxyIO, hop: Cred, via: Cred, host_id: str, history_prefix: str = ""
) -> None:
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
        # No `prompt` on the undo: `exit` asks for nothing, so there is no
        # credential for the engine to offer and nothing it should answer.
        # The identity check still applies -- an undo that silently failed to
        # leave the elevated shell is exactly as wrong as a switch that never
        # entered one, and strands the session as the wrong user.
        #
        # But it applies only when the shell we are returning TO has a name.
        # An empty `via.login` means "whoever the session logged in as", which
        # otto does not necessarily know; the forward path may default a
        # nameless target to root (bare `su` really does go there), yet the
        # same default backwards would assert we land on root after leaving
        # root -- failing every undo out of a session whose login user was
        # never recorded. An unknown identity is not checkable, so it is not
        # checked.
        await _resync_shell(io, host_id, hop.login, history_prefix, expect_user=via.login)
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
    history_prefix: str = "",
) -> list[Cred]:
    """Become *user* from *current_user*; return the hops applied, in order.

    Semantics preserved from the pre-proxy ``switch_user``: ``user=""``
    targets root via an argument-less ``su``; an explicit *password*
    overrides the creds entry; a user with no creds entry is an ad-hoc ``su``
    target. Both of those go through the built-in ``"su"`` proxy like any
    other hop, so both get its login shell (``su -``) — an ad-hoc target has
    no ``params`` to opt out with, which is the one place the default is not
    overridable.
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
        applied += await perform_switch(
            io, creds, cred.via, None, current_user, host_id, history_prefix
        )
        current_user = applied[-1].login
    via = cred_for(creds, current_user) or Cred(login=current_user)
    await run_proxy(io, cred, via=via, host_id=host_id, history_prefix=history_prefix)
    applied.append(cred)
    return applied
