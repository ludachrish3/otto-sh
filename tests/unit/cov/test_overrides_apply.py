"""apply_asserted_entries: line-set resolution, as_of bounding, provenance, prune logs."""

import logging
from pathlib import Path

import pytest

from otto.coverage.overrides import AssertedEntry, OverrideConfigError, apply_asserted_entries
from otto.coverage.store.model import CoverageStore

SHA_NEW, SHA_MID, SHA_OLD = "n" * 40, "m" * 40, "o" * 40
FP_INDEX = {SHA_NEW: 0, SHA_MID: 1, SHA_OLD: 2}


def _store(tmp_path: Path, lines: dict[int, list[str]]) -> CoverageStore:
    """A store with one file a.c; *lines* maps lineno -> tiers already hit."""
    store = CoverageStore(tier_order=["bench"])
    rec = store.get_or_create_file(tmp_path / "a.c")
    for lineno, tiers in lines.items():
        lr = rec.get_or_create_line(lineno)
        for tier in tiers:
            lr.hits.add(tier, 1)
    return store


def _apply(store, tmp_path, entries, per_line_sha, ticket_commits=None):
    apply_asserted_entries(
        store,
        entries,
        repo_root=tmp_path,
        per_line_sha=per_line_sha,
        ticket_commits=ticket_commits or {},
        fp_index=FP_INDEX,
        path=tmp_path / "coverage-overrides.toml",
    )


def _line(store, tmp_path, lineno):
    return store.get_or_create_file(tmp_path / "a.c").lines[lineno]


def test_commit_entry_asserts_its_unhit_lines(tmp_path):
    store = _store(tmp_path, {1: [], 2: []})
    entry = AssertedEntry(id=0, tier="bench", reason="r", commit=SHA_MID)
    _apply(store, tmp_path, [entry], {"a.c": {1: SHA_MID, 2: SHA_NEW}})
    assert _line(store, tmp_path, 1).hits.for_tier("bench") == 1
    assert _line(store, tmp_path, 1).asserted == {"bench": [0]}
    assert _line(store, tmp_path, 2).hits.for_tier("bench") == 0
    assert _line(store, tmp_path, 2).asserted == {}
    (ov,) = store.overrides
    assert (ov.id, ov.key, ov.tier) == (0, f"commit:{SHA_MID}", "bench")


def test_already_hit_line_gets_no_mark_and_no_extra_hit(tmp_path):
    store = _store(tmp_path, {1: ["bench"]})
    entry = AssertedEntry(id=0, tier="bench", reason="r", commit=SHA_MID)
    _apply(store, tmp_path, [entry], {"a.c": {1: SHA_MID}})
    assert _line(store, tmp_path, 1).hits.for_tier("bench") == 1
    assert _line(store, tmp_path, 1).asserted == {}


def test_hit_in_another_tier_still_gets_asserted_in_its_own(tmp_path):
    store = _store(tmp_path, {1: ["unit"]})
    store.register_tier("unit")
    entry = AssertedEntry(id=0, tier="bench", reason="r", commit=SHA_MID)
    _apply(store, tmp_path, [entry], {"a.c": {1: SHA_MID}})
    assert _line(store, tmp_path, 1).asserted == {"bench": [0]}


def test_ticket_entry_respects_as_of_bound(tmp_path):
    store = _store(tmp_path, {1: [], 2: []})
    entry = AssertedEntry(id=0, tier="bench", reason="r", ticket="#1", as_of=SHA_MID)
    _apply(
        store,
        tmp_path,
        [entry],
        {"a.c": {1: SHA_OLD, 2: SHA_NEW}},  # line 2's commit is NEWER than as_of
        ticket_commits={"#1": [SHA_OLD, SHA_NEW]},
    )
    assert _line(store, tmp_path, 1).asserted == {"bench": [0]}
    assert _line(store, tmp_path, 2).asserted == {}  # after as_of: not blessed


def test_as_of_not_in_first_parent_history_fails_loud(tmp_path):
    store = _store(tmp_path, {1: []})
    entry = AssertedEntry(id=0, tier="bench", reason="r", ticket="#1", as_of="x" * 40)
    with pytest.raises(OverrideConfigError, match="first-parent"):
        _apply(store, tmp_path, [entry], {"a.c": {1: SHA_OLD}}, {"#1": [SHA_OLD]})


def test_ticket_with_no_commit_at_or_before_as_of_fails_loud(tmp_path):
    store = _store(tmp_path, {1: []})
    entry = AssertedEntry(id=0, tier="bench", reason="r", ticket="#1", as_of=SHA_MID)
    with pytest.raises(OverrideConfigError, match="at/before"):
        _apply(store, tmp_path, [entry], {"a.c": {1: SHA_NEW}}, {"#1": [SHA_NEW]})


def test_two_entries_same_line_both_ref_one_hit(tmp_path):
    store = _store(tmp_path, {1: []})
    entries = [
        AssertedEntry(id=0, tier="bench", reason="a", commit=SHA_MID),
        AssertedEntry(id=1, tier="bench", reason="b", ticket="#1", as_of=SHA_NEW),
    ]
    _apply(store, tmp_path, entries, {"a.c": {1: SHA_MID}}, {"#1": [SHA_MID]})
    assert _line(store, tmp_path, 1).asserted == {"bench": [0, 1]}
    assert _line(store, tmp_path, 1).hits.for_tier("bench") == 1


def test_fully_aged_out_entry_logs_prune_signal(tmp_path, caplog):
    store = _store(tmp_path, {1: []})
    entry = AssertedEntry(id=0, tier="bench", reason="old bench pass", commit=SHA_OLD)
    with caplog.at_level(logging.INFO, logger="otto.coverage.overrides"):
        _apply(store, tmp_path, [entry], {"a.c": {1: SHA_NEW}})
    assert "fully aged out" in caplog.text
    assert f"commit:{SHA_OLD}" in caplog.text
    assert "old bench pass" in caplog.text
    assert len(store.overrides) == 1  # inert entries still listed


def test_fully_covered_entry_logs_prune_signal(tmp_path, caplog):
    store = _store(tmp_path, {1: ["bench"]})
    entry = AssertedEntry(id=0, tier="bench", reason="r", commit=SHA_MID)
    with caplog.at_level(logging.INFO, logger="otto.coverage.overrides"):
        _apply(store, tmp_path, [entry], {"a.c": {1: SHA_MID}})
    assert "fully covered" in caplog.text


def test_contributing_entry_logs_nothing(tmp_path, caplog):
    store = _store(tmp_path, {1: []})
    entry = AssertedEntry(id=0, tier="bench", reason="r", commit=SHA_MID)
    with caplog.at_level(logging.INFO, logger="otto.coverage.overrides"):
        _apply(store, tmp_path, [entry], {"a.c": {1: SHA_MID}})
    assert "prune" not in caplog.text


def test_snapshot_ordering_entry_a_hit_does_not_hide_line_from_entry_b(tmp_path, caplog):
    """Entry 1's added hit must not make entry 2 read 'fully covered'."""
    store = _store(tmp_path, {1: []})
    entries = [
        AssertedEntry(id=0, tier="bench", reason="a", commit=SHA_MID),
        AssertedEntry(id=1, tier="bench", reason="b", commit=SHA_MID),
    ]
    with caplog.at_level(logging.INFO, logger="otto.coverage.overrides"):
        _apply(store, tmp_path, entries, {"a.c": {1: SHA_MID}})
    assert "fully covered" not in caplog.text
    assert _line(store, tmp_path, 1).asserted == {"bench": [0, 1]}
