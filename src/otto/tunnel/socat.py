"""The socat carrier: pure command/argv builders + the SocatCarrier registrant — no I/O.

Bidirectional ingress/relay/egress builders (#2b); every value is a string or
list of strings destined for ``host.exec``; running nothing keeps the whole
module unit-testable (assert exact argv).
"""

import re
from typing import ClassVar

from typing_extensions import override

from .carrier import TunnelCarrier, register_carrier

# Old-stable socat address keywords only (compatible down to procps/socat on
# Linux 2.6.32). ``fork`` lets one listener serve repeated datagrams/connections;
# ``reuseaddr`` avoids TIME_WAIT bind failures on teardown+re-add.
_LISTEN = {"udp": "UDP4-LISTEN", "tcp": "TCP4-LISTEN"}
_DELIVER = {"udp": "UDP4", "tcp": "TCP4"}

FREE_PORT_PROBE_COMMAND: str = "ss -Htln 2>/dev/null || netstat -tln 2>/dev/null || true"
"""Free-port probe run on each chain host — ``ss`` preferred, ``netstat``
fallback (both exist on CentOS 6). Parsed by :func:`parse_listening_ports`."""

_PORT_RE = re.compile(r":(\d{1,5})\b")
_MAX_PORT = 65535


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

    Safe superset of used ports — we only need to avoid them.
    """
    return {int(m) for m in _PORT_RE.findall(output) if 0 < int(m) <= _MAX_PORT}


def pick_free_port(used: set[int], lo: int = 49152, hi: int = 65535) -> int:
    """First port in ``[lo, hi]`` not in ``used``. Raises when exhausted."""
    for port in range(lo, hi + 1):
        if port not in used:
            return port
    raise RuntimeError(f"no free TCP port in [{lo}, {hi}]")


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
