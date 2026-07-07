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


async def test_default_discovery_not_implemented():
    with pytest.raises(NotImplementedError, match="sub-project #2"):
        await discover_dynamic_links(_lab_with([]))


async def test_all_links_default_discover_is_the_guard():
    """With no ``discover=`` argument, ``all_links`` calls the default guard, so
    #2's live swap-in is detectable (the default must stay the guard function).
    """
    with pytest.raises(NotImplementedError, match="sub-project #2"):
        await all_links(_lab_with([]))


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


async def test_dynamic_wins_on_same_route():
    declared = _declared()
    live = Link(  # same route -> same id; observed tunnel with ports
        a=LinkEndpoint(host="carrot_seed", interface="eth1", port=5000),
        b=LinkEndpoint(host="tomato_seed", interface="eth1", port=5001),
        protocol="udp",
        provenance=Provenance.DYNAMIC,
    )
    assert declared.id == live.id  # precondition: route ids reconcile

    async def fake(lab: Lab) -> list[Link]:
        return [live]

    (merged,) = [
        link
        for link in await all_links(_lab_with([declared]), discover=fake)
        if link.id == declared.id
    ]
    assert merged.provenance is Provenance.DYNAMIC
    assert merged.a.port == 5000
