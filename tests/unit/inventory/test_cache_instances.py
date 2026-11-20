"""Instances through the snapshot cache, ``export`` and ``diff`` (lab templating §6.2, §8).

A cached inventory must serve discovery, instances included, exactly as its
backend does, and a snapshot written before instances existed must keep being
served unchanged: same filename, same fingerprint, same export bytes, and no
request to the backend.
"""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from otto import bootstrap
from otto.cli.inventory import inventory_app
from otto.inventory.cache import SnapshotCache, snapshot_cache_of
from otto.inventory.discovery import Discovery, discover_inventory
from otto.inventory.json_backend import JsonInventory
from otto.inventory.snapshot import (
    diff_instances,
    diff_records,
    discovery_to_document,
    document_hash,
    document_to_discovery,
    records_to_document,
)
from otto.models.inventory import INVENTORY_V2_FORMAT, InventoryInstance, InventoryRecord
from tests._fixtures.netbox_replay import RequestTrap
from tests._fixtures.paths import TESTS_ROOT
from tests._fixtures.project_scaffold import make_project

FIXTURE = TESTS_ROOT / "_fixtures" / "legacy_snapshot"
_T0 = datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc)
"""One hour after the legacy meta's ``fetched_at``: inside its 24h TTL."""


def _toml_table(name: str, table: "dict[str, object]") -> str:
    """Render a flat table of scalars as TOML (JSON string syntax is TOML's basic string)."""
    lines = [f"[{name}]"]
    for key, value in table.items():
        assert isinstance(value, (str, int, bool)), (key, value)
        lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def test_a_pre_upgrade_snapshot_is_served_unchanged(tmp_path, monkeypatch):
    recorded = json.loads((FIXTURE / "recorded.json").read_text())
    names = recorded["filenames"]
    meta = json.loads((FIXTURE / names["meta"]).read_text())
    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_HOME", str(home))
    monkeypatch.setenv("NETBOX_TOKEN", "replay-token")
    cache_dir = home / "inventory-cache"
    cache_dir.mkdir(parents=True)
    for name in (names["snapshot"], names["meta"]):
        shutil.copyfile(FIXTURE / name, cache_dir / name)
    # The WHOLE compiled table, verbatim: the snapshot's filename is a digest of
    # all of it, so restating only the url would name a different snapshot.
    root = make_project(
        tmp_path / "project",
        name="legacy",
        extra=_toml_table("inventory", recorded["inventory"]),
    )
    monkeypatch.setenv("OTTOPATH", str(root))
    bootstrap._reset()
    monkeypatch.setattr("otto.inventory.cache._utc_now", lambda: _T0)
    trap = RequestTrap(monkeypatch)

    from otto.declared import get_projects
    from otto.inventory.config import build_inventory

    inventory = build_inventory(projects=get_projects())
    assert inventory is not None
    cache = snapshot_cache_of(inventory=inventory)
    assert cache is not None
    # Before any verb: a wrong slug fails here, not as a RequestTrap hit below.
    assert cache.snapshot_path.name == names["snapshot"]

    found = discover_inventory(inventory=inventory)
    assert sorted(found.records) == ["nb1", "nb2", "nb3"]
    assert found.instances is None

    out = tmp_path / "export.json"
    result = CliRunner().invoke(inventory_app, ["export", str(out)])
    assert result.exit_code == 0, result.output

    assert trap.attempts == 0, trap.urls
    assert inventory.fingerprint() == meta["sha256"]
    assert out.read_bytes() == (FIXTURE / names["snapshot"]).read_bytes()


# --------------------------------------------------------------------------- #
# The cache over a discovery-capable backend.
# --------------------------------------------------------------------------- #

_V2 = {
    "$format": INVENTORY_V2_FORMAT,
    "records": {
        "edge-17": {"ip": "10.0.0.17", "extra": {"rack": "r1"}},
        "ixia-9": {"ip": "10.0.0.9"},
    },
    "instances": {
        "rig-7": {"roles": {"dut": ["edge-17"], "tgen": ["ixia-9"]}},
        "spare": {"members": ["edge-17"]},
        "empty": {},
    },
}


class CountingDiscovery:
    """A JsonInventory behind a counter: every ``inventory_discovery`` call is one fetch."""

    def __init__(self, path: Path) -> None:
        self._inner = JsonInventory(path, supplies=["ip"])
        self.label = self._inner.label
        self.supplies = self._inner.supplies
        self.fetches = 0

    def inventory_discovery(self) -> Discovery:
        self.fetches += 1
        return self._inner.inventory_discovery()

    def lookup(self, key: str) -> InventoryRecord:
        return self._inner.lookup(key)

    def list_keys(self) -> list[str]:
        return self._inner.list_keys()

    def fingerprint(self) -> None:
        return None


def _v2_file(path: Path, doc: "dict[str, object]") -> Path:
    path.write_text(json.dumps(doc))
    return path


def _cache(tmp_path: Path, inner: CountingDiscovery, *, slug: str = "v2") -> SnapshotCache:
    return SnapshotCache(
        inner,
        ttl=timedelta(hours=24),
        cache_dir=tmp_path / "inventory-cache",
        slug_material=slug,
        clock=lambda: _T0,
    )


def test_cold_and_warm_reads_agree_on_instances(tmp_path):
    inner = CountingDiscovery(_v2_file(tmp_path / "inv.json", _V2))
    cold = _cache(tmp_path, inner).inventory_discovery()
    assert inner.fetches == 1
    assert cold.instances is not None
    assert sorted(cold.instances) == ["empty", "rig-7", "spare"]
    warm = _cache(tmp_path, inner).inventory_discovery()  # a second process, same snapshot
    assert inner.fetches == 1
    assert warm == cold


