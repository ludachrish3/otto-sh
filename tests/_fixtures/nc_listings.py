"""Canned netcat rung listings and answers, shared by the unit and BusyBox tiers.

What ``ss -tln``, ``netstat -tln`` and ``/proc/net/tcp`` print, the stderr
complaint a BusyBox ``netstat`` makes on a kernel without IPv6, and the
result of a rung whose tool failed. One copy, so a listing's shape is
changed in one place for every tier that runs the rung scripts.
"""

import re

from otto.host.transfer.nc_scripts import TOOL_FAILED
from otto.result import CommandResult
from otto.utils import Status

COMPLAINT = "netstat: /proc/net/tcp6: No such file or directory"
"""What BusyBox ``netstat`` prints on stderr on a kernel without IPv6."""

PORT = 41234
"""The port a listing shows as held."""

OTHER = 41240
"""A second port, held only where a test says so."""

SS_HEADER = "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
NETSTAT_HEADER = (
    "Active Internet connections (only servers)\n"
    "Proto Recv-Q Send-Q Local Address           Foreign Address         State\n"
)
TCP_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
    "   uid  timeout inode\n"
)


def ss_listing(port: int) -> str:
    """An ``ss -tln`` listing holding *port* and 22."""
    return (
        SS_HEADER
        + f"LISTEN 0      128          0.0.0.0:{port}      0.0.0.0:*\n"
        + "LISTEN 0      128          0.0.0.0:22         0.0.0.0:*\n"
    )


def netstat_listing(port: int) -> str:
    """A ``netstat -tln`` listing holding *port* and 22."""
    return (
        NETSTAT_HEADER
        + f"tcp        0      0 0.0.0.0:{port}         0.0.0.0:*               LISTEN\n"
        + "tcp        0      0 0.0.0.0:22              0.0.0.0:*               LISTEN\n"
    )


def tcp_row(sl: int, port: int, *, v6: bool = False) -> str:
    """One LISTEN row of ``/proc/net/tcp`` (or ``tcp6`` when *v6*) on *port*."""
    addr = "0" * 32 if v6 else "00000000"
    return (
        f"   {sl}: {addr}:{port:04X} {addr}:0000 0A 00000000:00000000 00:00000000"
        " 00000000  1000        0 1234 1 0000000000000000 100 0 0 10 0\n"
    )


def one_port(out: str) -> int:
    """The port in *out*, which must be exactly one line of digits."""
    assert re.fullmatch(r"[0-9]+\n", out), f"not one clean port line: {out!r}"
    return int(out)


def tool_failed() -> CommandResult:
    """The result of a built-in rung whose tool is missing or failed."""
    return CommandResult(command="", value=f"{TOOL_FAILED}\n", status=Status.Failed, retcode=3)
