"""Pure derivations of the static link layer (implicit hop edges + declared links).

No I/O and no live host access: callers hand in host dicts / host objects,
these functions hand back :class:`~otto.link.model.Link` objects. That keeps
every rule here unit-testable without a lab.
"""

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import final

from ..host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from ..host.element import Element
from ..host.factory import host_identity
from ..host.remote_host import slug
from ..models.link import LinkSpec
from .model import Link, LinkEndpoint, Provenance

if TYPE_CHECKING:
    from ..host.os_profile import ProfileContext
    from ..labs.protocol import LabScope, SkippedHostRecord

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, kw_only=True)
class HostAddressing:
    """The minimal addressing view of a host a link endpoint needs."""

    ip: str
    interfaces: dict[str, str] = field(default_factory=dict)
    """Interface name -> address (values already flattened to strings)."""


@final
@dataclass(frozen=True, kw_only=True)
class DeclaredLinkEntry:
    """One raw ``links`` entry of a lab document, with where it was declared."""

    origin: Path
    """The document the entry was declared in, which its error names."""

    index: int
    """The entry's index in that document's ``links`` section."""

    entry: object
    """The entry as the document holds it, before validation."""


# DEBT(no-tuple-return): resolved id plus its addressing.
# ast-grep-ignore: no-tuple-return
def addressing_from_dict(
    host_data: dict[str, Any], element: Element, *, profiles: "ProfileContext | None" = None
) -> tuple[str, HostAddressing]:
    """``(host_id, HostAddressing)`` from a raw lab.json host dict and its element.

    Applies the interface string-shorthand (a bare string value is the ip),
    mirroring ``InterfaceSpec``'s coercion — it takes dicts rather than hosts
    so cross-lab (dangling) endpoints resolve without constructing hosts.

    The id comes from :func:`~otto.host.factory.host_identity`, NOT from
    formatting the raw fields: it must equal the id ``lab.hosts`` is keyed by,
    or a declared link either fails to resolve against a host that plainly
    exists, or resolves to an endpoint naming a host that does not. Hand
    derivation diverges under any ``os_profile`` that defaults ``board`` /
    ``slot`` — those id parts are invisible in the raw dict.

    Consequently this is NOT a pure reader of self-contained data: it resolves
    through the ``os_profile`` / ``command_frame`` registries and validates.
    A record naming a profile or frame the current process never registered
    RAISES, and callers enumerating whole files must skip such records rather
    than let one deny the rest. *profiles* is the project data the ``os_type``
    resolves through, as in :func:`~otto.host.factory.host_identity`.
    """
    host_id = host_identity(host_data, element=element, profiles=profiles).id
    raw = host_data.get("interfaces", {})
    interfaces = {
        name: (entry if isinstance(entry, str) else entry.get("ip", ""))
        for name, entry in raw.items()
        if not name.startswith("_")
    }
    return host_id, HostAddressing(ip=host_data.get("ip", ""), interfaces=interfaces)


def addressing_of_host(host: object) -> HostAddressing:
    """Return a BUILT host's addressing, as :func:`addressing_from_dict` reads its record.

    Duck-typed, as :func:`implicit_links` reads hosts: ``ip`` and each
    ``interfaces`` value's ``ip``. A host of any backend is then a link
    endpoint, whether or not a lab document declared it.
    """
    interfaces = {
        name: str(getattr(entry, "ip", entry))
        for name, entry in (getattr(host, "interfaces", None) or {}).items()
    }
    return HostAddressing(ip=getattr(host, "ip", "") or "", interfaces=interfaces)


def _is_host_of(host_id: str, element: str) -> bool:
    """Return whether *host_id* is an id an element named *element* composes.

    Ids are ``<element>-<board>[-<slot>]``: the element's slug is the id, or a
    prefix of it up to a ``-``. The one statement of that rule for a name that
    is not a known id: the near-miss hint and the skipped-record match read it.
    """
    stem = slug(element)
    return host_id == stem or host_id.startswith(f"{stem}-")


def _skipped_as(host_id: str, skipped: "Sequence[SkippedHostRecord]") -> "str | None":
    """Return why *host_id* was not loaded, when records of *skipped* could be that host.

    A skipped record has no id (it could not be read), so it is matched by its
    element (:func:`_is_host_of`). That is a match on the name alone: a
    skipped record of element ``bb`` is also reported for an endpoint
    ``bb-1310-qemu`` that belongs to an element ``bb-1310`` no file holds.
    The endpoint is missing either way; the message then names a record that
    is not its own.
    """
    causes = [
        f"the record of element {record.element!r} in {record.origin} was skipped: {record.reason}"
        for record in skipped
        if _is_host_of(host_id, record.element)
    ]
    if not causes:
        return None
    return f"host {host_id!r} was not loaded: {'; '.join(causes)}"


def _resolve_endpoint(
    host_id: str,
    interface: str | None,
    hosts: Mapping[str, HostAddressing],
    skipped: "Callable[[], Sequence[SkippedHostRecord]]",
) -> LinkEndpoint:
    addressing = hosts.get(host_id)
    if addressing is None:
        # A record that was found and skipped is not an unknown host: say what
        # happened to it, which the skip itself only logs at debug.
        not_loaded = _skipped_as(host_id, skipped())
        if not_loaded is not None:
            raise ValueError(not_loaded)
        # Name the near-miss: a link endpoint written by hand is easy to get
        # subtly wrong (an id whose board/slot come from an os_profile, or a
        # name whose slug the author spelled as they typed it), and "no such
        # host" for a host that is plainly in the file is a dead end without
        # this.
        # The commonest slip is the ELEMENT name typed where the composed id
        # belongs; ids are ``<element>-<board>[-<slot>]``, so a prefix match
        # finds it however short the element name is (a plain similarity
        # ratio drops under its cutoff once the id grows a board and slot).
        close = [h for h in sorted(hosts) if _is_host_of(h, host_id)][:3]
        if not close:
            close = get_close_matches(host_id, sorted(hosts), n=3)
        hint = f" — did you mean {', '.join(repr(c) for c in close)}?" if close else ""
        raise ValueError(f"unknown host {host_id!r} (no such host in any lab file){hint}")
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


