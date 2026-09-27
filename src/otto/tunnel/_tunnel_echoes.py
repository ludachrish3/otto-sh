"""The tagged echo listeners of ``otto tunnel check``: find, kill, start and clean them up.

Every echo the check starts carries an ``otto-check:v1`` tag in its process
name (:mod:`otto.tunnel.check_probes`), and this module is everything that
works through that tag: the sweep scan that finds echoes by it, the kill
that is scanned again to confirm it took, starting an echo and confirming
the socket it binds is its own, and cleaning one run's echoes off a hop.
The probe rows that aim at the echoes live in ``otto.tunnel._tunnel_rows``.
Every host command goes through :func:`~otto.check.fingerprint.check_read`.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any

from ..check import CheckHostUnreachableError
from ..check.fingerprint import check_read
from ..host.daemon import kill_command, parse_ps_output
from .check_probes import (
    CHECK_PREFIX,
    PS_FAILED,
    SWEEP_COMMAND,
    EchoTag,
    echo_launch_command,
    parse_echo_sentinel,
)
from .manage import ResolvedHop
from .socat import parse_port_holders

READY_DEADLINE_S = 5.0
"""Echo readiness: how long, on the wall clock, a launched echo gets to bind before its row fails.

The ``setsid`` launch fallback returns before socat has even started, and a
``systemd-run --user`` unit on a loaded host can take seconds to exec it, so a
probe sent straight after the launch could race the bind and blame the
network. A deadline, not a number of tries, so a host whose every command is
slow gets the same time as a fast one."""
_READY_INTERVAL_S = 0.2
"""How often the socket listing is read while an echo is not yet bound, and how often a
host is scanned again while a killed echo is still listed."""
KILL_SETTLE_S = 1.0
"""How long, on the wall clock, killed echoes get to exit before one still listed survived its kill.

socat exits promptly on SIGTERM, but a scan sent straight after the ``kill``
can still list it for a moment; one scan alone would then call it a
survivor. The scan is repeated until the killed pids are gone, ``ps``
cannot list, or this deadline passes."""


_LOCAL_FIELD = 3
"""The local-address column of an ``ss -H`` or ``netstat`` listing line."""

sleep = asyncio.sleep
"""Every wait goes through here, so tests can make the readiness wait instant."""
clock = time.monotonic
"""The readiness deadline reads this clock, so tests can move it with their own waits."""


def listen_command(protocol: str) -> str:
    """List *protocol*'s listening sockets: ``ss``, or ``netstat`` where there is no ``ss``."""
    flag = {"tcp": "t", "udp": "u"}[protocol]
    return f"ss -H{flag}ln 2>/dev/null || netstat -{flag}ln 2>/dev/null"


@dataclass(frozen=True)
class TaggedEcho:
    """One otto-check echo seen in a sweep scan."""

    pid: int
    tag: EchoTag
    age_s: int | None = None
    """How long ago it started, from ``ps``; ``None`` when ``ps`` did not say.

    The sweep replaces it with its run's age (its oldest echo's) before judging it."""


@dataclass(frozen=True)
class EchoScan:
    """What one sweep scan of a host saw."""

    echoes: list[TaggedEcho]
    listed: bool = True
    """``False`` when ``ps`` could not list processes at all (it rejected the scan's
    ``-o`` fields): then nothing is known about the host's echoes, which is not
    the same as none running."""

    @property
    def tags(self) -> list[EchoTag]:
        """Every echo's tag, in the order ``ps`` listed them."""
        return [echo.tag for echo in self.echoes]


async def scan(host: Any) -> EchoScan:
    """Every ``otto-check:v1`` echo running on *host*, whatever run started it."""
    result = await check_read(host, SWEEP_COMMAND)
    said = result.value or ""
    if PS_FAILED in [line.strip() for line in said.splitlines()]:
        return EchoScan([], listed=False)
    found = []
    for proc in parse_ps_output(said, CHECK_PREFIX):
        tag = parse_echo_sentinel(proc.token)
        if tag is not None:
            age = proc.age_seconds if proc.age_known else None
            found.append(TaggedEcho(proc.pid, tag, age))
    return EchoScan(found)


