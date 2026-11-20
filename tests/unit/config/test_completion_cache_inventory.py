"""The completion digest tracks the process inventory, and the enumeration uses it (spec §11).

``OTTO_HOME`` is relocated INSIDE every test: ``build_inventory`` falls back
to ``~/.otto/settings.toml``, so a dev machine that declares an inventory
there would otherwise steer these digests. ``tests/conftest.py`` strips
``OTTO_``-prefixed variables at import time, which is why the relocation
cannot live at module scope.
"""

import contextlib
import json
import logging
from pathlib import Path

import otto.config.completion_cache as cc
from otto.host.os_profile import ProfileContext
from otto.inventory import register_inventory_backend
from otto.inventory.registry import INVENTORY_BACKENDS
from otto.models.base import OttoModel
from otto.models.inventory import InventoryRecord
from tests._fixtures.fake_project import fake_project
from tests._fixtures.labdata import json_lab_sources, write_lab_json
from tests._fixtures.project_scaffold import touch_settings

_REFERENCED = {"inventory": "dut-1", "element": "dut", "creds": [{"login": "u", "password": "p"}]}
_RECORD = {"dut-1": {"ip": "10.0.0.1"}}


def _project(
    tmp_path: Path, inventory_settings: dict, *, hosts: list[dict] | None = None, name: str = "sut"
):
    """A Project stand-in with one json lab source and the given ``[inventory]`` table.

    *name* is the project's directory under *tmp_path* — two stand-ins in one
    workspace need two.
    """
    sut = tmp_path / name
    lab = sut / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    touch_settings(sut)
    write_lab_json(lab / "lab.json", hosts if hosts is not None else [], declare_labs=True)
    return fake_project(
        root=sut,
        lab_sources=json_lab_sources(sut, [lab]),
        settings={"inventory": dict(inventory_settings)},
    )


def _digest(projects) -> str:
    """The ``names`` section's digest: every section ends with the inventory's line."""
    from otto.config.cache_sections import section_by_name, section_digest

    return section_digest(section_by_name("names"), projects)


def _with_inventory(project, inventory_settings: dict):
    """The SAME project — same files, same mtimes — carrying a different ``[inventory]``.

    Calling ``_project`` twice would rewrite ``settings.toml`` and ``lab.json``,
    and :func:`hash_file` reads ``st_mtime_ns``: the two digests would then
    differ by TIMING rather than by the inventory, so the comparison would
    pass with the inventory term deleted whenever the two writes landed in
    different clock ticks. (Observed: mutating the term out left this file at
    "3 failed, 1 passed" once and "4 failed" on the reruns.) Varying ONLY the
    settings table makes every other fingerprint input byte-identical, which
    is what the comparison claims.
    """
    return fake_project(**{**vars(project), "settings": {"inventory": dict(inventory_settings)}})


def _json_inventory(tmp_path: Path, records: dict, *, name: str = "sut") -> dict:
    """Write ``<name>/inventory.json`` and return the ``[inventory]`` table naming it.

    Not a fingerprint input in its own right — only lab files are stat'd — so
    writing it does not move the digest by itself. It reaches the digest only
    through the inventory's own ``fingerprint()``, which is the term under
    test. The table's relative ``path`` anchors to the declaring project, so
    *name* must be that project's directory.
    """
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / name / "inventory.json").write_text(json.dumps(records))
    return {"backend": "json", "path": "inventory.json", "supplies": ["ip"]}


def test_a_declared_inventory_changes_the_digest(tmp_path, monkeypatch):
    """The positive control for the whole feature: the line contributes at all.

    ONE project, two ``[inventory]`` tables — same settings file, same lab file,
    same paths, same mtimes — differing only in whether an inventory is
    declared. Without the inventory term in the section digests these two
    digests are equal, and a project that switched its inventory on would be
    served the pre-inventory cache entry.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    table = _json_inventory(tmp_path, _RECORD)
    without = _project(tmp_path, {})
    assert _digest([without]) != _digest([_with_inventory(without, table)])


def test_without_an_inventory_the_digest_is_stable(tmp_path, monkeypatch):
    """``none`` is a constant: no inventory must not mean no caching."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {})
    assert _digest([project]) == _digest([project])


