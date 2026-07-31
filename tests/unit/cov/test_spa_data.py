"""Tests for spa_data: IndexPayload/FileChunk emission for the coverage SPA."""

import json
import re
from pathlib import Path

from otto.coverage.renderer.spa_data import (
    build_index_payload,
    emit_chunks,
    make_stamp,
    mangle_path,
)
from otto.coverage.store.model import CoverageStore, OverrideRecord, Thresholds, TicketRecord


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _find_file(node: dict, name: str) -> dict | None:
    for f in node["files"]:
        if f["name"] == name:
            return f
    for d in node["dirs"]:
        found = _find_file(d, name)
        if found is not None:
            return found
    return None


class TestMakeStampAndManglePath:
    def test_make_stamp_matches_expected_shape(self):
        stamp = make_stamp()
        assert re.match(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$", stamp)

    def test_mangle_path_replaces_separators_and_strips_leading_underscore(self, tmp_path):
        mangled = mangle_path(tmp_path / "a" / "b.c")
        assert "/" not in mangled
        assert "\\" not in mangled
        assert not mangled.startswith("_")


class TestIndexPayload:
    def test_format_and_stamp_and_config_keys(self, tmp_path):
        store = CoverageStore(tier_order=["system", "manual"])
        store.thresholds = Thresholds(high=90, medium=75)
        payload = build_index_payload(
            store,
            project_name="Proj",
            prefix=None,
            stamp="20260725T140200Z-1a2b3c4d",
        )
        assert payload["format"] == 2
        assert payload["stamp"] == "20260725T140200Z-1a2b3c4d"
        assert payload["thresholds"] == {"high": 90.0, "medium": 75.0}
        assert payload["stat_types"] == ["line", "branch", "decision"]
        assert payload["state_colors"]["stale"] == "violet"

    def test_empty_store_tree_is_bare_root(self, tmp_path):
        store = CoverageStore(tier_order=["system"])
        payload = build_index_payload(store, project_name="Empty", prefix=None, stamp="S")
        tree = payload["tree"]
        assert tree["name"] == "Empty"
        assert tree["dirs"] == []
        assert tree["files"] == []
        assert tree["stats"]["lines"] == {
            "total": 0,
            "hit": 0,
            "per_tier": {"system": 0},
            "asserted_per_tier": {"system": 0},
            "asserted_only": 0,
        }
        assert payload["total_lines"] == 0

    def test_zero_line_file_still_appears_in_tree(self, tmp_path):
        src = _write(tmp_path, "empty.c", "")
        store = CoverageStore(tier_order=["system"])
        store.get_or_create_file(src)  # no lines added
        payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
        node = _find_file(payload["tree"], "empty.c")
        assert node is not None
        assert node["stats"]["lines"] == {
            "total": 0,
            "hit": 0,
            "per_tier": {"system": 0},
            "asserted_per_tier": {"system": 0},
            "asserted_only": 0,
        }


class TestTreeRollupAndCtxLines:
    def test_root_totals_nested_dirs_and_ctx_lines(self, tmp_path):
        x = _write(tmp_path, "a/x.c", "int a;\nint b;\n")
        y = _write(tmp_path, "a/b/y.c", "int c;\nint d;\n")
        store = CoverageStore(tier_order=["system"])
        run_r1 = store.add_run(tier="system", label="r1")

        fr_x = store.get_or_create_file(x)
        for lineno in (1, 2):
            lr = fr_x.get_or_create_line(lineno)
            lr.hits.add("system", 1)
            lr.run_hits[run_r1] = 1

        fr_y = store.get_or_create_file(y)
        lr_hit = fr_y.get_or_create_line(1)
        lr_hit.hits.add("system", 1)
        lr_hit.run_hits[run_r1] = 1
        fr_y.get_or_create_line(2)  # miss, no run credit

        payload = build_index_payload(store, project_name="Proj", prefix=tmp_path, stamp="S")
        root = payload["tree"]

        assert root["name"] == "Proj"
        assert root["stats"]["lines"]["total"] == 4
        assert root["stats"]["lines"]["hit"] == 3
        assert root["stats"]["ctx_lines"]["r1"] == 3

        dir_a = next(d for d in root["dirs"] if d["name"] == "a")
        assert any(d["name"] == "b" for d in dir_a["dirs"])
        dir_b = next(d for d in dir_a["dirs"] if d["name"] == "b")
        assert dir_b["stats"]["lines"]["total"] == 2
        assert dir_b["stats"]["lines"]["hit"] == 1


class TestRunContrib:
    def test_lines_revoked_and_top_files_sorted_desc(self, tmp_path):
        x = _write(tmp_path, "a.c", "int a;\nint b;\n")
        y = _write(tmp_path, "b.c", "int c;\n")
        store = CoverageStore(tier_order=["system"])
        run_id = store.add_run(tier="system", label="r1")
        other_run = store.add_run(tier="system", label="r2")

        fr_x = store.get_or_create_file(x)
        l1 = fr_x.get_or_create_line(1)
        l1.hits.add("system", 1)
        l1.run_hits[run_id] = 1
        l2 = fr_x.get_or_create_line(2)
        l2.state = "stale"
        l2.stale_runs.append(run_id)

        fr_y = store.get_or_create_file(y)
        l3 = fr_y.get_or_create_line(1)
        l3.hits.add("system", 1)
        l3.run_hits[run_id] = 1

        payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
        contrib = payload["run_contrib"][str(run_id)]
        assert contrib["lines"] == 2
        assert contrib["revoked"] == 1
        assert contrib["files"] == [["a.c", 1], ["b.c", 1]]

        other_contrib = payload["run_contrib"][str(other_run)]
        assert other_contrib == {"lines": 0, "revoked": 0, "files": []}


class TestDisplayPathPrefixStripAndFallback:
    def test_prefix_strips_matching_path(self, tmp_path):
        src = _write(tmp_path, "product/main.c", "int a;\n")
        store = CoverageStore(tier_order=["system"])
        store.get_or_create_file(src)
        payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
        node = _find_file(payload["tree"], "main.c")
        assert node is not None
        assert node["path"] == "product/main.c"

    def test_file_outside_prefix_falls_back_to_full_path(self, tmp_path):
        inside_dir = tmp_path / "repo"
        src = _write(tmp_path, "elsewhere/b.c", "int b;\n")
        store = CoverageStore(tier_order=["system"])
        store.get_or_create_file(src)
        payload = build_index_payload(store, project_name="P", prefix=inside_dir, stamp="S")
        node = _find_file(payload["tree"], "b.c")
        assert node is not None
        assert node["path"] == str(src)


class TestOutOfRangeLines:
    def test_emit_chunks_handles_line_past_eof_without_crash(self, tmp_path):
        src = _write(tmp_path, "f.c", "a;\nb;\nc;\n")
        store = CoverageStore(tier_order=["system"])
        fr = store.get_or_create_file(src)
        fr.get_or_create_line(999).state = "stale"

        out_dir = tmp_path / "report"
        emit_chunks(
            store,
            out_dir,
            project_name="P",
            prefix=None,
            extra_markers=None,
            stamp="S",
        )

        chunk_path = out_dir / "cov_data" / "files" / f"{mangle_path(src)}.js"
        text = chunk_path.read_text()
        assert '"999"' in text

        payload = build_index_payload(store, project_name="P", prefix=None, stamp="S")
        assert payload["tree"]["stats"]["lines"]["total"] == 1
        assert payload["tree"]["stats"]["flags"]["stale"] == 1


class TestEmitChunks:
    def test_index_js_is_classic_assignment(self, tmp_path):
        src = _write(tmp_path, "f.c", "int a;\n")
        store = CoverageStore(tier_order=["system"])
        store.get_or_create_file(src)
        out_dir = tmp_path / "report"
        emit_chunks(
            store,
            out_dir,
            project_name="P",
            prefix=None,
            extra_markers=None,
            stamp="S",
        )
        text = (out_dir / "cov_data" / "index.js").read_text()
        assert text.startswith("window.__OTTO_COV__ = {")
        assert text.endswith("};\n")
        # the slice between them is valid JSON
        json.loads(text[len("window.__OTTO_COV__ = ") : -2])

    def test_file_chunk_wraps_call_and_matches_store_line_json(self, tmp_path):
        src = _write(tmp_path, "f.c", "int a;\nint b;\nint c;\n")
        store = CoverageStore(tier_order=["system", "manual"])
        run_a = store.add_run(tier="system", label="r1")
        run_b = store.add_run(tier="manual", label="r2")
        fr = store.get_or_create_file(src)

        lr1 = fr.get_or_create_line(1)
        lr1.hits.add("system", 3)
        lr1.run_hits[run_a] = 3

        lr2 = fr.get_or_create_line(2)
        lr2.state = "stale"
        lr2.stale_runs.append(run_b)

        fr.get_or_create_line(3)  # no run hits, no stale runs

        out_dir = tmp_path / "report"
        emit_chunks(
            store,
            out_dir,
            project_name="P",
            prefix=None,
            extra_markers=None,
            stamp="S",
        )

        chunk_path = out_dir / "cov_data" / "files" / f"{mangle_path(src)}.js"
        text = chunk_path.read_text()
        assert text.startswith("window.__OTTO_COV_FILE__(")
        assert text.endswith(");\n")
        body = text[len("window.__OTTO_COV_FILE__(") : -len(");\n")]
        chunk = json.loads(body)

        expected_lines = fr.to_dict()["lines"]
        for lineno_str in ("1", "2", "3"):
            assert chunk["lines"][lineno_str] == expected_lines[lineno_str]
        assert "run" in chunk["lines"]["1"]
        assert "run" not in chunk["lines"]["2"]
        assert "run" not in chunk["lines"]["3"]
        assert "stale_run" in chunk["lines"]["2"]
        assert "stale_run" not in chunk["lines"]["1"]
        assert "stale_run" not in chunk["lines"]["3"]

    def test_excluded_lines_annotated_on_store(self, tmp_path):
        src = _write(tmp_path, "f.c", "int a;\nint b; // LCOV_EXCL_LINE\nint c;\n")
        store = CoverageStore(tier_order=["system"])
        fr = store.get_or_create_file(src)
        fr.get_or_create_line(1).hits.add("system", 1)

        out_dir = tmp_path / "report"
        emit_chunks(
            store,
            out_dir,
            project_name="P",
            prefix=None,
            extra_markers=None,
            stamp="S",
        )

        assert fr.excluded_lines == {2}

    def test_empty_store_still_writes_index_js(self, tmp_path):
        store = CoverageStore(tier_order=["system"])
        out_dir = tmp_path / "report"
        emit_chunks(
            store,
            out_dir,
            project_name="P",
            prefix=None,
            extra_markers=None,
            stamp="S",
        )
        assert (out_dir / "cov_data" / "index.js").exists()


def _ticket_store(tmp_path):
    """Two lines owned by PROJ-1, one of them hit."""
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(tmp_path / "a.c")
    first = record.get_or_create_line(1)
    first.ticket = ["PROJ-1"]
    first.hits.add("unit", 1)
    second = record.get_or_create_line(2)
    second.ticket = ["PROJ-1"]
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url="u/1", commits=["abc"])
    return store


