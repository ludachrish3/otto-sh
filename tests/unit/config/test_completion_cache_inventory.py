"""The completion digest tracks the process inventory, and the enumeration uses it (spec §11).

``OTTO_HOME`` is relocated INSIDE every test: ``build_inventory`` falls back
to ``~/.otto/settings.toml``, so a dev machine that declares an inventory
there would otherwise steer these digests. ``tests/conftest.py`` strips
``OTTO_``-prefixed variables at import time, which is why the relocation
cannot live at module scope.
"""

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import otto.config.completion_cache as cc
from otto.inventory import register_inventory_backend
from otto.inventory.registry import INVENTORY_BACKENDS
from otto.models.inventory import InventoryRecord
from tests._fixtures.labdata import json_lab_sources, write_lab_json
from tests._fixtures.sutrepo import touch_settings

_REFERENCED = {"inventory": "dut-1", "element": "dut", "creds": [{"login": "u", "password": "p"}]}
_RECORD = {"dut-1": {"ip": "10.0.0.1"}}


def _repo(tmp_path: Path, inventory_settings: dict, *, hosts: list[dict] | None = None):
    """A Repo stand-in with one json lab source and the given ``[inventory]`` table."""
    sut = tmp_path / "sut"
    lab = sut / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    touch_settings(sut)
    write_lab_json(lab / "lab.json", hosts if hosts is not None else [], declare_labs=True)
    return SimpleNamespace(
        sut_dir=sut,
        init=[],
        libs=[],
        tests=[],
        lab_sources=json_lab_sources(sut, [lab]),
        inventory_settings=dict(inventory_settings),
    )


def _with_inventory(repo, inventory_settings: dict):
    """The SAME repo — same files, same mtimes — carrying a different ``[inventory]``.

    Calling ``_repo`` twice would rewrite ``settings.toml`` and ``lab.json``,
    and :func:`_hash_file` reads ``st_mtime_ns``: the two digests would then
    differ by TIMING rather than by the inventory, so the comparison would
    pass with the inventory term deleted whenever the two writes landed in
    different clock ticks. (Observed: mutating the term out left this file at
    "3 failed, 1 passed" once and "4 failed" on the reruns.) Varying ONLY the
    settings table makes every other fingerprint input byte-identical, which
    is what the comparison claims.
    """
    return SimpleNamespace(**{**vars(repo), "inventory_settings": dict(inventory_settings)})


def _json_inventory(tmp_path: Path, records: dict) -> dict:
    """Write ``sut/inventory.json`` and return the ``[inventory]`` table naming it.

    Not a fingerprint input in its own right — only lab files are stat'd — so
    writing it does not move the digest by itself. It reaches the digest only
    through the inventory's own ``fingerprint()``, which is the term under
    test.
    """
    (tmp_path / "sut").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sut" / "inventory.json").write_text(json.dumps(records))
    return {"backend": "json", "path": "inventory.json", "supplies": ["ip"]}


def test_a_declared_inventory_changes_the_digest(tmp_path, monkeypatch):
    """The positive control for the whole feature: the line contributes at all.

    ONE repo, two ``[inventory]`` tables — same settings file, same lab file,
    same paths, same mtimes — differing only in whether an inventory is
    declared. Without the inventory term in ``compute_fingerprint`` these two
    digests are equal, and a repo that switched its inventory on would be
    served the pre-inventory cache entry.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    table = _json_inventory(tmp_path, _RECORD)
    without = _repo(tmp_path, {})
    assert cc.compute_fingerprint([without]) != cc.compute_fingerprint(
        [_with_inventory(without, table)]
    )


def test_without_an_inventory_the_digest_is_stable(tmp_path, monkeypatch):
    """``none`` is a constant: no inventory must not mean no caching."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path, {})
    assert cc.compute_fingerprint([repo]) == cc.compute_fingerprint([repo])


