"""
Posix privilege-elevation mixin.

Shared by the posix-shell hosts (:class:`~otto.host.unix_host.UnixHost`,
:class:`~otto.host.local_host.LocalHost`,
:class:`~otto.host.docker_host.DockerContainerHost`). Implements the
``_elevate`` hook (``run(sudo=True)``) plus ``switch_user`` / ``as_user``.

**sudo** auto-answers the password prompt through the
``Expect`` channel (``run(expects=[...])``) — the
response is written directly by the session machinery and is never logged.

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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from ..logger.mode import LogMode
from .login_proxy import Cred, cred_for, perform_switch, run_undo

if TYPE_CHECKING:
    from .session import Expect

# Recognizable, locale-independent sudo prompt we match on.
_SUDO_PROMPT = "otto-sudo:"


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

    async def expect(self, pattern: str | re.Pattern[str], timeout: float = 10.0) -> str:
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

    def _elevate(self, cmd: str) -> tuple[str, list["Expect"]]:
        wrapped = f"sudo -S -p '{_SUDO_PROMPT}' {cmd}"
        pw = self._sudo_password()
        expects: "list[Expect]" = [] if pw is None else [(_SUDO_PROMPT, f"{pw}\n")]
        return wrapped, expects

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
        )
        self._session_mgr._set_current_user(applied[-1].login or "root")  # noqa: SLF001 — intra-package access to SessionManager._set_current_user for user elevation  # ty: ignore[unresolved-attribute]
        try:
            yield self
        finally:
            creds = self._switch_creds()
            for i, hop in enumerate(reversed(applied)):
                via_login = applied[-i - 2].login if i + 1 < len(applied) else prev
                # Look up the full via cred (password/params intact), mirroring
                # perform_switch's forward path — so a custom undo that needs
                # the via user's password sees it, and forward/undo stay symmetric.
                via = cred_for(creds, via_login) or Cred(login=via_login)
                await run_undo(_HostProxyIO(self), hop, via, getattr(self, "name", ""))
            self._session_mgr._set_current_user(prev)  # noqa: SLF001 — intra-package access to SessionManager._set_current_user to restore prior user  # ty: ignore[unresolved-attribute]