def test_editing_the_inventory_file_moves_the_digest(tmp_path, monkeypatch):
    """A record edit invalidates completion — the reason §11 mixes the fingerprint in.

    The json backend's ``fingerprint()`` is path/mtime/size, so the file is
    rewritten with a DIFFERENT LENGTH: an mtime-only change can land inside
    one filesystem timestamp tick, which would make this pass or fail by
    timing rather than by the rule under test.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    table = _json_inventory(tmp_path, _RECORD)
    project = _project(tmp_path, table)
    before = _digest([project])
    (tmp_path / "sut" / "inventory.json").write_text(
        json.dumps({"dut-1": {"ip": "10.0.0.1"}, "dut-2": {"ip": "10.0.0.2"}})
    )
    assert _digest([project]) != before


def test_a_broken_declaration_hashes_its_error_and_the_fix_moves_the_digest(tmp_path, monkeypatch):
    """A typo'd backend name is a state of its own, and fixing it moves the digest.

    Neither "crash the shell mid-TAB" nor "silently reuse the entry the
    working declaration wrote" — the error text IS the state being hashed.

    Three comparisons, because the first two alone do not say that. Replacing
    the error text with the constant ``"none"`` keeps the broken digest stable
    AND different from the working one, so a broken ``[inventory]`` would
    collide with having no inventory at all — a project that declared one and
    typo'd it would be served its pre-inventory entry. The third comparison is
    the one that catches that.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _json_inventory(tmp_path, _RECORD)
    # One project, three tables — see `_with_inventory` for why rebuilding it
    # would make these pass on mtime drift alone.
    broken = _project(tmp_path, {"backend": "no-such-backend", "path": "inventory.json"})
    fixed = _with_inventory(
        broken, {"backend": "json", "path": "inventory.json", "supplies": ["ip"]}
    )
    undeclared = _with_inventory(broken, {})
    assert _digest([broken]) == _digest([broken])
    assert _digest([broken]) != _digest([fixed])
    assert _digest([broken]) != _digest([undeclared])


class _Uncacheable:
    """A backend that cannot report freshness — §11's networked-CMDB case."""

    def __init__(self):
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


class _NoConfig(OttoModel, frozen=True):
    pass


@contextlib.contextmanager
def _registered(name: str, cls: type):
    register_inventory_backend(name, config=_NoConfig, factory=lambda c: cls())
    try:
        yield {"backend": name}
    finally:
        INVENTORY_BACKENDS.unregister(name=name)


def test_an_uncacheable_backend_never_matches_its_own_entry(tmp_path, monkeypatch):
    """``fingerprint() is None`` means "not cacheable" — the digest must not settle.

    A live CMDB that cannot report freshness would otherwise have its answers
    served for a whole TTL. Mixing the clock in costs that deployment the
    fast path and keeps completion correct, which is the documented trade.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    with _registered("uncacheable-test", _Uncacheable) as table:
        project = _project(tmp_path, table)
        assert _digest([project]) != _digest([project])


def test_an_uncacheable_inventory_writes_nothing_at_all(tmp_path, monkeypatch):
    """The other half of the clock-stamped digest: the WRITERS must stand down.

    A digest that never matches is correct on the read side and catastrophic
    on the write side — every writer merges into the existing file and none
    prunes, so each otto invocation would append one dead entry, forever, to
    a file every TAB parses whole. Both digest-keyed writers are checked,
    not just the largest: the payloads differ, the growth does not.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    with _registered("uncacheable-write-test", _Uncacheable) as table:
        project = _project(tmp_path, table)
        cache_path = cc._cache_path()
        assert cache_path is not None

        for _ in range(2):
            cc.write_cache([project], [], ["dut"])
            cc.record_tunnel_ids([project], ["tun-abc123def456-22"])

        entries = json.loads(cache_path.read_text()) if cache_path.is_file() else {}
        assert entries == {}, f"an uncacheable inventory must leave no entry behind, got {entries}"

    # Positive control, same project shape with a CACHEABLE inventory: the
    # writers are not simply broken. Without this a `return` at the top of
    # write_cache would pass the assertion above.
    cacheable = _project(tmp_path, _json_inventory(tmp_path, _RECORD))
    cc.write_cache([cacheable], [], ["dut"])
    assert cc.read_cache([cacheable]) is not None


