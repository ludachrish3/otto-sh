"""``tickets.json`` — otto's first public coverage export."""

import json

import pytest

from otto.coverage.store.model import CoverageStore, TicketRecord
from otto.coverage.ticket_export import (
    TICKET_EXPORT_FORMAT,
    build_ticket_export,
    group_ranges,
    write_ticket_export,
)


def _store(tmp_path):
    """A store that is adversarial *by construction*, not by a scaffolding
    edit that gets reverted after a one-off RED-proof run.

    "a.c" / ticket "PROJ-1" is the original single-file/single-ticket setup.
    "0.c" / ticket "PROJ-9" is layered on deliberately so removing either
    ``sorted()`` call in ``build_ticket_export`` produces a real, provable
    ordering bug rather than a no-op:

    - "0.c" sorts *before* "a.c" in path order but is added to the store
      *second* (files added out of path-insertion order).
    - "PROJ-9" sorts *after* "PROJ-1", but since ``store.files()`` always
      walks in path-sorted order ("0.c" then "a.c"), the ticket-accumulator
      dict's natural encounter order is "PROJ-9" then "PROJ-1" — the
      REVERSE of the required sorted-by-id output order. Without
      ``sorted(per_ticket)``, PROJ-9 would wrongly sort first.
    """
    store = CoverageStore(tier_order=["unit"])
    rec = store.get_or_create_file(tmp_path / "a.c")
    for n in (1, 2, 3, 4):
        line = rec.get_or_create_line(n)
        line.ticket = ["PROJ-1"]
    rec.lines[1].hits.add("unit", 1)
    rec.lines[4].hits.add("unit", 1)

    rec2 = store.get_or_create_file(tmp_path / "0.c")
    rec2.get_or_create_line(1).ticket = ["PROJ-9"]

    # Ticket metadata added in reverse-id order too (store.tickets isn't
    # what drives the accumulator's encounter order above — that's driven
    # by store.files()'s path-sorted file walk — but reverse-id insertion
    # here matches the same adversarial spirit and costs nothing).
    store.tickets["PROJ-9"] = TicketRecord(id="PROJ-9", url="u/9", commits=["def"])
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url="u/1", commits=["abc"])
    return store


def _store_with_reordering_paths(tmp_path):
    """One ticket owning **both** ``a.c`` and ``a/b.c`` — proves the
    file-level ``sorted(per_ticket[ticket_id])`` guard in
    ``build_ticket_export`` is load-bearing, unlike ``_store`` above (every
    ticket there owns exactly one file, so re-sorting a single-element dict
    is a no-op and that call can be deleted with every existing test still
    green).

    ``store.files()`` (``CoverageStore.files``) sorts by ``FileRecord.path``
    — a ``Path`` — whose ``__lt__`` compares **parts tuples**:
    ``Path("a/b.c").parts == ("a", "b.c")`` sorts *before*
    ``Path("a.c").parts == ("a.c",)``, because ``"a"`` is a strict prefix of
    ``"a.c"``. The export's own contract (design §7: tickets sorted by id,
    "files by path", ranges ascending) means **string** order of the
    posix-relative display path instead, where ``.`` (0x2E) sorts before
    ``/`` (0x2F): ``"a.c" < "a/b.c"``. The two orders disagree, so walking
    ``store.files()`` encounters ``"a/b.c"`` before ``"a.c"`` — exactly
    backwards — and ``build_ticket_export`` needs its own ``sorted()`` call
    over the display strings to produce the documented order.
    """
    store = CoverageStore(tier_order=["unit"])
    # Created in Path-sort order (b.c first) so a REMOVED `sorted()` call
    # would surface the bug via dict insertion order, not accidentally
    # mask it by already inserting in the desired final order.
    rec_b = store.get_or_create_file(tmp_path / "a" / "b.c")
    rec_b.get_or_create_line(1).ticket = ["PROJ-7"]
    rec_a = store.get_or_create_file(tmp_path / "a.c")
    rec_a.get_or_create_line(1).ticket = ["PROJ-7"]
    store.tickets["PROJ-7"] = TicketRecord(id="PROJ-7", url=None, commits=["abc"])
    return store


def test_export_orders_files_within_a_ticket_by_display_path(tmp_path):
    """Pins the file-level ordering guard directly (see
    ``_store_with_reordering_paths``'s docstring for the ``Path.__lt__``
    vs. string-sort divergence this exploits): without
    ``sorted(per_ticket[ticket_id])``, a ticket owning both ``a.c`` and
    ``a/b.c`` would list them in ``store.files()``'s PATH-sorted order
    (``a/b.c`` then ``a.c``) — the reverse of the documented, required
    display-string order."""
    store = _store_with_reordering_paths(tmp_path)
    payload = build_ticket_export(
        store,
        repo_root=tmp_path,
        project="p",
        otto_version="0.8.0",
        generated="2026-07-26T00:00:00Z",
    )
    assert len(payload["tickets"]) == 1
    paths = [f["path"] for f in payload["tickets"][0]["files"]]
    assert paths == ["a.c", "a/b.c"] == sorted(paths)