def test_index_payload_carries_ticket_summaries(tmp_path):
    payload = build_index_payload(
        _ticket_store(tmp_path), project_name="P", prefix=tmp_path, stamp="S"
    )
    assert payload["tickets"] == [
        {
            "id": "PROJ-1",
            "url": "u/1",
            "owned": 2,
            "covered": 1,
            "uncovered": 1,
            "per_tier": {"unit": 1},
            "asserted": {"unit": 0},
            "chunk": payload["tickets"][0]["chunk"],
        }
    ]
    assert payload["tickets"][0]["chunk"]


def test_ticket_chunks_are_emitted_per_ticket(tmp_path):
    out = tmp_path / "report"
    out.mkdir()
    emit_chunks(
        _ticket_store(tmp_path),
        out,
        project_name="P",
        prefix=tmp_path,
        extra_markers=None,
        stamp="S",
    )
    chunks = sorted((out / "cov_data" / "tickets").iterdir())
    assert len(chunks) == 1
    assert chunks[0].read_text().startswith("window.__OTTO_COV_TICKET__(")


def test_no_tickets_emits_empty_list_and_no_chunk_dir(tmp_path):
    store = CoverageStore(tier_order=["unit"])
    store.get_or_create_file(tmp_path / "a.c").get_or_create_line(1)
    out = tmp_path / "report"
    out.mkdir()
    payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
    emit_chunks(store, out, project_name="P", prefix=tmp_path, extra_markers=None, stamp="S")
    assert payload["tickets"] == []
    assert not (out / "cov_data" / "tickets").exists()


