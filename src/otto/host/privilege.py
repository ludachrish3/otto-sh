"""
Posix privilege-elevation mixin.

Shared by the posix-shell hosts (:class:`~otto.host.unix_host.UnixHost`,
:class:`~otto.host.local_host.LocalHost`,
:class:`~otto.host.docker_host.DockerContainerHost`). Implements the
``_elevate`` hook (``run(sudo=True)``) plus ``switch_user`` / ``as_user``.

Which mechanism ``_elevate`` reaches for is RESOLVED, not assumed:
:attr:`~otto.host.userland.Userland.elevation` answers ``sudo`` / ``su`` /
``none``, because a BusyBox userland frequently has ``su`` and no ``sudo`` at
all. A host with no resolver wired up keeps today's ``sudo`` construction
exactly — see ``PosixPrivilege._elevate`` below for why that default, and for
where resolution has to happen given that ``_elevate`` is synchronous.

**sudo** auto-answers the password prompt through the
``Expect`` channel (``run(expects=[...])``) — the
response is written directly by the session machinery and is never logged.
The one-shot ``su -c`` form answers the same prompt the same way, using the
TARGET account's password rather than the current user's.

**su** (and any other registered login proxy) sends its password via
``send(..., log=LogMode.NEVER)`` so it is delivered to stdin without being
emitted to any sink (console, ``console.log``, or ``verbose.log``).
``switch_user``/``as_user`` route through
:func:`~otto.host.login_proxy.perform_switch`, which recursively resolves
``via``-chains and drives whichever proxy the target cred names (defaulting
to the built-in ``"su"``).

The mixin carries no fields and declares ``__slots__ = ()`` so it composes with
the ``@dataclass(slots=True)`` hosts. Password sourcing is host-specific:
``_sudo_password`` defaults to ``None`` (passwordless) and
:class:`~otto.host.unix_host.UnixHost` overrides it from ``creds``.
``_switch_creds`` defaults to ``self.creds`` (or ``[]`` when the host has no
``creds`` field), so ``switch_user``/``as_user`` targets resolve against the
same cred list ``_sudo_password`` does.
"""

import re
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from ..logger.mode import LogMode
from .command_frame import history_prefix
from .errors import UnsupportedOnUserlandError
from .host import DEFAULT_COMMAND_TIMEOUT
from .login_proxy import _SU_PROMPT, Cred, cred_for, perform_switch, run_undo

if TYPE_CHECKING:
    from .session import Expect
    from .userland import Userland

# Recognizable, locale-independent sudo prompt we match on.
_SUDO_PROMPT = "otto-sudo:"

# The login `su` with no argument targets, and so the cred whose password
# answers its prompt.
_SU_TARGET = "root"


class _HostProxyIO:
    """Adapts a host's ``send``/``expect`` to :class:`~otto.host.login_proxy.ProxyIO`.

    ``PosixPrivilege`` is a mixin — it has no ``send``/``expect`` of its own,
    only what the concrete host it's composed into supplies at runtime.
    Wrapping that access here (rather than in ``switch_user``/``as_user``
    directly) keeps the unavoidable ``ty: ignore`` confined to one small
    adapter instead of scattered through the elevation flow.
    """

    __slots__ = ("_host",)

    def __init__(self, host: "PosixPrivilege") -> None:
        self._host = host

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        await self._host.send(text, log=log)  # ty: ignore[unresolved-attribute]

    async def expect(
        self, pattern: str | re.Pattern[str], timeout: float = DEFAULT_COMMAND_TIMEOUT
    ) -> str:
        return await self._host.expect(pattern, timeout)  # ty: ignore[unresolved-attribute]


