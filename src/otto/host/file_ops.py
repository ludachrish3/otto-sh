"""
Posix remote file-management mixin.

Mimics the unix CLI (``test``/``ls``/``mkdir``/``rm``/``cp``/``mv``/``cat``)
over the host's shell via :meth:`~otto.host.host.Host.oneshot`. These manage
files *already on / between locations on* the host — complementary to
``put``/``get`` (which move files local↔remote). Shared by the posix-shell hosts
(:class:`~otto.host.unix_host.UnixHost`,
:class:`~otto.host.local_host.LocalHost`,
:class:`~otto.host.docker_host.DockerContainerHost`).

These are a *family capability*, not part of the universal ``Host`` Protocol —
an embedded host implements only the subset its filesystem supports.
"""

import base64
import shlex
from pathlib import Path
from typing import Annotated

from ..logger.mode import LogMode
from ..utils import Arg, Status, cli_exposed


class PosixFileOps:
    """Mixin: unix-CLI-style remote file management for posix-shell hosts."""

    __slots__ = ()

    @staticmethod
    def _q(path: "str | Path") -> str:
        return shlex.quote(str(path))

    @cli_exposed(output_dir=False)
    async def exists(self, path: "str | Path") -> bool:
        """Return True when *path* exists on the host (``test -e``)."""
        result = await self.oneshot(f"test -e {self._q(path)}")  # ty: ignore[unresolved-attribute]
        return result.status.is_ok

    @cli_exposed(output_dir=False)
    async def ls(self, path: "Annotated[str | Path, Arg()]" = ".", all: bool = False) -> list[str]:  # noqa: A002 — CLI-exposed param name, maps to --all flag
        """List entry names in *path* (``ls -1``; *all* adds ``-A`` for dotfiles)."""
        flags = "-1A" if all else "-1"
        result = await self.oneshot(f"ls {flags} {self._q(path)}")  # ty: ignore[unresolved-attribute]
        if not result.status.is_ok:
            return []
        return [line for line in result.output.splitlines() if line]

    @cli_exposed
    async def mkdir(self, path: "str | Path", parents: bool = True) -> tuple[Status, str]:
        """Create directory *path* (``mkdir``; *parents* adds ``-p``)."""
        flag = "-p " if parents else ""
        result = await self.oneshot(f"mkdir {flag}{self._q(path)}")  # ty: ignore[unresolved-attribute]
        return result.status, result.output

    @cli_exposed
    async def rm(
        self, path: "str | Path", recursive: bool = False, force: bool = False
    ) -> tuple[Status, str]:
        """Remove *path* (``rm``; *recursive* → ``-r``, *force* → ``-f``)."""
        flags = "".join(f for f, on in (("r", recursive), ("f", force)) if on)
        opt = f"-{flags} " if flags else ""
        result = await self.oneshot(f"rm {opt}{self._q(path)}")  # ty: ignore[unresolved-attribute]
        return result.status, result.output

    @cli_exposed
    async def cp(
        self, src: "str | Path", dst: "str | Path", recursive: bool = False
    ) -> tuple[Status, str]:
        """Copy *src* to *dst* on the host (``cp``; *recursive* → ``-r``)."""
        opt = "-r " if recursive else ""
        result = await self.oneshot(  # ty: ignore[unresolved-attribute]
            f"cp {opt}{self._q(src)} {self._q(dst)}"
        )
        return result.status, result.output

    @cli_exposed
    async def mv(self, src: "str | Path", dst: "str | Path") -> tuple[Status, str]:
        """Move/rename *src* to *dst* on the host (``mv``)."""
        result = await self.oneshot(  # ty: ignore[unresolved-attribute]
            f"mv {self._q(src)} {self._q(dst)}"
        )
        return result.status, result.output

    @cli_exposed(output_dir=False)
    async def read_file(self, path: "str | Path") -> str:
        """Return the text contents of *path*.

        Reads via ``base64`` — the exact inverse of :meth:`write_file`'s base64
        transport — so content round-trips byte-exact regardless of trailing
        newlines, trailing whitespace, or shell metacharacters (``oneshot``'s
        per-line ``rstrip``/rejoin would otherwise corrupt them). Raises
        :class:`FileNotFoundError` when the read fails (missing path, permissions).
        """
        result = await self.oneshot(f"base64 {self._q(path)}")  # ty: ignore[unresolved-attribute]
        if not result.status.is_ok:
            raise FileNotFoundError(f"read_file({path!r}) failed: {result.output}")
        return base64.b64decode(result.output).decode()

    @cli_exposed
    async def write_file(
        self, path: "str | Path", data: str, append: bool = False
    ) -> tuple[Status, str]:
        """Write *data* to *path* (overwrite, or append).

        The payload is base64-encoded on the wire, so arbitrary content
        (newlines, quotes, shell metacharacters) is transferred safely. Sent
        with ``log=LogMode.QUIET`` so large bodies stay out of the console
        (still recorded in verbose.log).
        """
        encoded = base64.b64encode(data.encode()).decode()
        redirect = ">>" if append else ">"
        cmd = f"echo {encoded} | base64 -d {redirect} {self._q(path)}"
        result = await self.oneshot(cmd, log=LogMode.QUIET)  # ty: ignore[unresolved-attribute]
        return result.status, result.output