def test_line_json_carries_ticket_ids_and_omits_empty(tmp_path):
    out = tmp_path / "report"
    out.mkdir()
    emit_chunks(
        _ticket_store(tmp_path),
        out,
        project_name="P",
        prefix=tmp_path,
        extra_markers=None,
        stamp="S",
    )
    text = next((out / "cov_data" / "files").iterdir()).read_text()
    assert '"ticket": ["PROJ-1"]' in text

    plain = CoverageStore(tier_order=["unit"])
    plain.get_or_create_file(tmp_path / "b.c").get_or_create_line(1)
    out2 = tmp_path / "report2"
    out2.mkdir()
    emit_chunks(plain, out2, project_name="P", prefix=tmp_path, extra_markers=None, stamp="S")
    assert '"ticket"' not in next((out2 / "cov_data" / "files").iterdir()).read_text()


def test_tickets_totals_dedupes_a_line_owned_by_two_tickets(tmp_path):
    """Design spec §2/§6.1: overlap is the normal case — a line named by two
    tickets must count ONCE in the repo-truth totals, never once per ticket.

    Fixture is built so a summing implementation and a deduping one produce
    DIFFERENT numbers (line 1 is claimed by both PROJ-1 and PROJ-2 and is
    hit): summed-across-tickets would read owned=3 (2 + 1), covered=2 (1 +
    1); the deduped truth is owned=2, covered=1 — line 1 counted once.
    """
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(tmp_path / "a.c")
    shared = record.get_or_create_line(1)
    shared.ticket = ["PROJ-1", "PROJ-2"]
    shared.hits.add("unit", 1)
    solo = record.get_or_create_line(2)
    solo.ticket = ["PROJ-2"]
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url=None, commits=["a"])
    store.tickets["PROJ-2"] = TicketRecord(id="PROJ-2", url=None, commits=["b"])

    payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")

    assert payload["tickets_totals"] == {
        "owned": 2,
        "covered": 1,
        "uncovered": 1,
        "per_tier": {"unit": 1},
        "asserted": {"unit": 0},
    }
    # The per-ticket rows are UNCHANGED by this — they still overlap/sum to
    # more than the deduped card, which is exactly what the UI caption warns
    # about (design §2).
    by_id = {t["id"]: t for t in payload["tickets"]}
    assert by_id["PROJ-1"]["owned"] == 1
    assert by_id["PROJ-2"]["owned"] == 2


