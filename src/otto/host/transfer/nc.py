"""Unix/SSH-based file transfer backends (netcat) for UnixHost.

Registers ``nc`` into the shared transfer registry on import.

**THE GET DIRECTION ASKS THE DEVICE FOR ONE OPTION FIRST.** Both GET paths make
the device run ``nc -N`` -- the plain one to SEND, the tunnelled one to LISTEN
as ``nc -Nl`` -- and ``-N`` is an OpenBSD netcat option that BusyBox's applet
rejects outright. :func:`refuse_if_nc_rejects_dash_n` declines those up front on
a device measured to reject it, rather than letting the failure arrive as a
local server waiting for a peer that will never connect. The PUT direction is
deliberately not gated on that answer; the guard's own docstring says why.
"""

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from ..connections import ConnectionManager
    from ..options import NcOptions
    from ..userland import Userland

import logging

from typing_extensions import override

from ...result import CommandResult, Result
from ...utils import Status, WaitTimeoutError, wait_for_async
from ..errors import HostCommandError, HostUnreachableError
from ..userland import NC_APPLET, NC_DASH_N_REJECTED, refuse_if_gapped
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

# Hard lifetime cap for a remote `nc -l`, applied via `timeout` (see
# `_nc_listener_prefix`). One hour, not `listener_timeout`: this is the backstop
# for otto dying with a listener up, so it only needs to beat "unnoticed for
# days" — and capping an ESTABLISHED transfer would truncate large files.
_NC_LISTENER_HARD_CAP_S = 3600

# The cap's spelling per `timeout` calling convention, keyed by
# `Userland.timeout_style`. Measured against real BusyBox binaries: `-t SECS
# PROG` works up to 1.28.1, bare `SECS PROG` from 1.31.0, and the two are
# mutually exclusive on every build tested.
#
# `absent` is deliberately NOT a key, and neither is any style this table has
# not been taught: both degrade to no prefix at all, which is exactly the
# behaviour from before the cap existed. Wrapping in a convention the remote
# rejects would be worse than not wrapping — the applet fails to exec and takes
# the listener with it. Which styles exist is fixed by the `Literal` on
# `UserlandOptionsSpec.timeout_style`, and the coverage of this table against
# that vocabulary is pinned in tests/unit/host/test_transfer_nc_listener_reap.py.
_TIMEOUT_STYLE_PREFIXES = {
    "coreutils": f"timeout {_NC_LISTENER_HARD_CAP_S} ",
    "dash-t": f"timeout -t {_NC_LISTENER_HARD_CAP_S} ",
}

# How many per-file transfers may be in flight at once, derived from a channel
# budget rather than picked. A default OpenSSH server allows `MaxSessions 10`
# channels per CONNECTION and REFUSES the rest — it does not queue them — so an
# unbounded fan-out over the files turns "many files" into
# `ChannelOpenError('open failed')` for whichever transfers lose. Measured on a
# hopped host against a default sshd, concurrent execs give exactly
# `refused = N - 10`: N=10 refuses none, N=12 refuses 2, N=32 refuses 22.
#
# The same ceiling reached from the other side reads as
# "Remote nc listener on port P not ready within 5.0s": the readiness poll needs
# a channel too, so once the budget is gone a perfectly healthy listener cannot
# be confirmed.
#
# The bound is on whole TRANSFERS, not on channels, and that is load-bearing: a
# semaphore at the channel layer DEADLOCKS here, because an in-flight listener
# holds its channel while its own readiness poll asks for a second one — enough
# listeners would take every permit and block the polls that must finish to
# release them.
_NC_SSHD_DEFAULT_MAX_SESSIONS = 10
# One for the `nc -l` listener, held for the whole transfer; one for the
# readiness poll that runs while it is held.
_NC_CHANNELS_PER_TRANSFER = 2
# Left for the pooled control session and the exec the caller may already be
# inside. Without it a full budget would sit exactly at the ceiling, where any
# other concurrent exec on the same connection is the one that gets refused.
_NC_CHANNEL_HEADROOM = 2
_NC_MAX_CONCURRENT_TRANSFERS = (
    _NC_SSHD_DEFAULT_MAX_SESSIONS - _NC_CHANNEL_HEADROOM
) // _NC_CHANNELS_PER_TRANSFER

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


NC_DASH_N_CAPABILITY = "nc_dash_n"
"""The capability key this backend's refusal turns on.

Spelled once and read once, by
:meth:`~otto.host.userland.Userland.is_settled`, which validates it against the
capabilities that module resolves -- so a typo raises there rather than becoming
a condition that quietly never fires. The VALUE is read through
:attr:`~otto.host.userland.Userland.nc_dash_n`, a property, which cannot be
misspelled at all.
"""


