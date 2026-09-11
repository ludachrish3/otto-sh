"""Base classes and shared utilities for file-transfer backends.

Defines the abstract :class:`BaseFileTransfer` that every transfer backend
must subclass, the :class:`TransferContext` frozen data class (the uniform
construction seam for registered backends), :func:`validate_filename_lengths`
(guards against filesystem ``NAME_MAX`` violations before any bytes move), and
the :data:`TransferProgressHandler` / :data:`TransferProgressFactory` type
aliases consumed by the progress-bar wiring layer.
"""

import asyncio
import shlex
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from ...result import CommandResult, Result
from ...utils import Status

if TYPE_CHECKING:
    from ..connections import ConnectionManager
    from ..embedded_filesystem import EmbeddedFileSystem
    from ..options import NcOptions, ScpOptions, SftpOptions
    from ..userland import Userland

# (src_path, dst_path, bytes_done, bytes_total)  # noqa: ERA001 — signature doc
# Mirrors asyncssh's progress_handler signature exactly.
TransferProgressHandler = Callable[[str, str, int, int], None]

# Factory that creates a fresh, isolated TransferProgressHandler per file.
# Used for concurrent transfers so each coroutine has independent progress state.
TransferProgressFactory = Callable[[], TransferProgressHandler]


@dataclass(frozen=True)
class ProgressGranularity:
    """What a backend promises the progress bar, per direction.

    ``put`` / ``get``: the most ``bytes_done`` may advance between two
    consecutive progress events (the last event may advance less). ``None``
    means the backend emits exactly ONE event, at completion -- a whole-file
    transfer with no intermediate observation -- and MUST say why in ``note``,
    because that note is rendered beside the promise on the support-matrix
    page. The conformance surface ``transfer-progress`` sizes its payload from
    these numbers and refuses a stream that breaks them; see
    ``tests/_fixtures/progress.py``.
    """

    put: int | None
    get: int | None
    note: str = ""

    def __post_init__(self) -> None:
        for arm, value in (("put", self.put), ("get", self.get)):
            if value is not None and value <= 0:
                raise ValueError(
                    f"ProgressGranularity.{arm} must be a positive byte count "
                    f"or None, got {value!r}"
                )
        if (self.put is None or self.get is None) and not self.note.strip():
            raise ValueError(
                "ProgressGranularity: a None arm (one event at completion) must explain "
                "itself in `note` -- the note is published beside the promise"
            )


@dataclass(frozen=True)
class TransferContext:
    """Construction inputs a host provides to build its file transfer backend.

    The frozen public seam for custom transfer backends. Carries the union of what any family's
    built-ins receive at their call sites; a unix backend reads the unix fields, an embedded
    backend the embedded ones. Selector validation (host-family applicability) runs before
    construction, so a backend never sees a ctx missing the fields it needs.
    """

    transfer: str
    host_name: str
    max_filename_len: int = 255
    exec_cmd: "Callable[..., Coroutine[Any, Any, CommandResult]] | None" = None
    # unix-family fields
    connections: "ConnectionManager | None" = None
    nc_options: "NcOptions | None" = None
    scp_options: "ScpOptions | None" = None
    sftp_options: "SftpOptions | None" = None
    get_local_ip: "Callable[[], str] | None" = None
    # The host's shared capability resolver. Not per-protocol like the option
    # tables above it: one object answers for the whole device, and a backend
    # that needs to know how the device spells something reads it here rather
    # than probing on its own account.
    userland: "Userland | None" = None
    # How long a single line of a command this host's `exec_cmd` can carry, or
    # None when nothing bounds it -- asked of the host's SessionManager (see
    # `otto.host.session.SessionManager.exec_line_budget`) rather than derived
    # here, because the answer depends on which primitive `exec` routes
    # through and on the host's shell dialect, neither of which a transfer
    # backend can see. A CALLABLE, not a number: `term` has a setter and a
    # proxied login can be re-targeted, so the route is read when bytes are
    # about to move rather than when the backend was built. Read today only by
    # `ShellFileTransfer`, whose commands carry their payload IN the command
    # line; a backend that moves bytes some other way ignores it, and the
    # default None keeps every builder that does not pass it unchanged.
    exec_line_budget: "Callable[[], int | None] | None" = None
    # embedded-family fields
    filesystem: "EmbeddedFileSystem | None" = None


