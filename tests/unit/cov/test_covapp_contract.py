"""Python side of the covapp Python<->TypeScript contract.

The frontend re-declares, by hand and with no compiler in between, the whole
shape of what this package emits (``web/src/covapp/types.ts``): the
data-format version, every payload's keys, the closed per-line state
vocabularies, the sentinel ticket ids as TS string literals, the ``window``
callback names the emitted chunk scripts invoke, and the ``cov_data/``
directory layout the bundle fetches from. ``tests/_fixtures/covapp_contract.json`` is the shared
table both sides assert against; the TS half lives in
``web/src/covapp/contract.test.ts``. A drift on either side fails exactly
one of the two suites and names the language that moved.

Keys here are read off **real emitted payloads** rather than a list copied
into the test, so adding a field to the emitter without updating the
contract (and therefore without updating the frontend) fails immediately.
"""

import json
from pathlib import Path

import pytest

from otto.coverage.attribution import NO_TICKET, UNCOMMITTED_TICKET
from otto.coverage.colors import STATE_COLORS
from otto.coverage.renderer.spa_data import (
    OTTO_COV_DATA_FORMAT,
    build_index_payload,
    emit_chunks,
)
from otto.coverage.store.model import (
    BranchHits,
    CoverageStore,
    OverrideRecord,
    RunRecord,
    TicketRecord,
)

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "_fixtures" / "covapp_contract.json").read_text()
)


def _ticket_store(tmp_path: Path) -> CoverageStore:
    """Two lines owned by PROJ-1, one hit — enough to emit every ticket key."""
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(tmp_path / "a.c")
    first = record.get_or_create_line(1)
    first.ticket = ["PROJ-1"]
    first.hits.add("unit", 1)
    second = record.get_or_create_line(2)
    second.ticket = ["PROJ-1"]
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url="u/1", commits=["abc"])
    return store


def test_emitted_data_format_matches_the_contract(tmp_path):
    """The version the covapp refuses to render a mismatch of.

    ``dataGuard()`` compares ``index.format`` against the bundle's
    ``EXPECTED_DATA_FORMAT`` and shows GuardScreen instead of the report when
    they differ — so a Python-side bump with no TS-side bump ships a report
    that renders nothing, and a TS-side bump alone rejects every report otto
    has ever emitted. The two constants only ever said "bump both together or
    never" in prose, which is precisely the kind of rule this repo has
    watched decay. Read off a real emitted payload, not off the constant, so
    an emitter that stops writing the field fails here too.
    """
    payload = build_index_payload(
        _ticket_store(tmp_path), project_name="P", prefix=tmp_path, stamp="S"
    )
    remedy = (
        "A format bump must move ALL THREE together: OTTO_COV_DATA_FORMAT "
        "(src/otto/coverage/renderer/spa_data.py), EXPECTED_DATA_FORMAT "
        "(web/src/covapp/types.ts), and data_format in this fixture. Moving one "
        "ships either a report the covapp guard-screens or a bundle that rejects "
        "every report otto has already emitted."
    )
    assert payload["format"] == CONTRACT["data_format"], (
        f"emitted index format {payload['format']} != contract {CONTRACT['data_format']}. {remedy}"
    )
    # Not implied by the line above only in one case, but a real one: an
    # emitter that inlines a literal equal to the contract while the named
    # constant moves on, leaving OTTO_COV_DATA_FORMAT decorative.
    assert CONTRACT["data_format"] == OTTO_COV_DATA_FORMAT, (
        f"OTTO_COV_DATA_FORMAT is {OTTO_COV_DATA_FORMAT} but the contract says "
        f"{CONTRACT['data_format']}. {remedy}"
    )


def test_sentinel_ids_match_the_contract():
    assert sorted([NO_TICKET, UNCOMMITTED_TICKET]) == sorted(CONTRACT["sentinel_ticket_ids"])


def test_emitted_ticket_summary_keys_match_the_contract(tmp_path):
    payload = build_index_payload(
        _ticket_store(tmp_path), project_name="P", prefix=tmp_path, stamp="S"
    )
    assert sorted(payload["tickets"][0]) == CONTRACT["ticket_summary_keys"]


def test_emitted_ticket_totals_keys_match_the_contract(tmp_path):
    payload = build_index_payload(
        _ticket_store(tmp_path), project_name="P", prefix=tmp_path, stamp="S"
    )
    assert sorted(payload["tickets_totals"]) == CONTRACT["ticket_totals_keys"]


