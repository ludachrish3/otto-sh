"""all_links reconciliation: static and dynamic merged by route id."""

import pytest

from otto.configmodule.lab import Lab
from otto.link import Link, LinkEndpoint, Provenance
from otto.link.discovery import all_links, discover_dynamic_links

pytestmark = pytest.mark.asyncio


def _declared(proto="udp") -> Link:
    return Link(
        a=LinkEndpoint(host="carrot_seed", interface="eth1", ip="192.168.1.11"),
        b=LinkEndpoint(host="tomato_seed", interface="eth1", ip="192.168.1.12"),
        protocol=proto,
        provenance=Provenance.DECLARED,
    )


def _lab_with(links: list[Link]) -> Lab:
    lab = Lab(name="t")
    lab.links = links
    return lab


async def test_default_discovery_returns_empty_for_a_hostless_lab():
    """#2's live layer landed (Task 4): with no hosts to scan, discovery finds
    nothing and returns cleanly — no guard exception left to trip.
    """
    assert await discover_dynamic_links(_lab_with([])) == []


async def test_all_links_default_discover_is_live():
    """With no ``discover=`` argument, ``all_links`` calls the real
    :func:`discover_dynamic_links` (the default stays wired to it, not a
    stand-in) — a hostless lab's dynamic layer contributes nothing, so the
    result is exactly its static links.
    """
    declared = _declared()
    links = await all_links(_lab_with([declared]))
    assert [link.id for link in links] == [declared.id]


async def test_all_links_unions_static_and_dynamic():
    declared = _declared()
    dynamic = Link(
        a=LinkEndpoint(host="basil_seed", port=5000),
        b=LinkEndpoint(host="carrot_seed", port=5000),
        provenance=Provenance.DYNAMIC,
    )

    async def fake(lab: Lab) -> list[Link]:
        return [dynamic]

    ids = {link.id for link in await all_links(_lab_with([declared]), discover=fake)}
    assert ids == {declared.id, dynamic.id}


async def test_declared_and_dynamic_coexist_on_same_route():
    """A live tunnel and the declared route it realizes share a physical path
    but NOT an id: dynamic ids (``lnk-<hex>-<port>``) and static ids
    (``name``/``a--b``) are disjoint id-spaces (design spec §9.1). So
    ``all_links`` keeps BOTH as separate rows — no replace, no field-enrich —
    each retaining its own provenance.
    """
    declared = _declared()
    live = Link(  # same physical route, but a hashed+suffixed dynamic id
        a=LinkEndpoint(host="carrot_seed", interface="eth1", port=5000),
        b=LinkEndpoint(host="tomato_seed", interface="eth1", port=5001),
        protocol="udp",
        provenance=Provenance.DYNAMIC,
    )
    assert declared.id != live.id  # disjoint id-spaces: no collision

    async def fake(lab: Lab) -> list[Link]:
        return [live]

    by_id = {link.id: link for link in await all_links(_lab_with([declared]), discover=fake)}
    assert {declared.id, live.id} <= by_id.keys()  # both coexist
    assert by_id[declared.id].provenance is Provenance.DECLARED
    assert by_id[live.id].provenance is Provenance.DYNAMIC
    assert by_id[live.id].a.port == 5000  # dynamic row keeps its observed ports