def validate_filename_lengths(
    files: list[Path],
    limit: int,
    host_name: str,
) -> Result:
    """Reject files whose basename exceeds the host's filesystem cap.

    Shared by :class:`~otto.host.transfer.UnixFileTransfer` (Unix) and
    :class:`~otto.host.transfer.EmbeddedFileTransfer` (embedded) so every backend
    surfaces the same self-explaining error. Without this guard the
    failure modes are:

    - Unix SCP/SFTP/FTP: server returns ``File name too long`` (errno 36),
      mid-transfer, after the local file is already read.
    - Embedded FAT (8.3, no LFN) or LittleFS over ``NAME_MAX``: device
      fails ``fs_open`` with ``-ENOENT``, giving no hint that the *name*
      was the problem.

    Returns an ok :class:`~otto.result.Result` when every basename fits, or a
    failing one whose ``msg`` names the offending file.
    """
    for path in files:
        name = path.name
        if len(name) > limit:
            return Result(
                Status.Error,
                msg=(
                    f"filename {name!r} ({len(name)} chars) exceeds the "
                    f"{limit}-character basename limit for host "
                    f"{host_name!r}. The target filesystem cannot open longer "
                    f"names — rename the file or raise the firmware/filesystem "
                    f"limit (``CONFIG_FS_FATFS_MAX_LFN`` for FAT, "
                    f"``CONFIG_FS_LITTLEFS_NAME_MAX`` for LittleFS; ``NAME_MAX`` "
                    f"on POSIX)."
                ),
            )
    return Result(Status.Success)


MAX_FILE_MODE = 0o7777
"""Highest permission value ``mode`` accepts.

Twelve bits: setuid, setgid, sticky, then the three rwx triads — the same
range ``chmod(1)`` accepts as an octal argument.
"""


def parse_file_mode(value: int | str | None) -> Result:
    """Normalize a transfer permission *mode*, or explain why it is not one.

    Strings are **always** interpreted base-8, with or without a ``0o``/``0``
    prefix, because that is the only reading a permission mode can sensibly
    have — ``--mode 755`` read as decimal would silently mean ``0o1363``.
    Integers are taken as-is: a Python caller writing ``mode=0o755`` has
    already expressed the value, and re-reading it base-8 would corrupt it.

    Returns an ok :class:`~otto.result.Result` whose ``value`` is the ``int``
    mode (or ``None`` when no mode was requested), or a failing one whose
    ``msg`` names the offending input. Mirrors
    :func:`validate_filename_lengths` so the two fold identically in
    :meth:`BaseFileTransfer.put_files`.
    """
    if value is None:
        return Result(Status.Success, value=None)
    # bool subclasses int, so True would silently become 0o1 without this.
    if isinstance(value, bool):
        return Result(
            Status.Error,
            msg=f"invalid octal mode {value!r}: expected octal digits (e.g. 755, 0644, 0o4755)",
        )
    if isinstance(value, int):
        mode = value
    else:
        try:
            mode = int(value, 8)
        except ValueError:
            return Result(
                Status.Error,
                msg=(f"invalid octal mode {value!r}: digits must be 0-7 (e.g. 755, 0644, 0o4755)"),
            )
    if mode < 0:
        return Result(Status.Error, msg=f"invalid octal mode {value!r}: must not be negative")
    if mode > MAX_FILE_MODE:
        return Result(
            Status.Error,
            msg=f"mode 0o{mode:o} out of range (max 0o{MAX_FILE_MODE:o})",
        )
    return Result(Status.Success, value=mode)


def chmod_command(mode: int, paths: list[Path]) -> str:
    """Build one batched ``chmod`` command covering every path in *paths*.

    A single invocation for the whole batch, so a multi-file transfer costs
    one extra round trip rather than one per file. The mode is rendered as
    bare octal because that is what ``chmod(1)`` expects — a ``0o`` prefix
    would be parsed as a filename on most implementations.

    The ``--`` terminator matters: ``shlex.quote`` leaves a leading-dash path
    like ``-rf`` unquoted (it contains no shell metacharacters), and a
    relative ``dest_dir`` collapses ``Path(".") / "-rf"`` to exactly that, so
    without ``--`` ``chmod`` would read the destination as option flags.
    """
    quoted = " ".join(shlex.quote(str(p)) for p in paths)
    return f"chmod {mode:o} -- {quoted}"