def _emit_ticket_chunk(tmp_path: Path) -> dict:
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
    text = min((out / "cov_data" / "tickets").iterdir()).read_text()
    # Unwrap the classic-script call wrapper, whose two arguments are the
    # chunk id and the payload, to get at the payload's own keys.
    body = text[text.index("(") + 1 : text.rindex(")")]
    _chunk_id, payload = json.loads(f"[{body}]")
    return payload


def test_emitted_ticket_chunk_keys_match_the_contract(tmp_path):
    assert sorted(_emit_ticket_chunk(tmp_path)) == CONTRACT["ticket_chunk_keys"]


def test_emitted_ticket_chunk_file_keys_match_the_contract(tmp_path):
    chunk = _emit_ticket_chunk(tmp_path)
    assert sorted(chunk["files"][0]) == CONTRACT["ticket_chunk_file_keys"]


def test_emitted_scripts_invoke_the_contract_callback_names(tmp_path):
    """The chunk scripts are plain classic scripts; the callback name IS the API."""
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
    cov_data = out / "cov_data"
    callbacks = CONTRACT["chunk_callbacks"]

    assert (cov_data / "index.js").read_text().startswith(f"window.{callbacks['index']} =")
    ticket_js = min((cov_data / "tickets").iterdir()).read_text()
    assert ticket_js.startswith(f"window.{callbacks['ticket']}(")
    file_js = min((cov_data / "files").iterdir()).read_text()
    assert file_js.startswith(f"window.{callbacks['file']}(")


# ── The rest of the payload surface ───────────────────────────────────────────


def _rich_store(tmp_path: Path) -> CoverageStore:
    """A store that emits EVERY key in the contract, optional ones included.

    `LineJson`'s `run` / `stale_run` / `ticket` / `asserted` are omitted when
    empty, so a minimal store would silently under-assert the key set — the
    contract would then describe less than the emitter produces, which is the
    drift it exists to catch.
    """
    (tmp_path / "a.c").write_text("int main(void) { return 0; }\nint x;\n")
    store = CoverageStore(tier_order=["unit"])
    store.runs.append(
        RunRecord(
            id=0,
            tier="unit",
            label="dut1",
            board="dut1",
            tester={"name": "chris", "email": "c@example.com"},
        )
    )
    store.overrides.append(
        OverrideRecord(id=0, tier="unit", key="ticket:PROJ-1", reason="why", as_of="2026-01-01")
    )
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url="u/1", commits=["abc"])
    record = store.get_or_create_file(tmp_path / "a.c")
    line = record.get_or_create_line(1)
    line.hits.add("unit", 1)
    line.run_hits[0] = 1
    line.stale_runs = [0]
    line.ticket = ["PROJ-1"]
    line.asserted = {"unit": [0]}
    line.state = "stale"
    branch = BranchHits(block=0, branch=0)
    branch.hits.add("unit", 1)
    branch.reachable["unit"] = True
    line.branches.append(branch)
    return store


def _rich_index(tmp_path: Path) -> dict:
    return build_index_payload(_rich_store(tmp_path), project_name="P", prefix=tmp_path, stamp="S")


def _rich_file_chunk(tmp_path: Path) -> dict:
    out = tmp_path / "report"
    out.mkdir()
    emit_chunks(
        _rich_store(tmp_path),
        out,
        project_name="P",
        prefix=tmp_path,
        extra_markers=None,
        stamp="S",
    )
    text = min((out / "cov_data" / "files").iterdir()).read_text()
    return json.loads(text[text.index("(") + 1 : text.rindex(")")])


def test_coverage_state_vocabulary_matches_the_contract(tmp_path):
    """The closed set of per-line states, read off the emitted payload.

    `types.ts` types it as `Record<CoverageState, string>` — a CLOSED union
    hand-copied from `STATE_COLORS`. Adding a fifth state Python-side left the
    TS record silently short, and `test_colors.py` only ever validated the
    colour VALUES.
    """
    payload = _rich_index(tmp_path)
    assert sorted(payload["state_colors"]) == CONTRACT["coverage_states"]
    assert sorted(STATE_COLORS) == CONTRACT["coverage_states"], (
        "the emitter must be passing STATE_COLORS through, not rebuilding it"
    )