def test_tickets_totals_is_zero_when_no_tickets(tmp_path):
    store = CoverageStore(tier_order=["unit"])
    store.get_or_create_file(tmp_path / "a.c").get_or_create_line(1)
    payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
    assert payload["tickets_totals"] == {
        "owned": 0,
        "covered": 0,
        "uncovered": 0,
        "per_tier": {"unit": 0},
        "asserted": {"unit": 0},
    }


def test_sentinel_ticket_ids_flow_through_summaries_and_get_their_own_chunk(tmp_path):
    """Task 14: `(no ticket)`/`(uncommitted)` are ordinary ids by the time
    they reach spa_data.py — `_build_ticket_summaries` doesn't know or care
    what a ticket id string looks like, so a sentinel-owned line must
    appear in the index rollup and get its own per-ticket chunk exactly
    like any real ticket id."""
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(tmp_path / "a.c")
    record.get_or_create_line(1).ticket = ["(no ticket)"]
    record.get_or_create_line(2).ticket = ["(uncommitted)"]
    store.tickets["(no ticket)"] = TicketRecord(id="(no ticket)", url=None, commits=["abc"])
    store.tickets["(uncommitted)"] = TicketRecord(id="(uncommitted)", url=None, commits=[])

    payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
    ids = {t["id"]: t for t in payload["tickets"]}
    assert set(ids) == {"(no ticket)", "(uncommitted)"}
    assert ids["(no ticket)"]["url"] is None
    assert ids["(uncommitted)"]["url"] is None

    out = tmp_path / "report"
    out.mkdir()
    emit_chunks(store, out, project_name="P", prefix=tmp_path, extra_markers=None, stamp="S")
    chunk_files = sorted((out / "cov_data" / "tickets").iterdir())
    assert len(chunk_files) == 2


