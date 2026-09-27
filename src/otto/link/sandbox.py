"""A throwaway netns + veth pair ``otto link check`` tests netem inside.

Every artifact a check creates is tagged ``otto-check-<id>`` and torn down in
a ``finally`` (global constraint); this module is that lifecycle. A run that
was killed mid-check leaves a namespace behind — :func:`sweep_stale` finds
and removes those before a new check starts, once they are older than any
run could still be using, so a crashed run never leaks a netns onto the host
permanently. Every sandbox has its own addresses: its ``/30`` is drawn to
miss every namespace the sweep's listing showed, and once it is up, the
host must route its namespace address through its own veth, or its rows
fail. So a leftover still too young to sweep, or a second check running on
the same host, never takes a sandbox's probes unnoticed.

Every command here is privileged (``ip netns``, ``ip link``) and goes through
:func:`~otto.check.fingerprint.check_root_run`, which never raises on a
non-ok result: a sandbox that fails to come up is a verdict for the caller
(:func:`open_sandbox` hands back the first failed result), not a transport
error.
"""

import contextlib
import functools
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from ipaddress import ip_network
from typing import Any

from ..check.fingerprint import check_exec, check_root_run
from ..check.sweep import left_line, sweepable, swept_note
from ..host import CommandResult

NETNS_DIR = "/var/run/netns"
"""Where ``ip netns add`` puts each namespace's file (a symlink into ``/run`` on
modern systems); its mtime is when the namespace was created."""
SWEEP_LIST_COMMAND = (
    "ip netns list 2>/dev/null || true; "
    'echo "@now $(date +%s)"; '
    f"stat -c '@mtime %Y %n' {NETNS_DIR}/otto-check-* 2>/dev/null || true"
)
"""One read-only command that lists every namespace, the host's clock, and
each ``otto-check-*`` namespace file's mtime, for :func:`parse_stale`."""
_NAME_PREFIX = "otto-check-"
_VETH_PREFIX = "ock"


SUBNET = ip_network("198.18.0.0/15")
"""Where every sandbox's addresses come from.

It is the range RFC 2544 reserves for network benchmarking, so it should not
clash with a real network."""
_SLOTS = SUBNET.num_addresses // 4
"""How many ``/30`` blocks :data:`SUBNET` holds: one per sandbox token, modulo this."""
_DRAWS = 8
"""How many tokens :func:`fresh_sandbox` draws before it keeps one whose ``/30`` is taken.

With 32768 blocks, even a host holding a hundred sandboxes turns a draw down
about one time in 300, so eight draws in a row never all miss in practice.
The route check in :func:`open_sandbox` catches the case anyway."""

_draw_token: Callable[[], str] = functools.partial(secrets.token_hex, 3)
"""Draws a fresh sandbox token; a module-level seam so a test can pin the draws."""


@dataclass(frozen=True)
class Sandbox:
    """The names and addresses of one throwaway netns + veth pair, all from one token."""

    name: str
    """The network namespace, ``otto-check-<6 hex>``."""

    veth: str
    """The root-side end of the veth pair, ``ock<6 hex>`` (fits IFNAMSIZ)."""

    peer: str
    """The namespace-side end, ``ock<6 hex>p``."""

    root_ip: str
    """Address given to :attr:`veth`, in the root namespace; the first host of its ``/30``."""

    ns_ip: str
    """Address given to :attr:`peer`, inside :attr:`name`; the second host of its ``/30``."""


def _from_token(token: str) -> Sandbox:
    """Name a sandbox after *token*, and give it the ``/30`` of :data:`SUBNET` the token picks.

    Every sandbox on a host has its own ``/30``. Two sharing one would each add
    the same connected route through their own veth, and the kernel would send
    both runs' probes into whichever namespace came first. A second check on
    the same host, or a leftover too young to sweep, would then measure the
    wrong namespace. Two tokens pick the same block one time in 32768;
    :func:`fresh_sandbox` steps around the blocks already in use, and
    :func:`open_sandbox` checks the route.
    """
    try:
        slot = int(token, 16) % _SLOTS
    except ValueError:
        slot = 0  # a name otto did not make: only ever torn down, never addressed
    base = SUBNET.network_address + 4 * slot
    return Sandbox(
        name=f"{_NAME_PREFIX}{token}",
        veth=f"{_VETH_PREFIX}{token}",
        peer=f"{_VETH_PREFIX}{token}p",
        root_ip=str(base + 1),
        ns_ip=str(base + 2),
    )


