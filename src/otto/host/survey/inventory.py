"""The inventory tier: every listening socket and, where root allows, its owner.

One compound script picks the first tool the host has (``ss`` → ``netstat`` →
a ``/proc`` walk), the nc backend's own port-finding idea, and ships as ONE
``sh -c '<body>'`` word so an elevating caller can prefix it textually. The
parser turns each tool's lines into :class:`Listener` rows; a blank owner
column is ``owner unknown``, never a parse failure; loopback-only binds are
dropped and counted. The runner tries elevation ONCE when asked and falls
back to a plain run -- a refused elevation is never retried.
"""

import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, cast

from ...result import CommandResult
from ...utils import Status
from ..errors import UnsupportedOnUserlandError

_INVENTORY_BODY = (
    "if command -v ss >/dev/null 2>&1; then echo OTTO_TOOL=ss; ss -tulnp 2>&1; "
    "elif command -v netstat >/dev/null 2>&1; then echo OTTO_TOOL=netstat; "
    "netstat -tulnp 2>&1; "
    "else echo OTTO_TOOL=proc; for f in tcp tcp6 udp udp6; do echo OTTO_FILE=$f; "
    "cat /proc/net/$f 2>/dev/null; done; "
    "echo OTTO_FDS; for p in /proc/[0-9]*; do c=$(cat $p/comm 2>/dev/null) || continue; "
    "for l in $p/fd/*; do t=$(readlink $l 2>/dev/null) || continue; "
    'case $t in socket:\\[*\\]) i=${t#socket:[}; echo "${i%]}|$c";; esac; done; done; fi'
)

INVENTORY_SCRIPT = "sh -c " + shlex.quote(_INVENTORY_BODY)
"""The inventory command: ONE shell word, so an elevating caller can prefix it.

:meth:`otto.host.privilege.PosixPrivilege._elevate` composes elevation by
textual prefixing -- ``sudo -S -p '<prompt>' <cmd>`` and
``su -c <shlex.quote(cmd)>`` -- so a bare multi-statement body arrives as
sudo's trailing ARGUMENT LIST: bash only reads ``if`` as the reserved word
that opens a conditional in command-start position, so spliced in as an
argument it is a literal word and the later ``then``/``else``/``fi`` are
syntax errors with no matching ``if``. bash then refuses the ENTIRE input
line -- the caller's own sentinel echoes included -- so the run hangs to its
outer timeout and the fallback reads as a refused elevation that never
happened. ``otto.host.daemon`` documents the same hazard for the only other
``sudo=True`` caller. Wrapping here rather than at the elevation site keeps
ONE script: the plain run, the ``sudo`` run and the ``su`` run all send the
same bytes, so what the bed proves about one holds for the others.
"""

_TOOL_LINE = re.compile(r"^OTTO_TOOL=(ss|netstat|proc)$")
_SS_ROW = re.compile(
    r"^(tcp|udp)\s+(LISTEN|UNCONN)\s+\d+\s+\d+\s+(\S+):(\d+)\s+\S+"
    r"(?:\s+users:\(\(\"([^\"]+)\")?"
)
_NETSTAT_ROW = re.compile(
    r"^(tcp6?|udp6?)\s+\d+\s+\d+\s+(\S+):(\d+)\s+\S+\s+(?:LISTEN(?:\s+|$))?(\S+)?\s*$"
)
_PROC_ROW = re.compile(
    r"^\s*\d+:\s+([0-9A-Fa-f]+):([0-9A-Fa-f]{4})\s+[0-9A-Fa-f]+:[0-9A-Fa-f]{4}\s+"
    r"([0-9A-Fa-f]{2})\s+\S+\s+\S+\s+\S+\s+\d+\s+\d+\s+(\d+)"
)
_PROC_LOOPBACK_V6 = "00000000000000000000000001000000"
_PROC_LISTEN = {"tcp": "0A", "udp": "07"}
_IPV4_HEX_LEN = 8
_LOOPBACK_NAMES = frozenset({"::1", "localhost"})

Transport = Literal["tcp", "udp"]


@dataclass(frozen=True, slots=True)
class Listener:
    """One bound socket: transport, address, port and (maybe) its owner process."""

    transport: Transport
    address: str
    port: int
    owner: str | None


@dataclass(frozen=True, slots=True)
class Inventory:
    """The parsed result of one inventory run; a non-empty ``error`` means it failed."""

    tool: str
    elevated: bool
    listeners: list[Listener] = field(default_factory=list)
    loopback_dropped: int = 0
    unparsed: int = 0
    error: str = ""


def is_loopback(address: str) -> bool:
    """Return whether *address* is loopback: ``127.0.0.0/8``, ``::1`` or ``localhost``."""
    a = address.strip("[]")
    return a in _LOOPBACK_NAMES or a.startswith("127.")


def _owner_from_netstat(cell: str | None) -> str | None:
    if not cell or cell == "-":
        return None
    return cell.split("/", 1)[1] if "/" in cell else cell


def _hex_addr(text: str) -> str:
    if len(text) == _IPV4_HEX_LEN:
        return ".".join(str(int(text[i : i + 2], 16)) for i in (6, 4, 2, 0))
    if text == _PROC_LOOPBACK_V6:
        return "::1"
    return "::" if set(text) == {"0"} else text.lower()