SSHD_DEFAULT_MAX_SESSIONS = 10
"""``MaxSessions`` of a default OpenSSH server: the channels one CONNECTION may
hold at once. sshd enforces it by REFUSING the excess channel (``open
failed``), never by queueing, so every per-file fan-out over one SSH
connection must stay under it."""

SSH_CHANNEL_HEADROOM = 2
"""The channels a fan-out leaves free -- the pooled control session and the
exec a caller may already be inside. Without it a full fan-out sits exactly at
the ceiling and the NEXT unrelated exec on the connection is the one refused."""


def derive_concurrency_limit(channels_per_transfer: int, *, sharing_objects: int = 1) -> int:
    """How many files one transfer object may have in flight against a default sshd.

    *channels_per_transfer* is what one in-flight file holds (scp/sftp: one
    session; nc: the listener's exec channel plus its readiness poll).
    *sharing_objects* is how many ways the usable budget is divided, so one
    batch takes only its share: scp and sftp pass ``2``, which leaves half
    the channels for whatever else the caller runs on the same connection
    while the batch is in flight. Never below one: a limit of zero would be a
    semaphore no permit ever comes out of.
    """
    if channels_per_transfer < 1 or sharing_objects < 1:
        raise ValueError("channels_per_transfer and sharing_objects must be at least 1")
    usable = SSHD_DEFAULT_MAX_SESSIONS - SSH_CHANNEL_HEADROOM
    return max(1, usable // (channels_per_transfer * sharing_objects))


def resolve_concurrency_limit(configured: int | None, *, derived: int, option: str) -> int:
    """Answer the limit a backend runs with: *configured* when set, else *derived*.

    Refused at construction, by name, when *configured* is below one -- a zero
    would otherwise park every transfer on that host forever with nothing to
    point at.
    """
    if configured is None:
        return derived
    if configured < 1:
        raise ValueError(f"{option} must be at least 1, got {configured}")
    return configured


DEFAULT_SESSION_TRANSFER_LIMIT = derive_concurrency_limit(1, sharing_objects=2)
"""The scp/sftp default -- one SSH session per file, and half the usable
budget rather than all of it.

The other half is left for whatever else the caller runs on the same
connection while the batch is in flight: its own exec calls, a second
transfer on another backend, an interactive session. The derivation lives in
:ref:`the host-options guide <transfer-channel-budget>`."""


def aggregate_transfer(per_file: dict[Path, Result]) -> Result:
    """Fold a per-file mapping into the aggregate transfer Result.

    Aggregate status is the first non-ok entry's status (Skipped counts as
    ok); aggregate msg joins each non-ok entry's diagnostic. The mapping is
    carried through unchanged as :attr:`~otto.result.Result.value`, keyed by
    the source paths exactly as passed.
    """
    status = next((r.status for r in per_file.values() if not r.is_ok), Status.Success)
    msg = "; ".join(r.msg for r in per_file.values() if not r.is_ok and r.msg)
    return Result(status=status, value=per_file, msg=msg)


DIRECTORY_SOURCE_HINT = "is a directory (pass recursive=True, or -r on the CLI, to transfer a tree)"
"""Per-file diagnostic for a directory handed to a non-recursive ``put``.

One spelling for every backend: the recursive path never hands a backend a
directory, so a backend seeing one means the caller forgot the flag.
"""


def refuse_directory_sources(src_files: list[Path]) -> Result | None:
    """Return the refusal aggregate a non-recursive put owes, or ``None``.

    Directory entries carry :data:`DIRECTORY_SOURCE_HINT` by name; the files
    beside them are ``Skipped`` (nothing was attempted), so the batch moves
    no byte at all rather than half of it.
    """
    dirs = [f for f in src_files if f.is_dir()]
    if not dirs:
        return None
    per_file: dict[Path, Result] = {}
    for f in src_files:
        if f in dirs:
            per_file[f] = Result(Status.Error, msg=f"{f}: {DIRECTORY_SOURCE_HINT}")
        else:
            per_file[f] = Result(Status.Skipped, msg="not attempted (directory source refused)")
    return aggregate_transfer(per_file)


class BaseFileTransfer(ABC):
    """Shared API + progress plumbing for any file-transfer backend.

    The public ``put_files`` / ``get_files`` surface (filename-length
    validation, shared Rich progress acquisition) is owned by this base.
    Concrete backends (Unix's :class:`~otto.host.transfer.UnixFileTransfer`
    subclasses (:class:`~otto.host.transfer.ScpFileTransfer`,
    :class:`~otto.host.transfer.SftpFileTransfer`,
    :class:`~otto.host.transfer.FtpFileTransfer`,
    :class:`~otto.host.transfer.NcFileTransfer`), embedded's
    :class:`~otto.host.transfer.EmbeddedFileTransfer` subclasses
    (:class:`~otto.host.transfer.ConsoleFileTransfer`,
    :class:`~otto.host.transfer.TftpFileTransfer`), and any
    future ones) implement two abstract methods —
    ``_run_put`` and ``_run_get`` — both of which receive a
    :data:`TransferProgressFactory` and a keyword-only ``concurrent``,
    and are responsible for invoking the factory
    at least once per source file, terminating with
    ``bytes_done == bytes_total`` to mark completion.

    The progress-bar capability is enforced at the *type system* level:
    ``abc.abstractmethod`` refuses to instantiate a subclass that omits
    either method, so a new backend cannot be defined without supplying a
    way to report progress. The runtime contract test
    (``TestTransferProgressContract``) verifies the factory is actually
    invoked, not just that the methods exist.
    """

    host_families: frozenset[str] = frozenset[str]()
    """
    Host-family selectors this backend serves — a subset of ``{'unix', 'embedded'}``.
    Subclasses declare it; the spec field_validator rejects a backend on a host
    of the wrong family. A backend with an empty set can never validate and is
    rejected at registration.
    """

    supports_mode: bool = False
    """Whether this backend can apply a permission ``mode`` after a put.

    Declarative, like :attr:`host_families`: :meth:`put_files` reads it
    **pre-flight** and refuses a ``mode`` it could never honour before any
    bytes move — a 200 MB upload that ends in "this backend has no permission
    model" helps nobody. A backend setting this ``True`` must implement
    ``_apply_mode``.

    ``False`` for embedded backends (``console``, ``tftp``): a Zephyr
    filesystem has no permission bits to set.
    """

    progress_granularity: ClassVar[ProgressGranularity]
    """
    The stride this backend promises the progress bar, per direction. Declared on
    every backend class; ``register_transfer_backend`` refuses a class without it.
    A backend whose stride is configured per instance (scp's
    ``ScpOptions.block_size``) declares its DEFAULT here and answers the
    configured value from
    :meth:`~otto.host.transfer.BaseFileTransfer.effective_progress_granularity`.

    The role is FULLY QUALIFIED deliberately. This docstring is INHERITED by
    every backend class and rendered once per class, so a bare ``:meth:`` role
    is resolved against each SUBCLASS's module -- none of which documents the
    method -- and every such rendering is an unresolved target that ``-W``
    turns into a build failure. Measured 2026-08-26: eight of them, from the
    bare form this replaces.
    """

    def effective_progress_granularity(self) -> ProgressGranularity:
        """Answer the promise THIS instance makes -- the class declaration unless overridden."""
        return type(self).progress_granularity

    @property
    def concurrency_limit(self) -> int:
        """How many files this backend may have in flight at once (``>= 1``).

        The base answers one: a backend with no fan-out story (shell, console,
        ftp, the local copy) inherits it, and ``concurrent=True`` is then a
        documented no-op. scp, sftp and nc override it from their options.
        """
        return 1

    async def _dispatch_per_file(
        self,
        src_files: list[Path],
        transfer_one: Callable[[Path], Coroutine[Any, Any, Result]],
        *,
        concurrent: bool,
    ) -> dict[Path, Result]:
        """Run *transfer_one* per source, keyed by source exactly as passed.

        The instance semaphore (sized from :attr:`concurrency_limit`) is
        acquired around EVERY file in BOTH modes, so overlapping calls on one
        object -- the levels of a tree, a caller's own ``gather`` of one-file
        puts -- share one budget. ``concurrent=True`` gathers the files;
        ``concurrent=False`` awaits them in order. Every file is attempted: an
        ``Exception`` from *transfer_one* folds into THAT file's ``Error``
        entry and its siblings run.

        Cancellation is the one thing that does NOT fold. Both arms run the
        same per-file wrapper, which catches ``Exception`` only, so a
        ``CancelledError`` -- the caller's, or one a per-file coroutine raises
        itself -- comes straight out of this call and no Result is fabricated
        for it. That is why the gather runs WITHOUT ``return_exceptions``:
        with it, a cancelled child would come back as a value to be written
        into the mapping as an ``Error`` with an empty diagnostic. In the
        concurrent arm, a raise cancels and drains the siblings before
        propagating; nothing is left running. The drain waits for each sibling
        to honour that cancellation, so a per-file coroutine that swallows
        ``CancelledError`` holds the drain for as long as it takes -- bounded
        in practice by each backend's own cancellation cleanup timeouts.

        The semaphore is rebuilt whenever the running loop is not the one it
        was made for. ``asyncio.Semaphore`` binds to a loop the first time a
        waiter has to be created, and a later acquire from another loop
        raises; the old loop's tasks cannot still be running, so a fresh
        semaphore loses no permit and one transfer object survives being
        reused across separate ``asyncio.run`` calls.
        """
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(self.concurrency_limit)
            self._semaphore_loop = loop
        semaphore = self._semaphore

        async def _bounded(src: Path) -> Result:
            async with semaphore:
                return await transfer_one(src)

        async def _attempted(src: Path) -> Result:
            try:
                return await _bounded(src)
            except Exception as exc:  # noqa: BLE001 — any per-file failure is that file's entry
                return Result(Status.Error, msg=f"{src}: {exc}")

        if not concurrent:
            per_file: dict[Path, Result] = {}
            for src in src_files:
                per_file[src] = await _attempted(src)
            return per_file
        tasks = [asyncio.ensure_future(_attempted(src)) for src in src_files]
        try:
            gathered = await asyncio.gather(*tasks)
        except BaseException:
            # A raise (a caller cancellation, or a per-file coroutine's own
            # CancelledError) must not leave the other files' tasks running
            # detached, each still holding a semaphore permit — cancel every
            # task, then drain (`return_exceptions=True`) so each one's own
            # CancelledError is collected here rather than surfacing later as
            # an unretrieved task exception.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return dict(zip(src_files, gathered, strict=True))

    @classmethod
    def create(cls, ctx: "TransferContext") -> "BaseFileTransfer":
        """Build a transfer backend from a :class:`TransferContext`.

        The uniform construction seam (WS#4). Concrete backends override this to
        run their exact construction against the ctx fields they need. Not an
        ``abstractmethod`` deliberately: only registered built-ins are ever
        constructed through ``create``, and test doubles that subclass
        ``BaseFileTransfer`` only to exercise the progress contract must not be
        forced to implement it.
        """
        raise NotImplementedError(
            f"{cls.__name__} does not implement create(); a registered transfer "
            f"backend must override create(cls, ctx)."
        )

    def __init__(self, name: str, max_filename_len: int = 255) -> None:
        self._name = name
        self._max_filename_len = max_filename_len
        # One budget per INSTANCE: the ceiling it stands for is per connection,
        # so a semaphore made per call would hand every overlapping transfer
        # on the same object its own full budget. Built lazily because a
        # subclass sets the options `concurrency_limit` reads AFTER this runs,
        # and keyed to the loop that built it: an asyncio.Semaphore binds to
        # the first loop that waits on it, so an object reused across separate
        # asyncio.run() calls needs a fresh one rather than a bound leftover.
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None

    async def put_files(
        self,
        src_files: list[Path],
        dest_dir: Path,
        show_progress: bool = True,
        mode: int | str | None = None,
        concurrent: bool = True,
    ) -> Result:
        """Upload *src_files* to *dest_dir*, validating filenames and driving progress display.

        Rejects a bad or unhonourable *mode* and over-limit basenames up front
        — in that order, cheapest and most specific first — then acquires the
        process-wide shared Rich progress bar (if *show_progress*) and
        delegates to the concrete backend's ``_run_put`` implementation. When
        *mode* is set, the files that landed are chmod-ed in one batch
        afterwards (see ``_apply_mode``).

        *mode* is the permission bits for the uploaded files: an ``int``
        (``0o755``) from Python, or a string that is **always** read as octal
        (``"755"``, ``"0755"``, ``"0o755"``). ``None`` leaves whatever
        permissions the backend's own defaults produce.

        *concurrent* decides how many files may be in flight at once: up to
        :attr:`concurrency_limit` when ``True``, exactly one when ``False``.
        Every file is attempted in both modes.

        Returns the aggregate :class:`~otto.result.Result` whose ``value`` maps
        each source path (exactly as passed) to its per-file
        :class:`~otto.result.Result`.
        """
        from .progress import _acquire_shared_progress, make_rich_progress_factory

        mode_check = parse_file_mode(mode)
        if not mode_check.is_ok:
            return aggregate_transfer(
                {f: Result(mode_check.status, msg=mode_check.msg) for f in src_files}
            )
        resolved_mode: int | None = mode_check.value  # ty: ignore[unsound-assignment] — Result.value is Any by design (payload slot); retired by the Result[T] workstream
        if resolved_mode is not None and not self.supports_mode:
            msg = (
                f"host {self._name!r}: {type(self).__name__} has no permission "
                f"model; cannot apply mode 0o{resolved_mode:o}. Drop the mode "
                f"argument (--mode on the CLI) or transfer with a backend that "
                f"supports it."
            )
            return aggregate_transfer({f: Result(Status.Error, msg=msg) for f in src_files})

        refused = refuse_directory_sources(src_files)
        if refused is not None:
            return refused

        name_check = validate_filename_lengths(
            src_files,
            self._max_filename_len,
            self._name,
        )
        if not name_check.is_ok:
            return aggregate_transfer(
                {f: Result(name_check.status, msg=name_check.msg) for f in src_files}
            )
        if not show_progress:
            per_file = await self._run_put(src_files, dest_dir, None, concurrent=concurrent)
        else:
            async with _acquire_shared_progress() as progress:
                per_file = await self._run_put(
                    src_files,
                    dest_dir,
                    make_rich_progress_factory(progress, self._name),
                    concurrent=concurrent,
                )
        return aggregate_transfer(await self._finish_put(per_file, resolved_mode))

    async def get_files(
        self,
        src_files: list[Path],
        dest_dir: Path,
        show_progress: bool = True,
        concurrent: bool = True,
    ) -> Result:
        """Download *src_files* into *dest_dir*, validating filenames and driving progress display.

        Same validation and shared-progress contract as :meth:`put_files`,
        but delegates to the concrete backend's ``_run_get`` implementation.

        *concurrent* decides how many files may be in flight at once: up to
        :attr:`concurrency_limit` when ``True``, exactly one when ``False``.
        Every file is attempted in both modes.

        Returns the aggregate :class:`~otto.result.Result` whose ``value`` maps
        each source path (exactly as passed) to its per-file
        :class:`~otto.result.Result`.
        """
        from .progress import _acquire_shared_progress, make_rich_progress_factory

        name_check = validate_filename_lengths(
            src_files,
            self._max_filename_len,
            self._name,
        )
        if not name_check.is_ok:
            return aggregate_transfer(
                {f: Result(name_check.status, msg=name_check.msg) for f in src_files}
            )
        if not show_progress:
            return aggregate_transfer(
                await self._run_get(src_files, dest_dir, None, concurrent=concurrent)
            )
        async with _acquire_shared_progress() as progress:
            return aggregate_transfer(
                await self._run_get(
                    src_files,
                    dest_dir,
                    make_rich_progress_factory(progress, self._name),
                    concurrent=concurrent,
                )
            )

    async def _apply_mode(self, dest_paths: list[Path], mode: int) -> Result:
        """Set *mode* on the already-transferred *dest_paths*.

        Called once per :meth:`put_files` with the destination paths that
        actually landed — never with files that failed or were skipped, and
        never at all when nothing landed. Implementations should apply the
        mode in a **single** batched operation (see :func:`chmod_command`) so
        a multi-file transfer costs one extra round trip rather than N.

        Deliberately not an ``abstractmethod``: backends that cannot support
        modes leave :attr:`supports_mode` ``False`` and never reach this. A
        backend that flips the flag without implementing this gets a loud
        failure rather than a silent no-op.
        """
        raise NotImplementedError(
            f"{type(self).__name__} sets supports_mode = True but does not implement _apply_mode()."
        )

    async def _finish_put(
        self,
        per_file: dict[Path, Result],
        mode: int | None,
    ) -> dict[Path, Result]:
        """Apply *mode* to the files that landed, downgrading them if chmod fails.

        A chmod failure keeps ``value=dest_path`` on the downgraded entry: the
        bytes did land, only the permissions did not, and a caller must be
        able to tell that apart from a transfer that never happened.
        """
        if mode is None:
            return per_file
        landed = {src: r for src, r in per_file.items() if r.status is Status.Success and r.value}
        if not landed:
            return per_file
        mode_result = await self._apply_mode([r.value for r in landed.values()], mode)
        if mode_result.is_ok:
            return per_file
        for src, r in landed.items():
            per_file[src] = Result(
                Status.Error,
                value=r.value,
                msg=f"{src}: transferred, but setting mode 0o{mode:o} failed: {mode_result.msg}",
            )
        return per_file

    @abstractmethod
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: "TransferProgressFactory | None",
        *,
        concurrent: bool,
    ) -> dict[Path, Result]:
        """Backend-specific put implementation.

        Returns a per-file mapping keyed by the source paths exactly as passed:
        each value is a :class:`~otto.result.Result` carrying ``value=dest_path``
        on success or a per-file ``msg`` on failure -- every file is attempted
        whatever its siblings do; hand the per-file coroutine to
        :meth:`_dispatch_per_file`, which honours *concurrent* and the
        instance's :attr:`concurrency_limit`.

        For each src file the implementation must call
        ``progress_factory()`` (if not ``None``) to obtain a fresh
        :data:`TransferProgressHandler`, then invoke that handler as bytes
        complete — at minimum once with ``bytes_done == bytes_total`` so
        the file's progress bar reaches 100%.
        """

    @abstractmethod
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: "TransferProgressFactory | None",
        *,
        concurrent: bool,
    ) -> dict[Path, Result]:
        """Backend-specific get implementation.

        Same per-file mapping and progress contract as :meth:`_run_put`.
        """


