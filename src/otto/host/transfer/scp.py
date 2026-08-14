"""SCP file transfer backend for UnixHost.

Registers ``scp`` into the shared transfer registry on import.

**The classic protocol execs a REMOTE BINARY**, which is what makes this
backend's one userland question worth asking: :func:`asyncssh.scp` speaks the
legacy protocol and runs ``scp`` on the far side to do it, so a device with no
such binary answers ``scp: not found`` and nothing lands.
:func:`~otto.host.transfer.scp.refuse_if_scp_is_absent` is what this module does about that — it
declines both directions up front on a device measured to have no ``scp``,
rather than letting the failure arrive as asyncssh's, one per file, after the
connection is up.
"""

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..connections import ConnectionManager
    from ..options import ScpOptions
    from ..userland import Userland

import logging

from typing_extensions import override

from ...result import CommandResult, Result
from ...utils import Status
from ..userland import APPLET_ABSENT, applet_capability, refuse_if_gapped
from .base import (
    TransferContext,
    TransferProgressFactory,
)
from .progress import _make_sftp_progress
from .registry import register_transfer_backend
from .unix_base import UnixFileTransfer

_logger = logging.getLogger(__name__)

SCP_APPLET = "scp"
"""The applet name this backend's refusal turns on.

One spelling, read twice — by :meth:`~otto.host.userland.Userland.has_applet`
and by :func:`~otto.host.userland.applet_capability` on the way into
:meth:`~otto.host.userland.Userland.is_settled` — and both validate it against
:data:`~otto.host.userland.PROBED_APPLETS`, so a typo raises rather than
becoming a condition that quietly never fires.
"""


