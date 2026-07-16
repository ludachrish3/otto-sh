"""Runtime record for one named network device on a host.

The ``host.interfaces`` map is keyed by the **netdev name** (``eth0``,
``eth1.100``, …) so link impairment/capture can address the device directly;
the value object is deliberately extensible (future: mac, cidr, role, …).
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Interface:
    """One named network device on a host."""

    ip: str
    """Address assigned to this interface."""

    subnet: str | None = None
    """Optional network this interface belongs to, in CIDR form
    (``"192.168.1.0/24"``). ``None`` = undeclared."""