def test_tickets_totals_counts_a_sentinel_owned_line_once(tmp_path):
    """Same dedup rule `test_tickets_totals_dedupes_a_line_owned_by_two_tickets`
    pins for two real tickets sharing a line must also hold for a
    sentinel-owned line — it is still exactly one attributed line, counted
    once, never once per (nonexistent) additional owner."""
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(tmp_path / "a.c")
    line = record.get_or_create_line(1)
    line.ticket = ["(no ticket)"]
    line.hits.add("unit", 1)
    store.tickets["(no ticket)"] = TicketRecord(id="(no ticket)", url=None, commits=["abc"])

    payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")

    assert payload["tickets_totals"] == {
        "owned": 1,
        "covered": 1,
        "uncovered": 0,
        "per_tier": {"unit": 1},
        "asserted": {"unit": 0},
    }


def test_ticket_chunk_carries_the_report_stamp(tmp_path):
    """Design §5: every data chunk carries the report stamp so a stamp
    mismatch renders the guard screen instead of silently showing stale
    data — ticket chunks were missing this."""
    out = tmp_path / "report"
    out.mkdir()
    emit_chunks(
        _ticket_store(tmp_path),
        out,
        project_name="P",
        prefix=tmp_path,
        extra_markers=None,
        stamp="the-stamp",
    )
    chunk_path = next((out / "cov_data" / "tickets").iterdir())
    assert '"stamp": "the-stamp"' in chunk_path.read_text()


def _two_file_two_tier_ticket_store(tmp_path):
    """PROJ-1 owns lines across two files, each covered by a DIFFERENT tier.

    The fixture property that makes failure possible: a per-file per-tier
    count must differ both from the ticket-wide per-tier rollup (which sums
    to one hit in each tier) and from the other file's (a.c is unit-only,
    b.c is system-only). A single-file or single-tier fixture would pass
    against a rollup wrongly copied onto every file.
    """
    store = CoverageStore(tier_order=["unit", "system"])
    a = store.get_or_create_file(tmp_path / "a.c")
    a_hit = a.get_or_create_line(1)
    a_hit.ticket = ["PROJ-1"]
    a_hit.hits.add("unit", 1)
    a_missing = a.get_or_create_line(2)
    a_missing.ticket = ["PROJ-1"]
    b = store.get_or_create_file(tmp_path / "b.c")
    b_hit = b.get_or_create_line(1)
    b_hit.ticket = ["PROJ-1"]
    b_hit.hits.add("system", 1)
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url=None, commits=["abc"])
    return store