async def refuse_if_scp_is_absent(
    userland: "Userland | None", *, host: str = "", attempted: str = ""
) -> None:
    """Refuse an scp transfer to a device measured to have no ``scp`` binary.

    **The gap registry's fifth product call site.** Everything otto knows about
    this failure lives in the ``scp-transfer`` record in
    :data:`~otto.host.userland.GAPS`; this function supplies the only thing a
    record cannot — whether THIS device is one the measurement covers — and
    hands the raise back to :func:`~otto.host.userland.refuse_if_gapped` so the
    message is the record's and not a second, drifting copy of it. Downgrading
    that record to ``untested`` stops the refusal: the CALLER decides this host
    is in the measured class, the TABLE decides whether that class is refused at
    all.

    **A REFUSAL AND NOT AN ADAPTATION**, which is the difference from
    :func:`otto.host.unix_host.shutdown_command`. That surface had a second
    spelling the device did have; this one has none —
    :class:`~otto.host.options.ScpOptions` has no
    binary-name override (its fields are ``preserve``, ``recurse``,
    ``block_size`` and ``extra``), and the name the far side runs is the
    protocol's, not otto's. So there is nothing to emit instead, and the
    record's own answer — use the ``shell`` backend — is a change of
    ``transfer``, not of this command.

    **IT KEYS ON THE DEVICE, NEVER ON THE PROFILE.** The ``busybox``
    os_profile lists ``scp`` in ``valid_transfers`` deliberately: a BusyBox
    device with a real ``scp`` installed alongside transfers perfectly well, and
    a lab entry that knows its device has one may pin ``transfer: scp``. Keying
    on the profile, or on ``os_type``, would refuse exactly that host. Only the
    applet answer decides.

    WHAT IT KEYS ON, then: an ``applet_scp`` of
    :data:`~otto.host.userland.APPLET_ABSENT` which
    :meth:`~otto.host.userland.Userland.is_settled` confirms was DECLARED in the
    host's ``userland_options`` or MEASURED on the device itself. The
    ``is_settled`` half is the same one
    :func:`otto.host.file_ops.refuse_if_base64_is_absent` and
    :func:`otto.host.unix_host.shutdown_command` ask for, and for the same
    reason: an applet batch that could not be ASKED must not become a verdict
    that the device has no ``scp``, or an sshd at its ``MaxSessions`` ceiling
    turns a working transfer into a permanent refusal. Such a host attempts the
    transfer exactly as it did before this guard existed.

    **A host with no resolver AT ALL (``userland is None``) is likewise not
    refused, and that arm is reachable here rather than defensive.**
    ``_userland()`` is an overridable hook whose base implementation in
    :class:`~otto.host.userland.UserlandHost` answers ``None``; ``scp`` is
    :class:`~otto.host.unix_host.UnixHost`'s DEFAULT ``transfer``, so a subclass
    that answers the hook that way still gets this backend built for it. Nothing
    has been measured about such a host's userland, so there is nothing to
    refuse from — the same asymmetry
    :func:`~otto.host.userland.refuse_if_gapped` applies to an ``untested``
    record.

    That gate is belt-and-braces TODAY and is written anyway, because what makes
    it redundant is a VALUE that can change. ``_UNASKABLE_DEFAULTS`` maps every
    applet to :data:`~otto.host.userland.APPLET_PRESENT`, so an unasked batch
    currently reads as "``scp`` is there" and never reaches the refusal. Flip
    that default the other way and an unsettled host would be refused with
    nothing measured — which is what the ``is_settled`` call stops, and what
    ``test_a_probe_round_that_never_arrived_is_not_refused`` holds by flipping
    exactly that default.

    **THIS PREDICATE COSTS A RESOLUTION THAT THIS PATH DID NOT PAY BEFORE, and
    that is the real price of the guard.** Unlike
    :func:`otto.host.unix_host.shutdown_command`, which rides a
    :meth:`~otto.host.userland.Userland.resolve` that ``run(sudo=True)`` was
    awaiting anyway, an scp transfer resolved NOTHING: ``put``/``get`` reach
    ``asyncssh.scp`` through the connection manager, and the only shell command
    on the path — the batched ``chmod`` a ``mode=`` put ends with — goes through
    ``Host.exec``, which does not resolve. So this adds a probe round where
    there was none. What that costs, stated rather than discovered:

    * on a healthy host, one probe round on the FIRST transfer — the
      :class:`~otto.host.userland.Userland` is built once per host object
      (``UnixHost._userland``) and shared with every other consumer, and
      ``resolve()`` is idempotent once everything is settled, so a later
      ``put``/``get`` on that host adds one lock acquisition and nothing on the
      wire;
    * on a host that refuses probes, up to ``_RESOLVE_BUDGET_S`` (30s) on the
      first transfer where today it spends none, bounded thereafter by
      ``_RETRY_COOLDOWN_S`` (60s), which allows one attempt per window however
      many transfers the caller runs;
    * an operator who does not want to pay it can pin ``applet_scp`` — indeed
      all thirteen capabilities — in ``userland_options``; ``otto host <id>
      probe`` (:meth:`~otto.host.userland.UserlandHost.probe`) prints them in
      exactly that form, and a declared capability skips the probe entirely.

    Affordable HERE for the same reason it was at
    :func:`otto.host.file_ops.refuse_if_base64_is_absent`: a transfer is a
    coarse, user-facing, already multi-round-trip operation, and nothing inside
    otto uses it as a per-command primitive. It is charged ONCE per
    ``put``/``get`` call and not once per file — the guard sits above the
    per-file fan-out, so a 200-file put pays it once. That is the opposite of
    the call :func:`otto.host.session.refuse_if_line_editor_would_truncate` and
    :func:`otto.host.daemon.refuse_if_launch_wrapper_needs_bash` made, both of
    which sit on per-command paths and both of which key on a DECLARED fact for
    that reason. There is no declared scp fact to key on: ``has_bash`` is
    unrelated, and the ``busybox`` profile deliberately carries no
    ``userland_options``.

    :meth:`~otto.host.userland.Userland.resolve` swallows a probe that cannot
    run rather than raising (see ``Userland._probe``), so this guard adds no new
    failure mode of its own: on an unreachable host the transfer goes on to fail
    with the transport's own error, as it does today.

    Args:
        userland: the host's capability resolver, from its ``_userland()``
            hook, or ``None`` when the host has none.
            :meth:`~otto.host.userland.Userland.resolve` is awaited here, so the
            caller does not have to.
        host: the host's name. Decorates the message; changes no verdict.
        attempted: what the caller was doing, in its own words — the record
            covers a class of userland and cannot know which direction was
            asked for or with how many files.

    Raises:
        ~otto.host.errors.UnsupportedOnUserlandError: this device settled
            ``applet_scp`` on absent and the ``scp-transfer`` record is
            ``measured-broken``. Nothing is sent, and no file is opened.
    """
    if userland is None:
        return
    await userland.resolve()
    if not userland.is_settled(applet_capability(SCP_APPLET)):
        return
    if userland.has_applet(SCP_APPLET) != APPLET_ABSENT:
        return
    refuse_if_gapped("scp-transfer", host=host, attempted=attempted)


