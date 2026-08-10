"""Unix/SSH-based file transfer backends (netcat) for UnixHost.

Registers ``nc`` into the shared transfer registry on import.
"""

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from ..connections import ConnectionManager
    from ..options import NcOptions

import logging

from typing_extensions import override

from ...result import CommandResult, Result
from ...utils import Status, WaitTimeoutError, wait_for_async
from ..errors import HostCommandError, HostUnreachableError
from .base import (
    NcListenerCheck,
    NcPortStrategy,
    TransferContext,
    TransferProgressFactory,
    TransferProgressHandler,
)
from .registry import register_transfer_backend
from .unix_base import UnixFileTransfer

_NC_BLOCK_SIZE = 8192
_NC_LISTENER_FAST_POLL_ITERS = (
    5  # number of fast-interval poll iterations before switching to slow interval
)

# Drain the nc writer every N blocks so `bytes_done` reported to the progress
# handler tracks bytes that have actually left the process, not bytes buffered
# inside `StreamWriter`. Too small = an await per 8 KB (death by context switch);
# too large = laggy progress. 64 blocks ≈ 512 KB gives smooth updates on a
# 12 MB/s link while keeping the overhead negligible.
_NC_DRAIN_EVERY = 64

# Zero-progress window on the data-phase awaits. A connection that lands in
# the remote listener's LISTEN-before-accept window is invisible from this
# side: the TCP handshake completes into the kernel backlog (through an SSH
# hop, asyncssh's local forward accepts immediately regardless), and a
# data-phase await then parks with no deadline of its own. Probed live
# 2026-08-07 against a listener holding that window open: GET's read() sat
# >20s — the confirmed shape of the natural GET hang. PUT's small-payload
# sends never reach a drain wait at all (everything fits under the
# transport's high-water mark), so the PUT hang that motivated the old
# retry markers is ATTRIBUTED, not proven, to the previously-unbounded
# forward setup (now bounded below); PUT's drain guard is prophylactic for
# large sends, where the probe showed the same indefinite park once buffers
# filled.
#
# These are ZERO-PROGRESS windows, not throughput floors: drain() only
# resolves at the transport's low-water mark, so a plain wait_for would
# demand ~512 KiB (one _NC_DRAIN_EVERY window) per timeout — a ~100 KiB/s
# minimum link rate that otto's own `link impair --rate` can shape well
# below. _drain_stall_bounded therefore re-arms the window whenever the
# write buffer shrank at all, and read() re-arms on any received block.
# The buffer gauge moves in bursts gated by socket writability, so "zero
# buffer change in a window" is not literally "zero bytes on the wire" —
# this is a far narrower throughput floor, not strictly none.
#
# Sizing: every error path retries once on a fresh port, and the
# integration wrapper (tests/integration/host/_transfer_retry.py) gives a
# whole transfer 30s — the data-path bounds must fit twice with headroom:
# 2 x (stall + forward setup + close) = 24s. Pinned by the budget pin in
# tests/unit/host/test_transfer_nc_put.py.
#
# Containment, not elimination: the LISTEN-vs-accept window itself stays
# open on every transfer. The todo's "probe past accept()" alternative is
# inapplicable here — nc's listener serves exactly ONE accept, so a
# throwaway readiness connect would consume it and take the data path.
# The design is instead: bound every step, verify what arrived, retry once
# on a fresh port.
_NC_STALL_TIMEOUT = 5.0

# Setup-step bound for creating the asyncssh local forward — a wedged hop
# channel must fail the attempt, not hang it.
_NC_FORWARD_SETUP_TIMEOUT = 5.0

# Hard lifetime cap for a remote `nc -l`, applied via coreutils `timeout` (see
# `_nc_listener_prefix`). One hour, not `listener_timeout`: this is the backstop
# for otto dying with a listener up, so it only needs to beat "unnoticed for
# days" — and capping an ESTABLISHED transfer would truncate large files.
_NC_LISTENER_HARD_CAP_S = 3600

# Close-handshake bound; past it the transport is aborted (a stalled channel
# never flushes, so its graceful close never completes — leaking the fd, the
# buffered bytes, and through a hop the asyncssh forward channel).
_NC_CLOSE_TIMEOUT = 2.0

_logger = logging.getLogger(__name__)


def _probe_failure(timed_out: bool, message: str) -> HostUnreachableError | HostCommandError:
    """Pick the error type for a failed control-plane probe; the text is the caller's.

    Every port-finding strategy runs a shell script through ``_control_run``
    and reads its exit code, which cannot tell a script that SAID no from one
    that was killed by its timeout — ``retcode == -1`` also means "never ran".
    ``timed_out`` is the field that can, and the two answers send a caller
    somewhere different: an exhausted port range is about the remote host's
    sockets, a timeout is about reaching it at all.
    """
    return HostUnreachableError(message) if timed_out else HostCommandError(message)


# ---------------------------------------------------------------------------
# Shell script templates for port-finding strategies
# ---------------------------------------------------------------------------

# Port scripts run inside `( ... )` so their `exit 0` / `exit 1` only
# terminates the subshell. Without the subshell wrap, a failure-path `exit`
# kills the whole telnet control session, forcing a 1-2 s reopen on every
# subsequent call.
_SS_PORT_SCRIPT = (
    '( used=$(ss -tln | grep -oE ":[0-9]+ " | tr -d ": " | sort -un); '
    'reserved=" {reserved} "; '
    "p={base_port}; "
    "while [ $p -le 65535 ]; do "
    '  case "$reserved" in *" $p "*) p=$((p+1)); continue;; esac; '
    '  echo "$used" | grep -qx "$p" || {{ echo $p; exit 0; }}; '
    "  p=$((p+1)); "
    "done; "
    "exit 1 )"
)

_NETSTAT_PORT_SCRIPT = (
    '( used=$(netstat -tln | grep -oE ":[0-9]+ " | tr -d ": " | sort -un); '
    'reserved=" {reserved} "; '
    "p={base_port}; "
    "while [ $p -le 65535 ]; do "
    '  case "$reserved" in *" $p "*) p=$((p+1)); continue;; esac; '
    '  echo "$used" | grep -qx "$p" || {{ echo $p; exit 0; }}; '
    "  p=$((p+1)); "
    "done; "
    "exit 1 )"
)

_PYTHON_PORT_CMD = (
    "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()"
)

