"""The shim section: keys, inventory kind, ttl and tree — written with every cache write."""

import json
from pathlib import Path

from otto import bootstrap
from otto.config import completion_cache as cc
from otto.config.cache_sections import SECTIONS, section_by_name
from otto.config.completion_tree import build_shim_payload, inventory_block, stat_triple
from tests._fixtures.generated_repo import generate_repo


def _repos(tmp_path, monkeypatch):
    repo = generate_repo(tmp_path, files=4, dirs=1, top_level=1)
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    bootstrap.invalidate()
    return repo, bootstrap.discover().repos


def test_stat_triple_records_a_file_and_a_missing_path(tmp_path: Path):
    f = tmp_path / "f"
    f.write_text("x")
    st = f.stat()
    assert stat_triple(f) == [str(f), st.st_mtime_ns, st.st_size]
    assert stat_triple(tmp_path / "nope") == [str(tmp_path / "nope"), None, None]


def test_payload_keys_mirror_the_two_sections_key_sets(tmp_path, monkeypatch):
    _, repos = _repos(tmp_path, monkeypatch)
    payload = build_shim_payload(repos)
    for name in ("names", "tests"):
        expected = [stat_triple(p) for p in sorted(set(section_by_name(name).key_paths(repos)))]
        assert payload["keys"][name] == expected
    assert payload["ttl_seconds"] == cc._cache_ttl_seconds(repos)
    assert payload["tests_digest"] == cc.compute_fingerprint(repos)
    assert payload["inventory"] == {"kind": "none"}
    assert payload["tree"]["name"] == "otto"
    assert "host_classes" in payload["tree"]


def test_shim_section_keys_on_names_and_tests_together(tmp_path, monkeypatch):
    _, repos = _repos(tmp_path, monkeypatch)
    shim = section_by_name("shim")
    assert set(shim.key_paths(repos)) == set(section_by_name("names").key_paths(repos)) | set(
        section_by_name("tests").key_paths(repos)
    )
    assert [s.name for s in SECTIONS] == ["names", "tests", "shim"]


def test_write_cache_stores_the_shim_section_when_given(tmp_path, monkeypatch):
    _, repos = _repos(tmp_path, monkeypatch)
    cc.write_cache(repos, [], [], [], shim=build_shim_payload(repos))
    data = json.loads(cc._cache_path().read_text())
    assert data["schema"] == cc.SCHEMA_VERSION == 18
    assert set(data["sections"]) == {"names", "tests", "shim"}
    assert data["sections"]["shim"]["tainted"] is False
    assert cc.cache_rebuild_is_worthwhile(repos) is False


def test_the_section_collects_exactly_what_the_writer_stores(tmp_path, monkeypatch):
    """The Section and `entry()` both call `build_shim_payload(repos)`: one source of the tree.

    This guards against either side growing an argument of its own (an explicit
    `app`, a different repo list) — the two would then drift silently.
    """
    _, repos = _repos(tmp_path, monkeypatch)
    assert section_by_name("shim").collect(repos) == build_shim_payload(repos)


def test_a_missing_shim_section_makes_a_rebuild_worthwhile(tmp_path, monkeypatch):
    _, repos = _repos(tmp_path, monkeypatch)
    cc.write_cache(repos, [], [], [], shim=build_shim_payload(repos))
    assert cc.cache_rebuild_is_worthwhile(repos) is False
    path = cc._cache_path()
    data = json.loads(path.read_text())
    del data["sections"]["shim"]
    path.write_text(json.dumps(data))
    assert cc.cache_rebuild_is_worthwhile(repos) is True, (
        "both siblings validate, but the shim entry is gone"
    )


def test_require_widens_validation_never_the_merged_view(tmp_path, monkeypatch):
    """`require` makes `read_cache` also validate an extra section, but never merges it in.

    Two properties, both load-bearing:

    1. With a real `shim` entry on disk, `require=(SHIM_SECTION,)` still
       returns a view carrying none of `shim`'s own keys (`ttl_seconds`,
       `tree`, `keys`, `inventory`, `tests_digest`) — only merged-view keys.
       On its own this is guaranteed by `read_cache`'s fixed-key return
       regardless of how the merge loop is written, so it cannot fail on its
       own — property 2 is the one that actually pins the loop.
    2. A required section whose payload happens to share a key name with the
       merged view (a `throwaway` section publishing `instructions`, exactly
       like a `names`/`tests` payload would) must NOT be able to clobber the
       real value: `require` only widens which sections must VALIDATE, never
       which payloads get merged. `for payload in payloads.values(): ...`
       would merge `throwaway` last (dict insertion order) and let its
       injected value win; `for name in MERGED_VIEW_SECTIONS: ...` never
       looks at `throwaway`'s payload at all.
    """
    from otto.config import cache_sections as cs
    from otto.config.completion_tree import SHIM_SECTION

    _, repos = _repos(tmp_path, monkeypatch)
    cc.write_cache(repos, [{"name": "real", "options": []}], [], [], shim=build_shim_payload(repos))

    merged = cc.read_cache(repos, require=(SHIM_SECTION,))
    assert merged is not None
    assert not {"ttl_seconds", "tree", "keys", "inventory", "tests_digest"} & set(merged)
    assert merged["instructions"] == [{"name": "real", "options": []}]

    throwaway = cs.Section(
        name="throwaway",
        key_paths=lambda repos: [],
        collect=lambda repos: {"instructions": ["INJECTED"]},
    )
    monkeypatch.setattr(cs, "SECTIONS", [*cs.SECTIONS, throwaway])
    cs.write_section(repos, "throwaway", {"instructions": ["INJECTED"]})

    merged = cc.read_cache(repos, require=("throwaway",))
    assert merged is not None
    assert merged["instructions"] == [{"name": "real", "options": []}], (
        "a required section's payload must never clobber the merged view"
    )


def test_inventory_block_kinds(tmp_path, monkeypatch):
    import otto.config.completion_tree as ct

    monkeypatch.setattr(ct, "_build_inventory", lambda repos: None)
    assert inventory_block([]) == {"kind": "none"}

    class Opaque:
        def fingerprint(self):
            return "h"

    monkeypatch.setattr(ct, "_build_inventory", lambda repos: Opaque())
    assert inventory_block([]) == {"kind": "opaque"}
    f = tmp_path / "inv.json"
    f.write_text("{}")

    class Stat:
        def fingerprint(self):
            return "x"

        def stat_paths(self):
            return [f]

    monkeypatch.setattr(ct, "_build_inventory", lambda repos: Stat())
    assert inventory_block([]) == {"kind": "stat", "files": [stat_triple(f)]}