async def refuse_if_nc_rejects_dash_n(
    userland: "Userland | None", *, exec_name: str, host: str = "", attempted: str = ""
) -> None:
    """Refuse a netcat GET to a device whose ``nc`` was measured to reject ``-N``.

    **The gap registry's sixth product call site**, and the first whose
    predicate is an OPTION rather than a presence. Everything otto knows about
    this failure lives in the ``nc-transfer`` record in
    :data:`~otto.host.userland.GAPS`; this function supplies the only thing a
    record cannot -- whether THIS device is one the measurement covers -- and
    hands the raise back to :func:`~otto.host.userland.refuse_if_gapped` so the
    message is the record's and not a second, drifting copy of it. Downgrading
    that record to ``untested`` stops the refusal.

    **PRESENCE WAS NEVER THE QUESTION HERE, WHICH IS WHY THIS IS NOT THE
    ``scp`` GUARD WITH A DIFFERENT NAME.** ``nc`` is present on all five matrix
    artifacts; what BusyBox's applet does not have is the OPTION. So the
    predicate is a SETTLED :attr:`~otto.host.userland.Userland.nc_dash_n` of
    :data:`~otto.host.userland.NC_DASH_N_REJECTED` -- the device ran its own
    ``nc`` and the option did not parse. Read
    ``Userland._probe_nc_dash_n`` for how that is
    measured without connecting anything.

    **IT KEYS ON THE BINARY otto WILL ACTUALLY EXEC, WHICH IS WHY IT TAKES
    *exec_name*.** :attr:`~otto.host.options.NcOptions.exec_name` lets an
    operator point this backend at ``ncat``, ``netcat``, or an absolute path,
    and the capability is an answer about the name
    :data:`~otto.host.userland.NC_APPLET` (``nc``) alone. Measuring one binary
    and refusing on behalf of another is exactly the false refusal the
    ``nc-transfer`` record warns about -- a BusyBox device with a real OpenBSD
    netcat installed alongside transfers perfectly well -- so when *exec_name*
    is anything but that name this returns without refusing, whatever the
    device answered. The cost is stated rather than hidden: such a host is
    never protected by this guard, and gets the same timeout it got before the
    guard existed. What the shared name buys, when they DO match, is that the
    probe and the transfer resolve it through the same ``Host.exec`` and the
    same ``PATH`` -- so a locally-installed ``/usr/local/bin/nc`` shadowing the
    applet is measured, not assumed away.

    **THE PUT PATH IS DELIBERATELY NOT REFUSED FROM THIS ANSWER**, and the
    asymmetry is the honest one rather than an omission.
    ``NcFileTransfer._put_files_nc`` spawns ``nc -l -w SECS PORT``, which
    carries no ``-N`` at all: it is broken on BusyBox for a DIFFERENT reason
    (the applet spells a listener ``-l -p PORT``), and nothing measured here
    establishes that a netcat rejecting ``-N`` also rejects the OpenBSD
    listener form. The two facts coincide on every matrix row and are still two
    facts. Settling the second one would mean asking a device to LISTEN, which
    binds a port -- a probe with a side effect on the host it is asking about,
    and one that can leave a process behind on exactly the devices otto is
    least able to clean up. So that path stays ``PATH_OPEN`` in the record,
    which is what it already was.

    **IT RESPECTS ``is_settled``**, like every refusing consumer here. The
    cannot-ask default for this capability is
    :data:`~otto.host.userland.NC_DASH_N_SUPPORTED`, so an unsettled host
    currently reads as "the option parses" and never reaches the refusal --
    but the gate is written anyway, because what makes it redundant is a VALUE
    that can change, and because the alternative is that an sshd at its
    ``MaxSessions`` ceiling (the very condition a bulk transfer creates) turns
    a refused probe round into a verdict that this device cannot send files.
    ``test_a_probe_round_that_never_arrived_is_not_refused`` holds it by
    flipping exactly that default.

    **A host with no resolver at all (``userland is None``) is likewise not
    refused.** ``_userland()`` is an overridable hook whose base implementation
    answers ``None``, and :class:`NcFileTransfer` accepts such a context rather
    than rejecting it -- see its ``__init__``. Nothing has been measured about
    such a host, so there is nothing to refuse from.

    **IT COSTS NO ROUND TRIP THIS PATH DID NOT ALREADY PAY**, unlike
    :func:`otto.host.transfer.scp.refuse_if_scp_is_absent`, which added a
    resolution to a path that awaited none. :meth:`NcFileTransfer.prepare`
    already resolves the userland as its FIRST statement on every transfer, and
    :meth:`~otto.host.userland.Userland.resolve` is idempotent once settled, so
    the ``await`` here is the same round the listener cap
    (``NcFileTransfer._nc_listener_prefix``) was already awaiting. It is
    awaited HERE rather than left to ``prepare`` because the guard runs before
    ``_warmup_for_transfer`` -- refusing before the pool is warmed is the point.
    What the CAPABILITY costs, on every host and not just this path, is two
    probes in the resolution round; that is argued at
    ``Userland._probe_nc_dash_n``.

    Args:
        userland: the host's capability resolver, from its ``_userland()``
            hook, or ``None`` when the host has none.
            :meth:`~otto.host.userland.Userland.resolve` is awaited here, so the
            caller does not have to.
        exec_name: the netcat this backend will exec --
            :attr:`~otto.host.options.NcOptions.exec_name`. Not decoration: it
            decides whether the measurement is about otto's binary at all.
        host: the host's name. Decorates the message; changes no verdict.
        attempted: what the caller was doing, in its own words.

    Raises:
        ~otto.host.errors.UnsupportedOnUserlandError: this device settled
            ``nc_dash_n`` on ``rejected``, otto would exec that same ``nc``, and
            the ``nc-transfer`` record is ``measured-broken``. Nothing is sent,
            no listener is spawned, and no local server is bound.
    """
    if userland is None:
        return
    if exec_name != NC_APPLET:
        return
    await userland.resolve()
    if not userland.is_settled(NC_DASH_N_CAPABILITY):
        return
    if userland.nc_dash_n != NC_DASH_N_REJECTED:
        return
    refuse_if_gapped("nc-transfer", host=host, attempted=attempted)


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
        *,
        userland: "Userland | None",
    ) -> None:
        """Build an nc backend. See :meth:`_nc_listener_prefix` for *userland*.

        *userland* is the host's shared capability resolver, and it is the only
        thing that decides which ``timeout`` calling convention the hard cap is
        spelled in. :class:`~otto.host.unix_host.UnixHost` supplies its own
        through :class:`~otto.host.transfer.base.TransferContext`, so the
        production path always has one.

        **Required, with no default, and that is the point.** ``None`` is
        still a legal answer — a caller constructing this directly, with no
        host behind it, has no resolver to give — and it degrades to the
        documented backstop-absent behaviour: no cap, and a transfer that
        still works. What the missing default removes is losing the cap by
        SILENCE. A new construction site now has to say which of the two it
        means, because the version that gets this wrong by omission produces
        listeners that outlive an otto killed outright, and nothing downstream
        can tell that from a host whose userland genuinely has no ``timeout``.
        """
        super().__init__(
            connections=connections,
            name=name,
            exec_cmd=exec_cmd,
            max_filename_len=max_filename_len,
        )
        self.transfer = transfer
        self._nc_options = nc_options
        self._get_local_ip = get_local_ip
        self._userland = userland
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
        # Resolved once, here, rather than read per transfer: an out-of-range
        # value must fail loudly at construction. A limit of 0 would otherwise
        # hand `_gather_per_file` a semaphore no permit ever comes out of, and a
        # bulk transfer would hang with nothing to point at.
        limit = nc_options.max_concurrent_transfers
        if limit is None:
            limit = _NC_MAX_CONCURRENT_TRANSFERS
        elif limit < 1:
            raise ValueError(f"nc_options.max_concurrent_transfers must be at least 1, got {limit}")
        self._max_concurrent_transfers = limit
        # One budget per INSTANCE, because the ceiling it stands for is per
        # CONNECTION. A semaphore created inside the dispatcher would bound one
        # bulk transfer while handing every other concurrent transfer on the
        # same host its own full budget — and they all spend the same channels.
        # `test_real_nc_high_fanout_put` is that shape: 20 separate one-file
        # puts gathered against one host.
        self._transfer_semaphore = asyncio.Semaphore(limit)

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
        # Threaded through rather than validated like the four above: a ctx
        # without a userland still builds a working backend, it just builds one
        # whose listeners run uncapped. Rejecting it here would overstate the
        # requirement — the cap is a backstop, not a dependency.
        return cls(
            connections=ctx.connections,
            name=ctx.host_name,
            transfer=ctx.transfer,
            nc_options=ctx.nc_options,
            get_local_ip=ctx.get_local_ip,
            exec_cmd=ctx.exec_cmd,
            max_filename_len=ctx.max_filename_len,
            userland=ctx.userland,
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

        Keyed on the CALLING CONVENTION, not the binary's name. That something
        called ``timeout`` exists does not say which of two spellings it
        speaks:

        ``SECS PROG``
            GNU coreutils and BusyBox from 1.31.0.
        ``-t SECS PROG``
            BusyBox up to 1.28.1, which reads a bare leading number as the
            PROGRAM. Getting it wrong there builds ``timeout 3600 nc -l``, the
            applet fails to exec ``3600``, and the listener never starts — the
            backstop becoming an outage, strictly worse than no backstop.

        The answer comes from :attr:`~otto.host.userland.Userland.timeout_style`
        and from nowhere else. This method used to embed a shell probe of its
        own, which was correct but private — a sixth answer to a question five
        sibling capabilities already resolve, cache and log through the
        userland layer, and so a divergence waiting to happen.

        **Resolution is this method's precondition, and it is synchronous.**
        Every ``Userland`` capability raises if it is read before ``resolve()``
        has been awaited, and there is no awaiting from here, so the error is
        allowed to propagate rather than being caught and answered "no cap": a
        silent fallback would reinstate the divergence the mapping removes, on
        the path where the cap is the only defence left. What keeps it from
        firing is :meth:`prepare`, which resolves the userland as its FIRST
        statement — ahead of its own early return, so a host with both nc
        strategies declared resolves too — and which every spawning path
        reaches through ``_warmup_for_transfer``. Both halves are pinned in
        ``tests/unit/host/test_transfer_nc_listener_reap.py``.

        **Precondition, unobvious and load-bearing.** GNU ``timeout`` calls
        ``setpgid(0,0)``, which would move ``nc`` out of the foreground
        process group and so out of reach of the SIGHUP a session hangup
        delivers — the cap outliving the session it was meant to be bounded
        by. It does not, because a job-control shell has *already* put the
        foreground job in its own process group and handed it the terminal, so
        the call is a no-op. Measured on a pty running ``bash -i`` by closing
        the master: unwrapped, wrapped, and wrapped with ``--foreground`` all
        die on hangup; only ``set +m`` (job control off) leaves the listener
        alive with ``ppid=1``.

        So this is correct only while the listener is spawned as a plain
        foreground job. Compose it into ``( … )``, ``$( … )``, or any shell
        with job control disabled and the leak comes back. That is pinned by
        ``test_the_listener_spawns_are_plain_foreground_commands`` rather than
        defended with ``--foreground``, which measurably changes nothing on
        either otto path.

        **Degrades to empty in FOUR cases, not three.** The device has no
        ``timeout``; it has one whose style this build was never taught; no
        userland is wired up at all; or — the one that is new, and the one
        with no analogue before this — the userland's probe round could not be
        ASKED. ``timeout_style``'s cannot-ask default is ``absent``, so a host
        that HAS a working ``timeout`` spawns an uncapped listener whenever its
        probes were refused, and ``Userland._RETRY_COOLDOWN_S`` holds that for
        up to a minute. The refusal that causes it is sshd at its
        ``MaxSessions`` ceiling, which is the very condition a bulk transfer's
        own fan-out creates.

        Which baseline that "degrades to" is measured against matters, because
        there are two. Before the CAP existed there was no cap, and the
        sentence is true. But the cap's first implementation resolved the
        convention IN-BAND — a shell one-liner spliced into the spawn command
        itself — so it cost nothing, could not fail independently of the spawn,
        and was lost only when the device genuinely had no usable ``timeout``.
        Against THAT baseline the fourth case is a regression, and this
        docstring used to claim a parity it no longer has.

        Shipped as it stands because the blast radius is narrow and the
        direction is safe: it needs the probe round refused AND otto to die
        during that transfer window, and no cap is a stale listener while a
        WRONG cap (``timeout 3600`` on a ``-t`` host) is a transfer that never
        starts. It is a backstop, not a dependency.

        The third case is no longer the production state.
        :class:`~otto.host.unix_host.UnixHost` builds one ``Userland`` per host
        and passes it on the ``TransferContext``, so a registry-built backend
        always has a resolver and the cap is live wherever the device's
        ``timeout`` can carry it. ``None`` survives only for a backend
        constructed directly with no host behind it, which is why
        :meth:`__init__` makes the argument required — the degraded path
        should be chosen, never inherited by omission.

        Costs nothing at spawn time: the convention was resolved once, when the
        host's userland was, so this is a dict lookup rather than the extra
        fork of ``true`` per listener the embedded probe used to pay. That
        saving and the fourth degradation case are the same trade seen from
        its two ends.

        Note the host class this actually rescues is narrower than "BusyBox":
        BusyBox's own ``nc`` applet wants ``-l -p PORT`` and has no ``-N``, so
        otto cannot drive it regardless (see ``NcOptions.exec_name``). The
        beneficiary is an old-BusyBox userland with an OpenBSD-style netcat
        installed alongside — Alpine <= 3.8, OpenWrt <= 18.06.
        """
        if self._userland is None:
            return ""
        return _TIMEOUT_STYLE_PREFIXES.get(self._userland.timeout_style, "")

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
        """Resolve the userland, then port + listener strategies in one round-trip.

        Runs the shared `_STRATEGY_PROBE` script through `_control_run` so the
        port and listener strategies are resolved up front rather than lazily
        at first-transfer time. Idempotent — a second call with both
        strategies already cached is a no-op.

        Callers use `_warmup_for_transfer` to run this concurrently with
        exec-pool warming; direct callers can invoke `prepare()` alone.

        If the strategy probe itself fails (non-zero exit, malformed output),
        the caches stay unset and the lazy cascades in `_find_free_port_auto` /
        `_resolve_listener_strategy` still kick in as fallbacks.

        The userland resolves FIRST, and that position is load-bearing rather
        than stylistic: everything below it is skipped whenever both strategies
        are declared, so a resolution placed after the early return would
        happen only on hosts that use `auto` — and `_nc_listener_prefix`, which
        is synchronous and cannot resolve on demand, would then raise on a
        perfectly legitimate configuration. `Userland.resolve()` is idempotent,
        concurrency-safe, and never raises for a failed probe, so calling it on
        every prepare costs nothing after the first.
        """
        if self._userland is not None:
            await self._userland.resolve()
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

    async def _gather_per_file(
        self,
        src_files: list[Path],
        transfer_one: Callable[[Path], Coroutine[Any, Any, Result]],
    ) -> dict[Path, Result]:
        """Run ``transfer_one`` per file with a BOUNDED fan-out, keyed by source.

        The bound is the point of this helper: every nc direction used to
        dispatch its files through a bare ``asyncio.gather``, which turns "many
        files" into "many simultaneous SSH channels" and runs into the remote
        sshd's ``MaxSessions`` — see ``_NC_MAX_CONCURRENT_TRANSFERS``. Sharing
        one dispatcher is what makes that structural rather than something each
        of the three call sites has to remember; the three used to hold
        identical copies of this gather-and-zip, and the fix would have been
        applied to whichever one the failing test happened to name.

        ``return_exceptions=True`` and the per-source mapping are preserved
        from those copies: one file's failure must not cancel its siblings, and
        the caller reports per file.
        """

        # A permit spans one whole transfer, not one channel, and that is
        # load-bearing. Bounding channels DEADLOCKS: an in-flight listener
        # holds its channel while its own readiness poll asks for a second, so
        # enough listeners would take every permit and block the very polls
        # that must finish to release them.
        #
        # The semaphore is the instance's, not one made here — see `__init__`.
        async def _bounded(src: Path) -> Result:
            async with self._transfer_semaphore:
                return await transfer_one(src)

        gathered = await asyncio.gather(
            *(_bounded(src) for src in src_files),
            return_exceptions=True,
        )
        per_file: dict[Path, Result] = {}
        for src, outcome in zip(src_files, gathered, strict=True):
            if isinstance(outcome, BaseException):
                per_file[src] = Result(Status.Error, msg=f"{src}: {outcome}")
            else:
                per_file[src] = outcome
        return per_file

    async def _get_files_nc(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        # ABOVE THE TUNNEL DISPATCH, and that position is the whole reason there
        # is one guard here and not two. Both arms make the device parse `-N` --
        # this one to SEND (`nc -N <ip> <port>`), the tunnelled one to LISTEN
        # (`nc -Nl <port>`) -- and `_get_files_nc_tunneled` has exactly one
        # caller, the dispatch immediately below. A second call inside it would
        # be a guard that could never be the one to fire, which is this repo's
        # most common defect; the record states that path PROTECTED instead,
        # naming this guard.
        await refuse_if_nc_rejects_dash_n(
            self._userland,
            exec_name=self._nc_exec,
            host=self._name,
            attempted=(
                f"get of {len(src_files)} file(s) over the `nc` backend, which asks the "
                f"device to run `{self._nc_exec} -N` -- to send directly, or to serve a "
                f"`-Nl` listener when the connection is tunnelled"
            ),
        )
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

        return await self._gather_per_file(src_files, _get_one)

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

        return await self._gather_per_file(src_files, _get_one)

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

        per_file = await self._gather_per_file(src_files, _put_one)
        if all(r.is_ok for r in per_file.values()):
            _logger.debug("Finished nc transfers")
        return per_file


register_transfer_backend("nc", NcFileTransfer)
