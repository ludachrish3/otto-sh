"""Staleness [D2]: which inventory errors may serve an expired snapshot, and for how long.

One decision, :func:`~otto.inventory.cache.stale_refusal`: a data error never
falls back; an unreachable backend falls back only within its ``max_stale``;
a plain ``InventoryError`` (NetBox, every existing backend) falls back at any
age, as before. Each case builds a FRESH :class:`SnapshotCache` (``_ensure``
memoizes) with an injected clock.
"""

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from otto.inventory import (
    InventoryDataError,
    InventoryError,
    InventoryUnreachableError,
    parse_max_stale,
    register_inventory_backend,
)
from otto.inventory.cache import SNAPSHOT_POLICY_KEYS, SnapshotCache, snapshot_slug_material
from otto.inventory.config import compile_inventory, construct_inventory
from otto.models.base import OttoModel
from otto.models.settings import InventoryConfigSpec
from tests._fixtures.named_inventory import FakeDiscoveryInventory
from tests._fixtures.paths import TESTS_ROOT

_T0 = datetime(2026, 8, 27, 9, 14, tzinfo=timezone.utc)
_TTL = timedelta(hours=24)
_RECORDS = {"k": {"ip": "10.0.0.1"}}
_LEGACY = TESTS_ROOT / "_fixtures" / "legacy_snapshot"


def _cache(tmp_path, inner, *, age=timedelta(0), slug_material="fake", ttl=_TTL):
    """A fresh cache over *inner* whose clock reads ``_T0 + age``."""
    return SnapshotCache(
        inner,
        ttl=ttl,
        cache_dir=tmp_path / "inventory-cache",
        slug_material=slug_material,
        clock=lambda: _T0 + age,
    )


def _seed(tmp_path, **kw):
    """Write a snapshot of ``_RECORDS`` at ``_T0`` through one good fetch; return the cache."""
    cache = _cache(tmp_path, FakeDiscoveryInventory(_RECORDS, supplies=["ip"]), **kw)
    assert cache.list_keys() == ["k"]
    return cache


def _down(fail, *, max_stale=None):
    return FakeDiscoveryInventory(_RECORDS, supplies=["ip"], fail=fail, max_stale=max_stale)


def test_a_data_error_never_falls_back(tmp_path):
    _seed(tmp_path)
    inner = _down("data")
    cache = _cache(tmp_path, inner, age=timedelta(hours=25))
    with pytest.raises(InventoryDataError) as e:
        cache.list_keys()
    assert inner.fetches == 1
    assert e.value.__cause__ is inner.raised
    assert e.value is not inner.raised
    assert "fake:disc" in str(e.value)
    assert "the cached snapshot (from 2026-08-27 09:14 UTC, 25h old) is not served" in str(e.value)
    assert cache.stale_notice is None


def test_unreachable_within_max_stale_serves_and_says_so(tmp_path):
    _seed(tmp_path)
    cache = _cache(tmp_path, _down("unreachable", max_stale="48h"), age=timedelta(hours=30))
    assert cache.lookup("k").ip == "10.0.0.1"
    assert cache.stale_notice is not None
    assert "30h old" in cache.stale_notice


def test_unreachable_past_max_stale_is_refused(tmp_path):
    _seed(tmp_path)
    inner = _down("unreachable", max_stale="48h")
    cache = _cache(tmp_path, inner, age=timedelta(hours=49))
    with pytest.raises(InventoryUnreachableError) as e:
        cache.lookup("k")
    assert "2d 1h old" in str(e.value)
    assert "because 2d 1h is past the max_stale of 2d 0h in its inventory table" in str(e.value)
    assert e.value.max_stale == timedelta(hours=48)
    assert e.value.__cause__ is inner.raised
    assert cache.stale_notice is None


def test_unreachable_without_max_stale_is_refused(tmp_path):
    _seed(tmp_path)
    inner = _down("unreachable")
    cache = _cache(tmp_path, inner, age=timedelta(hours=25))
    with pytest.raises(InventoryUnreachableError) as e:
        cache.list_keys()
    assert e.value.max_stale is None
    assert e.value.__cause__ is inner.raised
    assert "because no max_stale is set in its inventory table" in str(e.value)


def test_the_boundary_is_inclusive(tmp_path):
    _seed(tmp_path)
    cache = _cache(tmp_path, _down("unreachable", max_stale="48h"), age=timedelta(hours=48))
    assert cache.list_keys() == ["k"]
    assert cache.stale_notice is not None
    assert "2d 0h old" in cache.stale_notice


class _SlowDown(FakeDiscoveryInventory):
    """Unreachable after *timeout*: the clock advances DURING the failed fetch."""

    def __init__(self, *args, now: list[datetime], timeout: timedelta, **kw) -> None:
        super().__init__(*args, **kw)
        self._now = now
        self._timeout = timeout

    def _fetch(self) -> None:
        self._now[0] += self._timeout
        super()._fetch()