NcPortStrategy = Literal["auto", "ss", "netstat", "python", "proc", "custom"]
"""Strategy for finding free ports on the remote host for netcat transfers.

Available strategies:

- ``'auto'`` (default) — try each built-in strategy in order (ss → netstat →
  python → proc) and cache the first one that succeeds.
- ``'ss'`` — parse ``ss -tln`` output to find unused ports.
- ``'netstat'`` — parse ``netstat -tln`` output (fallback for hosts without ss).
- ``'python'`` — bind a socket to port 0 via a ``python``/``python3`` one-liner
  and let the OS assign a free port.
- ``'proc'`` — read ``/proc/net/tcp`` directly (Linux-only, always available as
  a last resort).
- ``'custom'`` — run the shell command specified in ``nc_port_cmd``; the command
  must print a free port number to stdout.
"""

NcListenerCheck = Literal["auto", "ss", "netstat", "proc", "custom"]
"""Strategy for checking if a remote nc listener is ready.

Available strategies:

- ``'auto'`` (default) — probe for ss, then netstat, falling back to proc.
  The first tool found is cached and reused for subsequent checks.
- ``'ss'`` — check for a LISTEN socket via ``ss -tln sport = :<port>``.
- ``'netstat'`` — grep ``netstat -tln`` output for the port.
- ``'proc'`` — scan ``/proc/net/tcp`` for LISTEN state (0A) on the port
  (Linux-only, always available as a last resort).
- ``'custom'`` — run the shell command specified in ``nc_listener_cmd`` with a
  ``{port}`` placeholder. Must exit 0 when the port is listening.
"""