def could_not_list(host_id: str) -> str:
    """Say that ``ps`` on *host_id* lists nothing, the way ``otto link check``'s sweep does."""
    return f"could not list processes on {host_id}; sweep skipped"


async def kill(host: Any, pids: list[int]) -> None:
    """Kill *pids* on *host*; nothing to kill sends nothing."""
    if pids:
        await check_read(host, kill_command(pids))


async def _scan_after_kill(host: Any, pids: list[int]) -> EchoScan:
    """Scan *host* until none of the killed *pids* is listed, and return that last scan.

    It also stops when ``ps`` cannot list, or once :data:`KILL_SETTLE_S` has
    passed; what is still listed then survived its kill. With no *pids* it
    is one scan.
    """
    deadline = clock() + KILL_SETTLE_S
    while True:
        after = await scan(host)
        listed = {echo.pid for echo in after.echoes}
        if not after.listed or not listed & set(pids) or clock() >= deadline:
            return after
        await sleep(_READY_INTERVAL_S)


async def kill_confirmed(host: Any, pids: list[int]) -> set[int] | None:
    """Kill *pids* on *host*, then scan it again; return the ones still running.

    ``kill`` exits non-zero for a pid it may not signal, such as an echo
    another user started, and that is no error here: the scans after it,
    given :data:`KILL_SETTLE_S` to see each one go, say whether it went.
    ``None`` when a scan could not list processes, so nothing is known
    either way. Nothing to kill sends nothing, and scans nothing.
    """
    if not pids:
        return set()
    await kill(host, pids)
    after = await _scan_after_kill(host, pids)
    if not after.listed:
        return None
    return {echo.pid for echo in after.echoes if echo.pid in pids}


async def kill_matching(host: Any, wanted: Callable[[EchoTag], bool]) -> list[TaggedEcho]:
    """Kill every echo on *host* whose tag is *wanted*; return what was killed.

    A host whose ``ps`` lists nothing has nothing killed: the teardown's own
    scan of it then says it could not look.
    """
    found = [echo for echo in (await scan(host)).echoes if wanted(echo.tag)]
    await kill(host, [echo.pid for echo in found])
    return found


@dataclass(frozen=True)
class Cleaned:
    """What cleaning one hop of a run's echoes left behind, and a hop that did not answer."""

    problems: list[str]
    unreachable: CheckHostUnreachableError | None = None
    seen: list[EchoTag] = field(default_factory=list)
    """This run's echoes the first scan found, before anything was killed."""
    listed: bool = True
    """``False`` when ``ps`` on the hop could not list processes: :attr:`seen` is then
    empty because nothing could be looked at, not because nothing was there."""


async def clean_hop(hop: ResolvedHop, token: str, *, keep_tunnel: str | None = None) -> Cleaned:
    """Kill run *token*'s echoes on *hop*, then re-scan it and name any that survived.

    The re-scan gives the killed echoes :data:`KILL_SETTLE_S` to exit, so one
    still exiting is not called a survivor.

    Echoes of *keep_tunnel* are left running (and are not survivors): they are
    what lets a later sweep find a tunnel whose removal did not verify. Every
    echo of this run the first scan found, kept or killed, is handed back as
    :attr:`Cleaned.seen`, so the caller can tell one that was already gone. A
    hop whose ``ps`` lists nothing is a problem of its own, and says nothing
    about which echoes are there.
    """

    def ours(tag: EchoTag) -> bool:
        return tag.run == token and (keep_tunnel is None or tag.tunnel_id != keep_tunnel)

    blind = Cleaned(
        [f"could not list processes on {hop.hop.host} to confirm this run's echoes there are gone"],
        listed=False,
    )
    try:
        first = await scan(hop.host)
        if not first.listed:
            return blind
        found = [echo for echo in first.echoes if echo.tag.run == token]
        killed = [echo.pid for echo in found if ours(echo.tag)]
        await kill(hop.host, killed)
        after = await _scan_after_kill(hop.host, killed)
        if not after.listed:
            return blind
        left = [echo for echo in after.echoes if ours(echo.tag)]
    except CheckHostUnreachableError as e:
        return Cleaned([f"could not reach {hop.hop.host} to clean up this run's echoes"], e)
    return Cleaned(
        [f"{echo.tag.role} echo pid {echo.pid} survived on {hop.hop.host}" for echo in left],
        seen=[echo.tag for echo in found],
    )