def test_a_backend_whose_freshness_probe_raises_does_not_crash_the_command(tmp_path, monkeypatch):
    """§11's networked backend can raise anything; the digest may not.

    ``construct_inventory`` wraps only ``TypeError``/``ValueError`` from a
    third-party CONSTRUCTOR, so a probe raising ``RuntimeError`` reaches
    the section digest — which runs inside ``write_cache``, past
    ``otto.cli.main``'s ``suppress(OSError)``, and would traceback an
    otherwise-successful command after its real work was already done.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    with _registered("exploding-test", _Exploding) as table:
        project = _project(tmp_path, table)
        digest = _digest([project])
        assert isinstance(digest, str)
        assert len(digest) == 64  # sha256 hex — a real digest, not a swallowed None
        cc.write_cache([project], [], ["dut"])  # must not raise


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
        project = _project(tmp_path, table)
        cache_path = cc._cache_path()
        assert cache_path is not None

        for _ in range(2):
            cc.write_cache([project], [], ["dut"])
            cc.record_tunnel_ids([project], ["tun-abc123def456-22"])

        entries = json.loads(cache_path.read_text()) if cache_path.is_file() else {}
        assert entries == {}, f"a failed probe must leave no entry behind, got {entries}"

    # Positive control, same shape with a WORKING inventory: the writers are
    # not simply broken. Without it a bare `return` in write_cache would
    # satisfy the assertion above.
    working = _project(tmp_path, _json_inventory(tmp_path, _RECORD))
    cc.write_cache([working], [], ["dut"])
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
    with_inventory = _project(tmp_path, table, hosts=[_REFERENCED])
    without = _project(tmp_path, {}, hosts=[_REFERENCED])

    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    resolved = cc.resolve_process_inventory([with_inventory])
    summaries = {
        s.id: s
        for s in cc.project_host_summaries(
            with_inventory, resolved, profiles=ProfileContext.empty()
        )
    }
    assert summaries["dut"].ip == "10.0.0.1"

    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    assert (
        cc.project_host_summaries(
            without, cc.resolve_process_inventory([without]), profiles=ProfileContext.empty()
        )
        == []
    )


def test_a_host_referencing_another_projects_inventory_completes(tmp_path, monkeypatch):
    """The inventory is resolved ONCE over every active project (spec §8) — for completion too.

    Project ``inv`` declares ``[inventory]`` and carries no hosts; project ``sut``
    references a key and declares nothing. Dispatch resolves the process
    inventory over both, so ``otto -l L --list-hosts`` listed ``dut`` — while
    the enumeration resolved each project against ITS OWN declaration, else the
    user file, so ``dut`` completed nowhere and the cache was written that
    way (observed 2026-09-03). Red against the per-project resolution.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    declaring = _project(tmp_path, _json_inventory(tmp_path, _RECORD, name="inv"), name="inv")
    referencing = _project(tmp_path, {}, hosts=[_REFERENCED])

    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    assert "dut" in cc.collect_host_ids([declaring, referencing])

    # The same answer through the per-project entry point, given the SAME
    # resolution a collector makes over the whole workspace.
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    resolved = cc.resolve_process_inventory([declaring, referencing])
    summaries = {
        s.id: s
        for s in cc.project_host_summaries(
            referencing,
            resolved,
            profiles=ProfileContext.from_projects(projects=[declaring, referencing]),
        )
    }
    assert summaries["dut"].ip == "10.0.0.1"


def test_a_broken_declaration_empties_every_project_and_warns_once(tmp_path, monkeypatch, caplog):
    """A declaration that cannot be built fails EVERY lab load at dispatch, so
    completion offers nothing for any project — the inline host of a project that
    declared nothing included — and says why once, not once per project.

    Per-project resolution offered that inline host (its own resolution saw no
    error) while dispatch refuses it: an id that cannot dispatch, and a
    warning the declaring project repeated on every collector.
    """
    from otto.host.builtin_hosts import builtin_host_ids

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    broken = _project(tmp_path, {"backend": "json"}, name="broken")  # json needs a path
    creds = [{"login": "u", "password": "p"}]
    inline_host = {"ip": "10.0.0.9", "element": "inline", "creds": creds}
    inline = _project(tmp_path, {}, hosts=[inline_host])

    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    with caplog.at_level(logging.WARNING, logger="otto.config.completion_cache"):
        ids = cc.collect_host_ids([broken, inline])

    assert ids == sorted(builtin_host_ids()), ids
    said = [r.message for r in caplog.records if "could not be resolved" in r.message]
    assert len(said) == 1, caplog.text
    assert "parse failed: path: Field required" in said[0]


