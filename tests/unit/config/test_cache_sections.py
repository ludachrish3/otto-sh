"""The section registry: per-section digests, taint, and digest reuse (Task 6).

The storage layout these tests pin is::

    {"schema": N, "sections": {"<name>": {"fingerprint", "generated_at",
                                          "tainted", "payload"}, ...}}

so a names-only reader can locate and validate its entry without the
full-corpus walk the old fingerprint-keyed layout forced on every reader.
"""

import json

import pytest

from otto import bootstrap
from tests._fixtures.generated_repo import generate_repo


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """Discovered repos for a generated tree.

    discover() caches module-globally (bootstrap.py:192-224), so every fixture
    must invalidate or it silently returns the previous test's repos and the
    assertions pass against the wrong tree.
    """
    repo = generate_repo(tmp_path, files=20, dirs=2, top_level=2)
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    bootstrap.invalidate()
    yield repo, bootstrap.discover().repos
    bootstrap.invalidate()


def test_names_digest_ignores_nested_test_edits(repos):
    """Nested files cannot register, so they must not key the names section."""
    from otto.config.cache_sections import section_by_name, section_digest

    repo, discovered = repos
    names = section_by_name("names")
    before = section_digest(names, discovered)

    nested = next(repo.rglob("sub*/test_*.py"))
    nested.write_text("def test_x():\n    pass\n# edited\n")

    assert section_digest(names, discovered) == before


def test_names_digest_moves_on_top_level_test_edit(repos):
    """Top-level test files CAN register, so they must key the names section."""
    from otto.config.cache_sections import section_by_name, section_digest

    repo, discovered = repos
    names = section_by_name("names")
    before = section_digest(names, discovered)

    top = repo / "tests" / "test_top0.py"
    top.write_text("def test_y():\n    pass\n# edited\n")

    assert section_digest(names, discovered) != before


def test_names_digest_moves_on_settings_edit(repos):
    """settings.toml decides init list, libs and tests dirs — it keys every section."""
    from otto.config.cache_sections import section_by_name, section_digest

    repo, discovered = repos
    names = section_by_name("names")
    before = section_digest(names, discovered)

    settings = repo / ".otto" / "settings.toml"
    edited = settings.read_text() + "\n# edited\n"
    settings.write_text(edited)  # sutrepo-exempt: edits a scaffolded file to move a digest

    assert section_digest(names, discovered) != before


def test_tainted_section_is_not_served(repos):
    """A section written while bootstrap reported errors must force a full load.

    Otherwise help goes silently and permanently partial: the broken file's
    hash is stable until edited, so the digest never moves.
    """
    from otto.config.cache_sections import read_section, write_section

    _, discovered = repos
    write_section(discovered, "names", {"commands": []}, tainted=True)
    assert read_section(discovered, "names") is None


def test_write_section_round_trips_and_leaves_others_cold(repos):
    """The untainted positive control, and per-section independence.

    One written section serves back exactly its payload; a section nobody
    wrote stays a miss without poisoning the written one.
    """
    from otto.config.cache_sections import read_section, write_section

    _, discovered = repos
    payload = {"commands": [{"name": "x", "help": None, "lab_free": True}]}
    write_section(discovered, "names", payload)
    assert read_section(discovered, "names") == payload
    assert read_section(discovered, "tests") is None


def test_unknown_section_names_raise_key_error(repos):
    """An unregistered name is a caller bug and must raise, never read as a miss."""
    from otto.config import completion_cache as cc
    from otto.config.cache_sections import section_by_name, write_section

    _, discovered = repos
    with pytest.raises(KeyError):
        section_by_name("no_such_section")
    with pytest.raises(KeyError):
        write_section(discovered, "no_such_section", {})
    with pytest.raises(KeyError):
        cc.read_sections(discovered, ["no_such_section"])


def test_a_miss_then_write_hashes_each_section_key_set_at_most_once(repos, monkeypatch):
    """The spec's never-compute-the-fingerprint-twice, as an executable claim.

    Seed a valid entry, invalidate the tests section by editing a nested
    file, then run the exact slow-path cycle entry() runs: the validity
    check computes each section's digest ONCE, and write_cache must store
    those digests instead of hashing every key set again.
    """
    from collections import Counter

    from otto.config import completion_cache as cc
    from otto.config.cache_sections import SECTIONS

    repo, discovered = repos
    cc.write_cache(discovered, [], [], [])
    nested = next(repo.rglob("sub*/test_*.py"))
    nested.write_text("def test_x():\n    pass\n\ndef test_y():\n    pass\n")

    counts: Counter = Counter()
    real_hash_file = cc.hash_file

    def counting(h, path):
        counts[path] += 1
        real_hash_file(h, path)

    monkeypatch.setattr(cc, "hash_file", counting)

    digests: dict[str, str] = {}
    assert cc.cache_rebuild_is_worthwhile(discovered, digests=digests) is True
    cc.write_cache(discovered, [], [], [], digests=digests)

    assert counts, "no hashing observed at all — the validity check never ran"
    budget: Counter = Counter()
    for section in SECTIONS:
        for path in set(section.key_paths(discovered)):
            budget[path] += 1
    over = {str(p): (counts[p], budget[p]) for p in counts if counts[p] > budget[p]}
    assert not over, f"paths hashed more often than once per owning section (got, allowed): {over}"


