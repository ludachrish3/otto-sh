"""InventoryRef: provenance of a host built from an inventory record (spec §7)."""

import pytest

from otto.host.factory import create_host_from_dict, host_identity, validate_host_dict
from otto.host.inventory_ref import InventoryRef
from otto.host.product import register_product_provider

_ENTRY = {"ip": "10.0.0.1", "element": "dut", "creds": [{"login": "u", "password": "p"}]}


@pytest.fixture(autouse=True)
def _isolate_provider_registry():
    from otto.host import product as product_mod

    saved = list(product_mod._PRODUCT_PROVIDERS)
    try:
        yield
    finally:
        product_mod._PRODUCT_PROVIDERS[:] = saved


def test_ref_is_unhashable():
    """Pins the docstring's claim: ``extra`` is a dict, so hashing raises."""
    with pytest.raises(TypeError):
        hash(InventoryRef())


def test_ref_copies_extra_and_reports_referenced():
    extra = {"asset": "A-1"}
    ref = InventoryRef(key="k", backend="json:/x", extra=extra)
    extra["asset"] = "mutated"
    assert ref.extra == {"asset": "A-1"}
    assert ref.referenced is True
    assert InventoryRef().referenced is False


def test_factory_stamps_the_ref_before_providers_run():
    """A provider that reads ``host.inventory_ref`` sees it (the ``source_lab``
    stamped-before-providers-run lesson, applied to ``inventory_ref``)."""
    seen: list[InventoryRef] = []

    def provider(host):
        seen.append(host.inventory_ref)

    register_product_provider(provider)
    host = create_host_from_dict(dict(_ENTRY), inventory_ref=InventoryRef(key="k", backend="b"))
    assert host.inventory_ref == InventoryRef(key="k", backend="b")
    assert seen[0].key == "k"


def test_inline_host_carries_an_empty_ref():
    host = create_host_from_dict(dict(_ENTRY))
    assert host.inventory_ref == InventoryRef()
    assert host.inventory_ref.referenced is False


def test_a_null_inventory_key_is_not_a_reference():
    """R7: ``"inventory": None`` (the field's own default) references nothing —
    schema-legal round-tripping must not trip the unresolved-reference guard."""
    host = create_host_from_dict({**_ENTRY, "inventory": None})
    assert host.inventory_ref == InventoryRef()


@pytest.mark.parametrize("entry_point", [create_host_from_dict, host_identity, validate_host_dict])
def test_an_unresolved_reference_is_refused_loudly(entry_point):
    with pytest.raises(ValueError, match=r"references inventory key 'k'.*resolve_host_entry"):
        entry_point({**_ENTRY, "inventory": "k"})


@pytest.mark.parametrize("entry_point", [create_host_from_dict, host_identity, validate_host_dict])
def test_a_null_inventory_key_never_trips_the_guard(entry_point):
    entry_point({**_ENTRY, "inventory": None})  # must not raise
