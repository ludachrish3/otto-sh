"""Override file end-to-end: asserted coverage, as_of bounding, reattribution."""

import json
import logging
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import blob_sha, head_commit
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.capture.store_dir import write_manual_capture
from otto.coverage.overrides import DEFAULT_OVERRIDES_RELPATH, load_override_config
from otto.coverage.reporter import run_coverage_report
from otto.coverage.store.model import STORE_FORMAT_VERSION
from otto.coverage.ticket_export import build_ticket_export
from otto.coverage.tickets import build_ticket_spec
from otto.coverage.tiers import load_tiers
from tests._fixtures.gitrepo import TmpGitRepo

COV = {
    "tiers": {
        "seed": {"kind": "manual", "precedence": 1},
        "bench": {"kind": "manual", "precedence": 2},
    },
    "tickets": {"pattern": "#(?P<n>[0-9]+)"},
}

SPEC = build_ticket_spec("#(?P<n>[0-9]+)", None)


def _mk_repo(tmp_path: Path):
    """An empty hermetic repo on `main`, plus its bound git runner."""
    sut = TmpGitRepo(tmp_path / "sut")
    return sut.root, sut.git


def _seed_lines(repo: Path, rel: str, linenos: list[int]) -> None:
    """Materialize *linenos* in the store, uncovered, via a 0-hit capture."""
    cap = Capture(
        tier="seed",
        base_commit=head_commit(repo),
        captured_at="2026-07-01T00:00:00Z",
        board="b1",
        files={
            rel: CaptureFileCov(blob=blob_sha(repo, Path(rel)), lines=dict.fromkeys(linenos, 0))
        },
    )
    write_manual_capture(cap, repo)


def _overrides(repo: Path, text: str) -> None:
    path = repo / DEFAULT_OVERRIDES_RELPATH
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)


async def _report(repo: Path, out: Path):
    tier_configs = load_tiers(COV)
    return await run_coverage_report(
        [],
        out,
        repo_root=repo,
        tier_configs=tier_configs,
        ticket_spec=SPEC,
        overrides=load_override_config(COV, repo, tier_configs),
    )


def _file_rec(store, name: str):
    (rec,) = [f for f in store.files() if f.path.name == name]
    return rec