def _parse_ss(lines: list[str]) -> "tuple[list[Listener], int]":
    rows: list[Listener] = []
    unparsed = 0
    for line in lines:
        if not line.strip():
            continue
        m = _SS_ROW.match(line)
        if m is None:
            if line.startswith("Netid"):
                continue
            unparsed += 1
            continue
        rows.append(
            Listener(
                transport=cast("Transport", m.group(1)),
                address=m.group(3),
                port=int(m.group(4)),
                owner=m.group(5),
            )
        )
    return rows, unparsed


def _parse_netstat(lines: list[str]) -> "tuple[list[Listener], int]":
    rows: list[Listener] = []
    unparsed = 0
    for line in lines:
        if not line.strip():
            continue
        m = _NETSTAT_ROW.match(line)
        if m is None:
            if line.startswith(("Active", "Proto", "(", " will", "netstat:")):
                continue
            unparsed += 1
            continue
        transport = "udp" if m.group(1).startswith("udp") else "tcp"
        rows.append(
            Listener(
                transport=transport,
                address=m.group(2),
                port=int(m.group(3)),
                owner=_owner_from_netstat(m.group(4)),
            )
        )
    return rows, unparsed


def _parse_tool_lines(tool: str, lines: list[str]) -> "tuple[list[Listener], int]":
    if tool == "proc":
        return _parse_proc(lines)
    if tool == "ss":
        return _parse_ss(lines)
    return _parse_netstat(lines)


def _parse_proc(lines: list[str]) -> "tuple[list[Listener], int]":
    rows: list[tuple[Listener, str]] = []
    comm: dict[str, str] = {}
    unparsed = 0
    section = ""
    in_fds = False
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("OTTO_FILE="):
            section = line.split("=", 1)[1]
            in_fds = False
            continue
        if line == "OTTO_FDS":
            in_fds = True
            continue
        if in_fds:
            inode, _, name = line.partition("|")
            comm[inode] = name
            continue
        if line.lstrip().startswith("sl "):
            continue
        m = _PROC_ROW.match(line)
        if m is None:
            unparsed += 1
            continue
        transport = "udp" if section.startswith("udp") else "tcp"
        if m.group(3).upper() != _PROC_LISTEN[transport]:
            continue
        listener = Listener(
            transport=transport,
            address=_hex_addr(m.group(1)),
            port=int(m.group(2), 16),
            owner=None,
        )
        rows.append((listener, m.group(4)))
    joined = [
        Listener(transport=r.transport, address=r.address, port=r.port, owner=comm.get(inode))
        for r, inode in rows
    ]
    return joined, unparsed


def parse_inventory(output: str, *, elevated: bool) -> Inventory:
    """Turn the script's output into an :class:`Inventory`."""
    lines = output.splitlines()
    found = next(
        (
            (idx, tool_match)
            for idx, line in enumerate(lines)
            if (tool_match := _TOOL_LINE.match(line.strip())) is not None
        ),
        None,
    )
    if found is None:
        head = output.strip().splitlines()[:1]
        detail = head[0] if head else "<empty>"
        return Inventory(
            tool="", elevated=elevated, error=f"inventory: unrecognised output: {detail}"
        )
    first, tool_match = found
    tool = tool_match.group(1)
    rows, unparsed = _parse_tool_lines(tool, lines[first + 1 :])
    kept = [r for r in rows if not is_loopback(r.address)]
    return Inventory(
        tool=tool,
        elevated=elevated,
        listeners=kept,
        loopback_dropped=len(rows) - len(kept),
        unparsed=unparsed,
    )


async def _run_once(
    run: "Callable[..., Awaitable[CommandResult]]", *, sudo: bool
) -> "CommandResult | str":
    try:
        result = await run(INVENTORY_SCRIPT, sudo=sudo)
        if result.status is Status.NotRun:
            return f"not run: {result.msg or 'declined'}"
        if result.timed_out:
            return "timed out"
        if result.retcode != 0:
            return f"exit {result.retcode}: {str(result.value).strip()}"
    except UnsupportedOnUserlandError:
        # Elevation is asked for only when the userland resolved `sudo`/`su` AND
        # the session's cred has a password to answer it, so a refusal here is
        # the resolution and the host disagreeing -- a real error the operator
        # has to see, never a quiet fall back to an unelevated run. A sudo the
        # TARGET refuses arrives as a non-zero retcode (or NotRun) above and
        # still falls back, with the spec's "elevation refused" footnote.
        raise
    except Exception as exc:  # noqa: BLE001 — every failure shape becomes one stated reason
        return f"{type(exc).__name__}: {exc}"
    return result


async def run_inventory(
    run: "Callable[..., Awaitable[CommandResult]]", *, elevate: bool
) -> Inventory:
    """Run the script, elevated once when asked, plain otherwise; never elevate twice."""
    if elevate:
        got = await _run_once(run, sudo=True)
        if isinstance(got, CommandResult):
            return parse_inventory(str(got.value), elevated=True)
    got = await _run_once(run, sudo=False)
    if isinstance(got, CommandResult):
        return parse_inventory(str(got.value), elevated=False)
    return Inventory(tool="", elevated=False, error=f"inventory failed: {got}")
