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


# Old-stable socat address keywords only (compatible down to procps/socat on
# Linux 2.6.32). ``fork`` lets one listener serve repeated datagrams/connections;
# ``reuseaddr`` avoids TIME_WAIT bind failures on teardown+re-add.
_LISTEN = {"udp": "UDP4-LISTEN", "tcp": "TCP4-LISTEN"}
_DELIVER = {"udp": "UDP4", "tcp": "TCP4"}

_EPHEMERAL_MARKER = "otto-ephemeral"
"""Tags the ephemeral-range line so it is never read as socket output."""

FREE_PORT_PROBE_COMMAND: str = (
    f"cat /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null"
    f" | sed -n 's/^/{_EPHEMERAL_MARKER} /p'; "
    "ss -Htln 2>/dev/null || netstat -tln 2>/dev/null || true"
)
"""Free-port probe run on each chain host, in two parts.

The listener half is ``ss`` preferred, ``netstat`` fallback (both exist on
CentOS 6), parsed by :func:`parse_listening_ports`. The half above it reports
the kernel's EPHEMERAL PORT RANGE, parsed by :func:`parse_ephemeral_ceiling` —
see :func:`carrier_port_floor` for why a carrier port must clear it. Both
halves are best-effort: a host that answers neither contributes nothing, which
is the pre-existing contract (spec §6.2). ``sed`` marks the range line rather
than positional parsing so the two halves stay tellable apart no matter what
the socket tool prints."""

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


def ingress_socat_args(
    protocol: str, service_port: int, bind_ip: str, next_ip: str, carrier_port: int
) -> list[str]:
    """Accept client traffic on the service port, ship over the TCP carrier.

    Binds the endpoint's data-plane ip specifically (never wildcard) so the
    reverse chain's loopback delivery on this same host cannot U-turn into
    this listener (spec §6.3 loop hazard).
    """
    listen = _LISTEN[protocol]
    return [
        "socat",
        f"{listen}:{service_port},bind={bind_ip},fork,reuseaddr",
        f"TCP4:{next_ip}:{carrier_port}",
    ]


def relay_socat_args(carrier_port: int, next_ip: str) -> list[str]:
    """Intermediate-hop pass-through: same carrier port on both sides (§6.2)."""
    return [
        "socat",
        f"TCP4-LISTEN:{carrier_port},fork,reuseaddr",
        f"TCP4:{next_ip}:{carrier_port}",
    ]


def egress_socat_args(
    protocol: str, service_port: int, deliver_ip: str, carrier_port: int
) -> list[str]:
    """Accept the TCP carrier, deliver to the service (loopback or ``--dest``)."""
    deliver = _DELIVER[protocol]
    return [
        "socat",
        f"TCP4-LISTEN:{carrier_port},fork,reuseaddr",
        f"{deliver}:{deliver_ip}:{service_port}",
    ]


def parse_listening_ports(output: str) -> set[int]:
    """Extract every port appearing as ``:<port>`` in ss/netstat output.

    Safe superset of used ports — we only need to avoid them. The
    :data:`_EPHEMERAL_MARKER` line is skipped explicitly: its two bare numbers
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
    * clearing it would leave under :data:`_MIN_CARRIER_WINDOW` ports.

    The floor never drops BELOW :data:`_LEGACY_PORT_FLOOR`: a host with a tight
    ephemeral range must not push carriers down into registered service ports.
    """
    if not ceilings:
        return _LEGACY_PORT_FLOOR
    floor = max(max(ceilings) + 1, _LEGACY_PORT_FLOOR)
    if _MAX_PORT - floor + 1 < _MIN_CARRIER_WINDOW:
        return _LEGACY_PORT_FLOOR
    return floor


def pick_free_port(used: set[int], lo: int = _LEGACY_PORT_FLOOR, hi: int = _MAX_PORT) -> int:
    """First port in ``[lo, hi]`` not in ``used``. Raises when exhausted."""
    for port in range(lo, hi + 1):
        if port not in used:
            return port
    raise NoFreePortError(f"no free TCP port in [{lo}, {hi}]")


class SocatCarrier(TunnelCarrier):
    """socat over a TCP4 carrier — the first-party tunnel transport (#2b)."""

    supported_protocols: ClassVar[frozenset[str]] = frozenset({"tcp", "udp"})
    requirements_command: ClassVar[str] = (
        "command -v socat >/dev/null 2>&1 && command -v bash >/dev/null 2>&1 && echo ok || echo no"
    )
    tools_description: ClassVar[str] = "socat and/or bash"

    @override
    def ingress_args(
        self, protocol: str, service_port: int, bind_ip: str, next_ip: str, carrier_port: int
    ) -> list[str]:
        """Delegate to :func:`ingress_socat_args` (the proven builder)."""
        return ingress_socat_args(protocol, service_port, bind_ip, next_ip, carrier_port)

    @override
    def relay_args(self, carrier_port: int, next_ip: str) -> list[str]:
        """Delegate to :func:`relay_socat_args`."""
        return relay_socat_args(carrier_port, next_ip)

    @override
    def egress_args(
        self, protocol: str, service_port: int, deliver_ip: str, carrier_port: int
    ) -> list[str]:
        """Delegate to :func:`egress_socat_args`."""
        return egress_socat_args(protocol, service_port, deliver_ip, carrier_port)


register_carrier("socat", SocatCarrier)
