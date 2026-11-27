"""A real otto host that answers a fixture probe without a shell, for hostless probe tests.

A probe reads a command's output through :func:`otto.host.host.exec_own`, which
takes a host's private entry where its ``exec`` is otto's own and the public
``exec`` anywhere else. :class:`StockOwnHost` is a ``BaseHost`` whose ``exec``
is otto's own, so ``exec_own`` reaches its ``_exec_own`` (no terminal: the plain
answer); the public ``exec``, patched by :func:`patch_public_exec`, is the
terminal one (the *coloured* answer, as iproute2 gives on a terminal).
"""

from typing import Any

import pytest

from otto.host.host import BaseHost
from otto.host.unix_host import UnixHost
from otto.result import CommandResult
from otto.utils import Status


class StockOwnHost(UnixHost):
    """An ssh-like host: ``_exec_own`` answers plain, the public ``exec`` answers coloured."""

    id = "own1"

    def __init__(
        self, plain: dict[str, str] | None = None, coloured: dict[str, str] | None = None
    ) -> None:  # no super().__init__: no connection is ever made
        self.plain = plain or {}
        self.coloured = coloured or {}
        self.own_calls: list[tuple[str, dict[str, Any]]] = []
        self.public_calls: list[tuple[str, dict[str, Any]]] = []

    async def close(self) -> None:  # ty: ignore[invalid-method-override]
        """Nothing is open."""

    @property
    def commands(self) -> list[str]:
        """Every command either entry received, in order."""
        return [c for c, _kw in (*self.own_calls, *self.public_calls)]

    async def _exec_own(self, cmd: str, **kw: Any) -> CommandResult:  # ty: ignore[invalid-method-override]
        self.own_calls.append((cmd, kw))
        return CommandResult(
            status=Status.Success, value=self.plain.get(cmd, ""), command=cmd, retcode=0
        )


def patch_public_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``BaseHost.exec`` the terminal entry of a :class:`StockOwnHost`: it answers coloured."""

    async def _public(self: StockOwnHost, cmd: str, **kw: Any) -> CommandResult:
        self.public_calls.append((cmd, kw))
        value = self.coloured.get(cmd, self.plain.get(cmd, ""))
        return CommandResult(status=Status.Success, value=value, command=cmd, retcode=0)

    monkeypatch.setattr(BaseHost, "exec", _public)
