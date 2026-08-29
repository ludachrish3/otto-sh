"""inventory= reaches the json backend's load, summaries and links (spec §6, §14).

The composite, ``host_summaries`` (both routes), ``config.lab.load_lab`` and
the reference example backend all forward it too.
"""

import json

import pytest

from otto.inventory import JsonInventory
from otto.labs import CompositeLabRepository, LabRepositoryError, LabSource, host_summaries
from otto.labs.json_repository import JsonFileLabRepository

_LAB = {
    "labs": {"l": {}},
    "elements": [
        {
            "name": "dut",
            "labs": ["l"],
            "hosts": [
                {
                    "inventory": "dut-1",
                    "os_type": "unix",
                    "creds": [{"login": "u", "password": "p"}],
                }
            ],
        },
        {
            "name": "plain",
            "labs": ["l"],
            "hosts": [
                {"ip": "10.0.0.9", "os_type": "unix", "creds": [{"login": "u", "password": "p"}]}
            ],
        },
    ],
    "links": [
        {
            "endpoints": [
                {"host": "dut", "interface": "eth0"},
                {"host": "plain", "interface": "eth0"},
            ],
            "protocol": "udp",
        }
    ],
}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "lab.json").write_text(
        json.dumps(
            {
                **_LAB,
                "elements": [
                    _LAB["elements"][0],
                    {
                        **_LAB["elements"][1],
                        "hosts": [
                            {
                                **_LAB["elements"][1]["hosts"][0],
                                "interfaces": {"eth0": "192.168.0.9"},
                            }
                        ],
                    },
                ],
            }
        )
    )
    return JsonFileLabRepository(search_paths=[tmp_path])


@pytest.fixture
def inventory(tmp_path):
    p = tmp_path / "inventory.json"
    p.write_text(
        json.dumps(
            {
                "dut-1": {"ip": "10.0.0.1", "interfaces": {"eth0": "192.168.0.1"}},
                "orphan": {"ip": "10.0.0.2"},
            }
        )
    )
    return JsonInventory(p, supplies=["ip", "interfaces"])


def test_load_resolves_referenced_entries_and_stamps_provenance(repo, inventory):
    lab = repo.load_lab("l", inventory=inventory)
    dut = lab.hosts["dut"]
    assert dut.ip == "10.0.0.1"
    assert dut.inventory_ref.key == "dut-1"
    assert dut.inventory_ref.backend == inventory.label
    assert lab.hosts["plain"].inventory_ref.referenced is False
    assert len(lab.links) == 1  # link derivation saw the inventory-supplied interface


def test_load_without_inventory_names_file_element_index_and_key(repo):
    # The lab name is spelled out rather than left to `.*`: the loader's
    # context prefix is file + element + index + LAB, and a pattern that
    # skips over the lab still passes with that half of it deleted.
    with pytest.raises(
        LabRepositoryError,
        match=(
            r"lab\.json': element 'dut' hosts\[0\] in lab 'l': references inventory "
            r"key 'dut-1' but no inventory is configured"
        ),
    ):
        repo.load_lab("l")


def test_unknown_key_is_a_lab_repository_error_with_context(repo, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    # Same narrow `supplies` as the real fixture: with the default (every
    # fillable field) the entry's own inline `creds` would trip the partition
    # rule FIRST, and this test would pass while saying nothing about the
    # unknown key it is named for.
    with pytest.raises(
        LabRepositoryError,
        match=(
            r"element 'dut' hosts\[0\] in lab 'l': inventory key 'dut-1' not found "
            r"in inventory 'json:"
        ),
    ):
        repo.load_lab("l", inventory=JsonInventory(empty, supplies=["ip", "interfaces"]))


def test_an_inventory_owned_field_stated_inline_names_the_entry_that_did_it(tmp_path):
    """The partition rule (spec §2) reaches the user with the loader's context.

    ``resolve_host_entry`` refuses an entry that states inline what the
    inventory owns; the value of joining IN THE LOADER is that the refusal
    arrives naming the file, the element, the index and the lab rather than
    as a bare sentence about a dict.
    """
    (tmp_path / "lab.json").write_text(
        json.dumps(
            {
                "labs": {"l": {}},
                "elements": [
                    {
                        "name": "dut",
                        "labs": ["l"],
                        "hosts": [
                            {
                                "inventory": "dut-1",
                                "ip": "10.9.9.9",  # the inventory owns this
                                "os_type": "unix",
                                "creds": [{"login": "u", "password": "p"}],
                            }
                        ],
                    }
                ],
            }
        )
    )
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps({"dut-1": {"ip": "10.0.0.1"}}))
    with pytest.raises(
        LabRepositoryError,
        match=(
            r"element 'dut' hosts\[0\] in lab 'l': 'ip' is inventory-owned — it comes "
            r"from inventory key 'dut-1'"
        ),
    ):
        JsonFileLabRepository(search_paths=[tmp_path]).load_lab(
            "l", inventory=JsonInventory(inv, supplies=["ip"])
        )