def test_group_ranges_collapses_runs_and_keeps_singletons():
    assert group_ranges([142, 143, 144, 204, 219, 220]) == [[142, 144], [204, 204], [219, 220]]


def test_group_ranges_empty():
    assert group_ranges([]) == []


def test_export_has_its_own_format_version(tmp_path):
    payload = build_ticket_export(
        _store(tmp_path),
        repo_root=tmp_path,
        project="p",
        otto_version="0.8.0",
        generated="2026-07-26T00:00:00Z",
    )
    assert payload["format"] == TICKET_EXPORT_FORMAT == 1


def test_export_counts_and_missing_ranges(tmp_path):
    payload = build_ticket_export(
        _store(tmp_path),
        repo_root=tmp_path,
        project="p",
        otto_version="0.8.0",
        generated="2026-07-26T00:00:00Z",
    )
    ticket = payload["tickets"][0]
    assert ticket["id"] == "PROJ-1"
    assert ticket["url"] == "u/1"
    assert ticket["lines"] == {"owned": 4, "covered": 2, "uncovered": 2}
    assert ticket["files"][0]["missing"] == [[2, 3]]


def test_export_orders_tickets_by_id_and_files_by_path(tmp_path):
    """Explicit ordering invariant, independent of the byte round-trip in
    test_export_is_byte_deterministic below: ticket order must be
    sorted-by-id, not the store's own file-visit/ticket-encounter order
    (which for this fixture is the exact reverse — see `_store`'s
    docstring)."""
    payload = build_ticket_export(
        _store(tmp_path),
        repo_root=tmp_path,
        project="p",
        otto_version="0.8.0",
        generated="2026-07-26T00:00:00Z",
    )
    ids = [t["id"] for t in payload["tickets"]]
    assert ids == ["PROJ-1", "PROJ-9"] == sorted(ids)
    for ticket in payload["tickets"]:
        paths = [f["path"] for f in ticket["files"]]
        assert paths == sorted(paths)


def test_export_is_byte_deterministic(tmp_path):
    """Ordering regressions are invisible to field-by-field assertions.

    A same-process round trip of one fixed store can't, by itself,
    distinguish "sorted correctly every time" from "sorted wrong,
    consistently, every time" — both calls would emit identical bytes
    either way, since nothing about the store or the code varies between
    them. This test therefore also asserts the actual ticket order (which
    `_store`'s adversarial encounter order — see its docstring — makes a
    real, provable check, not a trivial one), not just that the two writes
    agree with each other.
    """
    store = _store(tmp_path)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    for path in (a, b):
        write_ticket_export(
            store,
            path,
            repo_root=tmp_path,
            project="p",
            otto_version="0.8.0",
            generated="2026-07-26T00:00:00Z",
        )
    assert a.read_bytes() == b.read_bytes()
    payload = json.loads(a.read_bytes())
    assert [t["id"] for t in payload["tickets"]] == ["PROJ-1", "PROJ-9"]


def test_export_without_tickets_fails_loud(tmp_path):
    """An empty file would read as 'no uncovered ticket work'."""
    with pytest.raises(ValueError, match=r"\[coverage.tickets\]"):
        build_ticket_export(
            CoverageStore(tier_order=["unit"]),
            repo_root=tmp_path,
            project="p",
            otto_version="0.8.0",
            generated="2026-07-26T00:00:00Z",
        )


# ── Task 14: synthetic (no ticket) / (uncommitted) rows ─────────────────────


def test_export_treats_sentinel_tickets_as_ordinary_entries_in_sorted_position(tmp_path):
    """`(uncommitted)` and `(no ticket)` must appear as ordinary ticket
    entries (the shipped compatibility-policy bullet says so verbatim) —
    sorted alongside real ids, not appended/prepended specially. Both
    sentinel strings start with `(` (0x28), which sorts before any
    alphanumeric ticket id, so on a repo with poor ticket discipline they
    legitimately float to the top — asserted here via plain `sorted()`,
    not a hardcoded position, so this would catch a future change to
    either literal string just as readily as an ordering bug."""
    store = CoverageStore(tier_order=["unit"])
    rec = store.get_or_create_file(tmp_path / "a.c")
    rec.get_or_create_line(1).ticket = ["PROJ-1"]
    rec.get_or_create_line(2).ticket = ["(no ticket)"]
    rec.get_or_create_line(3).ticket = ["(uncommitted)"]
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url="u/1", commits=["abc"])
    store.tickets["(no ticket)"] = TicketRecord(id="(no ticket)", url=None, commits=["def"])
    store.tickets["(uncommitted)"] = TicketRecord(id="(uncommitted)", url=None, commits=[])

    payload = build_ticket_export(
        store,
        repo_root=tmp_path,
        project="p",
        otto_version="0.8.0",
        generated="2026-07-26T00:00:00Z",
    )

    ids = [t["id"] for t in payload["tickets"]]
    assert ids == sorted(["PROJ-1", "(no ticket)", "(uncommitted)"])
    assert ids == ["(no ticket)", "(uncommitted)", "PROJ-1"]
    by_id = {t["id"]: t for t in payload["tickets"]}
    assert by_id["(no ticket)"]["url"] is None
    assert by_id["(uncommitted)"]["url"] is None
    assert payload["totals"]["owned"] == 3  # every sentinel line counted, none dropped


