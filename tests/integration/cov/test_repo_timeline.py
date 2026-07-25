"""Aging-repo scenarios (spec §10) — movement, whitespace, EOL."""

from pathlib import Path

import pytest

from tests._fixtures._repo_timeline import RepoTimeline

SRC = "int a(void)\n{\n    run();\n    more();\n}\n"


@pytest.fixture
def tl(tmp_path: Path) -> RepoTimeline:
    t = RepoTimeline(tmp_path / "repo")
    t.write("a.c", SRC)
    t.commit("base")
    return t


class TestLineMovement:
    def test_insert_above_shifts_credits(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 4: 2}})
        tl.write("a.c", "/* new */\n/* new */\n" + SRC)
        tl.commit("insert above")
        d = tl.dispositions(tl.fold(), "a.c")
        assert d == {5: "hit", 6: "hit"}

    def test_delete_above_shifts_credits_up(self, tl):
        tl.write("a.c", "/* hdr */\n" + SRC)
        tl.commit("with header")
        tl.capture("run1", {"a.c": {4: 5}})
        tl.write("a.c", SRC)
        tl.commit("drop header")
        assert tl.dispositions(tl.fold(), "a.c") == {3: "hit"}

    def test_edit_inside_revokes_only_edited_lines(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 4: 2}})
        tl.write("a.c", "int a(void)\n{\n    changed();\n    more();\n}\n")
        tl.commit("edit line 3")
        d = tl.dispositions(tl.fold(), "a.c")
        assert d[3] == "stale"
        assert d[4] == "hit"


class TestWhitespaceImmunity:
    """Spec §8.1: whitespace/EOL changes NEVER revoke."""

    def test_reindent_keeps_all_credits(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 4: 2}})
        tl.write("a.c", "int a(void)\n{\n        run();\n        more();\n}\n")
        tl.commit("reformat")
        assert tl.dispositions(tl.fold(), "a.c") == {3: "hit", 4: "hit"}

    def test_crlf_conversion_keeps_all_credits(self, tl):
        tl.capture("run1", {"a.c": {3: 5}})
        (tl.root / "a.c").write_bytes(SRC.replace("\n", "\r\n").encode())
        tl.commit("crlf")
        assert tl.dispositions(tl.fold(), "a.c")[3] == "hit"

    def test_mixed_ws_and_real_edit_revokes_only_real(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 4: 2}})
        tl.write("a.c", "int a(void)\n{\n        run();\n    other();\n}\n")
        tl.commit("reindent 3, edit 4")
        d = tl.dispositions(tl.fold(), "a.c")
        assert d[3] == "hit"
        assert d[4] == "stale"


class TestFileIdentity:
    def test_clean_rename_follows(self, tl):
        tl.capture("run1", {"a.c": {3: 5}})
        tl.git("mv", "a.c", "b.c")
        tl.commit("mv")
        assert tl.dispositions(tl.fold(), "b.c")[3] == "hit"

    def test_rename_plus_edit_follows_unchanged_lines(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 4: 2}})
        tl.git("mv", "a.c", "b.c")
        tl.write("b.c", "int a(void)\n{\n    run();\n    edited();\n}\n")
        tl.commit("mv+edit")
        store = tl.fold()
        d = tl.dispositions(store, "b.c")
        assert d[3] == "hit"
        # The revoked line's stale marker follows the rename: it lands on the
        # NEW path at the base-coordinate line number (an approximate anchor,
        # same convention as in-place edits). The old path carries nothing.
        assert d[4] == "stale"
        assert tl.dispositions(store, "a.c") == {}

    def test_deleted_file_goes_stale_not_crash(self, tl):
        tl.capture("run1", {"a.c": {3: 5}})
        tl.git("rm", "-q", "a.c")
        tl.commit("rm")
        assert tl.dispositions(tl.fold(), "a.c")[3] == "stale"