def test_a_third_registered_section_does_not_break_the_merged_view(repos, monkeypatch):
    """Registering a Section must not turn every read_cache into a permanent miss.

    read_cache/write_cache are the LEGACY merged-view pair over a FIXED
    membership. If the reader validated every registered section while the
    writer wrote only the merged two, appending a Section would make
    cache_rebuild_is_worthwhile True forever — entry() would pay the full
    O(corpus) collect and write on every invocation, with no test failing.
    """
    from otto.config import cache_sections as cs
    from otto.config import completion_cache as cc

    _, discovered = repos
    throwaway = cs.Section(
        name="throwaway",
        key_paths=lambda repos: [],
        collect=lambda repos: {},
    )
    monkeypatch.setattr(cs, "SECTIONS", [*cs.SECTIONS, throwaway])

    cc.write_cache(discovered, [], [], [])
    assert cc.cache_rebuild_is_worthwhile(discovered) is False, (
        "a freshly written cache reads as a permanent miss once a 3rd section is registered"
    )


def test_lab_file_edits_move_the_names_digest_only(tmp_path, monkeypatch):
    """The declared widening of the names key set: lab files belong to it.

    Hosts/labs payloads are served from the names section, so a lab.json
    edit must invalidate it exactly as it invalidated the monolithic
    fingerprint — and it has no business invalidating the tests section.
    The edit APPENDS (size change), so this cannot pass or fail on a
    same-clock-tick mtime coincidence.
    """
    from types import SimpleNamespace

    from otto.config.cache_sections import section_by_name, section_digest
    from tests._fixtures.labdata import json_lab_sources, write_lab_json
    from tests._fixtures.sutrepo import touch_settings

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    sut = tmp_path / "sut"
    lab = sut / "lab"
    lab.mkdir(parents=True)
    touch_settings(sut)
    lab_file = lab / "lab.json"
    write_lab_json(lab_file, [], declare_labs=True)
    repo = SimpleNamespace(
        sut_dir=sut,
        init=[],
        libs=[],
        tests=[],
        lab_sources=json_lab_sources(sut, [lab]),
        inventory_settings={},
    )
    names, tests = section_by_name("names"), section_by_name("tests")
    names_before = section_digest(names, [repo])
    tests_before = section_digest(tests, [repo])

    lab_file.write_text(lab_file.read_text() + "\n")

    assert section_digest(names, [repo]) != names_before, (
        "a lab.json edit must invalidate the names section (hosts are served from it)"
    )
    assert section_digest(tests, [repo]) == tests_before, (
        "a lab.json edit has no business invalidating the tests section"
    )


def test_write_cache_preserves_reserved_namespaces_and_drops_old_entries(repos):
    """Reserved ``__*__`` namespaces survive a sections write; pre-v15
    fingerprint-keyed entries do not — they can never be served again and
    would otherwise be parsed by every TAB forever."""
    from otto.config import completion_cache as cc

    _, discovered = repos
    cache_path = cc._cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    dead_key = "a" * 64
    cache_path.write_text(
        json.dumps(
            {
                dead_key: {"schema_version": 14, "generated_at": 0},
                cc.COLLECTED_TESTS_KEY: {
                    "fp": {"schema_version": 1, "generated_at": 0, "names": ["test_kept"]}
                },
            }
        )
    )

    cc.write_cache(discovered, [], [], [])

    data = json.loads(cache_path.read_text())
    assert cc.COLLECTED_TESTS_KEY in data, "reserved namespace clobbered by the sections writer"
    assert dead_key not in data, "dead pre-sections entry carried forward forever"
    assert data["schema"] == cc.SCHEMA_VERSION