def test_ticket_chunk_files_carry_per_tier_covered_counts(tmp_path):
    """Design §6.1 gap: without this the frontend can only decline to a
    single aggregate row under ticket scope, because a TicketChunk carried
    owned/covered counts but no tier breakdown per file."""
    out = tmp_path / "report"
    out.mkdir()
    emit_chunks(
        _two_file_two_tier_ticket_store(tmp_path),
        out,
        project_name="P",
        prefix=tmp_path,
        extra_markers=None,
        stamp="S",
    )
    text = min((out / "cov_data" / "tickets").iterdir()).read_text()
    body = text[text.index("(") + 1 : text.rindex(")")]
    _chunk_id, payload = json.loads(f"[{body}]")

    by_path = {f["path"]: f for f in payload["files"]}
    assert by_path["a.c"]["per_tier"] == {"unit": 1, "system": 0}
    assert by_path["b.c"]["per_tier"] == {"unit": 0, "system": 1}


class TestOverrideProvenance:
    """Format-2 additions: `LineJson.asserted`, `IndexPayload.overrides`,
    `Stats.lines.asserted_per_tier`/`asserted_only`, and per-ticket
    `asserted` counts (manual-overrides spec §3/§5/§9)."""

    def test_data_format_is_2(self, tmp_path):
        store = CoverageStore(tier_order=["system"])
        payload = build_index_payload(store, project_name="P", prefix=None, stamp="S")
        assert payload["format"] == 2

    def test_line_json_carries_asserted_map_and_omits_when_empty(self, tmp_path):
        out = tmp_path / "report"
        out.mkdir()
        store = CoverageStore(tier_order=["bench"])
        fr = store.get_or_create_file(tmp_path / "a.c")
        asserted_line = fr.get_or_create_line(1)
        asserted_line.hits.add("bench", 1)
        asserted_line.asserted = {"bench": [0]}
        fr.get_or_create_line(2)  # plain line

        emit_chunks(store, out, project_name="P", prefix=tmp_path, extra_markers=None, stamp="S")
        chunk_path = out / "cov_data" / "files" / f"{mangle_path(tmp_path / 'a.c')}.js"
        text = chunk_path.read_text()
        body = text[len("window.__OTTO_COV_FILE__(") : -len(");\n")]
        chunk = json.loads(body)

        assert chunk["lines"]["1"]["asserted"] == {"bench": [0]}
        assert "asserted" not in chunk["lines"]["2"]

    def test_index_payload_carries_overrides_table_in_id_order(self, tmp_path):
        store = CoverageStore(tier_order=["bench"])
        store.overrides.append(
            OverrideRecord(id=1, tier="bench", key="ticket:PROJ-2", reason="second", as_of=None)
        )
        store.overrides.append(
            OverrideRecord(
                id=0, tier="bench", key="ticket:PROJ-1", reason="first", as_of="2026-07-01"
            )
        )
        payload = build_index_payload(store, project_name="P", prefix=None, stamp="S")
        assert payload["overrides"] == [
            {
                "id": 0,
                "tier": "bench",
                "key": "ticket:PROJ-1",
                "reason": "first",
                "as_of": "2026-07-01",
            },
            {"id": 1, "tier": "bench", "key": "ticket:PROJ-2", "reason": "second", "as_of": None},
        ]

    def test_stats_roll_up_asserted_per_tier(self, tmp_path):
        src = _write(tmp_path, "a.c", "int a;\nint b;\nint c;\n")
        store = CoverageStore(tier_order=["bench"])
        fr = store.get_or_create_file(src)
        for lineno in (1, 2):
            lr = fr.get_or_create_line(lineno)
            lr.hits.add("bench", 1)
            lr.asserted = {"bench": [0]}
        really_hit = fr.get_or_create_line(3)
        really_hit.hits.add("bench", 1)

        payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
        node = _find_file(payload["tree"], "a.c")
        assert node["stats"]["lines"]["asserted_per_tier"]["bench"] == 2
        assert payload["tree"]["stats"]["lines"]["asserted_per_tier"]["bench"] == 2

    def test_asserted_only_requires_every_hitting_tier_be_asserted(self, tmp_path):
        """A line hit for real in one tier and merely asserted in another
        must NOT count toward `asserted_only` — only a line whose every
        hitting tier is asserted counts."""
        src = _write(tmp_path, "a.c", "int a;\nint b;\n")
        store = CoverageStore(tier_order=["bench", "unit"])
        fr = store.get_or_create_file(src)

        mixed = fr.get_or_create_line(1)
        mixed.hits.add("bench", 1)  # real hit, unasserted
        mixed.hits.add("unit", 1)
        mixed.asserted = {"unit": [0]}  # only unit is asserted

        pure = fr.get_or_create_line(2)
        pure.hits.add("bench", 1)
        # Zero-count tier entry (review finding F3): `pure.hits.counts` now
        # has a "unit" key present with count 0, pinning that the
        # asserted_only filter reads `n > 0` (a zero-count entry is not a
        # hitting tier) rather than merely `tier in lr.hits.counts` (which a
        # zero-count key would wrongly satisfy) — the line must still count.
        pure.hits.add("unit", 0)
        pure.asserted = {"bench": [0]}  # bench is asserted, and it's the only hitting tier

        payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
        node = _find_file(payload["tree"], "a.c")
        assert node["stats"]["lines"]["asserted_only"] == 1
        assert payload["tree"]["stats"]["lines"]["asserted_only"] == 1

    def test_ticket_summary_and_totals_carry_asserted(self, tmp_path):
        store = CoverageStore(tier_order=["bench"])
        record = store.get_or_create_file(tmp_path / "a.c")
        asserted_line = record.get_or_create_line(1)
        asserted_line.ticket = ["PROJ-1"]
        asserted_line.hits.add("bench", 1)
        asserted_line.asserted = {"bench": [0]}
        plain_line = record.get_or_create_line(2)
        plain_line.ticket = ["PROJ-1"]
        store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url=None, commits=["abc"])

        payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
        summary = next(t for t in payload["tickets"] if t["id"] == "PROJ-1")
        assert summary["asserted"] == {"bench": 1}
        assert payload["tickets_totals"]["asserted"] == {"bench": 1}

    def test_tickets_totals_dedupes_an_asserted_line_owned_by_two_tickets(self, tmp_path):
        """Review finding F2: a single-owner fixture can't distinguish the
        totals-dedup path from the per-ticket path — a line asserted for one
        tier and owned by TWO tickets must count ONCE in the deduped
        `tickets_totals["asserted"]`, while the two per-ticket `asserted` rows
        (which deliberately attribute the line to both owners) sum to TWO —
        the same dedup contract `per_tier`/`owned`/`covered` already have,
        now pinned for `asserted` specifically."""
        store = CoverageStore(tier_order=["bench"])
        record = store.get_or_create_file(tmp_path / "a.c")
        shared = record.get_or_create_line(1)
        shared.ticket = ["PROJ-1", "PROJ-2"]
        shared.hits.add("bench", 1)
        shared.asserted = {"bench": [0]}
        store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url=None, commits=["a"])
        store.tickets["PROJ-2"] = TicketRecord(id="PROJ-2", url=None, commits=["b"])

        payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")

        assert payload["tickets_totals"]["asserted"] == {"bench": 1}
        by_id = {t["id"]: t for t in payload["tickets"]}
        assert by_id["PROJ-1"]["asserted"] == {"bench": 1}
        assert by_id["PROJ-2"]["asserted"] == {"bench": 1}
        summed = sum(by_id[t]["asserted"]["bench"] for t in ("PROJ-1", "PROJ-2"))
        assert summed == 2