def _validate_impair(
    impair: str | None, a: LinkEndpoint, b: LinkEndpoint, hosts: Mapping[str, HostAddressing]
) -> None:
    if impair is None:
        return
    if impair not in hosts:
        raise ValueError(f"impair host {impair!r} is not a known host")
    if impair in (a.host, b.host):
        raise ValueError(
            f"impair host {impair!r} is an endpoint of the link — "
            "an in-path middlebox must be a third host"
        )


def raw_endpoint_host_ids(entry: object) -> list[str]:
    """Best-effort endpoint host ids from a *raw* link entry, before validation.

    Used only to decide relevance (does this link touch the requested lab?), so
    it must never raise: a non-dict entry, a non-list ``endpoints``, or a
    non-dict endpoint yields no ids — an unrelated lab's malformed link is then
    treated as non-relevant and skipped rather than crashing the load.

    Public because relevance has two callers that must agree:
    :func:`resolve_declared_links` decides which links a lab LOADS, and
    ``otto.config.completion_cache.collect_link_ids`` decides which it OFFERS.
    A completer applying its own version of this rule offers links the loader
    then refuses.
    """
    if not isinstance(entry, dict):
        return []
    endpoints = entry.get("endpoints")
    if not isinstance(endpoints, list):
        return []
    ids: list[str] = []
    for ep in endpoints:
        if isinstance(ep, dict):
            host = ep.get("host")
            if isinstance(host, str):
                ids.append(host)
    return ids


def link_attaches(
    *,
    endpoint_ids: Collection[str],
    selection: frozenset[str] | None,
    loaded_ids: Collection[str] | None,
    scope: "LabScope | None",
) -> bool:
    """Return whether a declared link with *endpoint_ids* belongs to the selected labs.

    The one relevance decision. *loaded_ids* ``None`` attaches every link (a
    caller that offers every link); otherwise a link attaches when at least
    one of its endpoints is a loaded host, and, when *scope* and *selection*
    are both set, some selected lab is inside *scope* (a scoped source's link
    never joins a lab outside its scope). *selection* is the set of selected
    lab names, ``None`` when no lab is selected.
    """
    if loaded_ids is None:
        return True
    if not any(host_id in loaded_ids for host_id in endpoint_ids):
        return False
    if scope is not None and selection is not None:
        return any(scope.contains(name=name) for name in selection)
    return True


def resolve_declared_links(
    entries: Sequence[DeclaredLinkEntry],
    hosts: Mapping[str, HostAddressing],
    *,
    loaded_ids: Collection[str],
    selection: frozenset[str] | None,
    scope: "LabScope | None" = None,
    skipped: "Callable[[], Sequence[SkippedHostRecord]]" = tuple,
) -> list[Link]:
    """Validate + resolve raw ``links`` entries into DECLARED ``Link`` objects.

    *skipped* returns the host records the sources found and could not read;
    it is called only when an attached link names an endpoint *hosts* does not
    address, and such an endpoint is then reported as not loaded, with the
    record's reason, rather than as unknown.

    *loaded_ids* is the id set of the requested lab's hosts and *selection*
    the selected lab names (``None``: none, as :func:`link_attaches` reads
    it); *entries* may span other labs' links (so cross-lab dangling
    endpoints resolve against *hosts*), and an entry that does not attach
    (:func:`link_attaches`: neither endpoint in *loaded_ids*, or outside
    *scope*) belongs to an unrelated lab and is **skipped** — its errors
    (unknown host, bad interface, or even a structurally malformed record)
    must not break this lab's load, symmetric with the cross-lab host-record
    containment of the lab-file sources. An entry that attaches is resolved
    and fails loud, naming its document and its index there.
    """
    links: list[Link] = []
    for declared in entries:
        entry = declared.entry
        # Relevance is read from the raw entry BEFORE strict validation, so an
        # unrelated lab's malformed link is filtered out before it can raise.
        if not link_attaches(
            endpoint_ids=raw_endpoint_host_ids(entry),
            selection=selection,
            loaded_ids=loaded_ids,
            scope=scope,
        ):
            logger.debug(
                f"Skipping unrelated-lab link at index {declared.index} in "
                f"{declared.origin}: {entry!r}"
            )
            continue
        try:
            spec = LinkSpec.model_validate(entry)
            first, second = spec.endpoints
            a = _resolve_endpoint(first.host, first.interface, hosts, skipped)
            b = _resolve_endpoint(second.host, second.interface, hosts, skipped)
            _validate_impair(spec.impair, a, b, hosts)
        except ValueError as e:
            raise ValueError(
                f"Invalid link in {declared.origin} at index {declared.index}: {e}"
            ) from e
        links.append(
            Link(
                a=a,
                b=b,
                protocol=spec.protocol,
                provenance=Provenance.DECLARED,
                name=spec.name,
                impair=spec.impair,
            )
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