def test_write_cache_split_matches_the_collectors(repos):
    """write_cache's payload split and the sections' collectors cannot drift.

    The registry's collect callables are the section-shaped rebuild path;
    the legacy write_cache keywords are the entry() path. If a new key lands
    in one and not the other, a consumer reading the section payload would
    silently lose it.
    """
    from otto.config import completion_cache as cc
    from otto.config.cache_sections import MERGED_VIEW_SECTIONS, section_by_name

    _, discovered = repos
    cc.write_cache(discovered, [], [], [])
    data = json.loads(cc._cache_path().read_text())
    # write_cache must write EVERY section the merged view then validates, or
    # its own output reads back as a permanent miss. The third-section guard
    # pins the pair from the read side and leaves this direction open: adding
    # a name to MERGED_VIEW_SECTIONS without a matching payload here is the
    # way that guard's invariant breaks next.
    assert set(data["sections"]) >= set(MERGED_VIEW_SECTIONS)
    keys_by_section: dict[str, set] = {}
    for name in ("names", "tests"):
        stored_keys = set(data["sections"][name]["payload"])
        collected_keys = set(section_by_name(name).collect(discovered))
        assert stored_keys == collected_keys, name
        keys_by_section[name] = stored_keys
    # The merged view flattens payloads with dict.update, so a key claimed by
    # two sections would resolve silently by SECTIONS order. Pin disjointness.
    section_names = list(keys_by_section)
    for i, a in enumerate(section_names):
        for b in section_names[i + 1 :]:
            overlap = keys_by_section[a] & keys_by_section[b]
            assert not overlap, f"sections {a!r} and {b!r} both claim {sorted(overlap)}"


def test_names_payload_keys_are_all_checked_or_delegated(repos):
    """Every key the names collector emits must be classified: checked or delegated.

    ``_cached_names_payload`` shape-checks ``RAW_ITERATED_NAMES_KEYS`` because
    they reach a raw iterator deep in click's help/completion pipeline, and
    DELEGATES every other key to a completer that does its own isinstance
    check with a live fallback (``tests``, the twelfth key overall, lives in
    its own section and never reaches this reader at all). The day a new key
    lands in the names collector, this test fails until its author decides
    which bucket it belongs in — checked or delegated — rather than the
    contract staying prose nobody re-reads.
    """
    from otto.cli.main import DELEGATED_NAMES_KEYS, RAW_ITERATED_NAMES_KEYS
    from otto.config.cache_sections import section_by_name

    _, discovered = repos
    assert (
        set(section_by_name("names").collect(discovered))
        == set(RAW_ITERATED_NAMES_KEYS) | DELEGATED_NAMES_KEYS
    )
    # Union alone lets a key sit in BOTH buckets and still satisfy the equality;
    # `test_write_cache_split_matches_the_collectors` pins disjointness across
    # sections, so pin it across buckets too.
    assert not set(RAW_ITERATED_NAMES_KEYS) & DELEGATED_NAMES_KEYS


def test_a_wholly_tainted_rewrite_of_an_identical_entry_is_skipped(repos):
    """A tainted entry is never served, so re-storing it byte-for-byte is cost.

    Task 5's skip exists because a write on a network filesystem needs a
    commit and invalidates client cache; without this arm a workspace with a
    broken init module would pay that on EVERY invocation, forever, since the
    entry it refuses to serve is exactly the entry it would write back.
    """
    from otto.config import completion_cache as cc

    _, discovered = repos
    cc.write_cache(discovered, [], [], [], tainted=True)
    cache = cc._cache_path()
    first = cache.stat().st_mtime_ns

    cc.write_cache(discovered, [], [], [], tainted=True)
    assert cache.stat().st_mtime_ns == first, "an identical tainted entry was rewritten"


def test_a_tainted_write_still_replaces_an_untainted_entry(repos):
    """THE OTHER HALF of the skip's condition, and it cannot be dropped.

    Stored-clean + writing-tainted + matching digest: the workspace was
    servable and no longer is. Skipping here would leave the CLEAN entry in
    place — and clean entries ARE served, so every later ``--help`` and TAB
    would answer from a snapshot of a workspace that no longer loads.
    Digest-matching is reachable without a file edit (a dependency or
    inventory that stops resolving), which is why the taint of the STORED
    entry is checked rather than inferred from the digest.
    """
    from otto.config import completion_cache as cc

    _, discovered = repos
    cc.write_cache(discovered, [], [], [])  # clean
    cache = cc._cache_path()
    before = json.loads(cache.read_text())["sections"]
    assert before["names"]["tainted"] is False

    cc.write_cache(discovered, [], [], [], tainted=True)
    after = json.loads(cache.read_text())["sections"]
    assert after["names"]["fingerprint"] == before["names"]["fingerprint"], (
        "the digests must MATCH, or this proves nothing about the taint arm"
    )
    assert after["names"]["tainted"] is True, "a clean entry survived a tainted write"
