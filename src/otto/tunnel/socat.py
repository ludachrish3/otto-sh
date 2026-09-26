"""The socat carrier: pure command/argv builders + the SocatCarrier registrant — no I/O.

Bidirectional ingress/relay/egress builders (#2b); every value is a string or
list of strings destined for ``host.exec``; running nothing keeps the whole
module unit-testable (assert exact argv).
"""

import re
from typing import ClassVar

from typing_extensions import override

from ..errors import OttoError
from .carrier import TunnelCarrier, register_carrier


class NoFreePortError(OttoError, RuntimeError):
    """Every port in the carrier range is taken — a pure-local exhaustion.

    No host is involved (the range is scanned against a set the caller
    already gathered), which is why this is its own class rather than one of
    the :mod:`otto.host.errors` pair.
    """


# Old-stable socat address keywords and options only (compatible down to
# socat 1.7 on Linux 2.6.32). ``fork`` lets one listener serve repeated
# datagrams/connections; ``reuseaddr`` avoids TIME_WAIT bind failures on
# teardown+re-add.
_LISTEN = {"udp": "UDP4-LISTEN", "tcp": "TCP4-LISTEN"}
_CONNECT = {"udp": "UDP4", "tcp": "TCP4"}

UDP_BLOCK_BYTES: int = 65535
"""socat's transfer block for a UDP tunnel's processes (``-b``).

A datagram crosses each hop in ONE read and ONE write only if the block holds
it whole. socat's default of 8192 split anything larger into several
datagrams; 65535 covers the IPv4 maximum UDP payload (65,507 bytes)."""

_EPHEMERAL_MARKER = "otto-ephemeral"
"""Tags the ephemeral-range line so it is never read as socket output."""

FREE_PORT_PROBE_COMMAND: str = (
    f"cat /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null"
    f" | sed -n 's/^/{_EPHEMERAL_MARKER} /p'; "
    "ss -Htln 2>/dev/null || netstat -tln 2>/dev/null || true; "
    "ss -Huln 2>/dev/null || netstat -uln 2>/dev/null || true"
)
"""Free-port probe run on each chain host, in two parts.

The listener half reports both TCP and UDP listeners — ``ss`` preferred,
``netstat`` fallback (both exist on CentOS 6) — parsed by
:func:`parse_listening_ports`; their union is the used set. A UDP tunnel's
carrier ports are UDP, and a port held by either protocol is avoided, which is
a safe superset because allocation never needs a port that is free for one
protocol only. The half above it reports the kernel's EPHEMERAL PORT RANGE,
parsed by :func:`parse_ephemeral_ceiling` — see :func:`carrier_port_floor` for
why a carrier port must clear it. Every part is best-effort: a host that
answers none of them contributes nothing, which is the pre-existing contract
(spec §6.2). ``sed`` marks the range line rather than positional parsing so the
parts stay tellable apart no matter what the socket tool prints."""

_PORT_RE = re.compile(r":(\d{1,5})\b")
_NUM_RE = re.compile(r"\d+")
_RANGE_FIELDS = 2
"""``ip_local_port_range`` is exactly ``<low> <high>``; anything else is not it."""
_MAX_PORT = 65535
_LEGACY_PORT_FLOOR = 49152
"""IANA dynamic/private floor — where carrier ports came from before #284, and
still the fallback whenever the kernel's range cannot be cleared."""

_MIN_CARRIER_WINDOW = 256
"""Ports that must remain above the ephemeral ceiling for it to be worth
clearing. Two carrier ports is the hard need; the margin is for the deliberate
listeners (other tunnels included) that also live up there, so a chain does not
start failing to allocate merely because it moved out of the kernel's way."""


def _socat(protocol: str, idle_timeout: int | None) -> list[str]:
    """Build the program and options for *protocol*; no ``-T`` unless the user asked for one.

    Without *idle_timeout* nothing times out: a tunnel and every flow through
    it stay up until the tunnel is removed, and TCP's argv carries no options at
    all. With it, socat's ``-T`` ends a TCP connection, or a UDP flow's
    per-peer child, after that many seconds without traffic. The listening
    parent never exits, so the tunnel stays up and the flow's next packet
    starts afresh.
    """
    argv = ["socat", "-b", str(UDP_BLOCK_BYTES)] if protocol == "udp" else ["socat"]
    if idle_timeout is not None:
        argv += ["-T", str(idle_timeout)]
    return argv


