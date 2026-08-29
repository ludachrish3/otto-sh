"""element_resources: the element's set, copied onto each member host (spec §3).

A LOADER argument like ``element_metadata`` — the element level has no runtime
registry, so the only way "the union over elements in play" falls out of "the
union over hosts in play" is for each member host to carry its element's set.

The product-provider registry these tests write to is snapshot-restored by the
root conftest's autouse ``_restore_provider_registries``; no local fixture.
"""

import dataclasses

import pytest

from otto.host.builtin_hosts import make_builtin_local_host
from otto.host.docker_host import DockerContainerHost
from otto.host.factory import create_host_from_dict
from otto.host.product import register_product_provider

_ENTRY = {
    "ip": "10.0.0.1",
    "element": "chassis",
    "element_id": 1,
    "creds": [{"login": "u", "password": "p"}],
}


def test_factory_stamps_element_resources_before_providers_run():
    """A provider gated on a reservation must be able to read the stamp.

    The same lesson ``source_lab`` and ``inventory_ref`` already carry: a stamp
    applied after ``apply_product_providers`` is invisible to exactly the code
    that needs it.
    """
    seen: list[frozenset[str]] = []

    def provider(host):
        seen.append(host.element_resources)

    register_product_provider(provider)
    host = create_host_from_dict(dict(_ENTRY), element_resources=["chassis-1", "chassis-1"])
    assert host.element_resources == frozenset({"chassis-1"})
    assert isinstance(host.element_resources, frozenset)
    assert seen == [frozenset({"chassis-1"})]


def test_defaults_are_empty_and_separate_from_the_hosts_own():
    """Three levels, three sets: the host's own slot is not the element's."""
    host = create_host_from_dict({**_ENTRY, "resources": ["slot-1"]})
    assert host.element_resources == frozenset()
    assert host.resources == frozenset({"slot-1"})


def test_element_resources_is_a_loader_argument_not_an_entry_field():
    """The host specs are ``extra='forbid'``, so a lab entry cannot claim its
    element's reservation for itself. Anchored on pydantic's exact sentence for
    the key, not a loose ``element_resources``."""
    with pytest.raises(ValueError, match=r"element_resources\s+Extra inputs are not permitted"):
        create_host_from_dict({**_ENTRY, "element_resources": ["x"]})


def test_local_and_containers_carry_nothing():
    """Neither is a reservable unit (spec §3).

    ``local`` is asserted on the real built-in host; a container is asserted on
    the dataclass defaults, because constructing one needs a parent host — the
    defaults ARE the contract for a class no lab entry ever declares.
    """
    local = make_builtin_local_host()
    assert local.resources == frozenset()
    assert local.element_resources == frozenset()

    by_name = {f.name: f for f in dataclasses.fields(DockerContainerHost)}
    assert by_name["resources"].default_factory is frozenset
    assert by_name["element_resources"].default_factory is frozenset
