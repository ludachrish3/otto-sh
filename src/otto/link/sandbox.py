"""A throwaway netns + veth pair ``otto link check`` tests netem inside.

Every artifact a check creates is tagged ``otto-check-<id>`` and torn down in
a ``finally`` (global constraint); this module is that lifecycle. A run that
was killed mid-check leaves a namespace behind — :func:`sweep_stale` finds
and removes those before a new check starts, so a crashed run never leaks a
netns onto the host permanently.

Every command here is privileged (``ip netns``, ``ip link``) and goes through
:func:`~otto.check.fingerprint.check_root_run`, which never raises on a
non-ok result: a sandbox that fails to come up is a verdict for the caller
(:func:`open_sandbox` hands back the first failed result), not a transport
error.
"""

import contextlib
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..check.fingerprint import check_exec, check_root_run
from ..host import CommandResult

SWEEP_LIST_COMMAND = "ip netns list 2>/dev/null || true"
_NAME_PREFIX = "otto-check-"
_VETH_PREFIX = "ock"


@dataclass(frozen=True)
class Sandbox:
    """The names of one throwaway netns + veth pair, all sharing one token."""

    name: str
    """The network namespace, ``otto-check-<6 hex>``."""

    veth: str
    """The root-side end of the veth pair, ``ock<6 hex>`` (fits IFNAMSIZ)."""

    peer: str
    """The namespace-side end, ``ock<6 hex>p``."""

    ROOT_IP = "198.18.0.1"
    """Address given to :attr:`veth`, in the root namespace."""

    NS_IP = "198.18.0.2"
    """Address given to :attr:`peer`, inside :attr:`name`."""


def new_sandbox(token: str | None = None) -> Sandbox:
    """Build a :class:`Sandbox` from *token*, or a fresh random one."""
    token = token if token is not None else secrets.token_hex(3)
    return Sandbox(
        name=f"{_NAME_PREFIX}{token}", veth=f"{_VETH_PREFIX}{token}", peer=f"{_VETH_PREFIX}{token}p"
    )


def sandbox_from_name(name: str) -> Sandbox:
    """Rebuild the :class:`Sandbox` whose namespace is *name* — the inverse of :func:`new_sandbox`.

    Used by :func:`sweep_stale`, which only ever has the namespace name (from
    ``ip netns list``) and needs the veth/peer names to tear it down the same
    way :func:`open_sandbox` would have.
    """
    token = name.removeprefix(_NAME_PREFIX)
    return Sandbox(name=name, veth=f"{_VETH_PREFIX}{token}", peer=f"{_VETH_PREFIX}{token}p")


def setup_commands(sb: Sandbox) -> list[str]:
    """Build the ordered privileged commands that bring *sb* up."""
    ns = f"ip netns exec {sb.name} "
    return [
        f"ip netns add {sb.name}",
        f"ip link add {sb.veth} type veth peer name {sb.peer}",
        f"ip link set {sb.peer} netns {sb.name}",
        f"ip addr add {Sandbox.ROOT_IP}/30 dev {sb.veth}",
        f"ip link set {sb.veth} up",
        f"{ns}ip addr add {Sandbox.NS_IP}/30 dev {sb.peer}",
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


def parse_stale(output: str) -> list[str]:
    """Pull ``otto-check-*`` namespace names out of ``ip netns list`` output.

    ``ip netns list`` may annotate each line (e.g. ``" (id: 0)"``); only the
    first token is the namespace name, and anything not ours is ignored.
    """
    names = []
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0].startswith(_NAME_PREFIX):
            names.append(fields[0])
    return names


async def sweep_stale(host: Any) -> list[str]:
    """Tear down every ``otto-check-*`` namespace left on *host*; return their names."""
    listed = await check_exec(host, SWEEP_LIST_COMMAND)
    names = parse_stale(listed.value or "")
    for name in names:
        sb = sandbox_from_name(name)
        for cmd in teardown_commands(sb):
            await check_root_run(host, cmd)
    return names


@contextlib.asynccontextmanager
async def open_sandbox(host: Any, sb: Sandbox) -> AsyncIterator[CommandResult | None]:
    """Bring *sb* up on *host*, yield the outcome, ALWAYS tear it down.

    Runs :func:`setup_commands` in order, stopping at the first failure.
    Yields ``None`` when every step succeeded, or the first failed
    :class:`~otto.result.CommandResult` — the caller marks every row
    ``skipped`` with that output rather than guessing why the sandbox never
    came up. :func:`teardown_commands` always runs in a ``finally``, whether
    setup failed part-way or the caller's own body raises.
    """
    failure: CommandResult | None = None
    try:
        for cmd in setup_commands(sb):
            result = await check_root_run(host, cmd)
            if not result.is_ok:
                failure = result
                break
        yield failure
    finally:
        for cmd in teardown_commands(sb):
            await check_root_run(host, cmd)