def ingress_socat_args(
    protocol: str,
    service_port: int,
    bind_ip: str,
    next_ip: str,
    carrier_port: int,
    *,
    idle_timeout: int | None = None,
) -> list[str]:
    """Accept client traffic on the service port and ship it over the carrier.

    The carrier speaks the service's protocol: a UDP tunnel carries each
    datagram as a datagram hop to hop, so boundaries survive (a TCP stream
    would merge and re-cut them). Binds the endpoint's data-plane ip
    specifically (never wildcard) so the reverse chain's loopback delivery on
    this same host cannot U-turn into this listener (spec §6.3 loop hazard).
    """
    return [
        *_socat(protocol, idle_timeout),
        f"{_LISTEN[protocol]}:{service_port},bind={bind_ip},fork,reuseaddr",
        f"{_CONNECT[protocol]}:{next_ip}:{carrier_port}",
    ]


def relay_socat_args(
    protocol: str, carrier_port: int, next_ip: str, *, idle_timeout: int | None = None
) -> list[str]:
    """Intermediate-hop pass-through: same carrier port on both sides (§6.2)."""
    return [
        *_socat(protocol, idle_timeout),
        f"{_LISTEN[protocol]}:{carrier_port},fork,reuseaddr",
        f"{_CONNECT[protocol]}:{next_ip}:{carrier_port}",
    ]


def egress_socat_args(
    protocol: str,
    service_port: int,
    deliver_ip: str,
    carrier_port: int,
    *,
    idle_timeout: int | None = None,
) -> list[str]:
    """Accept the carrier and deliver to the service (loopback or ``--dest``)."""
    return [
        *_socat(protocol, idle_timeout),
        f"{_LISTEN[protocol]}:{carrier_port},fork,reuseaddr",
        f"{_CONNECT[protocol]}:{deliver_ip}:{service_port}",
    ]


def parse_listening_ports(output: str) -> set[int]:
    """Extract every port appearing as ``:<port>`` in ss/netstat output.

    Safe superset of used ports — we only need to avoid them. The
    ``_EPHEMERAL_MARKER`` line is skipped explicitly: its two bare numbers
    are a RANGE, not two bound ports, and reading them as ports would blacklist
    two arbitrary ports on every chain.
    """
    return {
        int(m)
        for line in output.splitlines()
        if not line.startswith(_EPHEMERAL_MARKER)
        for m in _PORT_RE.findall(line)
        if 0 < int(m) <= _MAX_PORT
    }


def parse_ephemeral_ceiling(output: str) -> int | None:
    """Highest port this host's kernel may auto-assign, or ``None`` if unsaid.

    ``None`` is the honest answer for a host that has no
    ``/proc/sys/net/ipv4/ip_local_port_range`` (or no ``sed``) — it is not the
    same claim as "this kernel assigns nothing", and
    :func:`carrier_port_floor` treats it as the absence of evidence it is.
    """
    for line in output.splitlines():
        if not line.startswith(_EPHEMERAL_MARKER):
            continue
        nums = _NUM_RE.findall(line[len(_EPHEMERAL_MARKER) :])
        if len(nums) == _RANGE_FIELDS and 0 < int(nums[-1]) <= _MAX_PORT:
            return int(nums[-1])
    return None


def carrier_port_floor(ceilings: list[int]) -> int:
    """Lowest carrier port no chain kernel will hand out on its own (#284).

    The free-port probe sees LISTENING sockets only, so a port held as the
    *source* port of an outbound connection is invisible to it and gets
    allocated anyway. ``reuseaddr`` does not rescue the bind that follows:
    ``SO_REUSEADDR`` waives a conflict against ``TIME_WAIT`` and against other
    reuse-flagged sockets, NOT against an ordinary active socket, so socat
    exits instantly with ``EADDRINUSE``. The legacy ``[49152, 65535]`` window
    overlaps Linux's default ephemeral range (``32768 60999``) across
    ``49152-60999``, which made every carrier port stealable — rarely, and only
    under the connection churn of a loaded host, which is exactly the shape
    #284 was reported with.

    Starting ABOVE every chain host's ceiling closes that structurally rather
    than narrowing it: a port the kernel will never auto-assign cannot be
    raced. The HIGHEST ceiling governs, because one permissive host in the
    chain is enough to steal a port the others considered safe.

    Two cases keep the legacy floor, both deliberately best-effort rather than
    a refusal to build the tunnel — the post-add verify still catches a real
    collision, which is strictly more than #284 had:

    * no host answered, so there is no ceiling to clear;
    * clearing it would leave under ``_MIN_CARRIER_WINDOW`` ports.

    The floor never drops BELOW ``_LEGACY_PORT_FLOOR``: a host with a tight
    ephemeral range must not push carriers down into registered service ports.
    """
    if not ceilings:
        return _LEGACY_PORT_FLOOR
    floor = max(max(ceilings) + 1, _LEGACY_PORT_FLOOR)
    if _MAX_PORT - floor + 1 < _MIN_CARRIER_WINDOW:
        return _LEGACY_PORT_FLOOR
    return floor


