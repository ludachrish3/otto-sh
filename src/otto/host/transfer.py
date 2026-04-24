"""
File transfer utilities for RemoteHost.

Defines the canonical TransferProgressHandler callback type used across all
transfer protocols (SCP, SFTP, FTP, netcat).  Rich (or any other progress
reporting library) lives only in make_rich_progress_handler — changing
Rich's API requires touching nothing else in the transfer stack.

Callback signature mirrors asyncssh's progress_handler so that SCP and SFTP
can forward it directly without any adaptation layer.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import aioftp
import asyncssh

from ..console import CONSOLE
from ..logger import getOttoLogger
from ..utils import CommandStatus, Status

if TYPE_CHECKING:
    from .connections import ConnectionManager
    from .options import NcOptions, ScpOptions
    from .session import HostSession
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

# (src_path, dst_path, bytes_done, bytes_total)
# Mirrors asyncssh's progress_handler signature exactly.
TransferProgressHandler = Callable[[str, str, int, int], None]

# Factory that creates a fresh, isolated TransferProgressHandler per file.
# Used for concurrent transfers so each coroutine has independent progress state.
TransferProgressFactory = Callable[[], TransferProgressHandler]


def make_rich_progress_handler(progress: Progress, host_name: str) -> TransferProgressHandler:
    """Return a TransferProgressHandler that drives the given Rich Progress bar.

    One task is created per source file, detected by a change in *src_path*.
    The caller is responsible for the Progress context (entering and exiting it).

    Example::

        with make_transfer_progress() as progress:
            handler = make_rich_progress_handler(progress, host_name=host.hostname)
            status, err = await host.get(files, dest, progress_handler=handler)
    """
    current_src: str | None = None
    task_id: TaskID | None = None

    def handler(src: str, dst: str, bytes_done: int, bytes_total: int) -> None:
        nonlocal current_src, task_id
        if src != current_src:
            current_src = src
            description = f"[green]{host_name}[/] {Path(src).name}"
            task_id = progress.add_task(description, total=bytes_total)
        assert task_id is not None
        progress.update(task_id, completed=bytes_done)

    return handler


def make_rich_progress_factory(progress: Progress, host_name: str) -> TransferProgressFactory:
    """Return a factory that creates a fresh TransferProgressHandler per file.

    Each call to the returned factory produces an independent handler with its
    own closure state, so concurrent transfers don't share progress tracking.

    Example::

        with make_transfer_progress() as progress:
            factory = make_rich_progress_factory(progress, host_name=host.name)
            status, err = await host.put(files, dest)
    """
    def factory() -> TransferProgressHandler:
        return make_rich_progress_handler(progress, host_name)
    return factory


def make_transfer_progress() -> Progress:
    """Return a pre-configured Rich Progress suited for file transfers."""
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(elapsed_when_finished=True),
        console=CONSOLE,
    )


# Rich's Live isn't meant to run multiple instances on the same console — two
# Lives rendering simultaneously produce overlapping cursor escapes and ghost
# rows. Concurrent host transfers (e.g. `asyncio.gather(host_a.put(...),
# host_b.get(...))`) used to hit exactly that. Instead we share one
# Progress across every in-flight transfer: the first caller to enter starts
# the Live, subsequent callers just attach a task, and the last caller to
# leave stops the Live and drops the singleton. Single-threaded asyncio makes
# the naive ref-count safe without a lock.
_shared_progress: Progress | None = None
_shared_progress_refs: int = 0


@asynccontextmanager
async def _acquire_shared_progress() -> AsyncIterator[Progress]:
    """Yield a process-wide Progress, creating/destroying the Live on demand."""
    global _shared_progress, _shared_progress_refs
    if _shared_progress is None:
        _shared_progress = make_transfer_progress()
        _shared_progress.start()
    progress = _shared_progress
    _shared_progress_refs += 1
    try:
        yield progress
    finally:
        _shared_progress_refs -= 1
        if _shared_progress_refs == 0:
            progress.stop()
            _shared_progress = None


FileTransferType = Literal['scp', 'sftp', 'ftp', 'nc']

NcPortStrategy = Literal['auto', 'ss', 'netstat', 'python', 'proc', 'custom']
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

NcListenerCheck = Literal['auto', 'ss', 'netstat', 'proc', 'custom']
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

_NC_BLOCK_SIZE = 8192

# Drain the nc writer every N blocks so `bytes_done` reported to the progress
# handler tracks bytes that have actually left the process, not bytes buffered
# inside `StreamWriter`. Too small = an await per 8 KB (death by context switch);
# too large = laggy progress. 64 blocks ≈ 512 KB gives smooth updates on a
# 12 MB/s link while keeping the overhead negligible.
_NC_DRAIN_EVERY = 64

_logger = getOttoLogger()

# ---------------------------------------------------------------------------
# Shell script templates for port-finding strategies
# ---------------------------------------------------------------------------

# Port scripts run inside `( ... )` so their `exit 0` / `exit 1` only
# terminates the subshell. Without the subshell wrap, a failure-path `exit`
# kills the whole telnet monitor session, forcing a 1–2 s reopen on every
# subsequent call.
_SS_PORT_SCRIPT = (
    '( used=$(ss -tln | grep -oE ":[0-9]+ " | tr -d ": " | sort -un); '
    'reserved=" {reserved} "; '
    'p={base_port}; '
    'while [ $p -le 65535 ]; do '
    '  case "$reserved" in *" $p "*) p=$((p+1)); continue;; esac; '
    '  echo "$used" | grep -qx "$p" || {{ echo $p; exit 0; }}; '
    '  p=$((p+1)); '
    'done; '
    'exit 1 )'
)

_NETSTAT_PORT_SCRIPT = (
    '( used=$(netstat -tln | grep -oE ":[0-9]+ " | tr -d ": " | sort -un); '
    'reserved=" {reserved} "; '
    'p={base_port}; '
    'while [ $p -le 65535 ]; do '
    '  case "$reserved" in *" $p "*) p=$((p+1)); continue;; esac; '
    '  echo "$used" | grep -qx "$p" || {{ echo $p; exit 0; }}; '
    '  p=$((p+1)); '
    'done; '
    'exit 1 )'
)

_PYTHON_PORT_CMD = (
    "import socket; s=socket.socket(); s.bind(('',0)); "
    "print(s.getsockname()[1]); s.close()"
)

_PROC_PORT_SCRIPT = (
    '( used=""; '
    'while read line; do '
    '  set -- $line; '
    '  case $2 in *:*) h=${{2##*:}}; used="$used $(printf "%d" "0x$h")";; esac; '
    'done < /proc/net/tcp; '
    'reserved=" {reserved} "; '
    'p={base_port}; '
    'while [ $p -le 65535 ]; do '
    '  case "$reserved" in *" $p "*) p=$((p+1)); continue;; esac; '
    '  case " $used " in *" $p "*) ;; *) echo $p; exit 0;; esac; '
    '  p=$((p+1)); '
    'done; '
    'exit 1 )'
)

# ---------------------------------------------------------------------------
# Shell script templates for listener-check strategies
# ---------------------------------------------------------------------------

_SS_LISTENER_CHECK = 'ss -tln sport = :{port} | grep -q LISTEN'
_NETSTAT_LISTENER_CHECK = 'netstat -tln | grep -q ":{port} "'

# Precompute hex port in Python, then scan /proc/net/tcp for LISTEN state (0A).
_PROC_LISTENER_CHECK = (
    'while read line; do '
    'set -- $line; '
    'case $2 in *:{hex_port}) case $4 in 0A) exit 0;; esac;; esac; '
    'done < /proc/net/tcp; exit 1'
)

_PORT_STRATEGY_ORDER: list[NcPortStrategy] = ['ss', 'netstat', 'python', 'proc']
_LISTENER_CHECK_ORDER: list[NcListenerCheck] = ['ss', 'netstat', 'proc']

# Single-round-trip probe that picks a port-finding strategy and a
# listener-check strategy in one shell invocation. `command -v` is POSIX
# (unlike `which`, which varies across distros), short-circuits on the first
# hit, and treats exit-code as the availability signal. Output is one line of
# the form "<port> <listener>", e.g. "ss ss" or "python proc".
_STRATEGY_PROBE = (
    'port=proc; listener=proc; '
    'if command -v ss >/dev/null 2>&1; then port=ss; listener=ss; '
    'elif command -v netstat >/dev/null 2>&1; then port=netstat; listener=netstat; '
    'elif command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; '
    'then port=python; '
    'fi; '
    'echo "$port $listener"'
)


async def _connect_with_retry(
    host: str,
    port: int,
    timeout: float = 5.0,
    retry_interval: float = 0.1,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to a TCP port, retrying on ConnectionRefused until *timeout*."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=min(1.0, max(0.1, deadline - asyncio.get_running_loop().time())),
            )
        except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
            if asyncio.get_running_loop().time() >= deadline:
                raise ConnectionError(
                    f"Remote nc listener on {host}:{port} not ready within {timeout}s"
                )
            await asyncio.sleep(retry_interval)


def _make_sftp_progress(
    handler: TransferProgressHandler,
) -> Callable[[bytes, bytes, int, int], None]:
    """Wrap Otto's str-path TransferProgressHandler into asyncssh's bytes-path type."""
    def adapted(src: bytes, dst: bytes, done: int, total: int) -> None:
        handler(src.decode(), dst.decode(), done, total)
    return adapted


class FileTransfer:
    """Handles all file-transfer protocols (SCP, SFTP, FTP, netcat) for a RemoteHost.

    Receives injectable callables for open_session and oneshot so it can be tested
    without real connections.
    """

    def __init__(
        self,
        connections: 'ConnectionManager',
        name: str,
        transfer: FileTransferType,
        nc_options: 'NcOptions',
        scp_options: 'ScpOptions',
        get_local_ip: Callable[[], str],
        open_session: Callable[[str], Coroutine[Any, Any, 'HostSession']],
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandStatus]],
    ) -> None:
        self._connections = connections
        self._name = name
        self.transfer = transfer
        self._nc_options = nc_options
        self._scp_options = scp_options
        self._get_local_ip = get_local_ip
        self._open_session = open_session
        self._exec_cmd = exec_cmd
        self._resolved_port_strategy: NcPortStrategy | None = None
        self._resolved_listener_check: NcListenerCheck | None = None
        self._reserved_ports: set[int] = set()
        # Long-lived shell session for all nc control-plane ops (port-find,
        # listener probe). On telnet, opening a fresh session costs ~1-2 s per
        # call, so we pay that handshake ONCE per host and reuse the warm
        # session for every subsequent probe. None until first lazy open, and
        # when the connection drops, `alive` becomes False and we reopen.
        self._nc_monitor: 'HostSession | None' = None
        # Serializes run access to _nc_monitor — telnet sessions corrupt
        # under concurrent use (shared stdin/stdout), so every control-plane
        # call grabs this lock for its duration.
        self._nc_monitor_lock = asyncio.Lock()
        # Serializes port allocation so two concurrent `_find_free_port` calls
        # can't both return the same "free" port from parallel ss scans.
        self._port_lock = asyncio.Lock()
        # Serializes concurrent `prepare()` calls so the compound strategy
        # probe runs exactly once per host lifetime.
        self._prepare_lock = asyncio.Lock()

    @property
    def _nc_exec(self) -> str:
        return self._nc_options.exec_name

    @property
    def _nc_port(self) -> int:
        return self._nc_options.port

    @property
    def _nc_port_strategy(self) -> 'NcPortStrategy':
        return self._nc_options.port_strategy

    @property
    def _nc_port_cmd(self) -> str | None:
        return self._nc_options.port_cmd

    @property
    def _nc_listener_check(self) -> 'NcListenerCheck':
        return self._nc_options.listener_check

    @property
    def _nc_listener_cmd(self) -> str | None:
        return self._nc_options.listener_cmd

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_files(
        self,
        srcFiles: list[Path],
        destDir: Path,
        show_progress: bool = True,
    ) -> tuple[Status, str]:
        if not show_progress:
            return await self._get_files(srcFiles, destDir, None)
        async with _acquire_shared_progress() as progress:
            return await self._get_files(
                srcFiles, destDir, make_rich_progress_factory(progress, self._name),
            )

    async def put_files(
        self,
        srcFiles: list[Path],
        destDir: Path,
        show_progress: bool = True,
    ) -> tuple[Status, str]:
        if not show_progress:
            return await self._put_files(srcFiles, destDir, None)
        async with _acquire_shared_progress() as progress:
            return await self._put_files(
                srcFiles, destDir, make_rich_progress_factory(progress, self._name),
            )

    # ------------------------------------------------------------------
    # Protocol dispatch
    # ------------------------------------------------------------------

    async def _get_files(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        match self.transfer:
            case 'scp':
                return await self._get_files_scp(srcFiles, destDir, progress_factory)
            case 'sftp':
                return await self._get_files_sftp(srcFiles, destDir, progress_factory)
            case 'ftp':
                return await self._get_files_ftp(srcFiles, destDir, progress_factory)
            case 'nc':
                return await self._get_files_nc(srcFiles, destDir, progress_factory)
            case _:
                raise ValueError(f'{self._name}: unsupported file_transfer "{self.transfer}"')

    async def _put_files(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        match self.transfer:
            case 'scp':
                return await self._put_files_scp(srcFiles, destDir, progress_factory)
            case 'sftp':
                return await self._put_files_sftp(srcFiles, destDir, progress_factory)
            case 'ftp':
                return await self._put_files_ftp(srcFiles, destDir, progress_factory)
            case 'nc':
                return await self._put_files_nc(srcFiles, destDir, progress_factory)
            case _:
                raise ValueError(f'{self._name}: unsupported file_transfer "{self.transfer}"')

    # ------------------------------------------------------------------
    # SCP
    # ------------------------------------------------------------------

    async def _get_files_scp(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        ssh_conn = await self._connections.ssh()

        async def _get_one(src: Path) -> tuple[Status, str]:
            _progress = _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            _logger.debug(f"{self._name}: SCP get {src} -> {destDir}")
            await asyncssh.scp(
                (ssh_conn, str(src)),
                destDir,
                progress_handler=_progress,
                **self._scp_options._kwargs(),
            )
            return Status.Success, ''

        results: list[tuple[Status, str] | BaseException] = await asyncio.gather(
            *(_get_one(src) for src in srcFiles), return_exceptions=True
        )
        return _first_error(results)

    async def _put_files_scp(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        ssh_conn = await self._connections.ssh()

        async def _put_one(src: Path) -> tuple[Status, str]:
            _progress = _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            _logger.debug(f"{self._name}: SCP put {src} -> {destDir}")
            await asyncssh.scp(
                str(src),
                (ssh_conn, str(destDir)),
                progress_handler=_progress,
                **self._scp_options._kwargs(),
            )
            return Status.Success, ''

        results: list[tuple[Status, str] | BaseException] = await asyncio.gather(
            *(_put_one(src) for src in srcFiles), return_exceptions=True
        )
        return _first_error(results)

    # ------------------------------------------------------------------
    # SFTP
    # ------------------------------------------------------------------

    async def _get_files_sftp(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        sftp_conn = await self._connections.sftp()

        async def _get_one(src: Path) -> tuple[Status, str]:
            _progress = _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            _logger.debug(f"{self._name}: SFTP get {src} -> {destDir}")
            await sftp_conn.get(
                str(src),
                str(destDir / src.name),
                progress_handler=_progress,
            )
            return Status.Success, ''

        results: list[tuple[Status, str] | BaseException] = await asyncio.gather(
            *(_get_one(src) for src in srcFiles), return_exceptions=True
        )
        return _first_error(results)

    async def _put_files_sftp(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        sftp_conn = await self._connections.sftp()

        async def _put_one(src: Path) -> tuple[Status, str]:
            _progress = _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            _logger.debug(f"{self._name}: SFTP put {src} -> {destDir}")
            await sftp_conn.put(
                str(src),
                str(destDir / src.name),
                progress_handler=_progress,
            )
            return Status.Success, ''

        results: list[tuple[Status, str] | BaseException] = await asyncio.gather(
            *(_put_one(src) for src in srcFiles), return_exceptions=True
        )
        return _first_error(results)

    # ------------------------------------------------------------------
    # FTP
    # ------------------------------------------------------------------

    async def _get_files_ftp(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        # FTP transfers are sequential: aioftp.Client uses a single control
        # connection with one data channel per transfer, so concurrent ops on
        # the same client are not supported.
        ftp_conn = await self._connections.ftp()
        try:
            for src in srcFiles:
                dst = destDir / src.name
                _logger.debug(f"{self._name}: FTP get {src} -> {dst}")
                if progress_factory is None:
                    await ftp_conn.download(str(src), str(dst))
                else:
                    handler = progress_factory()
                    info = await ftp_conn.stat(str(src))
                    total = int(info.get('size', 0))
                    bytes_done = 0
                    async with ftp_conn.download_stream(str(src)) as stream:
                        with open(dst, 'wb') as f:
                            async for block in stream.iter_by_block():
                                f.write(block)
                                bytes_done += len(block)
                                handler(str(src), str(dst), bytes_done, total)
            return Status.Success, ''
        except Exception as e:
            return Status.Error, str(e)

    async def _put_files_ftp(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        # Sequential for the same reason as _get_files_ftp (single data channel).
        ftp_conn = await self._connections.ftp()
        try:
            for src in srcFiles:
                dst = destDir / src.name
                _logger.debug(f"{self._name}: FTP put {src} -> {dst}")
                if progress_factory is None:
                    await ftp_conn.upload(str(src), str(dst))
                else:
                    handler = progress_factory()
                    total = src.stat().st_size
                    bytes_done = 0
                    async with ftp_conn.upload_stream(str(dst)) as stream:
                        with open(src, 'rb') as f:
                            while True:
                                block = f.read(aioftp.DEFAULT_BLOCK_SIZE)
                                if not block:
                                    break
                                await stream.write(block)
                                bytes_done += len(block)
                                handler(str(src), str(dst), bytes_done, total)
            return Status.Success, ''
        except Exception as e:
            return Status.Error, str(e)

    # ------------------------------------------------------------------
    # Netcat
    # ------------------------------------------------------------------

    async def prepare(self) -> None:
        """Resolve port + listener strategies in a single round-trip.

        Runs the shared `_STRATEGY_PROBE` script through `_control_run` so on
        telnet the monitor session handshake is paid once here (and shared
        with subsequent control ops) instead of lazily at first-transfer
        time. Idempotent — a second call with both strategies already cached
        is a no-op.

        Callers use `_warmup_for_transfer` to run this concurrently with
        exec-pool warming; direct callers can invoke `prepare()` alone.

        If the probe itself fails (non-zero exit, malformed output), the
        caches stay unset and the lazy cascades in `_find_free_port_auto` /
        `_resolve_listener_strategy` still kick in as fallbacks.
        """
        port_auto = self._nc_port_strategy == 'auto' and self._resolved_port_strategy is None
        listener_auto = self._nc_listener_check == 'auto' and self._resolved_listener_check is None
        if not (port_auto or listener_auto):
            return
        async with self._prepare_lock:
            # Re-check under the lock — another coroutine may have finished
            # while we waited.
            port_auto = self._nc_port_strategy == 'auto' and self._resolved_port_strategy is None
            listener_auto = self._nc_listener_check == 'auto' and self._resolved_listener_check is None
            if not (port_auto or listener_auto):
                return
            result = await self._control_run(_STRATEGY_PROBE)
            if result.retcode != 0:
                _logger.debug(
                    f"{self._name}: strategy probe failed (retcode={result.retcode}, "
                    f"output={result.output!r}); lazy cascades will resolve"
                )
                return
            parts = result.output.strip().split()
            if len(parts) != 2:
                _logger.debug(
                    f"{self._name}: strategy probe returned malformed output "
                    f"{result.output!r}; lazy cascades will resolve"
                )
                return
            port_choice, listener_choice = parts
            if port_auto and port_choice in ('ss', 'netstat', 'python', 'proc'):
                self._resolved_port_strategy = cast(
                    Literal['ss', 'netstat', 'python', 'proc'], port_choice
                )
                _logger.debug(
                    f"{self._name}: cached port strategy '{port_choice}' via probe"
                )
            if listener_auto and listener_choice in ('ss', 'netstat', 'proc'):
                self._resolved_listener_check = cast(
                    Literal['ss', 'netstat', 'proc'], listener_choice
                )
                _logger.debug(
                    f"{self._name}: cached listener check strategy "
                    f"'{listener_choice}' via probe"
                )

    async def _warmup_for_transfer(self, file_count: int) -> None:
        """Open the monitor session, probe strategies, and pre-open exec
        sessions for the upcoming nc listeners — all concurrently.

        Without this, the first transfer on a cold telnet host pays its
        handshakes serially: monitor-open → strategy-probe → (per-file)
        exec-session-open. By firing them together we collapse wall-clock
        cost from ~N handshakes to ~max(handshakes).

        `file_count` sessions are pre-opened on telnet so each concurrent
        `nc -l` can pull a warm session from the pool. On SSH the exec path
        uses channels over the live connection, so no pool warming is needed
        and we just run `prepare()`.

        Safe to call multiple times; `prepare()` is idempotent and extra
        `_exec_cmd('true')` calls are cheap on warm sessions.
        """
        tasks: list[Coroutine[Any, Any, Any]] = [self.prepare()]
        if self._connections.term == 'telnet':
            for _ in range(max(file_count, 1)):
                tasks.append(self._exec_cmd('true'))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _control_run(self, cmd: str) -> CommandStatus:
        """Run an nc control-plane command on the warmest available runner.

        On telnet, every fresh shell session costs a 1–2 s auth handshake, so
        we route port-finding and listener-probing through a single long-lived
        ``_nc_monitor`` session — one handshake per host, amortized over every
        subsequent call.  The lock serializes concurrent callers (telnet
        sessions corrupt under shared stdin/stdout) but each call is brief.

        On SSH, exec channels over the existing connection are cheap, so we
        just use ``_exec_cmd`` directly — no monitor session needed.
        """
        if self._connections.term == 'ssh':
            return await self._exec_cmd(cmd)
        async with self._nc_monitor_lock:
            if self._nc_monitor is None or not self._nc_monitor.alive:
                self._nc_monitor = await self._open_session('_nc_monitor')
            return (await self._nc_monitor.run(cmd)).only

    async def _find_free_port(self) -> int:
        """Find a free port on the remote host using the configured strategy.

        The returned port is added to ``_reserved_ports`` so that concurrent
        transfers don't collide.  Callers must call ``_release_port`` in a
        ``finally`` block once the port is no longer needed.

        The whole scan+reserve sequence runs under ``_port_lock`` — without
        it, two parallel callers both read an empty reservation snapshot and
        both return the same port.
        """
        async with self._port_lock:
            strategy = self._nc_port_strategy
            if strategy == 'auto':
                port = await self._find_free_port_auto()
            else:
                port = await self._find_free_port_with(strategy)
            self._reserved_ports.add(port)
            return port

    async def _find_free_port_auto(self) -> int:
        """Resolve the port-finding strategy (via the compound probe) and run it.

        First call goes through `prepare()` so both the port and listener
        strategies resolve in one round-trip. If the probe's chosen strategy
        somehow fails on execution, fall back to the full cascade — keeps
        the original robustness guarantee.
        """
        if self._resolved_port_strategy is None:
            await self.prepare()
        if self._resolved_port_strategy is not None:
            try:
                return await self._find_free_port_with(self._resolved_port_strategy)
            except (RuntimeError, ValueError) as e:
                _logger.debug(
                    f"{self._name}: cached port strategy "
                    f"'{self._resolved_port_strategy}' failed ({e}); cascading"
                )
                self._resolved_port_strategy = None
        errors: list[str] = []
        for strategy in _PORT_STRATEGY_ORDER:
            try:
                port = await self._find_free_port_with(strategy)
                self._resolved_port_strategy = strategy
                _logger.debug(f"{self._name}: cached port strategy '{strategy}'")
                return port
            except (RuntimeError, ValueError) as e:
                errors.append(f"{strategy}: {e}")
        raise RuntimeError(
            f"All port-finding strategies failed on {self._name}: "
            + "; ".join(errors)
        )

    async def _find_free_port_with(self, strategy: NcPortStrategy) -> int:
        """Dispatch to a specific port-finding strategy."""
        match strategy:
            case 'ss':
                return await self._find_free_port_ss()
            case 'netstat':
                return await self._find_free_port_netstat()
            case 'python':
                return await self._find_free_port_python()
            case 'proc':
                return await self._find_free_port_proc()
            case 'custom':
                return await self._find_free_port_custom()
            case _:
                raise ValueError(f"Unknown port strategy: {strategy}")

    def _reserved_str(self) -> str:
        return ' '.join(str(p) for p in self._reserved_ports)

    async def _find_free_port_ss(self) -> int:
        script = _SS_PORT_SCRIPT.format(base_port=self._nc_port, reserved=self._reserved_str())
        result = await self._control_run(script)
        if result.retcode != 0:
            raise RuntimeError(f"ss port scan failed: {result.output}")
        return int(result.output.strip())

    async def _find_free_port_netstat(self) -> int:
        script = _NETSTAT_PORT_SCRIPT.format(base_port=self._nc_port, reserved=self._reserved_str())
        result = await self._control_run(script)
        if result.retcode != 0:
            raise RuntimeError(f"netstat port scan failed: {result.output}")
        return int(result.output.strip())

    async def _find_free_port_python(self) -> int:
        """Try ``python``, then ``python3`` for the bind-to-0 one-liner."""
        last_output = ''
        for exe in ('python', 'python3'):
            result = await self._control_run(f'{exe} -c "{_PYTHON_PORT_CMD}"')
            if result.retcode == 0:
                return int(result.output.strip())
            last_output = result.output
        raise RuntimeError(f"python port discovery failed: {last_output}")

    async def _find_free_port_proc(self) -> int:
        script = _PROC_PORT_SCRIPT.format(base_port=self._nc_port, reserved=self._reserved_str())
        result = await self._control_run(script)
        if result.retcode != 0:
            raise RuntimeError(f"/proc/net/tcp port scan failed: {result.output}")
        return int(result.output.strip())

    async def _find_free_port_custom(self) -> int:
        if self._nc_port_cmd is None:
            raise ValueError("nc_port_strategy is 'custom' but nc_port_cmd is None")
        result = await self._control_run(self._nc_port_cmd)
        if result.retcode != 0:
            raise RuntimeError(f"Custom port command failed: {result.output}")
        return int(result.output.strip())

    def _release_port(self, port: int) -> None:
        """Remove *port* from the reserved set after a transfer completes."""
        self._reserved_ports.discard(port)

    # ------------------------------------------------------------------
    # Listener check
    # ------------------------------------------------------------------

    async def _wait_for_remote_listener(
        self,
        port: int,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> None:
        """Poll the remote host until a TCP listener appears on *port*.

        Uses the configured ``nc_listener_check`` strategy to build the
        check command.  The ``auto`` strategy probes for available tools
        once and caches the result.

        Every probe runs through ``_control_run``, which on telnet hosts
        reuses a single warm monitor session instead of paying a fresh auth
        handshake per call.

        The poll interval starts at 0.05 s for the first handful of
        iterations (when nc usually becomes ready on a warm session) and
        ramps up to *interval* afterward, so fast-ready listeners don't
        pay the full *interval* tax on the very first miss.
        """
        check = await self._get_listener_check_cmd(port)
        deadline = asyncio.get_running_loop().time() + timeout
        fast_interval = min(0.05, interval)
        iterations = 0
        while asyncio.get_running_loop().time() < deadline:
            result = await self._control_run(check)
            if result.retcode == 0:
                return
            await asyncio.sleep(fast_interval if iterations < 5 else interval)
            iterations += 1
        raise ConnectionError(f"Remote nc listener on port {port} not ready within {timeout}s")

    async def _get_listener_check_cmd(self, port: int) -> str:
        """Return the shell command string for checking a listener on *port*."""
        strategy = self._nc_listener_check
        if strategy == 'auto':
            strategy = await self._resolve_listener_strategy()
        return self._listener_cmd_for(strategy, port)

    async def _resolve_listener_strategy(self) -> NcListenerCheck:
        """Return the cached listener strategy, running `prepare()` if needed.

        Falls back to the per-tool `type` cascade only if `prepare()` couldn't
        populate the cache (e.g. the compound probe failed). This keeps the
        original behavior as a safety net without paying for it on the hot
        path.
        """
        if self._resolved_listener_check is None:
            await self.prepare()
        if self._resolved_listener_check is not None:
            return self._resolved_listener_check
        for candidate in _LISTENER_CHECK_ORDER:
            if candidate == 'proc':
                self._resolved_listener_check = 'proc'
                _logger.debug(f"{self._name}: cached listener check strategy 'proc'")
                return 'proc'
            tool = candidate  # 'ss' or 'netstat'
            result = await self._control_run(f'type {tool} >/dev/null 2>&1')
            if result.retcode == 0:
                self._resolved_listener_check = candidate
                _logger.debug(f"{self._name}: cached listener check strategy '{candidate}'")
                return candidate
        return 'proc'  # pragma: no cover

    def _listener_cmd_for(self, strategy: NcListenerCheck, port: int) -> str:
        """Build the check command for a concrete (non-auto) strategy."""
        match strategy:
            case 'ss':
                return _SS_LISTENER_CHECK.format(port=port)
            case 'netstat':
                return _NETSTAT_LISTENER_CHECK.format(port=port)
            case 'proc':
                hex_port = f"{port:04X}"
                return _PROC_LISTENER_CHECK.format(hex_port=hex_port)
            case 'custom':
                if self._nc_listener_cmd is None:
                    raise ValueError("nc_listener_check is 'custom' but nc_listener_cmd is None")
                return self._nc_listener_cmd.format(port=port)
            case _:
                raise ValueError(f"Unknown listener check strategy: {strategy}")

    async def _verify_nc_dest_size(
        self, dst: Path, expected: int
    ) -> tuple[Status, str] | None:
        """Stat the remote destination and verify it matches *expected* bytes.

        Returns ``None`` on success or an ``(Status.Error, msg)`` tuple
        describing the mismatch. Factored out as a method so tests that
        drive ``_put_files_nc`` with mocked exec_cmd can patch the verify
        step without hand-rolling a stat response.
        """
        verify = await self._exec_cmd(
            f"stat -c %s {dst} 2>/dev/null || echo MISSING"
        )
        actual_output = verify.output.strip()
        if actual_output == "MISSING":
            return Status.Error, f"nc transfer to {dst}: destination file missing after listen_task exit"
        try:
            actual = int(actual_output)
        except ValueError:
            return Status.Error, f"nc transfer to {dst}: stat returned unparseable output {actual_output!r}"
        if actual != expected:
            return Status.Error, f"nc transfer to {dst}: expected {expected} bytes, got {actual}"
        return None

    async def _get_files_nc(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        if self._connections.has_tunnel:
            return await self._get_files_nc_tunneled(srcFiles, destDir, progress_factory)
        await self._warmup_for_transfer(len(srcFiles))
        local_ip = self._get_local_ip()
        # NOTE: monitor session intentionally NOT closed after each transfer — it persists
        # for reuse across multiple get() calls and is cleaned up by host.close().
        monitor = await self._open_session('_nc_monitor')

        # Pre-fetch remote file sizes sequentially via the persistent session.
        sizes: dict[Path, int] = {}
        for src in srcFiles:
            stat_result = (await monitor.run(f'stat -c %s {src}')).only
            sizes[src] = int(stat_result.output.strip()) if stat_result.retcode == 0 else 0

        async def _get_one(src: Path) -> tuple[Status, str]:
            dst = destDir / src.name
            total = sizes[src]
            handler = progress_factory() if progress_factory is not None else None
            _logger.debug(f"{self._name}: NC get {src} -> {dst}")

            done: asyncio.Future[tuple[Status, str]] = asyncio.get_running_loop().create_future()

            async def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                try:
                    bytes_done = 0
                    with open(dst, 'wb') as f:
                        while True:
                            block = await reader.read(_NC_BLOCK_SIZE)
                            if not block:
                                break
                            f.write(block)
                            bytes_done += len(block)
                            if handler is not None:
                                handler(str(src), str(dst), bytes_done, total)
                    writer.close()
                    done.set_result((Status.Success, ''))
                except Exception as e:
                    done.set_result((Status.Error, str(e)))

            # Port 0 lets the OS assign a free port — no collisions when
            # multiple hosts transfer concurrently.  asyncio.start_server
            # returns once the socket is bound, so no sleep is needed.
            server = await asyncio.start_server(_on_connect, '0.0.0.0', 0)
            port = server.sockets[0].getsockname()[1]
            try:
                send_task = asyncio.create_task(
                    self._exec_cmd(
                        f'{self._nc_exec} -N {local_ip} {port} < {src} 2>/dev/null',
                        timeout=None,
                    )
                )

                def _on_send_fail(task: asyncio.Task[Any]) -> None:
                    if done.done():
                        return
                    exc = task.exception()
                    if exc is not None:
                        done.set_result((Status.Error, str(exc)))

                send_task.add_done_callback(_on_send_fail)
                result = await done
                await send_task
                return result
            finally:
                server.close()
                await server.wait_closed()

        results: list[tuple[Status, str] | BaseException] = await asyncio.gather(
            *(_get_one(src) for src in srcFiles),
            return_exceptions=True,
        )
        return _first_error(results)

    async def _get_files_nc_tunneled(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        """Netcat GET through an SSH hop using a reversed-listener approach.

        The remote host runs ``nc -l <port> < <file>`` as a listener that
        sends file data.  Otto connects through an SSH port forward and
        reads the data — same tunnel mechanics as PUT, reversed data flow.
        """
        await self._warmup_for_transfer(len(srcFiles))
        # Pre-fetch remote file sizes via persistent monitor session.
        monitor = await self._open_session('_nc_monitor')
        sizes: dict[Path, int] = {}
        for src in srcFiles:
            stat_result = (await monitor.run(f'stat -c %s {src}')).only
            sizes[src] = int(stat_result.output.strip()) if stat_result.retcode == 0 else 0

        async def _get_one(src: Path) -> tuple[Status, str]:
            dst = destDir / src.name
            total = sizes[src]
            handler = progress_factory() if progress_factory is not None else None
            _logger.debug(f"{self._name}: NC get (tunneled) {src} -> {dst}")

            port = await self._find_free_port()
            try:
                # Remote listener sends file data to the first connecting client.
                listen_task = asyncio.create_task(
                    self._exec_cmd(
                        f'{self._nc_exec} -Nl {port} < {src} 2>/dev/null',
                        timeout=None,
                    )
                )

                try:
                    await self._wait_for_remote_listener(port)
                except ConnectionError:
                    listen_task.cancel()
                    await asyncio.gather(listen_task, return_exceptions=True)
                    return Status.Error, f"Remote nc listener on port {port} not ready"

                local_port = await self._connections.forward_port(port)

                try:
                    reader, writer = await _connect_with_retry(
                        'localhost', local_port, timeout=5.0,
                    )
                except ConnectionError:
                    listen_task.cancel()
                    await asyncio.gather(listen_task, return_exceptions=True)
                    return Status.Error, f"nc listener on localhost:{local_port} not ready"

                try:
                    bytes_done = 0
                    with open(dst, 'wb') as f:
                        while True:
                            block = await reader.read(_NC_BLOCK_SIZE)
                            if not block:
                                break
                            f.write(block)
                            bytes_done += len(block)
                            if handler is not None:
                                handler(str(src), str(dst), bytes_done, total)
                finally:
                    writer.close()
                    await writer.wait_closed()

                await listen_task
                return Status.Success, ''
            finally:
                self._release_port(port)

        results: list[tuple[Status, str] | BaseException] = await asyncio.gather(
            *(_get_one(src) for src in srcFiles),
            return_exceptions=True,
        )
        return _first_error(results)

    async def _put_files_nc(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        # Fire monitor-open + strategy-probe + pool-warming concurrently so
        # the first-transfer handshakes don't stack up serially on the
        # critical path. On a warm host this is a no-op.
        await self._warmup_for_transfer(len(srcFiles))

        async def _attempt(src: Path, dst: Path) -> tuple[Status, str]:
            # Use an ephemeral port on the remote side so multiple host objects
            # targeting the same IP don't collide.  `_find_free_port` holds a
            # lock so concurrent callers can't both reserve the same port.
            port = await self._find_free_port()
            try:
                listen_task = asyncio.create_task(
                    self._exec_cmd(
                        f'{self._nc_exec} -l {port} < /dev/null > {dst} 2>/dev/null',
                        timeout=None,
                    )
                )

                # Confirm the remote nc is actually listening before we try to
                # connect. Launching nc over SSH (or telnet) can exceed
                # `_connect_with_retry`'s budget on a loaded system; for tunnel
                # paths the local asyncssh listener accepts immediately
                # regardless, hiding the not-yet-listening remote entirely.
                # `_wait_for_remote_listener` routes through `_control_run`,
                # which on telnet hosts reuses one warm monitor session
                # instead of paying a fresh auth handshake per probe.
                await self._wait_for_remote_listener(port)
                if self._connections.has_tunnel:
                    local_port = await self._connections.forward_port(port)
                    connect_host = 'localhost'
                    connect_port = local_port
                else:
                    connect_host = self._connections.ip
                    connect_port = port

                # Adaptive retry — connects as soon as the remote nc listener is
                # ready. Allow more time when tunneled (extra hop latency).
                if self._connections.has_tunnel:
                    timeout = 5.0
                elif self._connections.term == 'telnet':
                    timeout = 5.0
                else:
                    timeout = 2.0

                try:
                    _, writer = await _connect_with_retry(
                        connect_host, connect_port, timeout=timeout,
                    )
                except ConnectionError:
                    listen_task.cancel()
                    await asyncio.gather(listen_task, return_exceptions=True)
                    return Status.Error, f"nc listener on {connect_host}:{connect_port} not ready"

                total = src.stat().st_size
                bytes_done = 0
                handler = progress_factory() if progress_factory is not None else None

                try:
                    with open(src, 'rb') as f:
                        blocks_since_drain = 0
                        while True:
                            block = f.read(_NC_BLOCK_SIZE)
                            if not block:
                                break
                            writer.write(block)
                            bytes_done += len(block)
                            blocks_since_drain += 1
                            if blocks_since_drain >= _NC_DRAIN_EVERY:
                                await writer.drain()
                                blocks_since_drain = 0
                            if handler is not None:
                                handler(str(src), str(dst), bytes_done, total)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

                await listen_task

                # `_wait_for_remote_listener` only checks socket LISTEN state,
                # not whether nc has entered its accept loop. Under load the
                # kernel can transition to LISTEN a hair before nc is ready to
                # read, and a connection that lands in that window gets dropped
                # — leaving the destination file at size 0 (or missing, if
                # listen_task was cancelled before the shell redirect opened
                # it). Verify the bytes actually arrived so callers can tell a
                # true success from a silent ghost.
                verify_error = await self._verify_nc_dest_size(dst, total)
                if verify_error is not None:
                    return verify_error
                return Status.Success, ''
            finally:
                self._release_port(port)

        async def _put_one(src: Path) -> tuple[Status, str]:
            dst = destDir / src.name
            _logger.debug(f"{self._name}: NC put {src} -> {dst}")
            result = await _attempt(src, dst)
            if not result[0].is_ok:
                # One retry on the narrow listener-readiness race. A second
                # failure is almost certainly a real problem (bad port,
                # permissions, disk full) and should propagate.
                _logger.debug(f"{self._name}: NC put retry after: {result[1]}")
                result = await _attempt(src, dst)
            return result

        results: list[tuple[Status, str] | BaseException] = await asyncio.gather(
            *(_put_one(src) for src in srcFiles),
            return_exceptions=True,
        )
        r = _first_error(results)
        if r[0].is_ok:
            _logger.debug('Finished nc transfers')
        return r


def _first_error(
    results: list[tuple[Status, str] | BaseException],
) -> tuple[Status, str]:
    """Return the first error from a list of gather results, or (Success, '') if all passed."""
    for result in results:
        if isinstance(result, BaseException):
            return Status.Error, str(result)
        if not result[0].is_ok:
            return result
    return Status.Success, ''