def _bound_at(listing: str, bind_ip: str, port: int) -> bool:
    """Whether *listing* shows a socket bound at *bind_ip*:*port*, or at a wildcard there."""
    for line in parse_port_holders(listing, port):
        local = line.split()[_LOCAL_FIELD].rsplit(":", 1)[0].strip("[]").split("%")[0]
        if local in (bind_ip, "*") or _unspecified(local):
            return True
    return False


def _unspecified(address: str) -> bool:
    try:
        return ip_address(address).is_unspecified
    except ValueError:
        return False


@dataclass(frozen=True)
class EchoStart:
    """What starting one echo listener showed: its evidence, and why it is not up."""

    commands: list[str]
    output: str | None
    problem: str | None
    """``None`` when the echo listens (or the host has no socket listing to tell)."""
    confirmed: bool = False
    """This run's echo was seen running once its address was up: only an echo seen
    running can later be said to have vanished."""


def unconfirmed(host_id: str) -> str:
    """Say that ``ps`` on *host_id* could not show whether a bound echo is this run's."""
    return f"could not confirm the echo is this run's: ps on {host_id} lists nothing"


def held_by_another(host_id: str, port: int) -> str:
    """Say that something other than this run's echo holds scratch *port* on *host_id*."""
    return f"scratch port {port} on {host_id} is held by something else (another check?)"


async def start_echo(host: Any, tag: EchoTag, bind_ip: str, port: int) -> EchoStart:
    """Launch a tagged echo on *host*, wait until *bind_ip*:*port* is up, and confirm it is ours.

    A socket at the address is not enough on its own: another check that drew
    the same scratch port may hold it, and then this echo exited at once and
    the probes would reach the other run's echo instead. So once the address
    is up, one sweep scan must find this echo's own tag running; if it does
    not, the address belongs to something else and the row fails, naming
    the port. That scan is one sample, not proof: a socat whose bind just
    failed can still be listed for a moment before it exits, and pass. The
    probes then reach the other run's echo, and the teardown, finding this
    echo gone, says so (:func:`~otto.tunnel._tunnel_rows.swept_detail`). A host whose ``ps`` lists
    nothing cannot tell this echo from another run's, so the row fails
    saying so (:func:`unconfirmed`), never blaming a collision. A host with
    neither ``ss`` nor ``netstat`` cannot say when the address is up: the
    echo is taken as started, and confirmed only if that one scan finds it.
    """
    launch = echo_launch_command(tag, tag.protocol, bind_ip, port)
    started = await check_read(host, launch)
    if not started.is_ok:
        return EchoStart(
            [launch], started.value, f"could not start the {tag.role} echo on {host.id}"
        )
    listing = listen_command(tag.protocol)
    deadline = clock() + READY_DEADLINE_S
    while True:
        listed = await check_read(host, listing)
        if not listed.is_ok:
            return EchoStart([launch], None, None, confirmed=tag in (await scan(host)).tags)
        if _bound_at(listed.value or "", bind_ip, port):
            seen = await scan(host)
            if not seen.listed:
                return EchoStart(
                    [launch, listing, SWEEP_COMMAND], listed.value, unconfirmed(host.id)
                )
            if tag in seen.tags:
                return EchoStart([launch], None, None, confirmed=True)
            return EchoStart(
                [launch, listing, SWEEP_COMMAND],
                listed.value,
                held_by_another(host.id, port),
            )
        if clock() >= deadline:
            return EchoStart(
                [launch, listing],
                listed.value,
                f"the {tag.role} echo on {host.id} never listened on {bind_ip}:{port}",
            )
        await sleep(_READY_INTERVAL_S)