def test_editing_the_inventory_file_moves_the_digest(tmp_path, monkeypatch):
    """A record edit invalidates completion — the reason §11 mixes the fingerprint in.

    The json backend's ``fingerprint()`` is path/mtime/size, so the file is
    rewritten with a DIFFERENT LENGTH: an mtime-only change can land inside
    one filesystem timestamp tick, which would make this pass or fail by
    timing rather than by the rule under test.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    table = _json_inventory(tmp_path, _RECORD)
    repo = _repo(tmp_path, table)
    before = cc.compute_fingerprint([repo])
    (tmp_path / "sut" / "inventory.json").write_text(
        json.dumps({"dut-1": {"ip": "10.0.0.1"}, "dut-2": {"ip": "10.0.0.2"}})
    )
    assert cc.compute_fingerprint([repo]) != before


def test_a_broken_declaration_hashes_its_error_and_the_fix_moves_the_digest(tmp_path, monkeypatch):
    """A typo'd backend name is a state of its own, and fixing it moves the digest.

    Neither "crash the shell mid-TAB" nor "silently reuse the entry the
    working declaration wrote" — the error text IS the state being hashed.

    Three comparisons, because the first two alone do not say that. Replacing
    the error text with the constant ``"none"`` keeps the broken digest stable
    AND different from the working one, so a broken ``[inventory]`` would
    collide with having no inventory at all — a repo that declared one and
    typo'd it would be served its pre-inventory entry. The third comparison is
    the one that catches that.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _json_inventory(tmp_path, _RECORD)
    # One repo, three tables — see `_with_inventory` for why rebuilding it
    # would make these pass on mtime drift alone.
    broken = _repo(tmp_path, {"backend": "no-such-backend", "path": "inventory.json"})
    fixed = _with_inventory(
        broken, {"backend": "json", "path": "inventory.json", "supplies": ["ip"]}
    )
    undeclared = _with_inventory(broken, {})
    assert cc.compute_fingerprint([broken]) == cc.compute_fingerprint([broken])
    assert cc.compute_fingerprint([broken]) != cc.compute_fingerprint([fixed])
    assert cc.compute_fingerprint([broken]) != cc.compute_fingerprint([undeclared])


class _Uncacheable:
    """A backend that cannot report freshness — §11's networked-CMDB case."""

    def __init__(self, **kwargs):
        self.label = "uncacheable:test"
        self.supplies = frozenset({"ip"})

    def lookup(self, key):
        return InventoryRecord(ip="10.0.0.1")

    def list_keys(self):
        return ["dut-1"]

    def fingerprint(self):
        return None


class _Exploding(_Uncacheable):
    """A backend whose freshness probe fails — an HTTP timeout, in the real case."""

    def fingerprint(self):
        raise RuntimeError("nb.example.com timed out after 5.0s")


@contextlib.contextmanager
def _registered(name: str, cls: type):
    register_inventory_backend(name, cls)
    try:
        yield {"backend": name}
    finally:
        INVENTORY_BACKENDS.unregister(name)


def test_an_uncacheable_backend_never_matches_its_own_entry(tmp_path, monkeypatch):
    """``fingerprint() is None`` means "not cacheable" — the digest must not settle.

    A live CMDB that cannot report freshness would otherwise have its answers
    served for a whole TTL. Mixing the clock in costs that deployment the
    fast path and keeps completion correct, which is the documented trade.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    with _registered("uncacheable-test", _Uncacheable) as table:
        repo = _repo(tmp_path, table)
        assert cc.compute_fingerprint([repo]) != cc.compute_fingerprint([repo])


def test_an_uncacheable_inventory_writes_nothing_at_all(tmp_path, monkeypatch):
    """The other half of the clock-stamped digest: the WRITERS must stand down.

    A digest that never matches is correct on the read side and catastrophic
    on the write side — every writer merges into the existing file and none
    prunes, so each otto invocation would append one dead entry, forever, to
    a file every TAB parses whole. All three fingerprint-keyed writers are
    checked, not just the largest: the payloads differ, the growth does not.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    with _registered("uncacheable-write-test", _Uncacheable) as table:
        repo = _repo(tmp_path, table)
        cache_path = cc._cache_path()
        assert cache_path is not None

        for _ in range(2):
            cc.write_cache([repo], [], [], ["dut"])
            cc._record_collected_tests([repo], ["test_x"])
            cc.record_tunnel_ids([repo], ["tun-abc123def456-22"])

        entries = json.loads(cache_path.read_text()) if cache_path.is_file() else {}
        assert entries == {}, f"an uncacheable inventory must leave no entry behind, got {entries}"

    # Positive control, same repo shape with a CACHEABLE inventory: the
    # writers are not simply broken. Without this a `return` at the top of
    # write_cache would pass the assertion above.
    cacheable = _repo(tmp_path, _json_inventory(tmp_path, _RECORD))
    cc.write_cache([cacheable], [], [], ["dut"])
    assert cc.read_cache([cacheable]) is not None


