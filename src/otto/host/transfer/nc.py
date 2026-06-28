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

from typing_extensions import override

from ...logger import get_otto_logger
from ...utils import CommandStatus, Status
from .base import (
    NcListenerCheck,
    NcPortStrategy,
    TransferContext,
    TransferProgressFactory,
    _first_error,
)
from .registry import register_transfer_backend
from .unix_base import UnixFileTransfer

_NC_BLOCK_SIZE = 8192

# Drain the nc writer every N blocks so `bytes_done` reported to the progress
# handler tracks bytes that have actually left the process, not bytes buffered
# inside `StreamWriter`. Too small = an await per 8 KB (death by context switch);
# too large = laggy progress. 64 blocks ≈ 512 KB gives smooth updates on a
# 12 MB/s link while keeping the overhead negligible.
_NC_DRAIN_EVERY = 64

_logger = get_otto_logger()

# ---------------------------------------------------------------------------
# Shell script templates for port-finding strategies
# ---------------------------------------------------------------------------

# Port scripts run inside `( ... )` so their `exit 0` / `exit 1` only
# terminates the subshell. Without the subshell wrap, a failure-path `exit`
# kills the whole telnet control session, forcing a 1–2 s reopen on every
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
    while True:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=min(1.0, max(0.1, deadline - asyncio.get_running_loop().time())),
            )
        except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as err:
            if asyncio.get_running_loop().time() >= deadline:
                raise ConnectionError(
                    f"Remote nc listener on {host}:{port} not ready within {timeout}s"
                ) from err
            await asyncio.sleep(retry_interval)


