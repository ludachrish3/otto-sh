"""Referenced ≡ inline: the worked example (spec §20) builds the SAME hosts as tech1 (spec §14).

Two comparisons. The resolved entry dicts must be EQUAL to the inline
flattened dicts — exact, field for field. Then the built hosts must agree on
the attributes a test can see. A mutation that drops one fill (remove
``interfaces`` from ``supplies`` in ``user-settings.toml``) turns both red.
"""

import pytest

from otto.config.lab import load_lab
from otto.config.user_settings import load_user_settings
from otto.inventory import compile_inventory, construct_inventory, resolve_host_entry
from otto.labs.json_repository import JsonFileLabRepository
from tests._fixtures.labdata import lab_data_dir

_UNIX = ["test1", "test2", "test3"]
_ATTRS = [
    "id",
    "ip",
    "element",
    "element_id",
    "is_virtual",
    "site",
    "rack",
    "shelf",
    "docker_capable",
    "valid_terms",
    "valid_transfers",
    "source_lab",
    "element_metadata",
    "lab_info",
    "hop",
]


@pytest.fixture
def inventory():
    root = lab_data_dir() / "tech1-inventory"
    user = load_user_settings(root / "user-settings.toml")
    assert user is not None
    assert user.inventory is not None
    return construct_inventory(compile_inventory(user.inventory, anchor_dir=root, origin="fixture"))


def _flat(tech):
    repo = JsonFileLabRepository(search_paths=[lab_data_dir() / tech])
    # The fixture's parsed elements, exactly as the loader sees them.
    docs = repo._load_documents()
    return {el.name: el.flatten()[0] for doc in docs for el in doc.elements if el.name in _UNIX}


def test_resolved_entries_equal_the_inline_entries(inventory):
    inline = _flat("tech1")
    referenced = _flat("tech1-inventory")
    for name in _UNIX:
        assert resolve_host_entry(referenced[name], inventory).host_data == inline[name], name


def test_built_hosts_agree(inventory):
    inline = load_lab("unix", search_paths=[lab_data_dir() / "tech1"])
    referenced = load_lab(
        "unix", search_paths=[lab_data_dir() / "tech1-inventory"], inventory=inventory
    )
    assert set(inline.hosts) == set(referenced.hosts)
    for name in _UNIX:
        a, b = inline.hosts[name], referenced.hosts[name]
        assert {k: getattr(a, k) for k in _ATTRS} == {k: getattr(b, k) for k in _ATTRS}, name
        assert [(c.login, c.password) for c in a.creds] == [
            (c.login, c.password) for c in b.creds
        ], name
        assert {k: (i.ip, i.subnet) for k, i in a.interfaces.items()} == {
            k: (i.ip, i.subnet) for k, i in b.interfaces.items()
        }, name
        assert b.inventory_ref.key == name
        assert b.inventory_ref.backend.startswith("json:")
        assert a.inventory_ref.referenced is False
    assert referenced.hosts["test1"].inventory_ref.extra == {"asset_tag": "VM-0011"}