class PosixPrivilege:
    """Mixin: ``sudo``/``su`` elevation for posix-shell hosts."""

    __slots__ = ()

    def _sudo_password(self) -> str | None:
        """Password for ``sudo -S``, or None when sudo is passwordless here."""
        return None

    def _switch_creds(self) -> list[Cred]:
        """Creds used to resolve ``switch_user``/``as_user`` targets.

        Default ``getattr(self, "creds", [])`` — hosts with a ``creds`` field
        (:class:`~otto.host.unix_host.UnixHost`) get real cred-chain
        resolution for free; hosts without one (:class:`~otto.host.local_host.LocalHost`,
        :class:`~otto.host.docker_host.DockerContainerHost`) fall back to an
        empty list (ad-hoc, passwordless ``su`` targets).
        """
        return getattr(self, "creds", [])

    def _userland(self) -> "Userland | None":
        """Return this host's resolved userland capabilities, or None when it has none.

        Default None, in the same shape as :meth:`_sudo_password` and
        :meth:`_switch_creds`: the mixin carries no fields, so a host that
        acquires a :class:`~otto.host.userland.Userland` supplies it by
        overriding this. :class:`~otto.host.unix_host.UnixHost` does, building
        one per host instance; :class:`~otto.host.local_host.LocalHost` and
        :class:`~otto.host.docker_host.DockerContainerHost` do not, and keep
        this default deliberately — see :meth:`_elevate` for why ``None`` has
        to mean ``sudo`` there.
        """
        return None

    def _su_password(self) -> str | None:
        """Password for ``su``, or None when the switch needs none.

        NOT :meth:`_sudo_password`, and the difference is the point: ``sudo``
        authenticates as the user already logged in, while ``su`` authenticates
        as the account being ENTERED.

        A direct ``root`` lookup, which is narrower than
        ``switch_user``/``as_user`` and deliberately so. Those resolve a
        ``via`` chain and RUN each hop; this wraps one command in one ``su -c``
        and cannot replay hops at all. Where the two do agree is whose password
        answers the final prompt — :func:`~otto.host.login_proxy.perform_switch`
        looks the TARGET's cred up the same way and
        :func:`~otto.host.login_proxy._su_proxy` sends
        ``ctx.target.password`` — so a ``via`` on the root cred changes which
        hops run, never which password is typed, and this stays correct for the
        one hop it performs.

        The limitation that leaves is NARROWER than "any chain". A one-hop
        ``proxy="su"`` cred is precisely what ``su -c`` already does, so the
        common proxied shape simply works. What is unserved is a chain of more
        than one hop from the current user, or a hop driven by a CUSTOM
        registered proxy: there the one-shot form types root's password at a
        prompt the chain was supposed to reach first.

        Be exact about how that fails, because "loudly" would be too strong.
        ``su`` rejects the password and the wrapped command does not run, but
        it reports that as a non-zero (or timed-out) ``CommandResult``, not as
        a raise — so it is only as visible as the caller's own result check.
        :meth:`~otto.host.unix_host.UnixHost._soft_reboot` deliberately does
        not check (the issue-race disconnect makes its result untrustworthy),
        so on that path specifically it is NOT visible. It is still a wrong
        password rather than a wrong mechanism, which is why this is a
        documented edge and not an
        :exc:`~otto.host.errors.UnsupportedOnUserlandError`. Use ``as_user``
        for anything needing a real chain.
        """
        cred = cred_for(self._switch_creds(), _SU_TARGET)
        return cred.password if cred else None

    async def _prepare_elevation(self) -> None:
        """Resolve everything :meth:`_elevate` will read. Awaited by ``run(sudo=True)``.

        :meth:`_elevate` is synchronous and every
        :class:`~otto.host.userland.Userland` capability raises when read
        before ``resolve()`` has been awaited, so ``_elevate`` cannot resolve
        on demand and must not guess. This is the async boundary above it:
        :meth:`~otto.host.host.BaseHost.run` awaits this whenever ``sudo=True``,
        ahead of BOTH of its ``_apply_sudo`` call sites, which is what makes
        the read safe rather than lucky. ``resolve()`` is idempotent,
        concurrency-safe and never raises for a failed probe.

        **What it costs, stated rather than implied.** ``resolve()`` has no
        scoped form: it settles all six capabilities, so the first elevated
        command on a host issues up to eleven probes to read the one — settled by
        probes 1-2 — that :meth:`_elevate` looks at. Free thereafter only once
        every capability is SETTLED; a round that could not ask leaves nothing
        settled, and the next elevated call outside ``_RETRY_COOLDOWN_S`` (60s)
        pays again. Worst case is ``_RESOLVE_BUDGET_S`` (30s), it is not
        charged to the caller's ``timeout=``, and callers with a short timeout
        of their own are the ones that notice —
        ``UnixHost._soft_reboot``'s ``timeout=10.0`` becomes up to 40s of wall
        clock on a host that answers nothing. See
        :meth:`otto.host.userland.Userland.resolve` for why the round is whole.
        """
        userland = self._userland()
        if userland is not None:
            await userland.resolve()

    def _elevate(self, cmd: str) -> tuple[str, list["Expect"]]:
        """Wrap *cmd* in this host's elevation mechanism, with its password expect.

        The mechanism is a MEASUREMENT, not an assumption:
        :attr:`~otto.host.userland.Userland.elevation` decides it. BusyBox
        systems frequently ship ``su`` and no ``sudo`` at all, where the old
        hard-coded wrapper produced ``sudo: not found`` — a message about
        otto's own guess, attributed to whatever the caller was doing. See
        ``docs/superpowers/specs/2026-08-11-busybox-host-support-design.md``.

        **No resolver means sudo, deliberately.**
        :class:`~otto.host.unix_host.UnixHost` now builds one, so the lab's
        remote hosts answer from measurement; ``None`` remains the state of the
        hosts that reach otto's own machine —
        :class:`~otto.host.local_host.LocalHost` and
        :class:`~otto.host.docker_host.DockerContainerHost` — and there it must
        stay byte-identical to the pre-change behaviour. The asymmetry is what
        settles it: a host that has told otto nothing has told it nothing that
        contradicts sudo, and refusing instead would break privileged
        operations everywhere at once in exchange for no information. Pinned as
        an exact command-and-expects tuple by
        ``tests/unit/host/test_privilege.py``, because this one function is how
        the entire lab elevates.

        **``su`` quotes; ``sudo`` does not.** ``sudo`` takes an argv tail, so
        *cmd* is appended raw — a shape ``otto.host.daemon`` depends on and
        documents. ``su -c`` takes a single argument, so *cmd* is quoted into
        one word; unquoted, the calling shell would keep everything after the
        first word and a redirect or ``;`` would run unelevated instead of
        failing.

        The su prompt is matched, not set. ``sudo -p`` lets otto choose a
        locale-independent prompt; ``su`` has no counterpart, so the pattern is
        ``login_proxy``'s (shared, not copied) and a non-English locale will
        not match it — the same limitation ``switch_user`` already has there.

        Raises:
            UnsupportedOnUserlandError: the host resolved an elevation of
                ``none`` (neither sudo nor su present), or a spelling this
                build has never been taught. Loudly, at the call site: a
                fallback to sudo here would look like it worked.
            RuntimeError: a userland is wired up but was never resolved. Also
                deliberate — see :meth:`_prepare_elevation`. Catching it and
                answering "sudo" would reinstate exactly the guess this method
                exists to remove, on the path where guessing is most expensive.
        """
        userland = self._userland()
        elevation = "sudo" if userland is None else userland.elevation
        # Declared once, ahead of the branches: an `Expect` pattern is
        # `str | re.Pattern`, and annotating inside the first arm lets the
        # second infer the narrower `list[tuple[str, str]]` and fail to match
        # the return type.
        expects: "list[Expect]"
        if elevation == "sudo":
            pw = self._sudo_password()
            expects = [] if pw is None else [(_SUDO_PROMPT, f"{pw}\n")]
            return f"sudo -S -p '{_SUDO_PROMPT}' {cmd}", expects
        if elevation == "su":
            pw = self._su_password()
            expects = [] if pw is None else [(_SU_PROMPT, f"{pw}\n")]
            return f"su -c {shlex.quote(cmd)}", expects
        raise UnsupportedOnUserlandError(
            f"{getattr(self, 'name', '')}: this host's userland provides no elevation "
            f"mechanism (resolved {elevation!r}: no sudo, no su), so {cmd!r} cannot be "
            "run with sudo=True. Declare one in the host's userland_options if the "
            "probe is wrong, or run the command unelevated."
        )

    def _history_prefix(self) -> str:
        """Return this host's history-suppression payload ("" when history is kept).

        ``getattr`` rather than direct attribute access because this mixin is
        also inherited by hosts that declare no ``shell_history`` field
        (LocalHost, DockerContainerHost). ``True`` is the right fallback for
        them — assume history is recorded and leave the shell as we found it.
        """
        return history_prefix(
            getattr(self, "command_frame", None), getattr(self, "shell_history", True)
        )

    async def switch_user(self, user: str = "", password: str | None = None) -> None:
        """``su`` the persistent (default) session to *user* (default root).

        Performs the real switch (recursively hopping through any ``via``
        chain via :func:`~otto.host.login_proxy.perform_switch`) and then
        records the new user so ``current_user`` reflects it. Mutates
        session state — affects subsequent ``run`` calls until the user
        exits back.
        """
        applied = await perform_switch(
            _HostProxyIO(self),
            self._switch_creds(),
            user,
            password,
            self._session_mgr.current_user,  # ty: ignore[unresolved-attribute]
            getattr(self, "name", ""),
            self._history_prefix(),
        )
        self._session_mgr._set_current_user(applied[-1].login or "root")  # noqa: SLF001 — intra-package access to SessionManager._set_current_user for user elevation  # ty: ignore[unresolved-attribute]

    @asynccontextmanager
    async def as_user(
        self, user: str = "root", password: str | None = None
    ) -> AsyncIterator["PosixPrivilege"]:
        """Run a block as *user*, returning to the original user on exit.

            async with host.as_user("root"):
                await host.run("systemctl restart foo")

        Tracks ``current_user`` across the switch and restores the prior
        user when the block exits, undoing each applied hop in reverse
        (innermost first) so a multi-hop ``via`` chain unwinds correctly.
        """
        prev = self._session_mgr.current_user  # ty: ignore[unresolved-attribute]
        applied = await perform_switch(
            _HostProxyIO(self),
            self._switch_creds(),
            user,
            password,
            prev,
            getattr(self, "name", ""),
            self._history_prefix(),
        )
        self._session_mgr._set_current_user(applied[-1].login or "root")  # noqa: SLF001 — intra-package access to SessionManager._set_current_user for user elevation  # ty: ignore[unresolved-attribute]
        try:
            yield self
        finally:
            # The undo chain is a compensating action: an interrupt landing
            # while it runs must not strand the session as the switched user
            # (chaos spec: shielded compensating actions). compensate() holds
            # the cancellation until every hop is unwound (bounded by the
            # teardown deadline), then re-raises it.
            # Imported here, not at module scope: otto.lifecycle is only needed
            # once a compensating action actually runs, and a top-level import
            # drags it onto every CLI --help path (import-budget guard).
            from ..lifecycle import compensate

            await compensate(
                self._undo_switch(applied, prev),
                what=f"{getattr(self, 'name', '')}: as_user undo to {prev or 'login user'!r}",
            )

    async def _undo_switch(self, applied: "list[Cred]", prev: str) -> None:
        """Unwind *applied* innermost-first, restoring ``current_user`` to *prev*."""
        creds = self._switch_creds()
        for i, hop in enumerate(reversed(applied)):
            via_login = applied[-i - 2].login if i + 1 < len(applied) else prev
            # Look up the full via cred (password/params intact), mirroring
            # perform_switch's forward path — so a custom undo that needs
            # the via user's password sees it, and forward/undo stay symmetric.
            via = cred_for(creds, via_login) or Cred(login=via_login)
            await run_undo(
                _HostProxyIO(self), hop, via, getattr(self, "name", ""), self._history_prefix()
            )
        self._session_mgr._set_current_user(prev)  # noqa: SLF001 — intra-package access to SessionManager._set_current_user to restore prior user  # ty: ignore[unresolved-attribute]