@pytest.mark.parametrize(
    ("contract_key", "path"),
    [
        ("index_payload_keys", ()),
        ("run_json_keys", ("runs", 0)),
        ("override_json_keys", ("overrides", 0)),
        ("stats_keys", ("tree", "stats")),
        ("line_stat_bucket_keys", ("tree", "stats", "lines")),
        ("stat_bucket_keys", ("tree", "stats", "branches")),
        ("dir_node_keys", ("tree",)),
        ("file_node_keys", ("tree", "files", 0)),
        ("thresholds_keys", ("thresholds",)),
    ],
)
def test_index_payload_shapes_match_the_contract(tmp_path, contract_key, path):
    """Every hand-mirrored shape in the index payload, not just the ticket ones.

    Only `tickets` / `tickets_totals` were pinned before, so adding or
    renaming a top-level key — or a key on any nested node — was undetected on
    the Python side and a compile error nobody would see on the TS side.
    """
    node = _rich_index(tmp_path)
    for step in path:
        node = node[step]
    assert sorted(node) == CONTRACT[contract_key]


def test_run_contrib_entry_keys_match_the_contract(tmp_path):
    payload = _rich_index(tmp_path)
    entry = next(iter(payload["run_contrib"].values()), None)
    # A real contribution, not just a pre-seeded empty row: run_contrib is
    # keyed off store.runs, so it is truthy whenever ANY run exists.
    assert entry is not None, payload["run_contrib"]
    assert entry["lines"], payload["run_contrib"]
    assert entry["files"], payload["run_contrib"]
    assert sorted(entry) == CONTRACT["run_contrib_keys"]


def test_file_chunk_and_line_shapes_match_the_contract(tmp_path):
    """`LineJson` carries a SECOND copy of the state vocabulary, so it drifts twice."""
    chunk = _rich_file_chunk(tmp_path)
    assert sorted(chunk) == CONTRACT["file_chunk_keys"]
    line = next(iter(chunk["lines"].values()))
    assert sorted(line) == CONTRACT["line_json_keys"]


def test_emitted_cov_data_layout_matches_the_contract(tmp_path):
    """The directory names `data.ts` and `covapp.html` fetch by hand.

    `./cov_data/index.js`, `./cov_data/files/`, `./cov_data/tickets/` are
    hard-coded on the TS side against this emitter, and were caught only
    incidentally by the browser lane driving a whole real report.
    """
    out = tmp_path / "report"
    out.mkdir()
    emit_chunks(
        _rich_store(tmp_path),
        out,
        project_name="P",
        prefix=tmp_path,
        extra_markers=None,
        stamp="S",
    )
    layout = CONTRACT["cov_data_layout"]
    cov_data = out / "cov_data"
    assert (cov_data / layout["index"]).is_file()
    assert (cov_data / layout["files_dir"]).is_dir()
    assert (cov_data / layout["tickets_dir"]).is_dir()


def test_branch_and_tester_shapes_match_the_contract(tmp_path):
    """Two shapes the emitter DELEGATES, so a key sweep of its own dicts misses them.

    `BranchRecord.to_dict()` and `RunRecord.tester` are hand-mirrored in
    `types.ts` like everything else, and a rename downgrades every branch
    marker / renders "—" for every run's tester with nothing failing.
    """
    payload = _rich_index(tmp_path)
    assert payload["runs"][0]["tester"], "positive control: the run must carry a tester"
    assert sorted(payload["runs"][0]["tester"]) == CONTRACT["tester_keys"]

    chunk = _rich_file_chunk(tmp_path)
    line = next(iter(chunk["lines"].values()))
    assert line["branches"], "positive control: the line must carry a branch"
    assert sorted(line["branches"][0]) == CONTRACT["branch_json_keys"]


def test_stats_flags_keys_match_the_contract(tmp_path):
    """`Stats.flags` is a nested literal, so pinning `stats_keys` only named it."""
    payload = _rich_index(tmp_path)
    assert sorted(payload["tree"]["stats"]["flags"]) == CONTRACT["stats_flags_keys"]


def test_line_state_domain_matches_the_contract(tmp_path):
    """`LineJson.state` is a SECOND, DIFFERENT vocabulary from CoverageState.

    `"stale" | "aging" | null` — narrower than the four tinted states — so
    `coverage_states` does not cover it, and the emitted value has to be in
    the set the TS union declares.
    """
    chunk = _rich_file_chunk(tmp_path)
    line = next(iter(chunk["lines"].values()))
    assert line["state"] is not None, "positive control: emit a non-null state"
    assert line["state"] in CONTRACT["line_states"]