def test_a_tab_over_a_profile_holding_a_secret_never_makes_the_key(tmp_path, monkeypatch):
    """The enumeration memo keys on the profiles, never their digest: a digest of a
    profile with a password would read (or create) ``secret_salt`` on every TAB."""
    from pydantic import SecretStr

    from otto.config.home import DIGEST_SALT_FILENAME, otto_home
    from otto.host.os_profile import OsProfile, ProfileFields
    from otto.registry import FrozenMap

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    fields = ProfileFields(
        defaults=FrozenMap.freeze_json(
            value={"creds": [{"login": "u", "password": SecretStr("pw")}]}
        )
    )
    profiles = ProfileContext(
        profiles=FrozenMap(mapping={"x": OsProfile("x", "unix", fields)}),
        owners=FrozenMap(mapping={"x": "sut"}),
    )
    project = _project(tmp_path, {}, hosts=[_REFERENCED])
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    resolved = cc.resolve_process_inventory([project])
    cc.project_host_summaries(project, resolved, profiles=profiles)
    cc.project_host_summaries(project, resolved, profiles=profiles)  # the memo's hit
    assert len(cc._SUMMARY_MEMO) == 1
    assert not (otto_home() / DIGEST_SALT_FILENAME).exists()


# ---------------------------------------------------------------------------
# Named inventories (#1176): completion resolves every reference through the
# process inventory view, and otherwise keeps today's design.
# ---------------------------------------------------------------------------

_CREDS = [{"login": "u", "password": "p"}]
_QUALIFIED = {"inventory": {"name": "physical", "key": "k"}, "element": "dut", "creds": _CREDS}
_BARE = {"inventory": "k", "element": "bare", "creds": _CREDS}
_INLINE = {"ip": "10.0.0.9", "element": "inline", "creds": _CREDS}


def _records_file(path: Path, records: dict) -> str:
    """Write *records* to *path* (parents created) and return the path as a string."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records))
    return str(path)


def _entry(name: str, path: Path, records: dict, **extra) -> dict:
    """An ``[[inventories]]`` entry *name*: a json inventory over *records* written to *path*."""
    return {"name": name, "backend": "json", "path": _records_file(path, records), **extra}


def _named(tmp_path: Path, name: str, *, settings: dict, hosts=(), lab_sources=None):
    """A Project stand-in at ``tmp_path/name``: one json lab of *hosts*, *settings* parsed."""
    root = tmp_path / name
    lab = root / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    touch_settings(root)
    write_lab_json(lab / "lab.json", list(hosts), declare_labs=True)
    return fake_project(
        root=root,
        lab_sources=json_lab_sources(root, [lab]) if lab_sources is None else lab_sources,
        settings=settings,
    )


def _resettled(project, settings: dict):
    """The SAME project stand-in — same root, same files — carrying other *settings*."""
    return fake_project(**{**vars(project), "settings": settings})


def _summaries(project, projects) -> dict:
    """*project*'s host summaries by id, resolved over the selection *projects*."""
    resolution = cc.resolve_process_inventory(projects)
    profiles = ProfileContext.from_projects(projects=projects)
    return {s.id: s for s in cc.project_host_summaries(project, resolution, profiles=profiles)}


def _example_source(root: Path, backend: str, labs: dict):
    """The ``[[lab.sources]]`` entry for the example (legacy, ``SupportsHostSummaries``) backend."""
    from otto.labs.sources import PendingLabSource
    from otto.registry import FrozenMap

    return [
        PendingLabSource(
            backend=backend,
            label=f"example/{backend}#1",
            raw=FrozenMap.freeze_json(value={"labs": labs}),
            origin=str(root / ".otto" / "settings.toml"),
            project_root=root,
        )
    ]


@contextlib.contextmanager
def _example_backend(name: str):
    """Register the example lab-source backend as *name* for the block."""
    from otto.examples.lab_repository import ExampleLabSourceConfig, example_lab_source
    from otto.labs import register_lab_repository
    from otto.labs.registry import LAB_REPOSITORIES

    register_lab_repository(name, config=ExampleLabSourceConfig, factory=example_lab_source)
    try:
        yield
    finally:
        LAB_REPOSITORIES.unregister(name=name)