def new_sandbox(token: str | None = None) -> Sandbox:
    """Build a :class:`Sandbox` from *token*, or a fresh random one."""
    return _from_token(token if token is not None else _draw_token())


def fresh_sandbox(listed: list[str]) -> Sandbox:
    """Draw a new :class:`Sandbox` whose ``/30`` no ``otto-check-*`` namespace in *listed* has.

    *listed* is every namespace the host's sweep listing showed
    (:attr:`SweepResult.listed`). A draw that lands on one of their blocks is
    drawn again, up to ``_DRAWS`` (8) times; the last draw is kept even
    then, and :func:`open_sandbox`'s route check fails it loudly.
    """
    taken = {sandbox_from_name(name).root_ip for name in listed if name.startswith(_NAME_PREFIX)}
    for _ in range(_DRAWS):
        sb = new_sandbox()
        if sb.root_ip not in taken:
            return sb
    return sb


def sandbox_from_name(name: str) -> Sandbox:
    """Rebuild the :class:`Sandbox` whose namespace is *name* — the inverse of :func:`new_sandbox`.

    Used by :func:`sweep_stale`, which only ever has the namespace name (from
    ``ip netns list``) and needs the veth/peer names to tear it down the same
    way :func:`open_sandbox` would have.
    """
    return _from_token(name.removeprefix(_NAME_PREFIX))


def setup_commands(sb: Sandbox) -> list[str]:
    """Build the ordered privileged commands that bring *sb* up."""
    ns = f"ip netns exec {sb.name} "
    return [
        f"ip netns add {sb.name}",
        f"ip link add {sb.veth} type veth peer name {sb.peer}",
        f"ip link set {sb.peer} netns {sb.name}",
        f"ip addr add {sb.root_ip}/30 dev {sb.veth}",
        f"ip link set {sb.veth} up",
        f"{ns}ip addr add {sb.ns_ip}/30 dev {sb.peer}",
        f"{ns}ip link set {sb.peer} up",
        f"{ns}ip link set lo up",
    ]


def teardown_commands(sb: Sandbox) -> list[str]:
    """Build the ordered privileged commands that remove *sb*, however far up it got.

    Killing the namespace's processes first matters: deleting a netns does
    not stop processes still running inside it, and a listener left behind
    keeps the namespace alive. Every step is its own best-effort — a step
    that never ran (setup failed before it) is harmless to attempt anyway.
    """
    return [
        f"for p in $(ip netns pids {sb.name} 2>/dev/null); do kill $p 2>/dev/null; done; true",
        f"ip link del {sb.veth} 2>/dev/null || true",
        f"ip netns del {sb.name} 2>/dev/null || true",
    ]


_NOW_FIELDS = 2
"""``@now <epoch>``."""
_MTIME_FIELDS = 3
"""``@mtime <epoch> <path>``."""


@dataclass(frozen=True)
class StaleNamespace:
    """One ``otto-check-*`` namespace a sweep found, and how old the host says it is."""

    name: str
    age_s: int | None
    """Seconds since it was created, on the host's own clock; ``None`` when the
    host could not say (no ``stat`` or ``date`` output for it)."""


def parse_stale(output: str) -> list[StaleNamespace]:
    """Pull ``otto-check-*`` namespaces and their ages out of :data:`SWEEP_LIST_COMMAND`'s output.

    ``ip netns list`` may annotate each line (e.g. ``" (id: 0)"``); only the
    first token is the namespace name, and anything not ours is ignored. A
    namespace's age is the host's clock (the ``@now`` line) less its file's
    mtime (an ``@mtime`` line), which ``ip netns add`` sets once, when it
    creates the namespace. Both come from the host, so the controller's clock
    never enters into it.
    """
    now: int | None = None
    created: dict[str, int] = {}
    names: list[str] = []
    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "@now" and len(fields) == _NOW_FIELDS and fields[1].isdigit():
            now = int(fields[1])
        elif fields[0] == "@mtime" and len(fields) == _MTIME_FIELDS and fields[1].isdigit():
            created[fields[2].rsplit("/", 1)[-1]] = int(fields[1])
        elif fields[0].startswith(_NAME_PREFIX):
            names.append(fields[0])
    return [
        StaleNamespace(
            name,
            now - created[name] if now is not None and name in created else None,
        )
        for name in dict.fromkeys(names)
    ]