_PROC_PORT_SCRIPT = (
    '( used=""; '
    "while read line; do "
    "  set -- $line; "
    '  case $2 in *:*) h=${{2##*:}}; used="$used $(printf "%d" "0x$h")";; esac; '
    "done < /proc/net/tcp; "
    'reserved=" {reserved} "; '
    "p={base_port}; "
    "while [ $p -le 65535 ]; do "
    '  case "$reserved" in *" $p "*) p=$((p+1)); continue;; esac; '
    '  case " $used " in *" $p "*) ;; *) echo $p; exit 0;; esac; '
    "  p=$((p+1)); "
    "done; "
    "exit 1 )"
)

# ---------------------------------------------------------------------------
# Shell script templates for listener-check strategies
# ---------------------------------------------------------------------------

_SS_LISTENER_CHECK = "ss -tln sport = :{port} | grep -q LISTEN"
_NETSTAT_LISTENER_CHECK = 'netstat -tln | grep -q ":{port} "'

# Precompute hex port in Python, then scan /proc/net/tcp for LISTEN state (0A).
_PROC_LISTENER_CHECK = (
    "while read line; do "
    "set -- $line; "
    "case $2 in *:{hex_port}) case $4 in 0A) exit 0;; esac;; esac; "
    "done < /proc/net/tcp; exit 1"
)

_PORT_STRATEGY_ORDER: list[NcPortStrategy] = ["ss", "netstat", "python", "proc"]
_LISTENER_CHECK_ORDER: list[NcListenerCheck] = ["ss", "netstat", "proc"]

# Single-round-trip probe that picks a port-finding strategy and a
# listener-check strategy in one shell invocation. `command -v` is POSIX
# (unlike `which`, which varies across distros), short-circuits on the first
# hit, and treats exit-code as the availability signal. Output is one line of
# the form "<port> <listener>", e.g. "ss ss" or "python proc".
_STRATEGY_PROBE = (
    "port=proc; listener=proc; "
    "if command -v ss >/dev/null 2>&1; then port=ss; listener=ss; "
    "elif command -v netstat >/dev/null 2>&1; then port=netstat; listener=netstat; "
    "elif command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; "
    "then port=python; "
    "fi; "
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
    connection: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
    last_err: Exception | None = None

    async def connected() -> bool:
        nonlocal connection, last_err
        try:
            connection = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                # Per-attempt bound: snappy (<= 1s) regardless of the overall
                # budget, floored at 0.1s so an attempt near the deadline edge
                # still gets a usable slice.
                timeout=min(1.0, max(0.1, deadline - asyncio.get_running_loop().time())),
            )
        except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as err:
            last_err = err
            return False
        return True

    try:
        await wait_for_async(
            connected,
            timeout,
            interval=retry_interval,
            on_timeout=f"Remote nc listener on {host}:{port} not ready within {timeout}s",
        )
    except WaitTimeoutError as expiry:
        raise ConnectionError(str(expiry)) from last_err
    # wait_for_async only returns once `connected` stored the pair.
    return cast("tuple[asyncio.StreamReader, asyncio.StreamWriter]", connection)