class NcFileTransfer(UnixFileTransfer):
    """Handles netcat file transfers for a UnixHost.

    Receives injectable callables for open_session and oneshot so it can be tested
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
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandStatus]],
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
        assert ctx.connections is not None
        assert ctx.exec_cmd is not None
        assert ctx.get_local_ip is not None
        assert ctx.nc_options is not None
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

    # ------------------------------------------------------------------
    # Protocol dispatch (implements BaseFileTransfer's abstract methods)
    # ------------------------------------------------------------------

    @override
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> tuple[Status, str]:
        return await self._get_files_nc(src_files, dest_dir, progress_factory)

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> tuple[Status, str]:
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

    async def _control_run(self, cmd: str) -> CommandStatus:
        """Run an nc control-plane command on the warmest available runner.

        All control-plane work (port-finding, listener probes, the strategy
        probe, remote file-size stats) goes through ``_exec_cmd`` — the same
        oneshot exec path the ``nc -l`` listeners use.

        On telnet, ``_control_lock`` serializes these calls so they reuse a
        single warm pooled session instead of fanning out and each paying a
        cold auth handshake. It is an economy measure, not a correctness one:
        the telnet oneshot pool already hands *concurrent* callers distinct
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
        last_output = ""
        for exe in ("python", "python3"):
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
        serializes control ops onto a single warm pooled session instead of
        paying a fresh auth handshake per call.

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

    async def _verify_nc_dest_size(self, dst: Path, expected: int) -> tuple[Status, str] | None:
        """Stat the remote destination and verify it matches *expected* bytes.

        Returns ``None`` on success or an ``(Status.Error, msg)`` tuple
        describing the mismatch. Factored out as a method so tests that
        drive ``_put_files_nc`` with mocked exec_cmd can patch the verify
        step without hand-rolling a stat response.
        """
        verify = await self._exec_cmd(f"stat -c %s {dst} 2>/dev/null || echo MISSING")
        actual_output = verify.output.strip()
        if actual_output == "MISSING":
            return (
                Status.Error,
                f"nc transfer to {dst}: destination file missing after listen_task exit",
            )
        try:
            actual = int(actual_output)
        except ValueError:
            return (
                Status.Error,
                f"nc transfer to {dst}: stat returned unparseable output {actual_output!r}",
            )
        if actual != expected:
            return Status.Error, f"nc transfer to {dst}: expected {expected} bytes, got {actual}"
        return None

    async def _get_files_nc(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
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
            sizes[src] = int(stat_result.output.strip()) if stat_result.retcode == 0 else 0

        async def _get_one(src: Path) -> tuple[Status, str]:
            dst = dest_dir / src.name
            total = sizes[src]
            handler = progress_factory() if progress_factory is not None else None
            _logger.debug(f"{self._name}: NC get {src} -> {dst}")

            done: asyncio.Future[tuple[Status, str]] = asyncio.get_running_loop().create_future()

            async def _on_connect(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                try:
                    bytes_done = 0
                    with open(dst, "wb") as f:
                        while True:
                            block = await reader.read(_NC_BLOCK_SIZE)
                            if not block:
                                break
                            f.write(block)
                            bytes_done += len(block)
                            if handler is not None:
                                handler(str(src), str(dst), bytes_done, total)
                    writer.close()
                    done.set_result((Status.Success, ""))
                except Exception as e:
                    done.set_result((Status.Error, str(e)))

            # Port 0 lets the OS assign a free port — no collisions when
            # multiple hosts transfer concurrently.  asyncio.start_server
            # returns once the socket is bound, so no sleep is needed.
            server = await asyncio.start_server(_on_connect, "0.0.0.0", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                send_task = asyncio.create_task(
                    self._exec_cmd(
                        f"{self._nc_exec} -N {local_ip} {port} < {src} 2>/dev/null",
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
            *(_get_one(src) for src in src_files),
            return_exceptions=True,
        )
        return _first_error(results)

    async def _get_files_nc_tunneled(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        """Netcat GET through an SSH hop using a reversed-listener approach.

        The remote host runs ``nc -l <port> < <file>`` as a listener that
        sends file data.  Otto connects through an SSH port forward and
        reads the data — same tunnel mechanics as PUT, reversed data flow.
        """
        await self._warmup_for_transfer(len(src_files))
        # Pre-fetch remote file sizes through `_control_run` — see
        # `_get_files_nc` for the rationale.
        sizes: dict[Path, int] = {}
        for src in src_files:
            stat_result = await self._control_run(f"stat -c %s {src}")
            sizes[src] = int(stat_result.output.strip()) if stat_result.retcode == 0 else 0

        async def _get_one(src: Path) -> tuple[Status, str]:
            dst = dest_dir / src.name
            total = sizes[src]
            handler = progress_factory() if progress_factory is not None else None
            _logger.debug(f"{self._name}: NC get (tunneled) {src} -> {dst}")

            port = await self._find_free_port()
            try:
                # Remote listener sends file data to the first connecting
                # client. `-w` bounds the wait for that client so an orphaned
                # listener (lost a port-collision race) self-terminates rather
                # than leaking and hanging the `await listen_task` below.
                listen_task = asyncio.create_task(
                    self._exec_cmd(
                        f"{self._nc_exec} -Nl -w {self._nc_listener_timeout} "
                        f"{port} < {src} 2>/dev/null",
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
                        "localhost",
                        local_port,
                        timeout=5.0,
                    )
                except ConnectionError:
                    listen_task.cancel()
                    await asyncio.gather(listen_task, return_exceptions=True)
                    return Status.Error, f"nc listener on localhost:{local_port} not ready"

                try:
                    bytes_done = 0
                    with open(dst, "wb") as f:
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

                # Reader drained the socket to EOF; the remote nc should exit
                # now. Bound the wait so an orphaned listener can't hang us —
                # `-w` caps it remote-side, this is the asyncio backstop.
                try:
                    await asyncio.wait_for(
                        listen_task,
                        timeout=self._nc_options.listener_timeout,
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    listen_task.cancel()
                    await asyncio.gather(listen_task, return_exceptions=True)
                    return Status.Error, (
                        f"nc listener on port {port} did not exit within "
                        f"{self._nc_options.listener_timeout}s of transfer end "
                        f"(orphaned listener — likely a remote port collision)"
                    )
                return Status.Success, ""
            finally:
                self._release_port(port)

        results: list[tuple[Status, str] | BaseException] = await asyncio.gather(
            *(_get_one(src) for src in src_files),
            return_exceptions=True,
        )
        return _first_error(results)

    async def _reap_nc_listener(self, port: int) -> None:
        """Best-effort: make a lingering remote ``nc -l`` exit immediately.

        ``nc -l ... < /dev/null`` exits as soon as a TCP peer connects and
        then disconnects. When a transfer is cancelled before its real
        sender ever connects, the listener would otherwise linger until its
        ``-w`` timeout. A throwaway connect-and-close reaps it now.

        Fully best-effort: a cancellation can land while the listener is
        still launching, so ``_connect_with_retry`` is given a short budget
        to catch a not-yet-bound port; if it never appears we simply give up
        (there was nothing to reap).
        """
        if self._connections.has_tunnel:
            try:
                host = "localhost"
                target_port = await self._connections.forward_port(port)
            except Exception:
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

    async def _put_files_nc(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> tuple[Status, str]:
        # Fire strategy-probe + pool-warming concurrently so the
        # first-transfer handshakes don't stack up serially on the critical
        # path. On a warm host this is a no-op.
        await self._warmup_for_transfer(len(src_files))

        async def _attempt(src: Path, dst: Path) -> tuple[Status, str]:
            # Use an ephemeral port on the remote side so multiple host objects
            # targeting the same IP don't collide.  `_find_free_port` holds a
            # lock so concurrent callers can't both reserve the same port.
            port = await self._find_free_port()
            listen_task: asyncio.Task[CommandStatus] | None = None
            try:
                # `-w` bounds how long this listener waits for a client. If a
                # racing process bound the same port first, our sender's bytes
                # go to *its* listener and ours never gets a connection — `-w`
                # makes it self-terminate instead of leaking and hanging us.
                listen_task = asyncio.create_task(
                    self._exec_cmd(
                        f"{self._nc_exec} -l -w {self._nc_listener_timeout} {port} "
                        f"< /dev/null > {dst} 2>/dev/null",
                        timeout=None,
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
                    local_port = await self._connections.forward_port(port)
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
                    listen_task.cancel()
                    await asyncio.gather(listen_task, return_exceptions=True)
                    return Status.Error, f"nc listener on {connect_host}:{connect_port} not ready"

                total = src.stat().st_size
                bytes_done = 0
                handler = progress_factory() if progress_factory is not None else None

                try:
                    with open(src, "rb") as f:
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

                # The sender has pushed every byte and closed the socket, so
                # the remote nc should see EOF and exit immediately. If it
                # doesn't, this listener is orphaned (a racing process won the
                # port and took our connection); bound the wait so a never-
                # exiting nc — or a wedged control channel — can't hang the
                # transfer. `-w` already caps it remote-side; this is the
                # asyncio-level backstop. On timeout, surface an error and let
                # `_put_one`'s retry take another port.
                try:
                    await asyncio.wait_for(
                        listen_task,
                        timeout=self._nc_options.listener_timeout,
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    listen_task.cancel()
                    await asyncio.gather(listen_task, return_exceptions=True)
                    return Status.Error, (
                        f"nc listener on port {port} did not exit within "
                        f"{self._nc_options.listener_timeout}s of transfer end "
                        f"(orphaned listener — likely a remote port collision)"
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
                return Status.Success, ""
            except asyncio.CancelledError:
                # External cancellation mid-transfer skips listen_task's
                # normal join points (the success / ConnectionError / timeout
                # branches below the create_task). Cancel it and reap the
                # remote `nc -l` so it doesn't linger until its `-w` timeout.
                # A writer opened in the send loop is already closed by that
                # loop's own `finally` — which makes nc exit on its own — so
                # this matters mainly for a cancel landing before the sender
                # ever connects.
                if listen_task is not None and not listen_task.done():
                    listen_task.cancel()
                    with suppress(BaseException):
                        await asyncio.gather(listen_task, return_exceptions=True)
                    with suppress(BaseException):
                        await self._reap_nc_listener(port)
                raise
            finally:
                self._release_port(port)

        async def _put_one(src: Path) -> tuple[Status, str]:
            dst = dest_dir / src.name
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
            *(_put_one(src) for src in src_files),
            return_exceptions=True,
        )
        r = _first_error(results)
        if r[0].is_ok:
            _logger.debug("Finished nc transfers")
        return r


register_transfer_backend("nc", NcFileTransfer)