@pytest.mark.asyncio
async def test_asserted_lines_count_in_their_tier_and_carry_provenance(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\nl2\nl3\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    sha = head_commit(repo)
    _seed_lines(repo, "a.c", [1, 2, 3])
    _overrides(repo, f'[[bench]]\nticket = "#1"\nas_of = "{sha}"\nreason = "legacy pass"\n')
    store = await _report(repo, tmp_path / "r")
    rec = _file_rec(store, "a.c")
    for n in (1, 2, 3):
        assert rec.lines[n].hits.for_tier("bench") == 1
        assert rec.lines[n].asserted == {"bench": [0]}
    (ov,) = store.overrides
    assert (ov.key, ov.tier, ov.as_of) == ("ticket:#1", "bench", sha)
    assert json.loads((tmp_path / "r" / "store.json").read_text())["format"] == STORE_FORMAT_VERSION


@pytest.mark.asyncio
async def test_commit_after_as_of_under_same_ticket_stays_uncovered(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\nl2\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    as_of = head_commit(repo)
    (repo / "a.c").write_text("l1\nl2\nl3\n")
    git("commit", "-aqm", "more work #1")
    _seed_lines(repo, "a.c", [1, 2, 3])
    _overrides(repo, f'[[bench]]\nticket = "#1"\nas_of = "{as_of}"\nreason = "r"\n')
    store = await _report(repo, tmp_path / "r")
    rec = _file_rec(store, "a.c")
    assert rec.lines[1].asserted == {"bench": [0]}
    assert rec.lines[2].asserted == {"bench": [0]}
    assert rec.lines[3].asserted == {}
    assert rec.lines[3].hits.for_tier("bench") == 0


@pytest.mark.asyncio
async def test_rewritten_line_drops_out_whitespace_edit_survives(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\nl2\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    as_of = head_commit(repo)
    (repo / "a.c").write_text("REWRITTEN\n    l2\n")  # l1 rewritten, l2 reindented
    git("commit", "-aqm", "later edit, no ticket")
    _seed_lines(repo, "a.c", [1, 2])
    _overrides(repo, f'[[bench]]\nticket = "#1"\nas_of = "{as_of}"\nreason = "r"\n')
    store = await _report(repo, tmp_path / "r")
    rec = _file_rec(store, "a.c")
    assert rec.lines[1].asserted == {}  # superseded — aged out by content
    assert rec.lines[2].asserted == {"bench": [0]}  # -w: whitespace never re-attributes


@pytest.mark.asyncio
async def test_reattribution_reaches_store_tickets(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\n")
    git("add", "-A")
    git("commit", "-qm", "fix #1")
    sha = head_commit(repo)
    _seed_lines(repo, "a.c", [1])
    _overrides(repo, f'[[reattribute]]\ncommit = "{sha}"\ntickets = ["#9"]\nreason = "wrong id"\n')
    store = await _report(repo, tmp_path / "r")
    rec = _file_rec(store, "a.c")
    assert rec.lines[1].ticket == ["#9"]
    assert "#9" in store.tickets
    assert "#1" not in store.tickets


@pytest.mark.asyncio
async def test_real_manual_run_clears_the_asserted_mark(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\nl2\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    sha = head_commit(repo)
    _seed_lines(repo, "a.c", [1, 2])
    # A real bench-tier capture proves line 1.
    cap = Capture(
        tier="bench",
        base_commit=head_commit(repo),
        captured_at="2026-07-02T00:00:00Z",
        board="b1",
        files={"a.c": CaptureFileCov(blob=blob_sha(repo, Path("a.c")), lines={1: 3})},
    )
    write_manual_capture(cap, repo)
    _overrides(repo, f'[[bench]]\nticket = "#1"\nas_of = "{sha}"\nreason = "r"\n')
    store = await _report(repo, tmp_path / "r")
    rec = _file_rec(store, "a.c")
    assert rec.lines[1].hits.for_tier("bench") == 3  # the real run's count, untouched
    assert rec.lines[1].asserted == {}  # proven — no mark
    assert rec.lines[2].asserted == {"bench": [0]}  # still asserted


@pytest.mark.asyncio
async def test_absent_file_and_key_is_identical(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    _seed_lines(repo, "a.c", [1])
    s1 = await _report(repo, tmp_path / "r1")  # no override file exists
    s2 = await run_coverage_report(  # overrides never resolved at all
        [],
        tmp_path / "r2",
        repo_root=repo,
        tier_configs=load_tiers(COV),
        ticket_spec=SPEC,
    )
    d1 = json.loads((tmp_path / "r1" / "store.json").read_text())
    d2 = json.loads((tmp_path / "r2" / "store.json").read_text())
    assert d1 == d2
    assert d1["overrides"] == []
    assert s1.overrides == []
    assert s2.overrides == []


@pytest.mark.asyncio
async def test_overrides_active_true_for_reattribute_only_file_false_with_none(tmp_path):
    """F2 (final review): spec §7 says `overrides_active` in `tickets.json`
    must be true when the override FILE is present — a reattribute-only
    file (no `[[<tier>]]` asserted entries at all) must still read true,
    not `bool(store.overrides)` (which is empty for this file, since
    reattribution never appends an `OverrideRecord`). No file at all must
    still read false.
    """
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\n")
    git("add", "-A")
    git("commit", "-qm", "fix #1")
    sha = head_commit(repo)
    _seed_lines(repo, "a.c", [1])
    _overrides(repo, f'[[reattribute]]\ncommit = "{sha}"\ntickets = ["#9"]\nreason = "wrong id"\n')
    store = await _report(repo, tmp_path / "r")
    assert store.overrides == []  # no asserted entries — the old, wrong proxy would read False
    payload = build_ticket_export(
        store, repo_root=repo, project="p", otto_version="0", generated="g"
    )
    assert payload["overrides_active"] is True

    # No override file at all: both the flag and the export read false.
    (repo / DEFAULT_OVERRIDES_RELPATH).unlink()
    store2 = await _report(repo, tmp_path / "r2")
    assert store2.overrides_file_active is False
    payload2 = build_ticket_export(
        store2, repo_root=repo, project="p", otto_version="0", generated="g"
    )
    assert payload2["overrides_active"] is False


@pytest.mark.asyncio
async def test_reattribute_only_file_never_spawns_the_first_parent_walk(tmp_path, monkeypatch):
    """F4 (final review): the `rev-list --first-parent` subprocess only
    exists to bound ticket-entry `as_of`; a reattribute-only file (no
    `[[<tier>]]` asserted entries) has nothing for it to bound, so
    `CoverageReporter._apply_overrides` must skip it entirely rather than
    paying for a walk `apply_asserted_entries` would immediately no-op on
    anyway (its `if not entries: return` early-out).
    """
    from otto.coverage.capture import gitio

    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\n")
    git("add", "-A")
    git("commit", "-qm", "fix #1")
    sha = head_commit(repo)
    _seed_lines(repo, "a.c", [1])
    _overrides(repo, f'[[reattribute]]\ncommit = "{sha}"\ntickets = ["#9"]\nreason = "wrong id"\n')

    def _boom(*_a, **_kw):
        raise AssertionError("rev_list_first_parent must not be called for a reattribute-only file")

    monkeypatch.setattr(gitio, "rev_list_first_parent", _boom)
    store = await _report(repo, tmp_path / "r")
    assert store.overrides_file_active is True
    assert store.overrides == []


@pytest.mark.asyncio
async def test_fully_aged_ticket_entry_is_inert_not_fatal(tmp_path, caplog):
    """F1 (final review): a ticket entry whose every line has since been
    superseded must be inert — the "fully aged out" prune log — not a loud
    OverrideConfigError. Before the fix, ``attribute_tickets``'s
    ticket->commits map only carried shas that currently own an attributed
    line, so a fully-superseded commit vanished from the map entirely and
    ``_entry_shas`` could not tell "legitimately aged out" from "typo'd id
    / wrong as_of" — it raised the typo error for both. The fix makes that
    map walk-complete (every commit the walk visited, not just current line
    owners), so the ticket is still found (as_of resolves fine) and the
    empty *current* line set falls through to the existing aged-out path.
    """
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    as_of = head_commit(repo)
    # Later no-ticket commit rewrites EVERY line ticket #1 touched, so #1
    # no longer owns any current line — full aging by content (spec §3).
    (repo / "a.c").write_text("REWRITTEN\n")
    git("commit", "-aqm", "later rewrite, no ticket")
    _seed_lines(repo, "a.c", [1])
    _overrides(repo, f'[[bench]]\nticket = "#1"\nas_of = "{as_of}"\nreason = "legacy"\n')
    with caplog.at_level(logging.INFO, logger="otto.coverage.overrides"):
        store = await _report(repo, tmp_path / "r")
    assert store is not None
    assert "fully aged out" in caplog.text
    assert "ticket:#1" in caplog.text
    assert "legacy" in caplog.text
    rec = _file_rec(store, "a.c")
    assert rec.lines[1].asserted == {}
    assert rec.lines[1].hits.for_tier("bench") == 0


@pytest.mark.asyncio
async def test_prune_signal_reaches_the_report_log(tmp_path, caplog):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    sha = head_commit(repo)
    cap = Capture(
        tier="bench",
        base_commit=head_commit(repo),
        captured_at="2026-07-02T00:00:00Z",
        board="b1",
        files={"a.c": CaptureFileCov(blob=blob_sha(repo, Path("a.c")), lines={1: 1})},
    )
    write_manual_capture(cap, repo)
    _overrides(repo, f'[[bench]]\ncommit = "{sha}"\nreason = "old pass"\n')
    with caplog.at_level(logging.INFO, logger="otto.coverage.overrides"):
        await _report(repo, tmp_path / "r")
    assert "fully covered" in caplog.text
    assert "old pass" in caplog.text