def test_a_role_swap_moves_only_the_fingerprint(tmp_path):
    swapped = json.loads(json.dumps(_V2))
    swapped["instances"]["rig-7"]["roles"] = {"dut": ["ixia-9"], "tgen": ["edge-17"]}
    a = _cache(tmp_path, CountingDiscovery(_v2_file(tmp_path / "a.json", _V2)), slug="a")
    b = _cache(tmp_path, CountingDiscovery(_v2_file(tmp_path / "b.json", swapped)), slug="b")
    left, right = a.inventory_discovery(), b.inventory_discovery()
    assert left.records == right.records
    assert left.instances != right.instances
    assert a.fingerprint() is not None
    assert a.fingerprint() != b.fingerprint()


def test_refresh_counts_records(tmp_path):
    cache = _cache(tmp_path, CountingDiscovery(_v2_file(tmp_path / "inv.json", _V2)))
    assert cache.refresh().count == 2  # two records, not the document's three sections


# --------------------------------------------------------------------------- #
# The document and the diff.
# --------------------------------------------------------------------------- #


def test_a_discovery_without_instances_is_the_records_document():
    records = {"b": InventoryRecord(ip="10.0.0.2"), "a": InventoryRecord(ip="10.0.0.1")}
    doc = discovery_to_document(discovery=Discovery(records=records))
    assert doc == records_to_document(records=records)
    assert document_hash(doc) == document_hash(records_to_document(records=records))


def test_the_v2_document_round_trips_without_creds(tmp_path):
    source = document_to_discovery(
        data={
            **_V2,
            "records": {"k": {"ip": "10.0.0.1", "creds": [{"login": "root", "password": "pw"}]}},
        },
        source="mem",
        supplies=frozenset({"ip", "creds"}),
    )
    doc = discovery_to_document(discovery=source)
    assert doc["$format"] == INVENTORY_V2_FORMAT
    assert doc["records"] == {"k": {"ip": "10.0.0.1"}}
    back = document_to_discovery(data=doc, source="mem", supplies=frozenset({"ip"}))
    assert back.instances == source.instances


def test_diff_instances_none_vs_empty_and_empty_vs_a_named_empty():
    assert [d.key for d in diff_instances(left=None, right={})] == ["(instances)"]
    assert [
        (d.key, d.field) for d in diff_instances(left={}, right={"x": InventoryInstance()})
    ] == [("x", None)]


def test_the_unsupported_row_is_told_apart_by_structure_never_by_name():
    unsupported = diff_instances(left=None, right={})
    assert [d.whole_map for d in unsupported] == [True]
    named = diff_instances(left={}, right={"(instances)": InventoryInstance()})
    assert [(d.key, d.field, d.whole_map) for d in named] == [("(instances)", None, False)]
    records = diff_records(left={"(instances)": InventoryRecord(ip="10.0.0.2")}, right={})
    assert [(d.key, d.field, d.whole_map) for d in records] == [("(instances)", None, False)]


def test_diff_instances_names_the_field_that_moved():
    left = {"rig": InventoryInstance(roles={"dut": ["a"], "tgen": ["b"]})}
    right = {"rig": InventoryInstance(roles={"dut": ["b"], "tgen": ["a"]})}
    rows = diff_instances(left=left, right=right)
    assert [(d.key, d.field) for d in rows] == [("rig", "roles")]
    assert diff_instances(left=left, right=left) == []
    assert diff_instances(left=None, right=None) == []


def test_snapshot_cache_of_peels_through_the_creds_overlay(tmp_path):
    from otto.creds.json_store import _JsonCredsStore
    from otto.inventory.creds import CredsOverlay, below_creds_overlay

    (tmp_path / "creds.json").write_text("{}")
    store = _JsonCredsStore(tmp_path / "creds.json")
    cache = _cache(tmp_path, CountingDiscovery(_v2_file(tmp_path / "inv.json", _V2)))
    wrapped = CredsOverlay(CredsOverlay(cache, store=store), store=store)
    assert below_creds_overlay(inventory=wrapped) is cache
    assert below_creds_overlay(inventory=cache) is cache
    assert snapshot_cache_of(inventory=wrapped) is cache


@pytest.mark.parametrize(
    ("records", "instances", "named"),
    [
        ({"_canary": {"ip": "10.0.0.9"}, "k": {"ip": "10.0.0.1"}}, None, "record '_canary'"),
        (
            {"_canary": {"ip": "10.0.0.9"}, "k": {"ip": "10.0.0.1"}},
            {"rig": {"members": ["k"]}},
            "record '_canary'",
        ),
        ({"k": {"ip": "10.0.0.1"}}, {"_rack": {"members": ["k"]}}, "instance '_rack'"),
    ],
)
def test_a_comment_shaped_name_is_refused_before_any_snapshot(tmp_path, records, instances, named):
    """A ``_`` name is a comment in the document, so no cold result may hold one (Astra I5).

    Main already read a ``_``-prefixed record key as a comment, losing it on
    the first warm read. Cold, warm and export now agree: the name is refused
    before a cold result or a snapshot exists.
    """
    from otto.inventory import InventoryDataError
    from tests._fixtures.named_inventory import FakeDiscoveryInventory

    inner = FakeDiscoveryInventory(records, instances=instances, supplies=["ip"])
    with pytest.raises(InventoryDataError, match=named):
        discovery_to_document(discovery=discover_inventory(inventory=inner))
    cache = SnapshotCache(
        inner,
        ttl=timedelta(hours=24),
        cache_dir=tmp_path / "inventory-cache",
        slug_material="comment",
        clock=lambda: _T0,
    )
    with pytest.raises(InventoryDataError, match=named):
        cache.inventory_discovery()
    assert not cache.snapshot_path.exists()