class ScpFileTransfer(UnixFileTransfer):
    """SCP file transfer backend for UnixHost.

    Inherits ``put_files`` / ``get_files`` from
    :class:`~otto.host.transfer.base.BaseFileTransfer` and unix scaffolding
    (``_connections``, ``_exec_cmd``, ``_warmup_for_transfer``) from
    :class:`~otto.host.transfer.unix_base.UnixFileTransfer`; implements
    ``_run_put`` / ``_run_get``
    directly for the SCP protocol.

    ``userland`` is THREADED THROUGH and not validated, unlike the three fields
    above it in :meth:`create`, and the reason is a property of this backend's
    position rather than of how much the guard is worth. **``scp`` is
    :class:`~otto.host.unix_host.UnixHost`'s DEFAULT ``transfer``**, so this
    class is constructed for every plain unix host otto builds — including one
    whose ``_userland()`` hook answers ``None``, which is an overridable hook
    whose base implementation in :class:`~otto.host.userland.UserlandHost` does
    exactly that. Rejecting a context without a resolver, as
    :class:`~otto.host.transfer.shell.ShellFileTransfer` legitimately does, would
    therefore convert "this host cannot be guarded" into "this host cannot be
    BUILT" — a ``ValueError`` out of ``__post_init__`` for a host that transfers
    perfectly well. (Measured: it reddens
    ``tests/unit/host/test_privilege.py::test_a_host_with_no_userland_builds_todays_exact_sudo_command``,
    which builds precisely that host.) So the resolver is optional here for the
    same shape of reason as on
    :class:`~otto.host.transfer.nc.NcFileTransfer`, and
    :func:`~otto.host.transfer.scp.refuse_if_scp_is_absent` carries the ``None``
    arm that follows from it: no measurement, therefore no refusal.
    """

    host_families = frozenset({"unix"})

    def __init__(
        self,
        connections: "ConnectionManager",
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandResult]],
        scp_options: "ScpOptions",
        max_filename_len: int = 255,
        *,
        userland: "Userland | None",
    ) -> None:
        super().__init__(
            connections=connections,
            name=name,
            exec_cmd=exec_cmd,
            max_filename_len=max_filename_len,
        )
        self._scp_options = scp_options
        self._userland = userland

    @override
    @classmethod
    def create(cls, ctx: "TransferContext") -> "ScpFileTransfer":
        """Build the backend from *ctx*, rejecting what this protocol cannot do without.

        Three fields are required and ``userland`` deliberately is not — see the
        class docstring for why the default unix transfer cannot afford a fourth
        requirement.

        Raises:
            ValueError: *ctx* carries no connection manager, no ``exec_cmd`` or
                no :class:`~otto.host.options.ScpOptions`.
        """
        if ctx.connections is None:
            raise ValueError(
                "ScpFileTransfer requires a connections manager on the transfer context"
            )
        if ctx.exec_cmd is None:
            raise ValueError("ScpFileTransfer requires exec_cmd on the transfer context")
        if ctx.scp_options is None:
            raise ValueError("ScpFileTransfer requires scp_options on the transfer context")
        # Threaded through rather than validated like the three above, and the
        # class docstring says why: `scp` is UnixHost's DEFAULT transfer, so a
        # rejection here is a host that cannot be BUILT rather than one that
        # cannot be guarded. A ctx without a resolver still builds a working
        # backend; what it loses is the refusal, and losing that is the same
        # outcome as the day before this guard existed.
        return cls(
            connections=ctx.connections,
            name=ctx.host_name,
            exec_cmd=ctx.exec_cmd,
            scp_options=ctx.scp_options,
            userland=ctx.userland,
            max_filename_len=ctx.max_filename_len,
        )

    @override
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        await refuse_if_scp_is_absent(
            self._userland,
            host=self._name,
            attempted=(
                f"get of {len(src_files)} file(s) over the `scp` backend, which runs the "
                f"legacy protocol and execs `scp` on the device to send them"
            ),
        )
        return await self._get_files_scp(src_files, dest_dir, progress_factory)

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        await refuse_if_scp_is_absent(
            self._userland,
            host=self._name,
            attempted=(
                f"put of {len(src_files)} file(s) over the `scp` backend, which runs the "
                f"legacy protocol and execs `scp` on the device to receive them"
            ),
        )
        return await self._put_files_scp(src_files, dest_dir, progress_factory)

    async def _get_files_scp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        import asyncssh

        ssh_conn = await self._connections.ssh()

        async def _get_one(src: Path) -> Result:
            _progress = (
                _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            )
            _logger.debug(f"{self._name}: SCP get {src} -> {dest_dir}")
            await asyncssh.scp(
                (ssh_conn, str(src)),
                dest_dir,
                progress_handler=_progress,
                **self._scp_options._kwargs(),  # noqa: SLF001 — intra-package access to ScpOptions._kwargs
            )
            return Result(Status.Success, value=dest_dir / src.name)

        gathered = await asyncio.gather(
            *(_get_one(src) for src in src_files), return_exceptions=True
        )
        per_file: dict[Path, Result] = {}
        for src, outcome in zip(src_files, gathered, strict=True):
            if isinstance(outcome, BaseException):
                per_file[src] = Result(Status.Error, msg=f"{src}: {outcome}")
            else:
                per_file[src] = outcome
        return per_file

    async def _put_files_scp(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        import asyncssh

        ssh_conn = await self._connections.ssh()

        async def _put_one(src: Path) -> Result:
            _progress = (
                _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            )
            _logger.debug(f"{self._name}: SCP put {src} -> {dest_dir}")
            await asyncssh.scp(
                str(src),
                (ssh_conn, str(dest_dir)),
                progress_handler=_progress,
                **self._scp_options._kwargs(),  # noqa: SLF001 — intra-package access to ScpOptions._kwargs
            )
            return Result(Status.Success, value=dest_dir / src.name)

        gathered = await asyncio.gather(
            *(_put_one(src) for src in src_files), return_exceptions=True
        )
        per_file: dict[Path, Result] = {}
        for src, outcome in zip(src_files, gathered, strict=True):
            if isinstance(outcome, BaseException):
                per_file[src] = Result(Status.Error, msg=f"{src}: {outcome}")
            else:
                per_file[src] = outcome
        return per_file


register_transfer_backend("scp", ScpFileTransfer)