class TestHistoryShape:
    def test_squash_merge_survival_via_blob(self, tl, tmp_path):
        """Spec §10: capture on branch → squash → branch deleted → gc → credits live."""
        tl.git("checkout", "-qb", "feature")
        tl.write("a.c", SRC + "int extra(void)\n{\n}\n")
        tl.commit("feature work")
        tl.capture("run1", {"a.c": {3: 5, 6: 1}})
        tl.git("checkout", "-q", "main")
        tl.git("merge", "--squash", "-q", "feature")
        tl.commit("squashed")
        tl.git("branch", "-qD", "feature")
        tl.git("reflog", "expire", "--expire=now", "--all")
        tl.git("gc", "--prune=now", "-q")
        d = tl.dispositions(tl.fold(), "a.c")
        assert d[3] == "hit"
        assert d[6] == "hit"

    def test_base_gone_and_blob_changed_is_stale_not_crash(self, tl):
        tl.git("checkout", "-qb", "feature")
        tl.write("a.c", "int a(void)\n{\n    feature_only();\n}\n")
        tl.commit("feature")
        tl.capture("run1", {"a.c": {3: 7}})
        tl.git("checkout", "-q", "main")  # main never sees the feature blob
        tl.git("branch", "-qD", "feature")
        tl.git("reflog", "expire", "--expire=now", "--all")
        tl.git("gc", "--prune=now", "-q")
        assert tl.dispositions(tl.fold(), "a.c")[3] == "stale"

    def test_shallow_clone_degrades_with_hint(self, tl, tmp_path, caplog):
        import logging

        tl.capture("run1", {"a.c": {3: 5}})
        tl.write("a.c", SRC + "// trailing\n")
        tl.commit("second")
        clone_root = tmp_path / "shallow"
        tl.git("clone", "-q", "--depth", "1", f"file://{tl.root}", str(clone_root))
        shallow = RepoTimeline.__new__(RepoTimeline)  # adopt existing clone
        shallow.root = clone_root
        shallow.captures = tl.captures
        with caplog.at_level(logging.WARNING):
            store = shallow.fold()
        assert "shallow clone" in caplog.text
        # Content still matches at line 3? blob fast-path may still save it:
        # the capture blob is unreachable in a depth-1 clone, so this stays
        # stale — pin the degradation, not a miracle.
        assert shallow.dispositions(store, "a.c")[3] == "stale"

    def test_revert_resurrects_credits(self, tl):
        """Spec §8.3: pinned so nobody 'fixes' resurrection away."""
        tl.capture("run1", {"a.c": {3: 5}})
        tl.write("a.c", "int a(void)\n{\n    other();\n    more();\n}\n")
        tl.commit("break it")
        assert tl.dispositions(tl.fold(), "a.c")[3] == "stale"
        tl.write("a.c", SRC)
        tl.commit("revert")
        assert tl.dispositions(tl.fold(), "a.c")[3] == "hit"


class TestTime:
    def test_aging_boundary_is_strictly_greater(self, tl):
        from datetime import datetime, timezone

        tl.capture("run1", {"a.c": {3: 5}}, captured_at="2026-06-01T00:00:00Z")
        at_limit = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)  # exactly 30d
        past_limit = datetime(2026, 7, 2, 0, 0, 0, tzinfo=timezone.utc)  # 31d
        assert tl.dispositions(tl.fold(max_age_days=30, today=at_limit), "a.c")[3] == "hit"
        d = tl.dispositions(tl.fold(max_age_days=30, today=past_limit), "a.c")
        assert d[3] == "hit"  # aging lines still carry hits...
        store = tl.fold(max_age_days=30, today=past_limit)
        rec = store.get_or_create_file(tl.root / "a.c")
        assert rec.lines[3].state == "aging"  # ...but the state marks them

    def test_future_dated_capture_is_not_aging(self, tl):
        from datetime import datetime, timezone

        tl.capture("run1", {"a.c": {3: 5}}, captured_at="2027-01-01T00:00:00Z")
        today = datetime(2026, 7, 1, tzinfo=timezone.utc)
        store = tl.fold(max_age_days=30, today=today)
        assert store.get_or_create_file(tl.root / "a.c").lines[3].state is None