def test_a_backend_whose_freshness_probe_raises_does_not_crash_the_command(tmp_path, monkeypatch):
    """§11's networked backend can raise anything; `compute_fingerprint` may not.

    ``construct_inventory`` wraps only ``TypeError``/``ValueError`` from a
    third-party CONSTRUCTOR, so a probe raising ``RuntimeError`` reaches
    ``compute_fingerprint`` — which runs inside ``write_cache``, past
    ``otto.cli.main``'s ``suppress(OSError)``, and would traceback an
    otherwise-successful command after its real work was already done.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    with _registered("exploding-test", _Exploding) as table:
        repo = _repo(tmp_path, table)
        digest = cc.compute_fingerprint([repo])
        assert isinstance(digest, str)
        assert len(digest) == 64  # sha256 hex — a real digest, not a swallowed None
        cc.write_cache([repo], [], [], ["dut"])  # must not raise


def test_an_erroring_probe_writes_nothing_either(tmp_path, monkeypatch):
    """R18: a failed probe is EPHEMERAL, exactly like a missing fingerprint.

    An inventory whose ``fingerprint()`` raised has no stable identity to key
    an entry on. Treating its error text as stable would stake the cache's
    boundedness on a THIRD PARTY'S error strings — a message carrying a
    timestamp, a request id or a resolved IP is ordinary, and each one would
    append a dead entry per invocation, which is exactly the growth the
    uncacheable case already stands down for.

    The read side is untouched, and deliberately so: the text still moves the
    digest, so a broken probe is never served the working inventory's entry.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    with _registered("exploding-write-test", _Exploding) as table:
        repo = _repo(tmp_path, table)
        cache_path = cc._cache_path()
        assert cache_path is not None

        for _ in range(2):
            cc.write_cache([repo], [], [], ["dut"])
            cc._record_collected_tests([repo], ["test_x"])
            cc.record_tunnel_ids([repo], ["tun-abc123def456-22"])

        entries = json.loads(cache_path.read_text()) if cache_path.is_file() else {}
        assert entries == {}, f"a failed probe must leave no entry behind, got {entries}"

    # Positive control, same shape with a WORKING inventory: the writers are
    # not simply broken. Without it a bare `return` in write_cache would
    # satisfy the assertion above.
    working = _repo(tmp_path, _json_inventory(tmp_path, _RECORD))
    cc.write_cache([working], [], [], ["dut"])
    assert cc.read_cache([working]) is not None


def test_a_referenced_host_completes_only_with_the_inventory(tmp_path, monkeypatch):
    """§11's user-visible half: ``otto host <TAB>`` offers an inventory-backed host.

    The enumeration identifies a referenced entry through its record, so
    dropping ``inventory=`` from the ``host_summaries`` call leaves the entry
    unresolvable and the id disappears — which is what the second assertion
    pins from the other side.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    table = _json_inventory(tmp_path, _RECORD)
    with_inventory = _repo(tmp_path, table, hosts=[_REFERENCED])
    without = _repo(tmp_path, {}, hosts=[_REFERENCED])

    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    summaries = {s.id: s for s in cc.repo_host_summaries(with_inventory)}
    assert summaries["dut"].ip == "10.0.0.1"

    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    assert cc.repo_host_summaries(without) == []
