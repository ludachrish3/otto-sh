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

``read_file``/``write_file`` are the two that need more than a shell: both move
their payload through the device's own ``base64``, and a userland that has no
such applet can run neither. :func:`refuse_if_base64_is_absent` is what this
module does about that — it refuses those two up front on a device measured to
have no ``base64``, rather than letting the device's ``not found`` come back
attributed to the file the caller named. The other operations here need nothing
but a shell.
"""

import base64
import binascii
import shlex
from pathlib import Path
from typing import Annotated

from ..logger.mode import LogMode
from ..result import Result
from ..utils import Arg, cli_exposed
from .userland import Userland, UserlandHost, refuse_if_gapped


async def refuse_if_base64_is_absent(
    userland: "Userland | None", *, host: str = "", attempted: str = ""
) -> None:
    """Refuse a file op whose codec this device was measured not to have.

    **The gap registry's third product call site.** Everything otto knows about
    this failure lives in the ``file-ops-base64`` record in
    :data:`~otto.host.userland.GAPS`; this function supplies the only thing a
    record cannot — whether THIS host is one the measurement covers — and hands
    the raise back to :func:`~otto.host.userland.refuse_if_gapped` so the
    message is the record's and not a second, drifting copy of it. Downgrading
    that record to ``untested`` stops the refusal: the CALLER decides this host
    is in the measured class, the TABLE decides whether that class is refused at
    all.

    WHAT IT KEYS ON: a :attr:`~otto.host.userland.Userland.base64_flag` of
    ``"absent"`` which
    :meth:`~otto.host.userland.Userland.is_settled` confirms was DECLARED in the
    host's ``userland_options`` or MEASURED on the device itself. Both halves are
    load-bearing:

    * ``"absent"`` is ALSO the value a capability holds when its probes could
      not be ASKED (``_UNASKABLE_DEFAULTS``), and a refusal built on that would
      turn a refused probe round — an sshd at its ``MaxSessions`` ceiling is
      the usual cause — into a verdict that the device has no ``base64``. That
      is the expensive direction, and the same asymmetry
      :func:`~otto.host.userland.refuse_if_gapped` applies to an ``untested``
      record: "we could not ask" must not become "does not work". Such a host
      is left to attempt the operation exactly as it did before this guard
      existed.
    * a host with no resolver AT ALL (``userland is None``) is likewise not
      refused. That is :class:`~otto.host.local_host.LocalHost` and
      :class:`~otto.host.docker_host.DockerContainerHost`, which never acquire
      one, so nothing has been measured about their userland either.

    **THIS PREDICATE COSTS A RESOLUTION, and that is a real change of cost.**
    It is the opposite of the call the first two call sites made, and the
    difference is the point rather than an inconsistency:
    :func:`otto.host.session.refuse_if_line_editor_would_truncate` and
    :func:`otto.host.daemon.refuse_if_launch_wrapper_needs_bash` both key on a
    DECLARED fact and both explicitly refuse to read a probe, because both sit
    on paths where a resolution would be paid per command. There is no declared
    base64 fact to key on here: ``has_bash`` is unrelated, and
    ``otto.host.os_profile``'s ``busybox`` profile deliberately carries NO
    ``userland_options``, on the stated ground that a declaration skips the
    probe and a wrong guess would then be unfixable from the device. So the
    device is the only honest source. What that costs, stated rather than
    discovered:

    * on a healthy host, one probe round on the FIRST call — cached on the host
      object (``UnixHost._userland``), and ``resolve()`` is idempotent once
      everything is settled, so every later ``read_file``/``write_file`` on that
      host adds one lock acquisition and nothing on the wire;
    * on a host that refuses probes, up to ``_RESOLVE_BUDGET_S`` (30s) on the
      first call where today it spends none. The repeat IS bounded:
      ``_RETRY_COOLDOWN_S`` (60s) allows one attempt per window however many
      times the caller asks, which is what keeps a loop of ``read_file`` calls
      from paying 30s each (pinned, for the resolution machinery itself, by
      ``test_a_wedged_host_is_not_amplified_by_the_fan_out`` in
      ``tests/unit/host/test_userland.py``);
    * an operator who does not want to pay it can pin all six capabilities in
      ``userland_options`` — the ``resolve()`` debug line prints them in
      exactly that form — at which point the round issues nothing.

    That trade is affordable HERE for reasons that are about these two methods
    specifically: they are coarse-grained, user-facing, already multi-round-trip
    operations, and nothing inside otto uses them as a primitive. The only
    caller under ``src/otto/`` is the generic ``@cli_exposed`` dispatcher
    (``otto.cli.expose``, one ``getattr(host, verb)`` call per CLI invocation);
    the only other mention is
    :class:`~otto.host.embedded_host.EmbeddedHost`, which overrides both to
    refuse. So there is no internal loop for the first call's cost to multiply
    through — unlike ``run()`` or ``exec()``, where a per-call resolution is
    what the first two guards refused to pay.

    :meth:`~otto.host.userland.Userland.resolve` swallows a probe that cannot
    run rather than raising (see ``Userland._probe``), so this guard adds no new
    failure mode of its own: on an unreachable host the operation goes on to
    fail with the transport's own error, as it does today.

    Args:
        userland: the host's resolver, from its ``_userland()`` hook. ``None``
            when the host has none.
        host: the host's id. Decorates the message; changes no verdict.
        attempted: what the caller was doing, in its own words — the record
            covers a class of userland and cannot know which of the two methods
            was called or on what path.

    Raises:
        ~otto.host.errors.UnsupportedOnUserlandError: this host settled
            ``base64_flag`` on ``"absent"`` and the ``file-ops-base64`` record
            is ``measured-broken``. Never raised for a host that resolved a
            usable flag, for one whose probes went unanswered, or while that
            record says anything else.
    """
    if userland is None:
        return
    await userland.resolve()
    if not userland.is_settled("base64_flag") or userland.base64_flag != "absent":
        return
    refuse_if_gapped("file-ops-base64", host=host, attempted=attempted)


class PosixFileOps(UserlandHost):
    """Mixin: unix-CLI-style remote file management for posix-shell hosts.

    Inherits the ``_userland()`` hook (see
    :class:`~otto.host.userland.UserlandHost`) because two of these methods —
    :meth:`read_file` and :meth:`write_file` — are built on a device applet
    whose presence only the device can answer for. The rest need nothing but a
    shell.
    """

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

        The codec is fixed, so a device with no ``base64`` is REFUSED here
        rather than asked — see :func:`refuse_if_base64_is_absent`, which is
        also where the resolution that predicate costs is argued. The
        refusal replaces the most misleading failure this method had: the
        device answers ``base64: not found``, the command is non-ok, and
        the :class:`FileNotFoundError` below then names the caller's path,
        sending them to look for a file that is present.

        Raises:
            ~otto.host.errors.UnsupportedOnUserlandError: this host was
                measured to have no ``base64`` at all. Nothing is sent.
            FileNotFoundError: the remote command itself failed (missing
                path, permissions) — nothing came back to decode.
            ValueError: the command succeeded but its output, once wrapping
                was flattened out, was not valid base64. This is a
                transport or content problem, not a missing file, and
                reporting it as :class:`FileNotFoundError` would send a
                caller looking for the wrong thing.
        """
        await refuse_if_base64_is_absent(
            self._userland(),
            host=str(getattr(self, "id", "")),
            attempted=(
                f"read_file({str(path)!r}), which reads the file by encoding it on the "
                f"device with `base64 <path>` and decoding the output locally"
            ),
        )
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

        The decode spelling is fixed, so a device with no ``base64`` is
        REFUSED before anything is sent — see
        :func:`refuse_if_base64_is_absent`. Refusing rather than emitting
        matters more on this side than on
        :meth:`read_file`'s, because the command otto would emit is
        DESTRUCTIVE on exactly the device that cannot run it: the shell
        opens ``> <path>`` before it resolves ``base64``, so an overwriting
        write to an existing file left it at zero bytes and returned a
        non-ok :class:`~otto.result.Result` (measured on the BusyBox 1.16.1
        artifact — see the record's ``measured_on``). ``append=True`` builds
        ``>>``, which does not truncate, and is refused on the same terms
        anyway: it would still write nothing.

        Raises:
            ~otto.host.errors.UnsupportedOnUserlandError: this host was
                measured to have no ``base64`` at all. Nothing is sent, so
                *path* is left exactly as it was found.
        """
        await refuse_if_base64_is_absent(
            self._userland(),
            host=str(getattr(self, "id", "")),
            attempted=(
                f"write_file({str(path)!r}, append={append}), which sends the payload "
                f"base64-encoded and decodes it on the device with `base64 -d`"
            ),
        )
        encoded = base64.b64encode(data.encode()).decode()
        redirect = ">>" if append else ">"
        cmd = f"echo {encoded} | base64 -d {redirect} {self._q(path)}"
        result = await self.exec(cmd, log=LogMode.QUIET)  # ty: ignore[unresolved-attribute]
        return Result(result.status, msg=result.value)
