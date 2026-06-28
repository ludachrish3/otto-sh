"""
Posix privilege-elevation mixin.

Shared by the posix-shell hosts (:class:`~otto.host.unix_host.UnixHost`,
:class:`~otto.host.local_host.LocalHost`,
:class:`~otto.host.docker_host.DockerContainerHost`). Implements the
``_elevate`` hook (``run(sudo=True)``) plus ``switch_user`` / ``as_user``.

**sudo** auto-answers the password prompt through the
``Expect`` channel (``run(expects=[...])``) — the
response is written directly by the session machinery and is never logged.

**su** sends the password via ``send(..., log=False)`` so it is delivered to
stdin without being emitted to the console or log file.

The mixin carries no fields and declares ``__slots__ = ()`` so it composes with
the ``@dataclass(slots=True)`` hosts. Password sourcing is host-specific:
``_sudo_password`` / ``_user_password`` default to ``None`` (passwordless) and
:class:`~otto.host.unix_host.UnixHost` overrides them from ``creds``.
"""

from __future__ import annotations

import shlex
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import Expect

# Recognizable, locale-independent sudo prompt we match on.
_SUDO_PROMPT = "otto-sudo:"


async def _perform_su(
    send: "Callable[..., Awaitable[None]]",
    expect: "Callable[..., Awaitable[str]]",
    user: str,
    password: str | None,
    user_password: "Callable[[str], str | None]",
) -> str:
    """Run the ``su`` exchange against a session's ``send``/``expect`` and
    return the resolved target user. Does **no** ``current_user`` bookkeeping —
    the caller stamps the session it elevated. Shared by
    :meth:`PosixPrivilege.switch_user` (default session) and
    :meth:`~otto.host.session.HostSession.switch_user` (named session).
    """
    target = user or "root"
    cmd = "su" if not user else f"su {shlex.quote(user)}"
    pw = password if password is not None else user_password(target)
    await send(cmd + "\n")
    if pw is not None:
        await expect(r"[Pp]assword:")
        await send(pw + "\n", log=False)
    return target


class PosixPrivilege:
    """Mixin: ``sudo``/``su`` elevation for posix-shell hosts."""

    __slots__ = ()

    def _sudo_password(self) -> str | None:
        """Password for ``sudo -S``, or None when sudo is passwordless here."""
        return None

    def _user_password(self, user: str) -> str | None:
        """Password for ``su <user>``, or None when none is known."""
        return None

    def _elevate(self, cmd: str) -> tuple[str, list["Expect"]]:
        wrapped = f"sudo -S -p '{_SUDO_PROMPT}' {cmd}"
        pw = self._sudo_password()
        expects: list[Expect] = [] if pw is None else [(_SUDO_PROMPT, f"{pw}\n")]
        return wrapped, expects

    async def switch_user(self, user: str = "", password: str | None = None) -> None:
        """``su`` the persistent (default) session to *user* (default root).

        Performs the real ``su`` and then records the new user so
        ``current_user`` reflects it. Mutates session state — affects
        subsequent ``run`` calls until the user exits back.
        """
        target = await _perform_su(
            self.send,  # ty: ignore[unresolved-attribute]
            self.expect,  # ty: ignore[unresolved-attribute]
            user,
            password,
            self._user_password,
        )
        self._session_mgr._set_current_user(target)  # ty: ignore[unresolved-attribute]

    @asynccontextmanager
    async def as_user(
        self, user: str = "root", password: str | None = None
    ) -> AsyncIterator["PosixPrivilege"]:
        """Run a block as *user*, returning to the original user on exit.

            async with host.as_user("root"):
                await host.run("systemctl restart foo")

        Tracks ``current_user`` across the switch and restores the prior
        user when the block exits.
        """
        prev = self._session_mgr.current_user  # ty: ignore[unresolved-attribute]
        await self.switch_user(user, password)
        try:
            yield self
        finally:
            await self.send("exit\n")  # ty: ignore[unresolved-attribute]
            self._session_mgr._set_current_user(prev)  # ty: ignore[unresolved-attribute]