class TestMultiCapture:
    def test_overlap_one_valid_one_stale_no_double_count(self, tl):
        tl.capture("old", {"a.c": {3: 5}}, host="bench-1")
        tl.write("a.c", "int a(void)\n{\n    run();\n    other();\n}\n")  # line 4 changes
        tl.commit("edit 4")
        tl.capture("new", {"a.c": {3: 2, 4: 9}}, host="bench-2", captured_at="2026-07-02T00:00:00Z")
        store = tl.fold()
        rec = store.get_or_create_file(tl.root / "a.c")
        assert rec.lines[3].hits.total() == 7  # 5 + 2, both credited once
        assert len(rec.lines[3].run_hits) == 2

    def test_supersede_visible_end_to_end(self, tl):
        from otto.coverage.capture.supersede import select_manual_captures

        tl.capture("run1", {"a.c": {3: 5}}, captured_at="2026-06-01T00:00:00Z")
        tl.capture("run1", {"a.c": {3: 9}}, captured_at="2026-07-01T00:00:00Z")
        winners = select_manual_captures(tl.captures)
        assert len(winners) == 1
        assert winners[0].captured_at == "2026-07-01T00:00:00Z"

    def test_wrong_repo_capture_warns_and_stales(self, tl, caplog):
        import logging

        tl.capture("run1", {"never/existed.c": {1: 3}})
        with caplog.at_level(logging.WARNING):
            store = tl.fold()
        assert "unverifiable" in caplog.text
        assert tl.dispositions(store, "never/existed.c")[1] == "stale"


class TestContentOddities:
    def test_non_utf8_source_folds_without_crash(self, tl):
        (tl.root / "l1.c").write_bytes(b"int a;\n/* caf\xe9 */\nint b;\n")
        tl.commit("latin1")
        tl.capture("run1", {"l1.c": {1: 2, 3: 1}})
        assert tl.dispositions(tl.fold(), "l1.c") == {1: "hit", 3: "hit"}

    def test_hits_past_eof_after_shrink_stay_stale_records(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 5: 2}})
        tl.write("a.c", "int a(void)\n{\n    run();\n")  # 3 lines now: old 4-5 deleted in-hunk
        tl.commit("shrink")
        d = tl.dispositions(tl.fold(), "a.c")
        assert d[3] == "hit"
        assert d[5] == "stale"


def test_golden_mixed_timeline(tmp_path):
    """Months of history in one run: every mechanism at once, aggregate sanity."""
    tl = RepoTimeline(tmp_path / "repo")
    tl.write("core.c", SRC)
    tl.write("util.c", "int u(void)\n{\n    helper();\n}\n")
    tl.commit("v1")
    tl.capture(
        "bring-up", {"core.c": {3: 4, 4: 4}, "util.c": {3: 8}}, captured_at="2026-01-10T00:00:00Z"
    )
    # month 2: reformat core (must not revoke), rename util (must follow)
    tl.write("core.c", SRC.replace("    ", "\t"))
    tl.git("mv", "util.c", "helpers.c")
    tl.commit("v2 reformat+rename")
    # month 3: real edit to core line 4; fresh re-capture of core only
    tl.write("core.c", "int a(void)\n{\n\trun();\n\tredone();\n}\n")
    tl.commit("v3 edit")
    tl.capture("bring-up", {"core.c": {3: 6, 4: 6}}, captured_at="2026-03-01T00:00:00Z")
    from otto.coverage.capture.supersede import select_manual_captures

    tl.captures = select_manual_captures(tl.captures)
    assert len(tl.captures) == 1  # same label+host: month-3 capture superseded month-1
    store = tl.fold()
    assert tl.dispositions(store, "core.c") == {3: "hit", 4: "hit"}
    # the superseded January capture no longer credits helpers.c at all:
    assert tl.dispositions(store, "helpers.c") == {}