def test_a_named_inventory_change_moves_the_digest_and_the_shim(tmp_path, monkeypatch):
    """Review Focus 2: a NON-default inventory's file change invalidates completion.

    ``virtual`` is rewritten with a different size (an mtime-only change can
    land inside one timestamp tick). The digest moves, and the real shim's
    inventory pass hands over on the block written before the change.
    """
    import pytest

    from otto import _shim_complete as sc
    from otto.config.completion_tree import inventory_block

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    virtual = tmp_path / "sut" / "virtual.json"
    project = _named(
        tmp_path,
        "sut",
        settings={
            "inventory": _json_inventory(tmp_path, _RECORD),
            "inventories": [_entry("virtual", virtual, {"k": {"ip": "10.0.2.1"}})],
        },
    )
    before = _digest([project])
    block = inventory_block([project])
    assert block["kind"] == "stat"
    sc._inventory_pass(block)  # nothing changed yet: the shim answers

    virtual.write_text(json.dumps({"k": {"ip": "10.0.2.1"}, "k2": {"ip": "10.0.2.2"}}))

    assert _digest([project]) != before
    with pytest.raises(sc.Handover):
        sc._inventory_pass(block)


def test_a_default_only_workspace_keeps_its_block_and_digest(tmp_path, monkeypatch):
    """A workspace without ``[[inventories]]`` keys completion exactly as before.

    The view holding only ``default`` passes its values through: the digest's
    inventory line is the json backend's own fingerprint, the shim block is
    its one file, and ``otto cache info`` names its label.
    """
    from otto.config.completion_tree import inventory_block, stat_triple
    from otto.inventory.json_backend import JsonInventory

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, _json_inventory(tmp_path, _RECORD))
    alone = JsonInventory(path=(tmp_path / "sut" / "inventory.json").resolve())

    assert cc.inventory_digest_text([project]) == alone.fingerprint()
    assert inventory_block([project]) == {"kind": "stat", "files": [stat_triple(alone.path)]}
    assert cc.describe_inventory([project]) == cc.InventoryDescription(text=alone.label)


