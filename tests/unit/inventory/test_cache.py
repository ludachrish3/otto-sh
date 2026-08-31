"""SnapshotCache: fresh → no fetch; stale → refetch; down → snapshot + warning (spec §9.5)."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest

from otto.inventory import (
    CredsOverlay,
    Inventory,
    InventoryError,
    JsonInventory,
    NetBoxInventory,
)
from otto.inventory import cache as cache_module
from otto.inventory.cache import SnapshotCache, format_age, reset_stale_warnings
from otto.inventory.config import CompiledInventory, construct_inventory
from otto.testing import assert_inventory_conforms

from .netbox_stub import TOKEN, NetBoxStub, device
from .test_resolve import FakeInventory

_T0 = datetime(2026, 8, 27, 9, 14, tzinfo=timezone.utc)
_LOGGER = "otto.inventory.cache"
_STALE = "using cached snapshot"


class Counting(FakeInventory):
    """A FakeInventory that counts fetches and can be taken offline."""

    def __init__(self, records, **kw):
        super().__init__(records, **kw)
        self.fetches = 0
        self.down = False

    def list_keys(self):
        if self.down:
            raise InventoryError("connection refused")
        self.fetches += 1
        return super().list_keys()

    def fingerprint(self):
        return None


def _cache(tmp_path, inner, *, ttl=timedelta(hours=24), now=_T0):
    clock = {"now": now}
    cache = SnapshotCache(
        inner,
        ttl=ttl,
        cache_dir=tmp_path / "inventory-cache",
        slug_material="fake",
        clock=lambda: clock["now"],
    )
    return cache, clock


def test_construction_touches_neither_the_backend_nor_the_disk(tmp_path):
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    assert inner.fetches == 0
    assert not (tmp_path / "inventory-cache").exists()
    assert cache.fingerprint() is None  # no snapshot yet — and asking made none
    assert inner.fetches == 0


def test_first_use_fetches_and_writes_a_snapshot_the_next_process_reads(tmp_path):
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    assert cache.lookup("k").ip == "10.0.0.1"
    assert inner.fetches == 1
    assert cache.snapshot_path.is_file()
    assert cache.meta_path.is_file()
    meta = json.loads(cache.meta_path.read_text())
    assert meta["fetched_at"] == _T0.isoformat()
    assert meta["sha256"] == cache.fingerprint()
    second, _ = _cache(tmp_path, inner, now=_T0 + timedelta(hours=1))  # new process, still fresh
    assert second.list_keys() == ["k"]
    assert inner.fetches == 1
    assert second.fingerprint() == cache.fingerprint()


def test_a_resolved_cache_keeps_answering_after_its_files_vanish(tmp_path):
    """``~/.otto`` may be swept at any moment; a process that already resolved must not care."""
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    assert cache.lookup("k").ip == "10.0.0.1"
    cache.snapshot_path.unlink()
    cache.meta_path.unlink()
    inner.down = True
    assert cache.list_keys() == ["k"]
    assert inner.fetches == 1


def test_an_unknown_key_raises_inventory_key_error_naming_the_inner_label(tmp_path):
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    with pytest.raises(
        InventoryError, match=r"inventory key 'nope' not found in inventory 'fake:mem'"
    ):
        cache.lookup("nope")


def test_fingerprint_reads_the_meta_file_and_never_fetches(tmp_path):
    """Completion calls this on EVERY otto command — a fetch here puts NetBox on that path."""
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    _cache(tmp_path, inner)[0].list_keys()
    later, _ = _cache(tmp_path, inner, now=_T0 + timedelta(days=9))  # far beyond the TTL
    assert later.fingerprint() == json.loads(later.meta_path.read_text())["sha256"]
    assert inner.fetches == 1


def test_stale_snapshot_is_refetched_silently(tmp_path, caplog):
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    _cache(tmp_path, inner)[0].list_keys()
    inner._records["k"] = inner._records["k"].model_copy(update={"ip": "10.0.0.2"})
    later, _ = _cache(tmp_path, inner, now=_T0 + timedelta(hours=25))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert later.lookup("k").ip == "10.0.0.2"
    assert inner.fetches == 2
    assert not caplog.records


def test_a_corrupt_snapshot_is_refetched_rather_than_raising(tmp_path):
    """Everything under ``otto_home()`` is derived state otto rebuilds without complaint."""
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    cache.list_keys()
    cache.snapshot_path.write_text("{ not json")
    fresh, _ = _cache(tmp_path, inner, now=_T0 + timedelta(minutes=5))  # well inside the TTL
    assert fresh.list_keys() == ["k"]
    assert inner.fetches == 2


def test_unreachable_serves_the_snapshot_with_an_age_warning(tmp_path, caplog):
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    _cache(tmp_path, inner)[0].list_keys()
    inner.down = True
    later, _ = _cache(tmp_path, inner, now=_T0 + timedelta(hours=31))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert later.lookup("k").ip == "10.0.0.1"
    (msg,) = [r.getMessage() for r in caplog.records]
    assert (
        "inventory 'fake:mem' unreachable (connection refused): using cached snapshot from "
        "2026-08-27 09:14 UTC, 31h old" in msg
    )
    assert "otto inventory refresh" in msg


def test_the_notice_is_readable_state_and_a_refresh_clears_it(tmp_path):
    """``stale_notice`` is the same sentence, READABLE — for a caller that must report it.

    ``otto inventory`` is that caller: ``_warn_stale`` logs once per snapshot
    per PROCESS, and the resolution that spends the warning can be ``entry()``'s
    completion-cache write — before the root callback installs a console
    handler — so the verbs would serve a stale snapshot in silence.

    Clearing it on a successful fetch is the half no CLI verb can exercise
    (each invocation builds a fresh cache), and it is exactly the half that
    matters to a LONG-LIVED holder of one — a process that saw the outage, then
    called :meth:`~otto.inventory.cache.SnapshotCache.refresh` once the
    inventory came back, would otherwise go on reporting an outage it has
    already cleared.
    """
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    _cache(tmp_path, inner)[0].list_keys()
    inner.down = True
    later, _ = _cache(tmp_path, inner, now=_T0 + timedelta(hours=31))
    assert later.stale_notice is None  # nothing resolved yet, nothing to report
    later.list_keys()
    assert later.stale_notice is not None
    assert "unreachable (connection refused)" in later.stale_notice
    assert "31h old" in later.stale_notice
    assert "otto inventory refresh" in later.stale_notice

    inner.down = False
    later.refresh()
    assert later.stale_notice is None


def test_the_stale_warning_is_emitted_once_per_process(tmp_path, caplog):
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    _cache(tmp_path, inner)[0].list_keys()
    inner.down = True
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        for _ in range(3):
            later, _ = _cache(tmp_path, inner, now=_T0 + timedelta(hours=31))
            assert later.lookup("k").ip == "10.0.0.1"
    assert len([r for r in caplog.records if _STALE in r.getMessage()]) == 1


def test_reset_stale_warnings_re_arms_the_once_per_process_memo(tmp_path, caplog):
    """The seam the root conftest's autouse fixture uses to isolate one test from the next."""
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    _cache(tmp_path, inner)[0].list_keys()
    inner.down = True
    stale = _T0 + timedelta(hours=31)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _cache(tmp_path, inner, now=stale)[0].list_keys()
        _cache(tmp_path, inner, now=stale)[0].list_keys()
        assert len([r for r in caplog.records if _STALE in r.getMessage()]) == 1
        reset_stale_warnings()
        _cache(tmp_path, inner, now=stale)[0].list_keys()
    assert len([r for r in caplog.records if _STALE in r.getMessage()]) == 2