_DUMP_FLAG = {"tcp": "t", "udp": "u"}


def socket_dump_command(protocol: str) -> str:
    """All-states socket dump for *protocol*, run only to DIAGNOSE a post-add verify failure.

    Deliberately not :data:`FREE_PORT_PROBE_COMMAND`: allocation wants
    listeners (the set to avoid), diagnosis wants every state, because the
    thief in a port race is precisely the socket that is not listening. One
    protocol per dump: a mixed ``ss -tu`` dump adds a leading Netid column and
    would move the local address off the column :func:`parse_port_holders`
    reads.
    """
    flag = _DUMP_FLAG[protocol]
    return f"ss -H{flag}an 2>/dev/null || netstat -{flag}an 2>/dev/null || true"


_LOCAL_ADDR_FIELD = 3
"""Column of the LOCAL address in both dumps: ``ss -H{t,u}an`` prints
``State Recv-Q Send-Q Local Peer`` and ``netstat -{t,u}an`` prints
``Proto Recv-Q Send-Q Local Foreign State`` — the same index by luck, pinned
for both tools by ``TestPortHolders`` in ``tests/unit/tunnel/test_socat.py``."""


def parse_port_holders(output: str, port: int) -> list[str]:
    """Dump lines whose LOCAL port is *port* — what to blame for a failed bind.

    Matches on the local address only. A line whose PEER is on *port* is
    somebody else's listener seen from this host and holds nothing here, so
    naming it would send the next reader after the wrong machine.

    Never raises: this runs on a host that has already misbehaved, and a
    diagnosis that throws would replace the real error with its own.
    """
    holders = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) > _LOCAL_ADDR_FIELD and fields[_LOCAL_ADDR_FIELD].rsplit(":", 1)[-1] == str(
            port
        ):
            holders.append(line.strip())
    return holders


def pick_free_port(used: set[int], lo: int = _LEGACY_PORT_FLOOR, hi: int = _MAX_PORT) -> int:
    """First port in ``[lo, hi]`` not in ``used``. Raises when exhausted."""
    for port in range(lo, hi + 1):
        if port not in used:
            return port
    raise NoFreePortError(f"no free port in [{lo}, {hi}]")


class SocatCarrier(TunnelCarrier):
    """socat, carrying each protocol as itself hop to hop — the first-party tunnel transport."""

    supported_protocols: ClassVar[frozenset[str]] = frozenset({"tcp", "udp"})
    requirements_command: ClassVar[str] = (
        "command -v socat >/dev/null 2>&1 && command -v bash >/dev/null 2>&1 && echo ok || echo no"
    )
    tools_description: ClassVar[str] = "socat and/or bash"

    @override
    def ingress_args(
        self,
        protocol: str,
        service_port: int,
        bind_ip: str,
        next_ip: str,
        carrier_port: int,
        *,
        idle_timeout: int | None = None,
    ) -> list[str]:
        """Delegate to :func:`ingress_socat_args` (the proven builder)."""
        return ingress_socat_args(
            protocol, service_port, bind_ip, next_ip, carrier_port, idle_timeout=idle_timeout
        )

    @override
    def relay_args(
        self, protocol: str, carrier_port: int, next_ip: str, *, idle_timeout: int | None = None
    ) -> list[str]:
        """Delegate to :func:`relay_socat_args`."""
        return relay_socat_args(protocol, carrier_port, next_ip, idle_timeout=idle_timeout)

    @override
    def egress_args(
        self,
        protocol: str,
        service_port: int,
        deliver_ip: str,
        carrier_port: int,
        *,
        idle_timeout: int | None = None,
    ) -> list[str]:
        """Delegate to :func:`egress_socat_args`."""
        return egress_socat_args(
            protocol, service_port, deliver_ip, carrier_port, idle_timeout=idle_timeout
        )


register_carrier("socat", SocatCarrier)