class NcFileTransfer(UnixFileTransfer):
    """Handles netcat file transfers for a UnixHost.

    Receives injectable callables for open_session and exec so it can be tested
    without real connections.

    Inherits ``put_files`` / ``get_files`` from :class:`BaseFileTransfer` and
    unix scaffolding (``_connections``, ``_exec_cmd``, ``_warmup_for_transfer``)
    from :class:`UnixFileTransfer`; implements the abstract ``_run_put`` /
    ``_run_get`` as direct calls to ``_put_files_nc`` / ``_get_files_nc``.
    """

    host_families = frozenset({"unix"})

    def __init__(
        self,
        connections: "ConnectionManager",
        name: str,
        transfer: str,
        nc_options: "NcOptions",
        get_local_ip: Callable[[], str],
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandResult]],
        max_filename_len: int = 255,
    ) -> None:
        super().__init__(
            connections=connections,
            name=name,
            exec_cmd=exec_cmd,
            max_filename_len=max_filename_len,
        )
        self.transfer = transfer
        self._nc_options = nc_options
        self._get_local_ip = get_local_ip
        self._resolved_port_strategy: NcPortStrategy | None = None
        self._resolved_listener_check: NcListenerCheck | None = None
        self._reserved_ports: set[int] = set()
        # Serializes nc control-plane ops (port-find, listener probe, strategy
        # probe, file-size stats). Not a correctness guard — `_exec_cmd`'s
        # telnet pool already hands concurrent callers distinct sessions — but
        # an economy one: serializing control ops makes them reuse a single
        # warm pooled session instead of fanning out and each paying a cold
        # auth handshake. Telnet only; SSH exec channels are already cheap.
        self._control_lock = asyncio.Lock()
        # Serializes port allocation so two concurrent `_find_free_port` calls
        # can't both return the same "free" port from parallel ss scans.
        self._port_lock = asyncio.Lock()
        # Serializes concurrent `prepare()` calls so the compound strategy
        # probe runs exactly once per host lifetime.
        self._prepare_lock = asyncio.Lock()

    @override
    @classmethod
    def create(cls, ctx: "TransferContext") -> "NcFileTransfer":
        if ctx.connections is None:
            raise ValueError(
                "NcFileTransfer requires a connections manager on the transfer context"
            )
        if ctx.exec_cmd is None:
            raise ValueError("NcFileTransfer requires exec_cmd on the transfer context")
        if ctx.get_local_ip is None:
            raise ValueError("NcFileTransfer requires get_local_ip on the transfer context")
        if ctx.nc_options is None:
            raise ValueError("NcFileTransfer requires nc_options on the transfer context")
        return cls(
            connections=ctx.connections,
            name=ctx.host_name,
            transfer=ctx.transfer,
            nc_options=ctx.nc_options,
            get_local_ip=ctx.get_local_ip,
            exec_cmd=ctx.exec_cmd,
            max_filename_len=ctx.max_filename_len,
        )

    @property
    def _nc_exec(self) -> str:
        return self._nc_options.exec_name

    @property
    def _nc_port(self) -> int:
        return self._nc_options.port

    @property
    def _nc_port_strategy(self) -> "NcPortStrategy":
        return self._nc_options.port_strategy

    @property
    def _nc_port_cmd(self) -> str | None:
        return self._nc_options.port_cmd

    @property
    def _nc_listener_check(self) -> "NcListenerCheck":
        return self._nc_options.listener_check

    @property
    def _nc_listener_cmd(self) -> str | None:
        return self._nc_options.listener_cmd

    @property
    def _nc_listener_timeout(self) -> int:
        """`nc -w` value — whole seconds, since nc takes an integer timeout."""
        return max(1, int(self._nc_options.listener_timeout))

    @property
    def _nc_listener_prefix(self) -> str:
        """Shell prefix giving the remote listener a hard lifetime cap.

        The reap on otto's error paths is the primary defence, and it handles
        every case where otto is alive to run it. This covers the one case it
        cannot: otto being killed outright, or the SSH channel dying, after the
        listener is up. Nothing on the remote side would then end it — OpenBSD
        netcat ignores ``-w`` for listeners — and the process holds its port
        until someone finds it. On the lab bed that took three days and six
        listeners before anyone did.

        Deliberately NOT ``listener_timeout``. That is 30s by default, and a
        cap on the *whole* listener lifetime at 30s would sever an established
        transfer of any real size mid-stream, turning a slow copy into a
        silently truncated file. This cap exists to convert "forever" into
        "finite", so it only has to be shorter than "nobody notices for days" —
        an hour does that while being far longer than any legitimate transfer
        on a lab network.

        Degrades on hosts without coreutils ``timeout``: the prefix collapses
        to empty and behaviour is exactly what it was before this existed, so a
        minimal remote can still transfer. It is a backstop, not a dependency.
        """
        return (
            f'T=""; command -v timeout >/dev/null 2>&1 '
            f'&& T="timeout {_NC_LISTENER_HARD_CAP_S}"; $T '
        )

    # ------------------------------------------------------------------
    # Protocol dispatch (implements BaseFileTransfer's abstract methods)
    # ------------------------------------------------------------------

    @override
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        return await self._get_files_nc(src_files, dest_dir, progress_factory)

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        return await self._put_files_nc(src_files, dest_dir, progress_factory)

    # ------------------------------------------------------------------
    # Netcat
    # ------------------------------------------------------------------

    @override
    async def prepare(self) -> None:
        """Resolve port + listener strategies in a single round-trip.

        Runs the shared `_STRATEGY_PROBE` script through `_control_run` so the
        port and listener strategies are resolved up front rather than lazily
        at first-transfer time. Idempotent — a second call with both
        strategies already cached is a no-op.

        Callers use `_warmup_for_transfer` to run this concurrently with
        exec-pool warming; direct callers can invoke `prepare()` alone.

        If the probe itself fails (non-zero exit, malformed output), the
        caches stay unset and the lazy cascades in `_find_free_port_auto` /
        `_resolve_listener_strategy` still kick in as fallbacks.
        """
        port_auto = self._nc_port_strategy == "auto" and self._resolved_port_strategy is None
        listener_auto = self._nc_listener_check == "auto" and self._resolved_listener_check is None
        if not (port_auto or listener_auto):
            return
        async with self._prepare_lock:
            # Re-check under the lock — another coroutine may have finished
            # while we waited.
            port_auto = self._nc_port_strategy == "auto" and self._resolved_port_strategy is None
            listener_auto = (
                self._nc_listener_check == "auto" and self._resolved_listener_check is None
            )
            if not (port_auto or listener_auto):
                return
            result = await self._control_run(_STRATEGY_PROBE)
            if result.retcode != 0:
                _logger.debug(
                    f"{self._name}: strategy probe failed (retcode={result.retcode}, "
                    f"output={result.value!r}); lazy cascades will resolve"
                )
                return
            parts = result.value.strip().split()
            if len(parts) != 2:  # noqa: PLR2004 — strategy probe output is exactly "port_choice listener_choice" (2 words)
                _logger.debug(
                    f"{self._name}: strategy probe returned malformed output "
                    f"{result.value!r}; lazy cascades will resolve"
                )
                return
            port_choice, listener_choice = parts
            if port_auto and port_choice in ("ss", "netstat", "python", "proc"):
                self._resolved_port_strategy = cast(
                    'Literal["ss", "netstat", "python", "proc"]', port_choice
                )
                _logger.debug(f"{self._name}: cached port strategy '{port_choice}' via probe")
            if listener_auto and listener_choice in ("ss", "netstat", "proc"):
                self._resolved_listener_check = cast(
                    'Literal["ss", "netstat", "proc"]', listener_choice
                )
                _logger.debug(
                    f"{self._name}: cached listener check strategy '{listener_choice}' via probe"
                )

    async def _control_run(self, cmd: str) -> CommandResult:
        """Run an nc control-plane command on the warmest available runner.

        All control-plane work (port-finding, listener probes, the strategy
        probe, remote file-size stats) goes through ``_exec_cmd`` — the same
        exec path the ``nc -l`` listeners use.

        On telnet, ``_control_lock`` serializes these calls so they reuse a
        single warm pooled session instead of fanning out and each paying a
        cold auth handshake. It is an economy measure, not a correctness one:
        the telnet exec pool already hands *concurrent* callers distinct
        sessions, so there is no shared-stdin corruption to guard against.

        On SSH, exec channels over the existing connection are cheap, so the
        calls run directly with no serialization.
        """
        if self._connections.term == "ssh":
            return await self._exec_cmd(cmd)
        async with self._control_lock:
            return await self._exec_cmd(cmd)

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
            if strategy == "auto":
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

        The cascade exists to survive a host that LACKS a tool, so an
        unreachable host is not a cascade case and stops it: the next strategy
        will not get an answer either, and trying all four burns four more
        timeouts before reporting the wrong cause ("all port-finding
        strategies failed", a HostCommandError, for a host that never
        answered). Both catch sites re-raise it ahead of their collector arm.
        """
        if self._resolved_port_strategy is None:
            await self.prepare()
        if self._resolved_port_strategy is not None:
            try:
                return await self._find_free_port_with(self._resolved_port_strategy)
            except HostUnreachableError:
                raise
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
            except HostUnreachableError:  # noqa: PERF203 — per-item resilience
                raise
            except (RuntimeError, ValueError) as e:
                errors.append(f"{strategy}: {e}")
            else:
                return port
        raise HostCommandError(
            f"All port-finding strategies failed on {self._name}: " + "; ".join(errors)
        )

    async def _find_free_port_with(self, strategy: NcPortStrategy) -> int:
        """Dispatch to a specific port-finding strategy."""
        match strategy:
            case "ss":
                return await self._find_free_port_ss()
            case "netstat":
                return await self._find_free_port_netstat()
            case "python":
                return await self._find_free_port_python()
            case "proc":
                return await self._find_free_port_proc()
            case "custom":
                return await self._find_free_port_custom()
            case _:
                raise ValueError(f"Unknown port strategy: {strategy}")

    def _reserved_str(self) -> str:
        return " ".join(str(p) for p in self._reserved_ports)

    async def _find_free_port_ss(self) -> int:
        script = _SS_PORT_SCRIPT.format(base_port=self._nc_port, reserved=self._reserved_str())
        result = await self._control_run(script)
        if result.retcode != 0:
            raise _probe_failure(result.timed_out, f"ss port scan failed: {result.value}")
        return int(result.value.strip())

    async def _find_free_port_netstat(self) -> int:
        script = _NETSTAT_PORT_SCRIPT.format(base_port=self._nc_port, reserved=self._reserved_str())
        result = await self._control_run(script)
        if result.retcode != 0:
            raise _probe_failure(result.timed_out, f"netstat port scan failed: {result.value}")
        return int(result.value.strip())

    async def _find_free_port_python(self) -> int:
        """Try ``python``, then ``python3`` for the bind-to-0 one-liner."""
        last_output = ""
        last_timed_out = False
        for exe in ("python", "python3"):
            result = await self._control_run(f'{exe} -c "{_PYTHON_PORT_CMD}"')
            if result.retcode == 0:
                return int(result.value.strip())
            last_output = result.value
            last_timed_out = result.timed_out
        raise _probe_failure(last_timed_out, f"python port discovery failed: {last_output}")

    async def _find_free_port_proc(self) -> int:
        script = _PROC_PORT_SCRIPT.format(base_port=self._nc_port, reserved=self._reserved_str())
        result = await self._control_run(script)
        if result.retcode != 0:
            raise _probe_failure(
                result.timed_out, f"/proc/net/tcp port scan failed: {result.value}"
            )
        return int(result.value.strip())

    async def _find_free_port_custom(self) -> int:
        if self._nc_port_cmd is None:
            raise ValueError("nc_port_strategy is 'custom' but nc_port_cmd is None")
        result = await self._control_run(self._nc_port_cmd)
        if result.retcode != 0:
            raise _probe_failure(result.timed_out, f"Custom port command failed: {result.value}")
        return int(result.value.strip())

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
        serializes control ops onto a single warm pooled session instead of
        paying a fresh auth handshake per call.

        The poll interval starts at 0.05 s for the first handful of
        iterations (when nc usually becomes ready on a warm session) and
        ramps up to *interval* afterward, so fast-ready listeners don't
        pay the full *interval* tax on the very first miss.
        """
        check = await self._get_listener_check_cmd(port)
        fast_interval = min(0.05, interval)

        async def listening() -> bool:
            return (await self._control_run(check)).retcode == 0

        try:
            await wait_for_async(
                listening,
                timeout,
                interval=lambda i: fast_interval if i < _NC_LISTENER_FAST_POLL_ITERS else interval,
                on_timeout=f"Remote nc listener on port {port} not ready within {timeout}s",
            )
        except WaitTimeoutError as expiry:
            raise ConnectionError(str(expiry)) from None

    async def _get_listener_check_cmd(self, port: int) -> str:
        """Return the shell command string for checking a listener on *port*."""
        strategy = self._nc_listener_check
        if strategy == "auto":
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
            if candidate == "proc":
                self._resolved_listener_check = "proc"
                _logger.debug(f"{self._name}: cached listener check strategy 'proc'")
                return "proc"
            tool = candidate  # 'ss' or 'netstat'
            result = await self._control_run(f"type {tool} >/dev/null 2>&1")
            if result.retcode == 0:
                self._resolved_listener_check = candidate
                _logger.debug(f"{self._name}: cached listener check strategy '{candidate}'")
                return candidate
        return "proc"  # pragma: no cover

    def _listener_cmd_for(self, strategy: NcListenerCheck, port: int) -> str:
        """Build the check command for a concrete (non-auto) strategy."""
        match strategy:
            case "ss":
                return _SS_LISTENER_CHECK.format(port=port)
            case "netstat":
                return _NETSTAT_LISTENER_CHECK.format(port=port)
            case "proc":
                hex_port = f"{port:04X}"
                return _PROC_LISTENER_CHECK.format(hex_port=hex_port)
            case "custom":
                if self._nc_listener_cmd is None:
                    raise ValueError("nc_listener_check is 'custom' but nc_listener_cmd is None")
                return self._nc_listener_cmd.format(port=port)
            case _:
                raise ValueError(f"Unknown listener check strategy: {strategy}")

    async def _verify_nc_dest_size(self, dst: Path, expected: int) -> Result | None:
        """Stat the remote destination and verify it matches *expected* bytes.

        Returns ``None`` on success or a failing :class:`~otto.result.Result`
        describing the mismatch. Factored out as a method so tests that
        drive ``_put_files_nc`` with mocked exec_cmd can patch the verify
        step without hand-rolling a stat response.
        """
        verify = await self._exec_cmd(f"stat -c %s {dst} 2>/dev/null || echo MISSING")
        actual_output = verify.value.strip()
        if actual_output == "MISSING":
            return Result(
                Status.Error,
                msg=f"nc transfer to {dst}: destination file missing after listen_task exit",
            )
        try:
            actual = int(actual_output)
        except ValueError:
            return Result(
                Status.Error,
                msg=f"nc transfer to {dst}: stat returned unparseable output {actual_output!r}",
            )
        if actual != expected:
            return Result(
                Status.Error,
                msg=f"nc transfer to {dst}: expected {expected} bytes, got {actual}",
            )
        return None

    async def _drain_stall_bounded(self, writer: asyncio.StreamWriter) -> None:
        """Await ``drain()``, failing only on a ZERO-progress window.

        ``drain()`` resolves when the transport un-pauses (write buffer back
        under the low-water mark), so a plain ``wait_for`` would impose a
        throughput floor, not a stall bound — a slow-but-healthy link (an
        impaired lab link shapes well below 100 KiB/s) would false-fail.
        Progress is measured directly instead: any window in which the write
        buffer shrank at all re-arms the wait; only a window with no
        observed movement raises. (The buffer gauge is burst-gated by
        socket writability, so this is a narrow throughput floor rather
        than strictly none.)
        """
        transport = writer.transport
        last = transport.get_write_buffer_size()
        while True:
            try:
                await asyncio.wait_for(writer.drain(), timeout=_NC_STALL_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError):  # noqa: PERF203 — the retry loop IS the semantics
                now = transport.get_write_buffer_size()
                if now >= last:
                    raise
                last = now
            else:
                return

    async def _close_writer_bounded(self, writer: asyncio.StreamWriter) -> None:
        """Close *writer* without letting a wedged channel hang cleanup.

        A stalled transport never flushes, so its graceful close never
        completes — past the bound the transport is aborted, releasing the
        fd, the buffered bytes, and (through a hop) the asyncssh forward
        channel. An ``OSError`` from the close handshake of an
        already-reset peer is logged, not raised: by this point the
        payload's outcome has been decided by the read loop / size checks.
        A healthy-but-glacial final flush (>2s for the sub-high-water tail)
        is sacrificed by the abort as well; PUT's size verify catches that
        and the attempt retries.
        """
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=_NC_CLOSE_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            transport = writer.transport
            if transport is not None:
                transport.abort()
        except OSError as exc:
            _logger.debug(f"{self._name}: nc writer close handshake raised {exc!r} (ignored)")

    async def _send_file_stall_bounded(
        self,
        writer: asyncio.StreamWriter,
        src: Path,
        dst: Path,
        total: int,
        handler: TransferProgressHandler | None,
    ) -> Result | None:
        """Send *src* through *writer*, every drain zero-progress-bounded.

        Returns ``None`` on success or the stall ``Result`` — the caller owns
        listener cleanup. The writer is closed on the way out either way,
        with the close handshake itself bounded (a stalled channel wedges
        that too). See ``_NC_STALL_TIMEOUT`` for the race this guards.
        """
        bytes_done = 0
        try:
            with src.open("rb") as f:
                blocks_since_drain = 0
                while True:
                    block = f.read(_NC_BLOCK_SIZE)
                    if not block:
                        break
                    writer.write(block)
                    bytes_done += len(block)
                    blocks_since_drain += 1
                    if blocks_since_drain >= _NC_DRAIN_EVERY:
                        await self._drain_stall_bounded(writer)
                        blocks_since_drain = 0
                    if handler is not None:
                        handler(str(src), str(dst), bytes_done, total)
            await self._drain_stall_bounded(writer)
        except (asyncio.TimeoutError, TimeoutError):
            return Result(
                Status.Error,
                msg=(
                    f"nc put to {dst}: no send progress for {_NC_STALL_TIMEOUT}s "
                    f"with {bytes_done} bytes written — abandoning this attempt"
                ),
            )
        finally:
            await self._close_writer_bounded(writer)
        return None

    async def _get_files_nc(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        if self._connections.has_tunnel:
            return await self._get_files_nc_tunneled(src_files, dest_dir, progress_factory)
        await self._warmup_for_transfer(len(src_files))
        local_ip = self._get_local_ip()

        # Pre-fetch remote file sizes through `_control_run` — same control-
        # plane path as the port/listener probes (telnet: serialized onto a
        # warm pooled session; ssh: direct exec).
        sizes: dict[Path, int] = {}
        for src in src_files:
            stat_result = await self._control_run(f"stat -c %s {src}")
            sizes[src] = int(stat_result.value.strip()) if stat_result.retcode == 0 else 0

        async def _get_one(src: Path) -> Result:
            dst = dest_dir / src.name
            total = sizes[src]
            handler = progress_factory() if progress_factory is not None else None
            _logger.debug(f"{self._name}: NC get {src} -> {dst}")

            done: asyncio.Future[Result] = asyncio.get_running_loop().create_future()

            async def _on_connect(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                try:
                    bytes_done = 0
                    with dst.open("wb") as f:
                        while True:
                            block = await reader.read(_NC_BLOCK_SIZE)
                            if not block:
                                break
                            f.write(block)
                            bytes_done += len(block)
                            if handler is not None:
                                handler(str(src), str(dst), bytes_done, total)
                    writer.close()
                    done.set_result(Result(Status.Success, value=dst))
                except Exception as e:  # noqa: BLE001 — nc server callback; any transfer failure maps to Error result
                    done.set_result(Result(Status.Error, msg=str(e)))

            # Port 0 lets the OS assign a free port — no collisions when
            # multiple hosts transfer concurrently.  asyncio.start_server
            # returns once the socket is bound, so no sleep is needed.
            server = await asyncio.start_server(_on_connect, "0.0.0.0", 0)  # noqa: S104 — intentional all-interface bind
            port = server.sockets[0].getsockname()[1]
            try:
                send_task = asyncio.create_task(
                    self._exec_cmd(
                        f"{self._nc_exec} -N {local_ip} {port} < {src} 2>/dev/null",
                        # Unbounded on purpose: the command's duration *is* the
                        # transfer, which scales with file size.
                        timeout=float("inf"),
                    )
                )

                def _on_send_fail(task: asyncio.Task[Any]) -> None:
                    if done.done():
                        return
                    exc = task.exception()
                    if exc is not None:
                        done.set_result(Result(Status.Error, msg=str(exc)))

                send_task.add_done_callback(_on_send_fail)
                result = await done
                await send_task
                return result
            finally:
                server.close()
                await server.wait_closed()

        gathered = await asyncio.gather(
            *(_get_one(src) for src in src_files),
            return_exceptions=True,
        )
        per_file: dict[Path, Result] = {}
        for src, outcome in zip(src_files, gathered, strict=True):
            if isinstance(outcome, BaseException):
                per_file[src] = Result(Status.Error, msg=f"{src}: {outcome}")
            else:
                per_file[src] = outcome
        return per_file

    async def _get_files_nc_tunneled(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        """Netcat GET through an SSH hop using a reversed-listener approach.

        The remote host runs ``nc -l <port> < <file>`` as a listener that
        sends file data.  Otto connects through an SSH port forward and
        reads the data — same tunnel mechanics as PUT, reversed data flow.

        Every data-phase step carries a zero-progress bound (see
        ``_NC_STALL_TIMEOUT``) and an empty transfer against a known
        non-empty size is rejected, so the LISTEN-vs-accept race surfaces
        as a fast, attributable error on the attempt — and ``_get_one``
        then retries once on a fresh port, mirroring ``_put_one``.
        """
        await self._warmup_for_transfer(len(src_files))
        # Pre-fetch remote file sizes through `_control_run` — see
        # `_get_files_nc` for the rationale. None = the stat failed, so no
        # size is known to vouch against. (A real empty file stats
        # successfully as 0; both skip the empty-transfer check below, so
        # the encoding distinguishes the WHY for the reader, not the
        # behavior.)
        sizes: dict[Path, int | None] = {}
        for src in src_files:
            stat_result = await self._control_run(f"stat -c %s {src}")
            sizes[src] = int(stat_result.value.strip()) if stat_result.retcode == 0 else None

        async def _attempt(src: Path, dst: Path) -> Result:
            total = sizes[src]
            handler = progress_factory() if progress_factory is not None else None

            port = await self._find_free_port()
            listen_task: asyncio.Task[CommandResult] | None = None
            try:
                # Remote listener sends file data to the first connecting
                # client. `-w` is passed for netcat variants that honour it on
                # a listener (GNU netcat, ncat) — but it CANNOT be relied on:
                # OpenBSD nc, the default on most distros, documents "the -w
                # flag has no effect on the -l option, i.e. nc will listen
                # forever for a connection". Measured on the bed 2026-08-10:
                # listeners spawned with `-w 30` were still alive after three
                # days. Every path out of this block must therefore reap the
                # remote listener itself (`_cancel_and_reap`), and nothing here
                # may assume a client that never arrives ends the process.
                listen_task = asyncio.create_task(
                    self._exec_cmd(
                        f"{self._nc_listener_prefix}{self._nc_exec} -Nl "
                        f"-w {self._nc_listener_timeout} {port} < {src} 2>/dev/null",
                        # No otto-side timeout: a healthy transfer of any size
                        # must not be cut off mid-flight, and this task is the
                        # only thing that observes the transfer finishing. The
                        # orphan case is bounded by `_cancel_and_reap` on every
                        # error path instead — NOT by `-w`, which OpenBSD nc
                        # ignores for listeners (see the spawn comment above).
                        timeout=float("inf"),
                    )
                )

                try:
                    await self._wait_for_remote_listener(port)
                except ConnectionError:
                    return Result(Status.Error, msg=f"Remote nc listener on port {port} not ready")

                try:
                    local_port = await asyncio.wait_for(
                        self._connections.forward_port(port),
                        timeout=_NC_FORWARD_SETUP_TIMEOUT,
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    return Result(
                        Status.Error,
                        msg=(
                            f"nc get of {src}: SSH port-forward setup for remote "
                            f"port {port} stalled beyond {_NC_FORWARD_SETUP_TIMEOUT}s"
                        ),
                    )

                try:
                    reader, writer = await _connect_with_retry(
                        "localhost",
                        local_port,
                        timeout=5.0,
                    )
                except ConnectionError:
                    return Result(
                        Status.Error, msg=f"nc listener on localhost:{local_port} not ready"
                    )

                bytes_done = 0
                try:
                    with dst.open("wb") as f:
                        while True:
                            # Zero-progress bound: an accepted-but-unserviced
                            # connection parks read() forever (probed live);
                            # any received block re-arms the window.
                            block = await asyncio.wait_for(
                                reader.read(_NC_BLOCK_SIZE), timeout=_NC_STALL_TIMEOUT
                            )
                            if not block:
                                break
                            f.write(block)
                            bytes_done += len(block)
                            if handler is not None:
                                handler(str(src), str(dst), bytes_done, total or 0)
                except (asyncio.TimeoutError, TimeoutError):
                    return Result(
                        Status.Error,
                        msg=(
                            f"nc get of {src}: no data for {_NC_STALL_TIMEOUT}s "
                            f"with {bytes_done} bytes received — abandoning this "
                            f"attempt"
                        ),
                    )
                finally:
                    await self._close_writer_bounded(writer)

                # EOF at ZERO bytes when the remote file is known non-empty:
                # the connection was dropped before the listener serviced it
                # (a clean close from this side, indistinguishable from an
                # empty send without the size). Deliberately narrow — a
                # non-zero short read is NOT failed, because the stat is a
                # pre-transfer snapshot and a growing/changing remote file
                # legitimately delivers a different byte count; and an
                # unknown size (stat failed → total is None) leaves nothing
                # to vouch against, a residual accepted and pinned rather
                # than guessed at. The narrowing also accepts a drop that
                # delivered SOME bytes as Success (a re-stat-on-mismatch
                # could split "file changed" from "dropped mid-transfer";
                # deliberately not taken in this wave).
                if total and bytes_done == 0:
                    return Result(
                        Status.Error,
                        msg=(
                            f"nc get of {src}: empty transfer (remote reports "
                            f"{total} bytes; received 0 before EOF)"
                        ),
                    )

                # Reader drained the socket to EOF; the remote nc should exit
                # now. Bound the wait so an orphaned listener can't hang us.
                # This is the ONLY bound: `-w` does not cap an OpenBSD listener
                # (see the spawn comment above), so the timeout branch below
                # must reap rather than merely report.
                try:
                    await asyncio.wait_for(
                        listen_task,
                        timeout=self._nc_options.listener_timeout,
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    return Result(
                        Status.Error,
                        msg=(
                            f"nc listener on port {port} did not exit within "
                            f"{self._nc_options.listener_timeout}s of transfer end "
                            f"(orphaned listener — likely a remote port collision)"
                        ),
                    )
                return Result(Status.Success, value=dst)
            except asyncio.CancelledError:
                # External cancellation mid-transfer skips listen_task's
                # normal join points (the ConnectionError / timeout / success
                # branches above). Cancel it and reap the remote `nc -l` so
                # it doesn't linger until its `-w` timeout — mirrors the put
                # path's `_attempt` handler (todo/chaos-teardown-followups.md
                # §1: pre-fix this listener outlived the 10s teardown
                # deadline by up to 20s). compensate() holds any FURTHER
                # cancellation until the reap resolves (chaos spec: shielded
                # compensating actions) — without it a second Ctrl+C tears
                # the reap and strands the listener after all.
                if listen_task is not None and not listen_task.done():
                    # Imported here, not at module scope: otto.lifecycle is
                    # only needed once a compensating action actually runs,
                    # and a top-level import drags it onto every CLI --help
                    # path (import-budget guard).
                    from ...lifecycle import compensate

                    await compensate(
                        self._cancel_and_reap(listen_task, port),
                        what=f"{self._name}: nc get listener reap (port {port})",
                    )
                raise
            finally:
                # Reap here, not per-branch. The ten error branches that used to
                # be patched individually were found by grepping
                # `listen_task.cancel()`, and an ELEVENTH path had no cancel to
                # find: PUT's bare `await self._wait_for_remote_listener(port)`
                # raises ConnectionError straight past every handler except
                # CancelledError, stranding both the remote listener and the
                # local task. A guard built from the same enumeration inherited
                # the same blind spot and reported clean. One exit covers every
                # branch, including ones nobody has written yet, and it runs
                # BEFORE `_release_port` so the port is never handed out while a
                # listener still holds it.
                if listen_task is not None and not listen_task.done():
                    await self._cancel_and_reap(listen_task, port)
                # Release the local forward with the remote port, for the same
                # reason and in the same place. Caching in the transport only
                # bounds the leak where the destination repeats; these files
                # transfer concurrently on distinct ports, so without this each
                # one strands a listening socket until the host closes. After
                # the reap, which needs the forward to reach the listener.
                self._connections.unforward_port(port)
                self._release_port(port)

        async def _get_one(src: Path) -> Result:
            dst = dest_dir / src.name
            _logger.debug(f"{self._name}: NC get (tunneled) {src} -> {dst}")
            result = await _attempt(src, dst)
            if not result.is_ok:
                # One retry on the narrow listener-readiness race, on a fresh
                # port — mirrors `_put_one`. A second failure is almost
                # certainly a real problem and should propagate.
                _logger.debug(f"{self._name}: NC get retry after: {result.msg}")
                result = await _attempt(src, dst)
            return result

        gathered = await asyncio.gather(
            *(_get_one(src) for src in src_files),
            return_exceptions=True,
        )
        per_file: dict[Path, Result] = {}
        for src, outcome in zip(src_files, gathered, strict=True):
            if isinstance(outcome, BaseException):
                per_file[src] = Result(Status.Error, msg=f"{src}: {outcome}")
            else:
                per_file[src] = outcome
        return per_file

    async def _reap_nc_listener(self, port: int) -> None:
        """Best-effort: make a lingering remote ``nc -l`` exit immediately.

        ``nc -l ... < /dev/null`` exits as soon as a TCP peer connects and
        then disconnects. When a transfer is cancelled before its real
        sender ever connects, the listener would otherwise linger FOREVER on
        an OpenBSD netcat, which ignores ``-w`` for listeners. A throwaway
        connect-and-close reaps it now. This is not an optimisation over
        waiting out ``-w`` — it is the only thing that ends the process.

        Fully best-effort: a cancellation can land while the listener is
        still launching, so ``_connect_with_retry`` is given a short budget
        to catch a not-yet-bound port; if it never appears we simply give up
        (there was nothing to reap).
        """
        if self._connections.has_tunnel:
            try:
                host = "localhost"
                # Bounded, and the bound is load-bearing rather than tidy: one
                # of the paths that now reaps is the forward-setup timeout
                # itself, so this call can be re-entering the very forward that
                # just stalled. Unbounded, the reap would park there forever and
                # convert a bounded failure into a hang — caught by
                # `test_a_stalled_forward_setup_fails_the_attempt` the moment
                # that branch started reaping. Giving up is the right answer:
                # a listener behind a wedged hop is unreachable by definition,
                # and the remote-side hard cap is what ends it.
                target_port = await asyncio.wait_for(
                    self._connections.forward_port(port),
                    timeout=_NC_FORWARD_SETUP_TIMEOUT,
                )
            except Exception:  # noqa: BLE001 — port-forward setup failed or stalled; nothing to reap, silently return
                return
        else:
            host = self._connections.ip
            target_port = port

        try:
            _, writer = await _connect_with_retry(host, target_port, timeout=2.0)
        except (ConnectionError, OSError):
            return  # listener never came up — nothing to reap
        writer.close()
        with suppress(asyncio.TimeoutError, OSError):
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)

    async def _cancel_and_reap(self, listen_task: "asyncio.Task[CommandResult]", port: int) -> None:
        """Join a cancelled listener task (get or put path), then reap the remote ``nc -l``.

        ``suppress(Exception)``, not ``BaseException``: this runs inside
        ``compensate()``'s shield, so a genuine ``CancelledError`` landing
        here (compensate's own deadline-fired ``task.cancel()``, or a caller
        cancellation racing the join) must propagate — swallowing it would
        let the reap start fresh network I/O AFTER the deadline already
        fired, which the caller's one remaining cancel can't then kill.
        ``gather(..., return_exceptions=True)`` only raises when THIS await
        itself is cancelled, so ``Exception`` alone still covers every
        listener-join failure ``_reap_nc_listener`` doesn't already
        best-effort internally.
        """
        if listen_task.done():
            # Nothing to reap: our nc already accepted and exited. Matters on
            # the GET empty-transfer branch, the DESIGNED recovery for the
            # accept-window race — without this it burns a full
            # `_connect_with_retry` against a closed port on every retry.
            return
        listen_task.cancel()
        with suppress(Exception):
            await asyncio.gather(listen_task, return_exceptions=True)
        with suppress(Exception):
            await self._reap_nc_listener(port)

    async def _put_files_nc(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        # Fire strategy-probe + pool-warming concurrently so the
        # first-transfer handshakes don't stack up serially on the critical
        # path. On a warm host this is a no-op.
        await self._warmup_for_transfer(len(src_files))

        async def _attempt(src: Path, dst: Path) -> Result:
            # Use an ephemeral port on the remote side so multiple host objects
            # targeting the same IP don't collide.  `_find_free_port` holds a
            # lock so concurrent callers can't both reserve the same port.
            port = await self._find_free_port()
            listen_task: asyncio.Task[CommandResult] | None = None
            try:
                # If a racing process bound the same port first, our sender's
                # bytes go to *its* listener and ours never gets a connection.
                # `-w` is passed for netcat variants that honour it on a
                # listener, but OpenBSD nc — the distro default — documents
                # that "-w has no effect on the -l option"; such a listener
                # waits forever. `_cancel_and_reap` on every error path is what
                # actually ends it.
                listen_task = asyncio.create_task(
                    self._exec_cmd(
                        f"{self._nc_listener_prefix}{self._nc_exec} -l "
                        f"-w {self._nc_listener_timeout} {port} "
                        f"< /dev/null > {dst} 2>/dev/null",
                        # No otto-side timeout: a healthy transfer of any size
                        # must not be cut off mid-flight. The orphan case is
                        # bounded by `_cancel_and_reap` on every error path
                        # instead — NOT by `-w` (see the spawn comment above).
                        timeout=float("inf"),
                    )
                )

                # Confirm the remote nc is actually listening before we try to
                # connect. Launching nc over SSH (or telnet) can exceed
                # `_connect_with_retry`'s budget on a loaded system; for tunnel
                # paths the local asyncssh listener accepts immediately
                # regardless, hiding the not-yet-listening remote entirely.
                # `_wait_for_remote_listener` routes through `_control_run`,
                # which on telnet hosts serializes probes onto one warm
                # pooled session instead of paying a fresh handshake each.
                await self._wait_for_remote_listener(port)
                if self._connections.has_tunnel:
                    try:
                        local_port = await asyncio.wait_for(
                            self._connections.forward_port(port),
                            timeout=_NC_FORWARD_SETUP_TIMEOUT,
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        return Result(
                            Status.Error,
                            msg=(
                                f"nc put to {dst}: SSH port-forward setup for remote "
                                f"port {port} stalled beyond {_NC_FORWARD_SETUP_TIMEOUT}s"
                            ),
                        )
                    connect_host = "localhost"
                    connect_port = local_port
                else:
                    connect_host = self._connections.ip
                    connect_port = port

                # Adaptive retry — connects as soon as the remote nc listener is
                # ready. Allow more time when tunneled (extra hop latency).
                if self._connections.has_tunnel or self._connections.term == "telnet":
                    timeout = 5.0
                else:
                    timeout = 2.0

                try:
                    _, writer = await _connect_with_retry(
                        connect_host,
                        connect_port,
                        timeout=timeout,
                    )
                except ConnectionError:
                    return Result(
                        Status.Error, msg=f"nc listener on {connect_host}:{connect_port} not ready"
                    )

                total = src.stat().st_size
                handler = progress_factory() if progress_factory is not None else None

                stall = await self._send_file_stall_bounded(writer, src, dst, total, handler)
                if stall is not None:
                    return stall

                # The sender has pushed every byte and closed the socket, so
                # the remote nc should see EOF and exit immediately. If it
                # doesn't, this listener is orphaned (a racing process won the
                # port and took our connection); bound the wait so a never-
                # exiting nc — or a wedged control channel — can't hang the
                # transfer. `-w` does NOT cap an OpenBSD listener; this is the
                # asyncio-level backstop. On timeout, surface an error and let
                # `_put_one`'s retry take another port.
                try:
                    await asyncio.wait_for(
                        listen_task,
                        timeout=self._nc_options.listener_timeout,
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    return Result(
                        Status.Error,
                        msg=(
                            f"nc listener on port {port} did not exit within "
                            f"{self._nc_options.listener_timeout}s of transfer end "
                            f"(orphaned listener — likely a remote port collision)"
                        ),
                    )

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
            except asyncio.CancelledError:
                # External cancellation mid-transfer skips listen_task's
                # normal join points (the success / ConnectionError / timeout
                # branches below the create_task). Cancel it and reap the
                # remote `nc -l` so it doesn't linger until its `-w` timeout.
                # A writer opened in the send loop is already closed by that
                # loop's own `finally` — which makes nc exit on its own — so
                # this matters mainly for a cancel landing before the sender
                # ever connects. compensate() holds any FURTHER cancellation
                # until the reap resolves (chaos spec: shielded compensating
                # actions) — without it a second Ctrl+C tears the reap and
                # strands the listener after all.
                if listen_task is not None and not listen_task.done():
                    # Imported here, not at module scope: otto.lifecycle is only
                    # needed once a compensating action actually runs, and a
                    # top-level import drags it onto every CLI --help path
                    # (import-budget guard).
                    from ...lifecycle import compensate

                    await compensate(
                        self._cancel_and_reap(listen_task, port),
                        what=f"{self._name}: nc listener reap (port {port})",
                    )
                raise
            else:
                return Result(Status.Success, value=dst)
            finally:
                # Reap here, not per-branch. The ten error branches that used to
                # be patched individually were found by grepping
                # `listen_task.cancel()`, and an ELEVENTH path had no cancel to
                # find: PUT's bare `await self._wait_for_remote_listener(port)`
                # raises ConnectionError straight past every handler except
                # CancelledError, stranding both the remote listener and the
                # local task. A guard built from the same enumeration inherited
                # the same blind spot and reported clean. One exit covers every
                # branch, including ones nobody has written yet, and it runs
                # BEFORE `_release_port` so the port is never handed out while a
                # listener still holds it.
                if listen_task is not None and not listen_task.done():
                    await self._cancel_and_reap(listen_task, port)
                # Release the local forward with the remote port, for the same
                # reason and in the same place. Caching in the transport only
                # bounds the leak where the destination repeats; these files
                # transfer concurrently on distinct ports, so without this each
                # one strands a listening socket until the host closes. After
                # the reap, which needs the forward to reach the listener.
                self._connections.unforward_port(port)
                self._release_port(port)

        async def _put_one(src: Path) -> Result:
            dst = dest_dir / src.name
            _logger.debug(f"{self._name}: NC put {src} -> {dst}")
            result = await _attempt(src, dst)
            if not result.is_ok:
                # One retry on the narrow listener-readiness race. A second
                # failure is almost certainly a real problem (bad port,
                # permissions, disk full) and should propagate.
                _logger.debug(f"{self._name}: NC put retry after: {result.msg}")
                result = await _attempt(src, dst)
            return result

        gathered = await asyncio.gather(
            *(_put_one(src) for src in src_files),
            return_exceptions=True,
        )
        per_file: dict[Path, Result] = {}
        for src, outcome in zip(src_files, gathered, strict=True):
            if isinstance(outcome, BaseException):
                per_file[src] = Result(Status.Error, msg=f"{src}: {outcome}")
            else:
                per_file[src] = outcome
        if all(r.is_ok for r in per_file.values()):
            _logger.debug("Finished nc transfers")
        return per_file


register_transfer_backend("nc", NcFileTransfer)
