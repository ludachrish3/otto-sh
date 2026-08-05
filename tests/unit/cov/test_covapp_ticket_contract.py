"""Python side of the per-ticket Python<->TypeScript contract.

The frontend re-declares, by hand and with no compiler in between, four
things this package owns: the data-format version, the keys of the ticket
payloads (``web/src/covapp/types.ts``), the sentinel ticket ids (as TS
string literals), and the ``window`` callback names the emitted chunk
scripts invoke. ``tests/_fixtures/covapp_ticket_contract.json`` is the shared
table both sides assert against; the TS half lives in
``web/src/covapp/contract.test.ts``. A drift on either side fails exactly
one of the two suites and names the language that moved.

Keys here are read off **real emitted payloads** rather than a list copied
into the test, so adding a field to the emitter without updating the
contract (and therefore without updating the frontend) fails immediately.
"""

import json
from pathlib import Path

from otto.coverage.attribution import NO_TICKET, UNCOMMITTED_TICKET
from otto.coverage.renderer.spa_data import (
    OTTO_COV_DATA_FORMAT,
    build_index_payload,
    emit_chunks,
)
from otto.coverage.store.model import CoverageStore, TicketRecord

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "_fixtures" / "covapp_ticket_contract.json").read_text()
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
