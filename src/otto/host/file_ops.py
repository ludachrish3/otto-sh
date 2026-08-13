"""
Posix remote file-management mixin.

Mimics the unix CLI (``test``/``ls``/``mkdir``/``rm``/``cp``/``mv``/``cat``)
over the host's shell via :meth:`~otto.host.host.Host.exec`. These manage
files *already on / between locations on* the host — complementary to
``put``/``get`` (which move files local↔remote). Shared by the posix-shell hosts
(:class:`~otto.host.unix_host.UnixHost`,
:class:`~otto.host.local_host.LocalHost`,
:class:`~otto.host.docker_host.DockerContainerHost`).

These are a *family capability*, not part of the universal ``Host`` Protocol —
an embedded host implements only the subset its filesystem supports.
"""

import base64
import binascii
import shlex
from pathlib import Path
from typing import Annotated

from ..logger.mode import LogMode
from ..result import Result
from ..utils import Arg, cli_exposed


class PosixFileOps:
    """Mixin: unix-CLI-style remote file management for posix-shell hosts."""

    __slots__ = ()

    @staticmethod
    def _q(path: "str | Path") -> str:
        return shlex.quote(str(path))

    @cli_exposed(output_dir=False)
    async def exists(self, path: "str | Path") -> bool:
        """Return True when *path* exists on the host (``test -e``)."""
        result = await self.exec(f"test -e {self._q(path)}")  # ty: ignore[unresolved-attribute]
        return result.status.is_ok

    @cli_exposed(output_dir=False)
    async def ls(self, path: "Annotated[str | Path, Arg()]" = ".", all: bool = False) -> list[str]:  # noqa: A002 — CLI-exposed param name, maps to --all flag
        """List entry names in *path* (``ls -1``; *all* adds ``-A`` for dotfiles)."""
        flags = "-1A" if all else "-1"
        result = await self.exec(f"ls {flags} {self._q(path)}")  # ty: ignore[unresolved-attribute]
        if not result.status.is_ok:
            return []
        return [line for line in result.value.splitlines() if line]

    @cli_exposed
    async def mkdir(self, path: "str | Path", parents: bool = True) -> Result:
        """Create directory *path* (``mkdir``; *parents* adds ``-p``)."""
        flag = "-p " if parents else ""
        result = await self.exec(f"mkdir {flag}{self._q(path)}")  # ty: ignore[unresolved-attribute]
        return Result(result.status, msg=result.value)

    @cli_exposed
    async def rm(self, path: "str | Path", recursive: bool = False, force: bool = False) -> Result:
        """Remove *path* (``rm``; *recursive* → ``-r``, *force* → ``-f``)."""
        flags = "".join(f for f, on in (("r", recursive), ("f", force)) if on)
        opt = f"-{flags} " if flags else ""
        result = await self.exec(f"rm {opt}{self._q(path)}")  # ty: ignore[unresolved-attribute]
        return Result(result.status, msg=result.value)

    @cli_exposed
    async def cp(self, src: "str | Path", dst: "str | Path", recursive: bool = False) -> Result:
        """Copy *src* to *dst* on the host (``cp``; *recursive* → ``-r``)."""
        opt = "-r " if recursive else ""
        result = await self.exec(  # ty: ignore[unresolved-attribute]
            f"cp {opt}{self._q(src)} {self._q(dst)}"
        )
        return Result(result.status, msg=result.value)

    @cli_exposed
    async def mv(self, src: "str | Path", dst: "str | Path") -> Result:
        """Move/rename *src* to *dst* on the host (``mv``)."""
        result = await self.exec(  # ty: ignore[unresolved-attribute]
            f"mv {self._q(src)} {self._q(dst)}"
        )
        return Result(result.status, msg=result.value)

    @cli_exposed(output_dir=False)
    async def read_file(self, path: "str | Path") -> str:
        """Return the text contents of *path*.

        Reads via ``base64`` — the exact inverse of :meth:`write_file`'s base64
        transport — so content round-trips byte-exact regardless of trailing
        newlines, trailing whitespace, or shell metacharacters (``exec``'s
        per-line ``rstrip``/rejoin would otherwise corrupt them).

        The device's ``base64`` wraps its output (measured: this machine's
        coreutils ``base64``, like BusyBox's, wraps at 76 columns by default),
        so the raw command output always contains newlines. Those are
        stripped locally before decoding — the base64 alphabet
        (``[A-Za-z0-9+/=]``) never contains whitespace, so flattening it out
        is lossless regardless of how the wrapping happened. Decoding then
        uses ``base64.b64decode(..., validate=True)`` rather than the stdlib
        default (``validate=False``): the default does not IGNORE bytes
        outside the alphabet, it silently DISCARDS them, so any
        contamination beyond the wrapping newlines the flatten step already
        accounts for — a login banner fragment, a stray warning line, a
        short or partial read — would decode without complaint.
        ``validate=True`` turns that into a loud failure instead, at the
        cost of requiring the flatten step to run first (bare wrapping
        newlines are themselves outside the alphabet and would otherwise be
        rejected as contamination).

        Be precise about what ``validate=True`` buys: it detects that the
        stream was corrupted, not that a corrupted stream decodes to WRONG
        bytes. Measured directly (see
        ``test_read_file_raises_on_a_stray_non_alphabet_byte_mid_stream``):
        injecting one stray non-alphabet byte into an otherwise-valid
        encoded string and decoding it with the stdlib default recovers the
        ORIGINAL bytes exactly — the discard removes the intruder and
        leaves a valid string, so that specific corruption is invisible
        under the default, not wrong-looking. A truncated or
        partially-read stream is the case that actually yields wrong bytes;
        this method still cannot distinguish that from a genuine short file,
        since both look like well-formed, complete base64.

        Raises:
            FileNotFoundError: the remote command itself failed (missing
                path, permissions) — nothing came back to decode.
            ValueError: the command succeeded but its output, once wrapping
                was flattened out, was not valid base64. This is a
                transport or content problem, not a missing file, and
                reporting it as :class:`FileNotFoundError` would send a
                caller looking for the wrong thing.
        """
        result = await self.exec(f"base64 {self._q(path)}")  # ty: ignore[unresolved-attribute]
        if not result.status.is_ok:
            raise FileNotFoundError(f"read_file({path!r}) failed: {result.value}")
        flattened = "".join((result.value or "").split())
        try:
            decoded = base64.b64decode(flattened, validate=True)
        except binascii.Error as exc:
            raise ValueError(
                f"read_file({path!r}): device output was not valid base64 once "
                f"wrapping was flattened ({exc}); the stream may be truncated or "
                f"contaminated by something other than ordinary line-wrapping"
            ) from exc
        return decoded.decode()

    @cli_exposed
    async def write_file(self, path: "str | Path", data: str, append: bool = False) -> Result:
        """Write *data* to *path* (overwrite, or append).

        The payload is base64-encoded on the wire, so arbitrary content
        (newlines, quotes, shell metacharacters) is transferred safely. Sent
        with ``log=LogMode.QUIET`` so large bodies stay out of the console
        (still recorded in verbose.log).
        """
        encoded = base64.b64encode(data.encode()).decode()
        redirect = ">>" if append else ">"
        cmd = f"echo {encoded} | base64 -d {redirect} {self._q(path)}"
        result = await self.exec(cmd, log=LogMode.QUIET)  # ty: ignore[unresolved-attribute]
        return Result(result.status, msg=result.value)
