"""What ``otto --show-lab`` shows: a loaded lab's hosts and links, as plain values.

A pure read of a built :class:`~otto.labs.loader.Lab`: nothing is contacted,
probed or resolved. Each host is named by what a reader needs to recognise
and reach it -- its id, its class, its address, its transports and the
products ingest attached -- and each link by its id, its two endpoints and
its protocol. Cells are strings (and lists of strings); the CLI owns
presentation (:func:`otto.cli.show_lab.render_lab`).
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..link.model import LinkEndpoint
    from .loader import Lab

LOCAL_ADDRESS = "this machine"
"""The address cell of the built-in ``local`` host, which otto reaches without a network."""


@dataclass
class HostOverview:
    """One host, as ``--show-lab`` names it."""

    id: str
    """The host's id, the name every command addresses it by."""

    kind: str
    """The host's class name (``UnixHost``, ``ZephyrHost``, ``LocalHost``, ...)."""

    address: str
    """The management ip, with ``(via <hop>)`` when a hop is crossed first; a
    container's parent host; :data:`LOCAL_ADDRESS` for the machine otto runs on."""

    transports: list[str] = field(default_factory=list)
    """The terminal, then the file-transfer protocol (``["ssh", "scp"]``); empty
    for a host that has neither as a setting."""

    products: list[str] = field(default_factory=list)
    """Names of the products attached to the host, in attachment order."""


@dataclass
class LinkOverview:
    """One link, as ``--show-lab`` names it."""

    id: str
    a: str
    """The first endpoint: ``host`` or ``host:interface``."""

    b: str
    """The second endpoint, in the same form."""

    protocol: str


@dataclass
class LabOverview:
    """The lab: its name, its hosts in lab order, its links in declaration order."""

    name: str
    hosts: list[HostOverview] = field(default_factory=list)
    links: list[LinkOverview] = field(default_factory=list)


def _address(host: Any) -> str:
    ip = getattr(host, "ip", None)
    if isinstance(ip, str) and ip:
        hop = getattr(host, "hop", None)
        return f"{ip} (via {hop})" if hop else ip
    parent = getattr(host, "parent", None)
    if parent is not None:
        container = getattr(host, "service", "") or getattr(host, "name", "")
        where = f"on {getattr(parent, 'id', '?')}"
        return f"container {container} {where}" if container else f"container {where}"
    from ..host.local_host import LocalHost  # local: keeps the host graph off this import

    return LOCAL_ADDRESS if isinstance(host, LocalHost) else ""


def _transports(host: Any) -> list[str]:
    return [
        value
        for value in (getattr(host, "term", None), getattr(host, "transfer", None))
        if isinstance(value, str) and value
    ]


def _endpoint(end: "LinkEndpoint") -> str:
    return f"{end.host}:{end.interface}" if end.interface else end.host


def lab_overview(lab: "Lab") -> LabOverview:
    """Return *lab*'s hosts and links as :class:`LabOverview` values, contacting nothing."""
    hosts = [
        HostOverview(
            id=host_id,
            kind=type(host).__name__,
            address=_address(host),
            transports=_transports(host),
            products=[product.name for product in getattr(host, "products", [])],
        )
        for host_id, host in lab.hosts.items()
    ]
    links = [
        LinkOverview(id=link.id, a=_endpoint(link.a), b=_endpoint(link.b), protocol=link.protocol)
        for link in lab.links
    ]
    return LabOverview(name=lab.name, hosts=hosts, links=links)