@dataclass(frozen=True)
class SweepResult:
    """What :func:`sweep_stale` did on one host, and every namespace it found there."""

    said: list[str]
    """One display line per ``otto-check-*`` namespace found, saying if it was swept or left."""
    listed: list[str]
    """Every ``otto-check-*`` namespace the listing showed, swept or left, for
    :func:`fresh_sandbox` to step around."""


async def sweep_stale(host: Any) -> SweepResult:
    """Tear down every ``otto-check-*`` namespace on *host* old enough to sweep; say what happened.

    A namespace older than :data:`~otto.check.sweep.SWEEP_MIN_AGE_S`, or one whose
    age the host could not give, is torn down the way :func:`open_sandbox`
    would have; a younger one may be a check running on this host right now,
    and is left. One display line per namespace found, either way.
    """
    listing = await check_exec(host, SWEEP_LIST_COMMAND)
    found = parse_stale(listing.value or "")
    said = []
    for stale in found:
        if stale.age_s is not None and not sweepable(stale.age_s):
            said.append(left_line(f"{stale.name} namespace", host.id, stale.age_s, verb="created"))
            continue
        for cmd in teardown_commands(sandbox_from_name(stale.name)):
            await check_root_run(host, cmd)
        note = swept_note(stale.age_s, verb="created")
        said.append(f"swept leftover sandbox {stale.name} on {host.id} ({note})")
    return SweepResult(said, [stale.name for stale in found])


def route_command(sb: Sandbox) -> str:
    """Ask the host which device reaches *sb*'s namespace address: it must be *sb*'s own veth."""
    return f"ip route get {sb.ns_ip}"


def _route_dev(output: str) -> str | None:
    """Read the device ``ip route get`` names (``… dev <name> …``); ``None`` if it names none."""
    fields = output.split()
    return fields[fields.index("dev") + 1] if "dev" in fields[:-1] else None


@dataclass(frozen=True)
class SandboxProblem:
    """Why a sandbox cannot be used: a setup step failed, or its address routes elsewhere."""

    result: CommandResult
    """The failed setup step's result, or the route check's."""
    clash: str | None = None
    """Set when the sandbox came up but the host routes its namespace address
    through another device: another namespace there has the same ``/30``, and
    this sandbox's probes would measure that one."""


@contextlib.asynccontextmanager
async def open_sandbox(host: Any, sb: Sandbox) -> AsyncIterator[SandboxProblem | None]:
    """Bring *sb* up on *host*, check its route, yield the outcome, ALWAYS tear it down.

    Runs :func:`setup_commands` in order, stopping at the first failure, then
    asks the host which device reaches the namespace (:func:`route_command`).
    Yields ``None`` when every step succeeded and the route goes through
    *sb*'s own veth. Otherwise it yields a :class:`SandboxProblem`: a failed
    setup step's result, for the caller to skip every row with, or a
    ``clash`` naming the device the address routes through instead, for it to
    fail every row with. :func:`teardown_commands` always runs in a
    ``finally``, whether setup failed part-way or the caller's own body raises.
    """
    problem: SandboxProblem | None = None
    try:
        for cmd in setup_commands(sb):
            result = await check_root_run(host, cmd)
            if not result.is_ok:
                problem = SandboxProblem(result)
                break
        if problem is None:
            problem = await _check_route(host, sb)
        yield problem
    finally:
        for cmd in teardown_commands(sb):
            await check_root_run(host, cmd)


async def _check_route(host: Any, sb: Sandbox) -> SandboxProblem | None:
    """``None`` when *host* routes *sb*'s namespace address through *sb*'s own veth."""
    routed = await check_exec(host, route_command(sb))
    dev = _route_dev(routed.value or "") if routed.is_ok else None
    if dev == sb.veth:
        return None
    return SandboxProblem(
        routed,
        clash=(
            f"the sandbox's address {sb.ns_ip} on {host.id} routes via "
            f"{dev or 'nothing otto could read'}, not {sb.veth}: another namespace there "
            "has the same /30 (another check?)"
        ),
    )