def test_a_successful_refresh_re_arms_the_warning(tmp_path, caplog):
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, clock = _cache(tmp_path, inner)
    cache.list_keys()
    inner.down = True
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _cache(tmp_path, inner, now=_T0 + timedelta(hours=31))[0].list_keys()
    assert [r for r in caplog.records if _STALE in r.getMessage()]
    caplog.clear()
    inner.down = False
    clock["now"] = _T0 + timedelta(hours=31)
    cache.refresh()
    inner.down = True
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _cache(tmp_path, inner, now=_T0 + timedelta(hours=80))[0].list_keys()
    assert [r for r in caplog.records if _STALE in r.getMessage()]


def test_unreachable_without_a_snapshot_raises(tmp_path):
    inner = Counting({}, supplies=["ip"])
    inner.down = True
    with pytest.raises(InventoryError, match="connection refused"):
        _cache(tmp_path, inner)[0].list_keys()


def test_refresh_forces_a_fetch_and_reports_the_previous_age(tmp_path):
    inner = Counting({"k": {"ip": "10.0.0.1"}, "j": {"ip": "10.0.0.3"}}, supplies=["ip"])
    cache, clock = _cache(tmp_path, inner)
    cache.list_keys()
    clock["now"] = _T0 + timedelta(hours=2)
    result = cache.refresh()
    assert result.previous_age == timedelta(hours=2)
    assert result.count == 2
    assert inner.fetches == 2
    # A separate directory: a cache that has never written a snapshot has no
    # previous age to report, and must not read this one's.
    fresh, _ = _cache(tmp_path / "elsewhere", Counting({}, supplies=["ip"]))
    assert fresh.refresh().previous_age is None