def test_export_succeeds_when_only_sentinel_tickets_attributed(tmp_path):
    """Task 14 changes the loud-fail gate's meaning: `store.tickets` is now
    non-empty whenever attribution ran at all (even if every commit matched
    no real ticket), not only when a real ticket was found. A repo with
    zero ticket-referencing commits must still export successfully —
    raising here would be the exact regression the brief warns about."""
    store = CoverageStore(tier_order=["unit"])
    rec = store.get_or_create_file(tmp_path / "a.c")
    rec.get_or_create_line(1).ticket = ["(no ticket)"]
    store.tickets["(no ticket)"] = TicketRecord(id="(no ticket)", url=None, commits=["abc"])

    payload = build_ticket_export(
        store,
        repo_root=tmp_path,
        project="p",
        otto_version="0.8.0",
        generated="2026-07-26T00:00:00Z",
    )

    assert [t["id"] for t in payload["tickets"]] == ["(no ticket)"]
    assert payload["totals"] == {"owned": 1, "covered": 0, "uncovered": 1}


def test_export_totals_dedupe_a_line_owned_by_two_tickets(tmp_path):
    """Fix round 1: `totals` must count each PHYSICAL line once, never once
    per ticket that names it — the same rule `spa_data.py`'s
    `_build_ticket_summaries` already enforces for `tickets_totals`
    (`test_tickets_totals_dedupes_a_line_owned_by_two_tickets` in
    test_spa_data.py), mirrored here with the IDENTICAL fixture numbers so
    the two layers can be compared directly.

    Line 1 is claimed by both PROJ-1 and PROJ-2 and is hit; line 2 is
    claimed only by PROJ-2 and is not hit. A summing implementation
    (the bug) reads owned=3 (1 + 2), covered=2 (1 + 1) — the deduped truth
    is owned=2, covered=1, since line 1 is exactly one physical line no
    matter how many tickets claim it. An overlap-free fixture (every other
    test in this file) cannot tell a summing implementation from a
    deduping one, which is exactly why nothing here caught this."""
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(tmp_path / "a.c")
    shared = record.get_or_create_line(1)
    shared.ticket = ["PROJ-1", "PROJ-2"]
    shared.hits.add("unit", 1)
    solo = record.get_or_create_line(2)
    solo.ticket = ["PROJ-2"]
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url=None, commits=["a"])
    store.tickets["PROJ-2"] = TicketRecord(id="PROJ-2", url=None, commits=["b"])

    payload = build_ticket_export(
        store,
        repo_root=tmp_path,
        project="p",
        otto_version="0.8.0",
        generated="2026-07-26T00:00:00Z",
    )

    assert payload["totals"] == {"owned": 2, "covered": 1, "uncovered": 1}
    # Per-ticket rows are UNCHANGED by this — they still overlap/sum to
    # more than the deduped totals above, exactly like the tickets page's
    # caption warns about for `tickets_totals` vs. the per-ticket rows.
    by_id = {t["id"]: t for t in payload["tickets"]}
    assert by_id["PROJ-1"]["lines"]["owned"] == 1
    assert by_id["PROJ-2"]["lines"]["owned"] == 2


def test_export_paths_are_repo_relative(tmp_path):
    """Public-export paths must be repo-relative posix, not absolute: two CI
    runners with different workspace roots must emit identical bytes for
    identical coverage, and an external consumer has no way to map an
    absolute, machine-specific path onto their own checkout."""
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    store = CoverageStore(tier_order=["unit"])
    rec = store.get_or_create_file(repo_root / "src" / "a.c")
    rec.get_or_create_line(1).ticket = ["PROJ-1"]
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url=None, commits=["abc"])

    payload = build_ticket_export(
        store,
        repo_root=repo_root,
        project="p",
        otto_version="0.8.0",
        generated="2026-07-26T00:00:00Z",
    )
    path = payload["tickets"][0]["files"][0]["path"]
    assert path == "src/a.c"
    assert not path.startswith("/")
    assert str(tmp_path) not in path
