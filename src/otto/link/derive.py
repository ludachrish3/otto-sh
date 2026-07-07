"""Pure derivations of the static link layer (implicit hop edges + declared links).

No I/O and no live host access: callers hand in host dicts / host objects,
these functions hand back :class:`~otto.link.model.Link` objects. That keeps
every rule here unit-testable without a lab.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from ..host.remote_host import make_host_id
from ..models.link import LinkSpec
from .model import Link, LinkEndpoint, Provenance


@dataclass(frozen=True)
class HostAddressing:
    """The minimal addressing view of a host a link endpoint needs."""

    ip: str
    interfaces: dict[str, str] = field(default_factory=dict)
    """Interface name -> address (values already flattened to strings)."""


def addressing_from_dict(host_data: dict[str, Any]) -> tuple[str, HostAddressing]:
    """``(host_id, HostAddressing)`` from a raw lab.json host dict.

    Applies the interface string-shorthand (a bare string value is the ip),
    mirroring ``InterfaceSpec``'s coercion — this reads *raw* dicts so
    cross-lab (dangling) endpoints resolve without constructing hosts.
    """
    host_id = make_host_id(
        host_data["element"],
        host_data.get("element_id"),
        host_data.get("board"),
        host_data.get("slot"),
    )
    raw = host_data.get("interfaces", {})
    interfaces = {
        name: (entry if isinstance(entry, str) else entry.get("ip", ""))
        for name, entry in raw.items()
        if not name.startswith("_")
    }
    return host_id, HostAddressing(ip=host_data.get("ip", ""), interfaces=interfaces)


def _resolve_endpoint(
    host_id: str, interface: str | None, hosts: Mapping[str, HostAddressing]
) -> LinkEndpoint:
    addressing = hosts.get(host_id)
    if addressing is None:
        raise ValueError(f"unknown host {host_id!r} (no such host in any lab file)")
    if interface is not None:
        if interface not in addressing.interfaces:
            known = ", ".join(sorted(addressing.interfaces)) or "<none defined>"
            raise ValueError(f"host {host_id!r} has no interface {interface!r} (known: {known})")
        return LinkEndpoint(host=host_id, interface=interface, ip=addressing.interfaces[interface])
    if len(addressing.interfaces) > 1:
        known = ", ".join(sorted(addressing.interfaces))
        raise ValueError(f"host {host_id!r}: ambiguous interface, specify one of: {known}")
    if len(addressing.interfaces) == 1:
        ((name, ip),) = addressing.interfaces.items()
        return LinkEndpoint(host=host_id, interface=name, ip=ip)
    return LinkEndpoint(host=host_id, interface=None, ip=addressing.ip)


def resolve_declared_links(
    link_data: list[dict[str, Any]],
    hosts: Mapping[str, HostAddressing],
    *,
    source: str,
) -> list[Link]:
    """Validate + resolve raw ``links`` entries into DECLARED ``Link`` objects.

    *source* names the origin (a file path or "lab.json") for error messages.
    """
    links: list[Link] = []
    for idx, entry in enumerate(link_data):
        try:
            spec = LinkSpec.model_validate(entry)
            a = _resolve_endpoint(spec.endpoints[0].host, spec.endpoints[0].interface, hosts)
            b = _resolve_endpoint(spec.endpoints[1].host, spec.endpoints[1].interface, hosts)
        except ValueError as e:
            raise ValueError(f"Invalid link in {source} at index {idx}: {e}") from e
        links.append(
            Link(a=a, b=b, protocol=spec.protocol, provenance=Provenance.DECLARED, name=spec.name)
        )
    return links


def implicit_links(hosts: Mapping[str, Any]) -> list[Link]:
    """IMPLICIT edges from ``hop`` chains, rooted at the built-in ``local`` host.

    Duck-typed on purpose (reads ``id``/``ip``/``hop``/``term``): callers pass
    ``lab.hosts``, tests pass stand-ins. A host with a ``hop`` edges to its hop
    host; a hop-less host edges to ``local`` (the "you are here" root — the
    monitor's reachability cascade needs the full chain back to local).
    Protocol = the child's management term (ssh/telnet).
    """
    links: list[Link] = []
    for host in hosts.values():
        host_id = getattr(host, "id", "")
        if host_id == BUILTIN_LOCAL_HOST_ID:
            continue
        hop_id = getattr(host, "hop", None) or BUILTIN_LOCAL_HOST_ID
        parent = hosts.get(hop_id)
        links.append(
            Link(
                a=LinkEndpoint(
                    host=hop_id, ip=getattr(parent, "ip", "") if parent is not None else ""
                ),
                b=LinkEndpoint(host=host_id, ip=getattr(host, "ip", "")),
                protocol=getattr(host, "term", "ssh") or "ssh",
                provenance=Provenance.IMPLICIT,
            )
        )
    return links