def test_summaries_carry_the_inventory_ip_and_skip_unresolvable_entries(repo, inventory):
    with_inv = {s.id: s for s in repo.list_host_summaries(inventory=inventory)}
    assert with_inv["dut"].ip == "10.0.0.1"
    without = {s.id for s in repo.list_host_summaries()}
    assert without == {"plain"}  # best-effort: a referenced entry needs its inventory


def test_composite_forwards_inventory(repo, inventory):
    composite = CompositeLabRepository([LabSource(label="json", repository=repo)])
    assert composite.load_lab("l", inventory=inventory).hosts["dut"].ip == "10.0.0.1"
    assert {s.id for s in composite.list_host_summaries(inventory=inventory)} == {"dut", "plain"}
    assert {s.id for s in host_summaries(composite, inventory=inventory)} == {"dut", "plain"}


def test_config_load_lab_accepts_and_forwards_inventory(repo, inventory):
    from otto.config.lab import load_lab

    lab = load_lab(
        "l",
        repository=CompositeLabRepository([LabSource(label="json", repository=repo)]),
        inventory=inventory,
    )
    assert lab.hosts["dut"].inventory_ref.key == "dut-1"
    # No `ExampleLabRepository` smoke-check here: the demo dataset holds no
    # referenced entry, so `example.load_lab(..., inventory=inventory).hosts`
    # passes with the resolve deleted. The real guard for that backend is
    # test_the_examples_backend_resolves_and_skips_like_the_json_one below.


def test_the_summaries_fallback_route_forwards_the_inventory_too(repo, inventory):
    """A backend without ``list_host_summaries`` gets its labs loaded — WITH the inventory.

    The fast path and the fallback must agree, and only the fast path has a
    test above. Without ``inventory=`` on the fallback's ``load_lab`` the
    whole lab raises, is skipped as best-effort, and the summary list is
    silently EMPTY rather than wrong — which is exactly the shape that goes
    unnoticed.
    """

    class LoadOnly:  # no list_host_summaries: forces otto.labs.host_summaries' fallback
        def load_lab(self, name, preferences=None, inventory=None):
            return repo.load_lab(name, preferences=preferences, inventory=inventory)

        def list_labs(self):
            return repo.list_labs()

    assert {s.id for s in host_summaries(LoadOnly(), inventory=inventory)} == {"dut", "plain"}
    assert host_summaries(LoadOnly()) == []  # no inventory: the whole lab fails, best-effort


def test_the_examples_backend_resolves_and_skips_like_the_json_one(inventory):
    """The reference implementation honours ``inventory=`` on BOTH its methods.

    Third-party authors copy this class; a sample that resolved on ``load_lab``
    but not on ``list_host_summaries`` would teach exactly the asymmetry the
    conformance suite exists to catch.
    """
    from otto.examples.lab_repository import ExampleLabRepository

    example = ExampleLabRepository(
        labs={"ref": [{"inventory": "dut-1", "element": "dut", "creds": [{"login": "u"}]}]},
        resources={"ref": set()},
    )
    lab = example.load_lab("ref", inventory=inventory)
    assert lab.hosts["dut"].ip == "10.0.0.1"
    assert lab.hosts["dut"].inventory_ref.key == "dut-1"
    assert [s.id for s in example.list_host_summaries(inventory=inventory)] == ["dut"]
    assert example.list_host_summaries() == []  # unresolvable without it, skipped not raised