def test_the_age_is_measured_after_the_failed_fetch(tmp_path):
    """119m old when asked, 122m old when the fetch gives up: past a 120m max_stale (Astra I3)."""
    _seed(tmp_path, ttl=timedelta(seconds=60))
    now = [_T0 + timedelta(minutes=119)]
    inner = _SlowDown(
        _RECORDS,
        supplies=["ip"],
        fail="unreachable",
        max_stale="120m",
        now=now,
        timeout=timedelta(minutes=3),
    )
    cache = SnapshotCache(
        inner,
        ttl=timedelta(seconds=60),
        cache_dir=tmp_path / "inventory-cache",
        slug_material="fake",
        clock=lambda: now[0],
    )
    with pytest.raises(InventoryUnreachableError) as e:
        cache.lookup("k")
    assert "is past the max_stale" in str(e.value)
    assert inner.fetches == 1
    assert cache.stale_notice is None


def test_the_stale_notice_states_the_age_after_the_failed_fetch(tmp_path):
    """The notice and the decision read one age, taken when the fetch failed."""
    _seed(tmp_path, ttl=timedelta(seconds=60))
    now = [_T0 + timedelta(minutes=119)]
    inner = _SlowDown(
        _RECORDS,
        supplies=["ip"],
        fail="unreachable",
        max_stale="3h",
        now=now,
        timeout=timedelta(minutes=3),
    )
    cache = SnapshotCache(
        inner,
        ttl=timedelta(seconds=60),
        cache_dir=tmp_path / "inventory-cache",
        slug_material="fake",
        clock=lambda: now[0],
    )
    assert cache.lookup("k").ip == "10.0.0.1"
    assert cache.stale_notice is not None
    assert "2h old" in cache.stale_notice


class _PlainDown(FakeDiscoveryInventory):
    """Fails as NetBox does today: a plain ``InventoryError``."""

    def _fetch(self) -> None:
        self.fetches += 1
        if self.fail is not None:
            raise InventoryError("connection refused")


def test_a_plain_inventory_error_still_falls_back_at_any_age(tmp_path):
    _seed(tmp_path)
    inner = _PlainDown(_RECORDS, supplies=["ip"], fail="unreachable")
    cache = _cache(tmp_path, inner, age=timedelta(days=30))
    assert cache.lookup("k").ip == "10.0.0.1"
    assert inner.fetches == 1
    assert cache.stale_notice is not None
    assert "30d 0h old" in cache.stale_notice


class _DiscConfig(OttoModel, frozen=True):
    """A backend table with the one policy key: ``max_stale``."""

    max_stale: str | None = None


def _register_disc(*, built: list) -> None:
    """Register ``fake:disc``; each build appends the fresh inventory to *built*."""

    def factory(c):
        inventory = FakeDiscoveryInventory(_RECORDS, supplies=["ip"], max_stale=c.config.max_stale)
        built.append(inventory)
        return inventory

    register_inventory_backend("fake:disc", config=_DiscConfig, factory=factory)


def _compiled(table, tmp_path):
    return compile_inventory(
        cfg=InventoryConfigSpec.model_validate({"backend": "fake:disc", **table}),
        anchor_dir=tmp_path,
        origin="o",
    )


def _slug(compiled):
    return snapshot_slug_material(
        compiled.prepared.backend,
        "fake:disc",
        compiled.prepared.normalized,
        cache_ttl=compiled.cache_ttl,
    )


def test_raising_max_stale_keeps_the_snapshot(tmp_path, monkeypatch):
    built: list[FakeDiscoveryInventory] = []
    _register_disc(built=built)
    strict = _compiled({"max_stale": "1h"}, tmp_path)
    first_inner = construct_inventory(compiled=dataclasses.replace(strict, cache_ttl=timedelta(0)))
    first = _cache(tmp_path, first_inner, slug_material=_slug(strict))
    first_records = first.inventory_discovery().records
    assert first_inner.fetches == 1
    # The outage: the operator raises max_stale to ride it out.
    loose = _compiled({"max_stale": "72h"}, tmp_path)
    assert not loose.same_as(other=strict)  # policy still distinguishes two declarations
    rebuilt = construct_inventory(compiled=dataclasses.replace(loose, cache_ttl=timedelta(0)))
    assert rebuilt is built[-1]
    assert rebuilt.max_stale == timedelta(hours=72)
    rebuilt.fail = "unreachable"
    cache = _cache(tmp_path, rebuilt, slug_material=_slug(loose), age=timedelta(hours=30))
    served = cache.inventory_discovery()
    assert rebuilt.fetches == 1  # one refresh attempt, which failed
    assert served.records == first_records
    assert cache.stale_notice is not None
    assert "30h old" in cache.stale_notice
    assert cache.snapshot_path == first.snapshot_path
    # The production path, no fetch and no clock: construction does no I/O.
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    strict_cache = construct_inventory(compiled=strict)
    loose_cache = construct_inventory(compiled=loose)
    assert isinstance(strict_cache, SnapshotCache)
    assert isinstance(loose_cache, SnapshotCache)
    assert strict_cache.snapshot_path == loose_cache.snapshot_path