def test_format_age_is_minutes_then_hours_then_days():
    """R3: under 48h the age is spelled in HOURS — the unit ``cache_ttl`` is written in."""
    assert format_age(timedelta(minutes=45)) == "45m"
    assert format_age(timedelta(seconds=30)) == "0m"
    assert format_age(timedelta(hours=1, minutes=30)) == "1h"
    assert format_age(timedelta(hours=31)) == "31h"
    assert format_age(timedelta(hours=47, minutes=59)) == "47h"
    assert format_age(timedelta(hours=48)) == "2d 0h"
    assert format_age(timedelta(days=3, hours=7)) == "3d 7h"


def test_the_cache_conforms_to_the_inventory_protocol(tmp_path):
    inner = Counting({"k": {"ip": "10.0.0.1"}, "j": {"ip": "10.0.0.3"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    cache.list_keys()  # a first fetch, so there is a snapshot to conform from
    assert isinstance(cache, Inventory)
    assert_inventory_conforms(cache, expected_keys=["j", "k"])


def test_the_ttl_boundary_is_exclusive(tmp_path):
    """Aged EXACTLY the TTL is expired; a second short of it is still served."""
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    ttl = timedelta(hours=24)
    _cache(tmp_path, inner, ttl=ttl)[0].list_keys()
    assert inner.fetches == 1
    inside, _ = _cache(tmp_path, inner, ttl=ttl, now=_T0 + ttl - timedelta(seconds=1))
    assert inside.list_keys() == ["k"]
    assert inner.fetches == 1
    at_the_bound, _ = _cache(tmp_path, inner, ttl=ttl, now=_T0 + ttl)
    assert at_the_bound.list_keys() == ["k"]
    assert inner.fetches == 2


def test_fingerprint_refuses_a_meta_that_does_not_describe_the_snapshot(tmp_path):
    """A torn pair must not be reported as fresh for a whole TTL (§11 keys on this)."""
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    first, _ = _cache(tmp_path, inner)
    first.list_keys()
    original = first.fingerprint()
    assert original is not None
    # The snapshot replaced without its meta: a crash between the two writes,
    # two processes interleaving their renames, or an operator hand-copying a
    # stage-1 document in - which the format positively invites.
    torn, _ = _cache(tmp_path, inner, now=_T0 + timedelta(minutes=1))
    torn.snapshot_path.write_text(json.dumps({"j": {"ip": "10.0.0.8"}, "k": {"ip": "10.0.0.9"}}))
    assert torn.fingerprint() is None  # honest: completion treats it as uncacheable
    assert torn.list_keys() == ["j", "k"]  # ...and the resolution repairs the meta
    assert inner.fetches == 1  # the snapshot was usable; only the meta was wrong
    repaired, _ = _cache(tmp_path, inner, now=_T0 + timedelta(minutes=2))
    assert repaired.fingerprint() not in (None, original)


def test_fingerprint_spots_a_replacement_the_file_stats_cannot_see(tmp_path):
    """Same size AND same mtime — which is not exotic.

    Measured on this project's dev filesystem: 46 of 50 back-to-back same-size
    rewrites shared an ``st_mtime_ns``. A ``(size, mtime)`` pair in the meta
    would call this snapshot unchanged and hand completion a digest for content
    that is no longer there.
    """
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    cache.list_keys()
    before = cache.snapshot_path.stat()
    original = cache.snapshot_path.read_text()

    swapped, _ = _cache(tmp_path, inner, now=_T0 + timedelta(minutes=1))
    swapped.snapshot_path.write_text(original.replace("10.0.0.1", "10.0.0.2"))
    os.utime(swapped.snapshot_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = swapped.snapshot_path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert swapped.fingerprint() is None


def test_the_metas_two_digests_come_from_one_read(tmp_path, monkeypatch):
    """A rename landing between the meta's two digests must not produce a meta that lies.

    Computing ``content_sha256`` by re-reading the snapshot pairs it with a
    ``sha256`` taken from DIFFERENT bytes: ``describes()`` then passes and
    ``fingerprint()`` hands the completion cache a digest for records that are
    not on disk, for a whole TTL.
    """
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    swapped = []
    real_digest = cache_module.content_digest

    def swapping(path):
        # Stand in for a concurrent writer's rename, landing at the first
        # moment anything hashes the snapshot by path.
        if not swapped:
            swapped.append(True)
            path.write_text(json.dumps({"j": {"ip": "10.0.0.8"}}))
        return real_digest(path)

    monkeypatch.setattr(cache_module, "content_digest", swapping)
    cache.list_keys()
    fresh, _ = _cache(tmp_path, inner, now=_T0 + timedelta(minutes=1))
    assert fresh.fingerprint() is None
    assert swapped  # the swap really did land, so the assertion above is not vacuous


def test_a_stale_snapshot_does_not_pay_for_a_meta_repair(tmp_path):
    """Repairing a meta otto is about to replace wholesale is work nothing reads."""
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    cache.list_keys()
    meta = json.loads(cache.meta_path.read_text())
    cache.meta_path.write_text(json.dumps({**meta, "content_sha256": "not-the-digest"}))
    before = cache.meta_path.read_bytes()
    inner.down = True  # so the refetch fails and the stale snapshot is served
    later, _ = _cache(tmp_path, inner, now=_T0 + timedelta(hours=31))
    assert later.list_keys() == ["k"]
    assert cache.meta_path.read_bytes() == before


@pytest.fixture
def read_only(tmp_path):
    """Take the write permission off a directory for one test, and give it back after.

    A callable rather than a plain directory so a test can POPULATE the cache
    first — the meta-repair case below needs a snapshot on disk before the
    write is taken away. Restored in the teardown whatever the test did, or
    ``tmp_path`` cleanup cannot remove the tree.

    Skipped under root, which ignores the mode bits entirely: the write would
    succeed and every assertion in these three tests would be vacuous.
    """
    locked: "list[object]" = []

    def _lock(directory):
        if os.geteuid() == 0:
            pytest.skip("root ignores directory modes; this guard needs an unwritable directory")
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o500)
        locked.append(directory)
        return directory

    try:
        yield _lock
    finally:
        for directory in locked:
            directory.chmod(0o700)


def test_a_snapshot_it_cannot_write_warns_and_still_serves_the_fetched_records(
    tmp_path, read_only, caplog
):
    """Everything under otto's home is derived state (§9.5) — a failed CACHE write is not a failure.

    The backend just answered. Letting the ``OSError`` out means the lab load
    fails ("Lab file …: PermissionError: …") and every ``otto inventory`` verb
    tracebacks, over a directory otto only ever uses to go faster.
    """
    read_only(tmp_path / "inventory-cache")
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert cache.lookup("k").ip == "10.0.0.1"
    assert inner.fetches == 1
    assert not cache.snapshot_path.exists()
    (msg,) = [r.getMessage() for r in caplog.records]
    assert "could not write inventory snapshot" in msg
    assert str(cache.snapshot_path) in msg
    assert "serving the fetched records without caching" in msg
    # This process answers with the digest of the records it is actually
    # serving; a process that finds nothing on disk gets None. Both are true
    # statements about what the asker would get.
    assert cache.fingerprint() is not None
    assert _cache(tmp_path, inner)[0].fingerprint() is None


def test_a_write_failure_warns_once_per_process_not_once_per_cache_object(
    tmp_path, read_only, caplog
):
    """Two ``SnapshotCache`` objects over the same unwritable dir warn ONCE between them.

    A real otto command builds more than one of these over the same
    configuration — bootstrap's resolution, the completion-cache writer's
    second one — so deduping per object would still print "could not write"
    twice for a single invocation. The stale-snapshot warning nine lines up
    already dedupes through the module-global ``_warned_snapshots``; this
    warning must share that promise (and that reset).
    """
    read_only(tmp_path / "inventory-cache")
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    first, _ = _cache(tmp_path, inner)
    second, _ = _cache(tmp_path, inner)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert first.lookup("k").ip == "10.0.0.1"
        assert second.lookup("k").ip == "10.0.0.1"
    messages = [r.getMessage() for r in caplog.records if "could not write" in r.getMessage()]
    assert len(messages) == 1


def test_refresh_fails_loudly_when_it_cannot_write_the_snapshot(tmp_path, read_only):
    """``otto inventory refresh`` asked for the write; a warning it cannot see is not an answer."""
    read_only(tmp_path / "inventory-cache")
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    with pytest.raises(InventoryError, match="could not write inventory snapshot"):
        cache.refresh()


def test_a_meta_repair_it_cannot_write_still_serves_the_snapshot(tmp_path, read_only):
    """The meta repair is best-effort: a READ must not fail because a write could not.

    The pair below is torn (the meta no longer describes the snapshot beside
    it) and inside the TTL — the one state ``_stored`` repairs on the next
    resolution. That repair sits OUTSIDE the fetch, so an unwritable directory
    would otherwise fail a load that had already read its records.
    """
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    cache.list_keys()
    meta = json.loads(cache.meta_path.read_text())
    cache.meta_path.write_text(json.dumps({**meta, "content_sha256": "not-the-digest"}))
    read_only(tmp_path / "inventory-cache")

    later, _ = _cache(tmp_path, inner, now=_T0 + timedelta(minutes=5))
    assert later.list_keys() == ["k"]
    assert inner.fetches == 1  # served from the snapshot; only the repair failed


def test_a_meta_with_a_naive_timestamp_reads_as_no_snapshot(tmp_path):
    """``fromisoformat`` accepts a naive timestamp — and ``now - fetched_at`` then raises.

    That subtraction sits OUTSIDE ``_stored``'s guard, so a hand-edited meta
    (the format invites hand-editing: it sits beside a stage-1 document
    operators are told they may copy around) would fail EVERY load until
    somebody deleted the file, against the rule that derived state under
    otto's home rebuilds without complaint.
    """
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    cache.list_keys()
    meta = json.loads(cache.meta_path.read_text())
    naive = datetime.fromisoformat(meta["fetched_at"]).replace(tzinfo=None)
    cache.meta_path.write_text(json.dumps({**meta, "fetched_at": naive.isoformat()}))

    later, _ = _cache(tmp_path, inner, now=_T0 + timedelta(minutes=5))
    assert later.list_keys() == ["k"]  # one refetch, not a crash
    assert inner.fetches == 2
    assert later.fingerprint() is not None  # ...and both files are repaired


def test_an_unusable_snapshot_says_so_at_debug(tmp_path, caplog):
    inner = Counting({"k": {"ip": "10.0.0.1"}}, supplies=["ip"])
    cache, _ = _cache(tmp_path, inner)
    cache.list_keys()
    cache.snapshot_path.write_text("{ not json")
    later, _ = _cache(tmp_path, inner, now=_T0 + timedelta(minutes=5))
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        later.list_keys()
    assert any("unusable snapshot" in r.getMessage() for r in caplog.records)


_HOUR = timedelta(hours=1)


def _json_kwargs(tmp_path):
    return {"path": tmp_path / "i.json", "supplies": None}


def _compiled(backend, kwargs, ttl, tmp_path):
    return CompiledInventory(
        backend=backend,
        kwargs=kwargs,
        creds_file=None,
        cache_ttl=ttl,
        anchor_dir=tmp_path,
        origin="o",
    )


def test_construct_wraps_netbox_but_never_json_and_ttl_zero_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
    (tmp_path / "i.json").write_text("{}")
    json_inv = construct_inventory(
        _compiled("json", _json_kwargs(tmp_path), timedelta(hours=24), tmp_path)
    )
    assert isinstance(json_inv, JsonInventory)
    nb = construct_inventory(
        _compiled("netbox", {"url": "http://127.0.0.1:9"}, timedelta(hours=24), tmp_path)
    )
    assert isinstance(nb, SnapshotCache)
    assert nb.snapshot_path.parent == tmp_path / "home" / "inventory-cache"
    assert isinstance(
        construct_inventory(
            _compiled("netbox", {"url": "http://127.0.0.1:9"}, timedelta(0), tmp_path)
        ),
        NetBoxInventory,
    )
    creds = tmp_path / "c.json"
    creds.write_text("{}")
    wrapped = construct_inventory(
        CompiledInventory(
            backend="netbox",
            kwargs={"url": "http://127.0.0.1:9"},
            creds_file=creds,
            cache_ttl=timedelta(hours=1),
            anchor_dir=tmp_path,
            origin="o",
        )
    )
    assert isinstance(wrapped, CredsOverlay)  # the overlay stays OUTERMOST
    assert isinstance(wrapped.inner, SnapshotCache)


def test_construction_never_probes_the_json_backend(tmp_path, monkeypatch):
    """The ``json`` short-circuit is what keeps construction I/O-free, not decoration."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    (tmp_path / "i.json").write_text("{}")

    def _boom(self):
        raise AssertionError("construct_inventory must not probe the json backend")

    monkeypatch.setattr(JsonInventory, "fingerprint", _boom)
    built = construct_inventory(
        _compiled("json", _json_kwargs(tmp_path), timedelta(hours=24), tmp_path)
    )
    assert isinstance(built, JsonInventory)


def test_two_configurations_never_share_a_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
    a = construct_inventory(
        _compiled("netbox", {"url": "http://a", "filter": {"site": "x"}}, _HOUR, tmp_path)
    )
    b = construct_inventory(
        _compiled("netbox", {"url": "http://a", "filter": {"site": "y"}}, _HOUR, tmp_path)
    )
    assert a.snapshot_path != b.snapshot_path


def test_two_spellings_of_one_url_share_a_snapshot(tmp_path, monkeypatch):
    """The slug hashes the backend's NORMALISED label, not the raw settings kwarg."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
    a = construct_inventory(_compiled("netbox", {"url": "http://a"}, timedelta(hours=1), tmp_path))
    b = construct_inventory(_compiled("netbox", {"url": "http://a/"}, timedelta(hours=1), tmp_path))
    assert a.snapshot_path == b.snapshot_path


def test_end_to_end_against_the_stub_one_round_per_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
    with NetBoxStub([device(1, "d1"), device(2, "d2"), device(3, "d3")], page_size=2) as stub:
        first = construct_inventory(
            _compiled("netbox", {"url": stub.base}, timedelta(hours=24), tmp_path)
        )
        assert first.list_keys() == ["d1", "d2", "d3"]
        rounds = len(stub.queries)
        assert rounds > 0
        second = construct_inventory(
            _compiled("netbox", {"url": stub.base}, timedelta(hours=24), tmp_path)
        )
        assert second.lookup("d2").ip == "10.0.0.1"
        assert len(stub.queries) == rounds  # not one request
        assert second.fingerprint() == first.fingerprint()
