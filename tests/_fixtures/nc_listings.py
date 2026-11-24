"""Canned netcat rung listings and answers, shared by the unit and BusyBox tiers.

What ``ss -tln``, ``netstat -tln`` and ``/proc/net/tcp`` print, the stderr
complaint a BusyBox ``netstat`` makes on a kernel without IPv6, and the
result of a rung whose tool failed. One copy, so a listing's shape is
changed in one place for every tier that runs the rung scripts.

Also the real listeners both tiers count: one port held on both address
families, and one port held twice on one family.
"""

import errno
import re
import socket
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

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


def ss_by_family(v4: list[str], v6: list[str]) -> str:
    """The body of a stand-in ``ss`` that honours ``-4`` and ``-6``.

    *v4* and *v6* are the local ``address:port`` of each family's LISTEN rows.
    Asked for one family it prints that family's rows; asked for neither it
    prints both, as the real tool does.
    """
    rows4 = "".join(f"LISTEN 0 10 {address} 0.0.0.0:*\n" for address in v4)
    rows6 = "".join(f"LISTEN 0 10 {address} [::]:*\n" for address in v6)
    return (
        'case " $* " in\n'
        f"*\" -4 \"*) cat <<'EOF'\n{SS_HEADER}{rows4}EOF\n;;\n"
        f"*\" -6 \"*) cat <<'EOF'\n{SS_HEADER}{rows6}EOF\n;;\n"
        f"*) cat <<'EOF'\n{SS_HEADER}{rows4}{rows6}EOF\n;;\n"
        "esac\n"
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


class NoIpv6Error(Exception):
    """This machine cannot hold an IPv6 listener on loopback."""


def _listening(family: socket.AddressFamily, address: str, port: int) -> socket.socket:
    """A listening socket of *family* on *address* and *port* that shares its port."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind((address, port))
        sock.listen(1)
    except OSError:
        sock.close()
        raise
    return sock


_PORT_PICKS = 16
"""How many kernel-picked ports :func:`_one_port_on_both_families` tries."""


def _ipv4_listener_beside(v6: socket.socket) -> socket.socket | None:
    """An IPv4 listener on *v6*'s port, or ``None`` when IPv4 already uses that port."""
    try:
        return _listening(socket.AF_INET, "127.0.0.1", v6.getsockname()[1])
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        return None


def _one_port_on_both_families() -> list[socket.socket]:
    """An IPv6-only listener and an IPv4 listener on one loopback port, in that order.

    The kernel picks a port that is free for IPv6. Another process may hold
    it on IPv4, and then the kernel is asked for another: this chooses a
    port, it does not retry a failure.
    """
    for _ in range(_PORT_PICKS):
        try:
            v6 = _listening(socket.AF_INET6, "::1", 0)
        except OSError as exc:
            raise NoIpv6Error(str(exc)) from exc
        # The stack closes the IPv6 socket unless both are handed to the caller.
        with ExitStack() as unless_handed_over:
            unless_handed_over.enter_context(v6)
            v4 = _ipv4_listener_beside(v6)
            if v4 is not None:
                unless_handed_over.pop_all()
                return [v6, v4]
    raise OSError(
        errno.EADDRINUSE, f"no loopback port free on both families in {_PORT_PICKS} picks"
    )


@contextmanager
def listener_on_both_families() -> Iterator[int]:
    """One port held by an IPv4 socket and an IPv6-only socket of this process.

    The shape one ``ncat -l -p PORT`` has: it binds a socket per address
    family. On loopback, so nothing off this machine can reach it.

    Raises:
        NoIpv6Error: if this machine has no IPv6 loopback to bind.
    """
    v6, v4 = _one_port_on_both_families()
    with v6, v4:
        yield v6.getsockname()[1]


@contextmanager
def two_listeners_on_one_family() -> Iterator[int]:
    """One port held by two IPv4 listeners, as two ``SO_REUSEPORT`` netcats hold it."""
    with _listening(socket.AF_INET, "127.0.0.1", 0) as first:
        port = first.getsockname()[1]
        with _listening(socket.AF_INET, "127.0.0.1", port):
            yield port
