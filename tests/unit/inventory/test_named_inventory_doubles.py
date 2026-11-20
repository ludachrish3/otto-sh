"""The shared named-inventory doubles (``tests/_fixtures/named_inventory.py``) do what they say."""

import pytest

from otto.inventory import InventoryKeyError
from otto.inventory.config import compile_inventory, construct_inventory
from otto.models.settings import InventoryConfigSpec
from tests._fixtures.named_inventory import (
    LOOKUP_COUNTING_BACKEND,
    LookupCounting,
    register_lookup_counting,
)


def test_lookup_counting_counts_every_lookup_found_or_not():
    inv = LookupCounting({"k": {"ip": "10.0.0.1"}})
    assert inv.lookups == 0
    assert inv.lookup("k").ip == "10.0.0.1"
    with pytest.raises(InventoryKeyError):
        inv.lookup("nope")
    assert inv.lookups == 2
    inv.list_keys()
    assert inv.lookups == 2  # listing is not a lookup


def test_a_settings_table_builds_the_registered_double(tmp_path, monkeypatch):
    counting = register_lookup_counting(monkeypatch, records={"k": {"ip": "10.0.0.1"}})
    compiled = compile_inventory(
        cfg=InventoryConfigSpec.model_validate({"backend": LOOKUP_COUNTING_BACKEND}),
        anchor_dir=tmp_path,
        origin="settings.toml",
    )
    built = construct_inventory(compiled=compiled)
    assert built is counting
    assert counting.lookups == 0


def test_one_test_registers_a_double_per_inventory(tmp_path, monkeypatch):
    """Distinct names give distinct backends, each building its own counter."""
    default = register_lookup_counting(monkeypatch, records={"k": {"ip": "10.0.0.1"}})
    physical = register_lookup_counting(
        monkeypatch, records={"k": {"ip": "10.0.0.2"}}, name="fake:physical"
    )
    built = {
        name: construct_inventory(
            compiled=compile_inventory(
                cfg=InventoryConfigSpec.model_validate({"backend": name}),
                anchor_dir=tmp_path,
                origin="settings.toml",
            )
        )
        for name in (LOOKUP_COUNTING_BACKEND, "fake:physical")
    }
    assert built == {LOOKUP_COUNTING_BACKEND: default, "fake:physical": physical}
    assert physical.label == "fake:physical"
    assert physical.lookup("k").ip == "10.0.0.2"
    assert (default.lookups, physical.lookups) == (0, 1)