def test_a_qualified_host_completes(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    physical = tmp_path / "sut" / "physical.json"
    project = _named(
        tmp_path,
        "sut",
        settings={"inventories": [_entry("physical", physical, {"k": {"ip": "10.0.1.1"}})]},
        hosts=[_QUALIFIED],
    )
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    assert "dut" in cc.collect_host_ids([project])
    assert _summaries(project, [project])["dut"].ip == "10.0.1.1"


def _d1_drops(project) -> list:
    resolution = cc.resolve_process_inventory([project])
    drops = cc.project_host_drops(project, resolution, profiles=ProfileContext.empty())
    return [d for d in drops if "bare inventory reference 'k' is refused" in d.reason]


def test_a_d1_refusal_is_a_completion_drop(tmp_path, monkeypatch):
    """D1 on both summaries routes: the json backend's and the legacy fast branch.

    The project declares ``[[inventories]]`` without binding ``default``, so
    its bare reference is refused: one drop holding the D1 text, never two,
    and the inline sibling still completes.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    settings = {
        "inventories": [_entry("physical", tmp_path / "phys.json", {"k": {"ip": "10.0.1.1"}})]
    }
    json_project = _named(tmp_path, "json", settings=settings, hosts=[_BARE, _INLINE])

    root = tmp_path / "legacy"
    labs = {
        "east": [
            {"name": "bare", "hosts": [{"inventory": "k", "creds": _CREDS}]},
            {"name": "inline", "hosts": [{"ip": "10.0.0.9", "creds": _CREDS}]},
        ]
    }
    with _example_backend("example-d1"):
        legacy = _named(
            tmp_path,
            "legacy",
            settings=settings,
            lab_sources=_example_source(root, "example-d1", labs),
        )
        for project in (json_project, legacy):
            monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
            assert len(_d1_drops(project)) == 1, project.root
            summaries = _summaries(project, [project])
            assert "inline" in summaries, project.root
            assert "bare" not in summaries, project.root


def test_summaries_carry_the_qualified_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    physical = tmp_path / "sut" / "physical.json"
    project = _named(
        tmp_path,
        "sut",
        settings={"inventories": [_entry("physical", physical, {"k": {"ip": "10.0.1.1"}})]},
        hosts=[_QUALIFIED],
    )
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    ref = _summaries(project, [project])["dut"].inventory_ref
    assert (ref.name, ref.key, ref.qualified) == ("physical", "k", True)


def test_two_selections_never_share_a_memo_entry(tmp_path, monkeypatch):
    """P references ``physical/k``; two selections bind ``physical`` to two files.

    No memo clear between them: each selection gets its own address.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    p = _named(tmp_path, "p", settings={}, hosts=[_QUALIFIED])
    a = _named(
        tmp_path,
        "sel",
        settings={
            "inventories": [_entry("physical", tmp_path / "a.json", {"k": {"ip": "10.1.0.1"}})]
        },
    )
    b = _resettled(
        a, {"inventories": [_entry("physical", tmp_path / "b.json", {"k": {"ip": "10.1.0.2"}})]}
    )
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    assert _summaries(p, [p, a])["dut"].ip == "10.1.0.1"
    assert _summaries(p, [p, b])["dut"].ip == "10.1.0.2"


class _SameLabelConfig(OttoModel, frozen=True):
    records: dict[str, dict]


def test_same_label_bindings_never_share_a_memo_entry(tmp_path, monkeypatch):
    """Two bindings of one label differ in configuration: the memo keys on the binding.

    The fake backend's label is the constant ``fake:same``; its records come
    from its configuration. Project P and the refusing roots stay fixed (the
    selecting project keeps its root), so only the binding tells the two
    selections apart.
    """
    from tests.unit.inventory.test_resolve import FakeInventory

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    register_inventory_backend(
        "fake:same",
        config=_SameLabelConfig,
        factory=lambda c: FakeInventory(c.config.records, label="fake:same"),
        snapshot_cache=False,
    )

    def selecting(ip: str) -> dict:
        entry = {"name": "physical", "backend": "fake:same", "records": {"k": {"ip": ip}}}
        return {"inventories": [entry]}

    p = _named(tmp_path, "p", settings={}, hosts=[_QUALIFIED])
    x = _named(tmp_path, "sel", settings=selecting("10.2.0.1"))
    y = _resettled(x, selecting("10.2.0.2"))
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    assert _summaries(p, [p, x])["dut"].ip == "10.2.0.1"
    assert _summaries(p, [p, y])["dut"].ip == "10.2.0.2"


def test_a_switched_symlink_never_reuses_the_memo(tmp_path, monkeypatch):
    """``default`` names ``current.json``; repointing the link changes what was constructed."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    root = tmp_path / "sut"
    a = Path(_records_file(root / "a.json", {"dut-1": {"ip": "10.3.0.1"}}))
    b = Path(_records_file(root / "b.json", {"dut-1": {"ip": "10.3.0.2"}}))
    current = root / "current.json"
    current.symlink_to(a)
    table = {"backend": "json", "path": "current.json", "supplies": ["ip"]}
    project = _project(tmp_path, table, hosts=[_REFERENCED])

    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    assert _summaries(project, [project])["dut"].ip == "10.3.0.1"
    current.unlink()
    current.symlink_to(b)
    assert _summaries(project, [project])["dut"].ip == "10.3.0.2"


def test_a_switched_creds_symlink_never_reuses_the_memo(tmp_path, monkeypatch):
    """``physical``'s creds store is a link; repointing it offers the new login on ``--user``."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    root = tmp_path / "sut"
    alice = Path(_records_file(root / "alice.json", {"k": [{"login": "alice", "password": "p"}]}))
    bob = Path(_records_file(root / "bob.json", {"k": [{"login": "bob", "password": "p"}]}))
    current = root / "current.json"
    current.symlink_to(alice)
    entry = _entry(
        "physical",
        root / "physical.json",
        {"k": {"ip": "10.4.0.1"}},
        creds={"backend": "json", "path": str(current)},
    )
    host = {"inventory": {"name": "physical", "key": "k"}, "element": "dut"}
    project = _named(tmp_path, "sut", settings={"inventories": [entry]}, hosts=[host])

    def logins() -> list[str]:
        return [e["login"] for e in cc.collect_logins_by_host([project]).get("dut", [])]

    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    assert logins() == ["alice"]
    current.unlink()
    current.symlink_to(bob)
    assert logins() == ["bob"]


class _StatusRaises:
    """A lab source whose status report must never be asked for a name-only request."""

    def list_labs(self) -> list[str]:
        return ["east"]

    def load_lab(self, name, **_):
        from otto.labs import LabNotFoundError

        raise LabNotFoundError(name)

    def list_lab_status(self, *, view):
        raise AssertionError("a name-only request asked for lab status")


def test_a_name_only_request_never_calls_status(tmp_path, monkeypatch):
    from otto.labs import register_lab_repository
    from otto.labs.sources import PendingLabSource
    from otto.registry import FrozenMap

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    register_lab_repository("status-raises", config=_NoConfig, factory=lambda c: _StatusRaises())
    root = tmp_path / "sut"
    source = PendingLabSource(
        backend="status-raises",
        label="sut/status-raises#1",
        raw=FrozenMap.freeze_json(value={}),
        origin=str(root / ".otto" / "settings.toml"),
        project_root=root,
    )
    project = _named(tmp_path, "sut", settings={}, lab_sources=[source])
    assert cc.collect_lab_names([project]) == ["east"]


def test_an_opaque_named_inventory_makes_the_block_opaque(tmp_path, monkeypatch):
    """A named inventory that names no freshness files makes the whole block opaque."""
    from otto.config.completion_tree import inventory_block
    from tests._fixtures.named_inventory import register_lookup_counting

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    register_lookup_counting(monkeypatch, records={"k": {"ip": "10.5.0.1"}}, name="fake:opaque")
    project = _named(
        tmp_path,
        "sut",
        settings={
            "inventory": _json_inventory(tmp_path, _RECORD),
            "inventories": [{"name": "lab2", "backend": "fake:opaque"}],
        },
    )
    assert inventory_block([project]) == {"kind": "opaque"}


def test_a_broken_named_inventory_empties_completion_and_warns_once(tmp_path, monkeypatch, caplog):
    """A named entry that cannot be built empties completion, as a broken ``[inventory]`` does."""
    from otto.host.builtin_hosts import builtin_host_ids

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    broken = _named(
        tmp_path,
        "broken",
        settings={"inventories": [{"name": "physical", "backend": "json"}]},  # json needs a path
    )
    inline = _named(tmp_path, "inline", settings={}, hosts=[_INLINE])

    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    with caplog.at_level(logging.WARNING, logger="otto.config.completion_cache"):
        ids = cc.collect_host_ids([broken, inline])

    assert ids == sorted(builtin_host_ids()), ids
    said = [r.message for r in caplog.records if "could not be resolved" in r.message]
    assert len(said) == 1, caplog.text
    assert "parse failed: path: Field required" in said[0]


def test_a_default_that_cannot_be_built_is_a_drop_never_a_raise(tmp_path, monkeypatch):
    """The legacy fast branch reads ``bare_inventory``; a ``default`` that raises is a drop.

    A deferred view over an unreadable user file cannot build ``default``;
    completion's enumeration records that source's failure and raises nothing.
    """
    from otto.inventory import deferred_inventory_view

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    user_file = tmp_path / "user-settings.toml"
    user_file.write_text("[inventory\n")  # a broken user file, not a project
    root = tmp_path / "legacy"
    labs = {"east": [{"name": "bare", "hosts": [{"inventory": "k", "creds": _CREDS}]}]}
    with _example_backend("example-broken-default"):
        project = _named(
            tmp_path,
            "legacy",
            settings={},
            lab_sources=_example_source(root, "example-broken-default", labs),
        )
        view = deferred_inventory_view(projects=[project], user_settings_path=user_file)
        enumeration = cc._enumerate_host_summaries(project, view, ProfileContext.empty())
    assert enumeration.summaries == []
    assert any(str(user_file) in d.reason for d in enumeration.drops), enumeration.drops


def test_the_empty_view_shapes_never_share_a_memo_key():
    """A legacy ``inventory=None`` view and an empty catalog refuse qualified names differently."""
    from otto.inventory import InventoryView

    legacy = cc.InventoryResolution(view=InventoryView.of_default(inventory=None))
    empty = cc.InventoryResolution(view=InventoryView(inventories={}))
    assert legacy.memo_key != empty.memo_key
    assert cc.InventoryResolution().memo_key == "none"
    assert cc.InventoryResolution(error="E: x").memo_key == "error:E: x"


def test_the_names_payload_carries_every_declared_inventory_name(tmp_path, monkeypatch):
    """``--inventory`` is answered from the cache: names from settings, no backend built."""
    from otto.inventory import config

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _named(
        tmp_path,
        "sut",
        settings={
            "inventory": _json_inventory(tmp_path, _RECORD),
            "inventories": [_entry("physical", tmp_path / "p.json", {"k": {"ip": "10.0.1.1"}})],
        },
    )

    def _never(**_kwargs):
        raise AssertionError("collecting inventory names constructed an inventory")

    with monkeypatch.context() as m:
        for builder in ("construct_inventory", "build_binding_from_declarations"):
            m.setattr(config, builder, _never)
        names = cc.collect_inventory_names([project])
    assert names == ["default", "physical"]

    cc.write_cache([project], [], ["local"], inventory_names=names)
    assert cc.read_cache([project])["inventory_names"] == ["default", "physical"]


def test_the_shim_answers_the_inventory_option_from_the_cache(tmp_path, monkeypatch):
    """A warm ``otto inventory list --inventory <TAB>`` never starts Python."""
    import os

    import otto.config.project as config_project
    from otto import _shim_complete as sc
    from otto import bootstrap
    from otto.cli.main import PENDING_SUBCMD_ARGS_KEY, app
    from otto.config.completion_tree import build_shim_payload
    from tests._fixtures.project_scaffold import make_project

    data = tmp_path / "data"
    default = _records_file(data / "default.json", {"k": {"ip": "10.0.0.1"}})
    physical = _records_file(data / "physical.json", {"k": {"ip": "10.0.1.1"}})
    extra = (
        f'[inventory]\nbackend = "json"\npath = "{default}"\n\n'
        f'[[inventories]]\nname = "physical"\nbackend = "json"\npath = "{physical}"\n'
    )
    root = make_project(tmp_path / "proj", extra=extra)
    monkeypatch.setenv("OTTOPATH", str(root))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    bootstrap.invalidate()
    try:
        projects = config_project.discover().projects
        cc.write_cache(
            projects,
            [],
            ["local"],
            inventory_names=cc.collect_inventory_names(projects),
            shim=build_shim_payload(projects, app=app, pending_key=PENDING_SUBCMD_ARGS_KEY),
        )
        environ = {
            **os.environ,
            "_OTTO_COMPLETE": "complete_bash",
            "COMP_WORDS": "otto inventory list --inventory ",
            "COMP_CWORD": "4",
        }
        out = sc.answer_or_reason(environ)
    finally:
        bootstrap.invalidate()
    assert out.items == ["default", "physical"], out.reason


class _PrivateStore:
    """A third-party backend's own ``store``: otto must never read it."""

    def __init__(self, label: object, *, broken: bool = False) -> None:
        self._label = label
        self._broken = broken

    @property
    def label(self) -> object:
        if self._broken:
            raise RuntimeError("the private store is offline")
        return self._label


class _WithPrivateStore(OttoModel, frozen=True):
    pass


def _register_private_store(name: str, *, store: object, label: object = None) -> None:
    from tests.unit.inventory.test_resolve import FakeInventory

    class _Backend(FakeInventory):
        @property
        def store(self) -> object:
            if store is None:
                raise RuntimeError("store is unavailable offline")
            return store

    def build(configured: object) -> FakeInventory:
        inventory = _Backend({"k": {"ip": "10.3.0.1"}}, label=name)
        if label is not None:
            inventory.label = label  # type: ignore[assignment]
        return inventory

    register_inventory_backend(name, config=_WithPrivateStore, factory=build, snapshot_cache=False)


def test_a_backends_own_store_is_never_read_for_its_binding(tmp_path, monkeypatch):
    """Only otto's credentials overlay has a store label; a backend's ``store`` is its own."""
    from otto.inventory.config import build_inventory_view

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    for name, store in (
        ("fake:raises", None),
        ("fake:listlabel", _PrivateStore(["not", "hashable"])),
        ("fake:broken", _PrivateStore("x", broken=True)),
    ):
        _register_private_store(name, store=store)
        p = _named(
            tmp_path,
            name.replace(":", "-"),
            settings={"inventories": [{"name": "physical", "backend": name}]},
            hosts=[_QUALIFIED],
        )
        view = build_inventory_view(projects=[p])
        assert view.keys["physical"].creds_label is None
        monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
        assert _summaries(p, [p])["dut"].ip == "10.3.0.1"


def test_an_unhashable_memo_key_is_the_resolutions_error(tmp_path, monkeypatch):
    """A key that cannot be hashed fails inside completion's boundary, never on the TAB."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    _register_private_store("fake:listed", store=_PrivateStore("s"), label=["a", "list"])
    p = _named(
        tmp_path,
        "p",
        settings={"inventories": [{"name": "physical", "backend": "fake:listed"}]},
        hosts=[_QUALIFIED],
    )
    resolution = cc.resolve_process_inventory([p])
    assert resolution.error is not None
    assert "unhashable" in resolution.error
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    assert _summaries(p, [p]) == {}