def test_a_config_without_max_stale_keeps_todays_slug(tmp_path, monkeypatch):
    recorded = json.loads((_LEGACY / "recorded.json").read_text())
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    inventory = construct_inventory(
        compiled=compile_inventory(
            cfg=InventoryConfigSpec.model_validate(recorded["inventory"]),
            anchor_dir=tmp_path,
            origin="o",
        )
    )
    assert isinstance(inventory, SnapshotCache)
    assert inventory.snapshot_path.name == recorded["filenames"]["snapshot"]
    # A policy key never reaches the slug, at any value, None included.
    assert frozenset({"max_stale"}) == SNAPSHOT_POLICY_KEYS
    bare = snapshot_slug_material("x", "x:y", {"url": "u"}, cache_ttl=_TTL)
    for value in (None, "1h", "72h"):
        normalized = {"url": "u", "max_stale": value}
        assert snapshot_slug_material("x", "x:y", normalized, cache_ttl=_TTL) == bare


@pytest.mark.parametrize("fail", ["data", "unreachable"])
@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_corrupt_and_missing_snapshots_raise_the_original(tmp_path, fail, damage):
    seeded = _seed(tmp_path)
    if damage == "corrupt":
        seeded.snapshot_path.write_text("{ not json")
    else:
        seeded.snapshot_path.unlink()
    inner = _down(fail, max_stale="72h")
    cache = _cache(tmp_path, inner, age=timedelta(hours=25))
    with pytest.raises(InventoryError) as e:
        cache.list_keys()
    assert e.value is inner.raised


@pytest.mark.parametrize("fail", ["data", "unreachable"])
def test_ttl_zero_has_no_snapshot(tmp_path, monkeypatch, fail):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    built: list[FakeDiscoveryInventory] = []
    _register_disc(built=built)
    inventory = construct_inventory(
        compiled=_compiled({"cache_ttl": "0", "max_stale": "72h"}, tmp_path)
    )
    assert inventory is built[-1]  # never wrapped
    inventory.fail = fail
    with pytest.raises(InventoryError) as e:
        inventory.list_keys()
    assert e.value is inventory.raised
    assert not (tmp_path / "home" / "inventory-cache").exists()


def test_parse_max_stale_names_its_setting():
    assert parse_max_stale(text=None) is None
    assert parse_max_stale(text="48h") == timedelta(hours=48)
    with pytest.raises(ValueError, match=r"^max_stale must be '0' or <n>m / <n>h / <n>d"):
        parse_max_stale(text="2 days")
    with pytest.raises(ValueError, match=r"^max_stale '1000000000d' is out of range"):
        parse_max_stale(text="1000000000d")


@pytest.mark.parametrize("fail", ["data", "unreachable", "stale"])
def test_a_named_inventorys_remedy_refreshes_that_inventory(tmp_path, monkeypatch, fail):
    """The notice and both refusals name ``--inventory physical`` for a named snapshot (F13)."""
    from otto.models.settings import NamedInventorySpec

    built: list[FakeDiscoveryInventory] = []
    _register_disc(built=built)
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    compiled = compile_inventory(
        cfg=NamedInventorySpec.model_validate(
            {"name": "physical", "backend": "fake:disc", "max_stale": "48h"}
        ),
        anchor_dir=tmp_path,
        origin="o",
    )
    assert compiled.name == "physical"
    named = construct_inventory(compiled=compiled)
    assert isinstance(named, SnapshotCache)
    assert named.list_keys() == ["k"]  # seeds the snapshot
    later = construct_inventory(compiled=dataclasses.replace(compiled))
    assert isinstance(later, SnapshotCache)
    later._ttl = timedelta(0)  # expire the seeded snapshot without a clock
    later.inner.fail = "data" if fail == "data" else "unreachable"  # type: ignore[attr-defined]
    if fail == "unreachable":
        later.inner.max_stale = None  # type: ignore[attr-defined]
    remedy = "`otto inventory refresh --inventory physical`"
    if fail == "stale":
        assert later.list_keys() == ["k"]
        assert later.stale_notice is not None
        assert remedy in later.stale_notice
    else:
        with pytest.raises(InventoryError) as e:
            later.list_keys()
        assert remedy in str(e.value)


def test_the_default_inventorys_remedy_is_unchanged(tmp_path):
    _seed(tmp_path)
    cache = _cache(tmp_path, _down("unreachable"), age=timedelta(hours=25))
    with pytest.raises(InventoryUnreachableError) as e:
        cache.list_keys()
    assert "run `otto inventory refresh` once the inventory is reachable" in str(e.value)
