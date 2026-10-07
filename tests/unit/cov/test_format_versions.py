"""The coverage formats against frozen samples (dump spec §13.5).

Every declared read version of ``store.json`` and ``capture.json`` has a
populated sample under ``tests/_fixtures/formats/`` that no writer ever
regenerates. Each sample is loaded through its reader's real entry points and
checked for meaning. Every declared write version of the three coverage
formats gets an emission test. Each reader and writer is shown to consult the
declared list, not a literal of its own.
"""

import json
from pathlib import Path

import pytest

import otto.coverage.capture.model as capture_model
import otto.coverage.store.model as store_model
from otto.coverage import formats
from otto.coverage.capture.model import Capture
from otto.coverage.capture.store_dir import (
    load_manual_captures,
    manual_store_dir,
    write_manual_capture,
)
from otto.coverage.store.model import CoverageStore
from otto.coverage.ticket_export import build_ticket_export
from tests._fixtures.paths import TESTS_ROOT

SAMPLES = TESTS_ROOT / "_fixtures" / "formats"


def _store_sample(version: int) -> Path:
    return SAMPLES / "coverage-store" / f"{version}.json"


def _capture_sample(version: int) -> Path:
    return SAMPLES / "coverage-capture" / f"{version}.json"


def _store_v8(store: CoverageStore) -> None:
    (rec,) = list(store.files())
    assert str(rec.path) == "/work/src/app.c"
    assert rec.lines[1].hits.for_tier("system") == 3
    assert rec.lines[1].ticket == ["PROJ-1"]
    assert rec.lines[1].run_hits == {0: 3}
    assert [(b.block, b.branch, b.reachable) for b in rec.lines[2].branches] == [
        (0, 0, {"system": True}),
        (0, 1, {"system": False}),
    ]
    assert rec.lines[2].branches[0].hits.for_tier("system") == 1
    assert rec.lines[2].asserted == {"manual": [0]}
    assert rec.excluded_lines == {4}
    assert rec.branch_excluded_lines == {5}
    assert rec.functions["main"].hits.for_tier("system") == 1
    assert [(r.tier, r.product, r.ticket) for r in store.runs] == [
        ("system", "app", None),
        ("manual", "app", "PROJ-1"),
    ]
    assert store.tickets["PROJ-1"].commits == ["a" * 40]
    assert [(o.id, o.key) for o in store.overrides] == [(0, "line:src/app.c:2")]
    assert store.overrides_file_active is True
    assert store.tier_order == ["system", "manual"]


def _capture_v3(capture: Capture) -> None:
    assert (capture.tier, capture.product, capture.board, capture.display_name) == (
        "manual",
        "app",
        "board1",
        "Board One",
    )
    assert capture.files["src/app.c"].lines == {1: 2, 2: 0}
    assert capture.files["src/app.c"].branches == {1: [(0, 0, 1), (0, 1, None)]}
    assert (capture.tester, capture.ticket) == ({"name": "Ada"}, "PROJ-2")


STORE_MEANING = {8: _store_v8}
"""What each declared store read version's sample must load as; a version with no entry fails."""

CAPTURE_MEANING = {3: _capture_v3}
"""What each declared capture read version's sample must load as."""


@pytest.mark.parametrize("version", formats.STORE_READ_VERSIONS)
def test_store_reads_each_declared_sample(version):
    STORE_MEANING[version](CoverageStore.load(_store_sample(version)))


@pytest.mark.parametrize("version", formats.CAPTURE_READ_VERSIONS)
def test_capture_reads_each_declared_sample_through_both_entry_points(version, tmp_path):
    CAPTURE_MEANING[version](Capture.load(_capture_sample(version)))
    manual_store_dir(tmp_path).mkdir(parents=True)
    (manual_store_dir(tmp_path) / "sample.json").write_bytes(_capture_sample(version).read_bytes())
    (loaded,) = load_manual_captures(tmp_path)
    CAPTURE_MEANING[version](loaded)


def test_the_readers_and_writers_hold_the_declared_lists():
    assert store_model.STORE_READ_VERSIONS is formats.STORE_READ_VERSIONS
    assert capture_model.CAPTURE_READ_VERSIONS is formats.CAPTURE_READ_VERSIONS
    assert capture_model.CAPTURE_WRITE_VERSIONS is formats.CAPTURE_WRITE_VERSIONS


def test_store_load_accepts_only_the_declared_read_versions(monkeypatch):
    monkeypatch.setattr(store_model, "STORE_READ_VERSIONS", [9])
    with pytest.raises(ValueError, match=r"coverage store format v9 required; found v8"):
        CoverageStore.load(_store_sample(8))


def test_capture_load_accepts_only_the_declared_read_versions(monkeypatch):
    monkeypatch.setattr(capture_model, "CAPTURE_READ_VERSIONS", [4])
    with pytest.raises(ValueError, match=r"capture format v4 required; found v3"):
        Capture.load(_capture_sample(3))


@pytest.mark.parametrize(
    ("stamp", "loads"), [(8.0, True), (True, False), ("8", False), (None, False)]
)
def test_the_store_stamp_is_still_judged_by_equality(stamp, loads, tmp_path):
    """Membership in the read list judges a stamp exactly as ``!=`` did before it."""
    data = json.loads(_store_sample(8).read_text())
    data["format"] = stamp
    path = tmp_path / "store.json"
    path.write_text(json.dumps(data))
    if loads:
        _store_v8(CoverageStore.load(path))
    else:
        with pytest.raises(ValueError, match="regenerate"):
            CoverageStore.load(path)


@pytest.mark.parametrize("version", formats.STORE_WRITE_VERSIONS)
def test_store_save_stamps_the_declared_write_version(version, tmp_path):
    store = CoverageStore.load(_store_sample(formats.STORE_READ_VERSIONS[0]))
    store.save(tmp_path / "store.json")
    assert json.loads((tmp_path / "store.json").read_text())["format"] == version
    if version in formats.STORE_READ_VERSIONS:
        STORE_MEANING[version](CoverageStore.load(tmp_path / "store.json"))


@pytest.mark.parametrize("version", formats.CAPTURE_WRITE_VERSIONS)
def test_capture_save_stamps_the_declared_write_version(version, tmp_path):
    """A capture built with no ``schema``, as otto's producer builds it, stamps that version."""
    Capture(tier="manual", product="app", base_commit="b" * 40).save(tmp_path / "capture.json")
    assert json.loads((tmp_path / "capture.json").read_text())["schema"] == version


def test_capture_save_writes_the_stamp_it_holds(tmp_path):
    """``save`` writes whatever ``schema`` the model holds, as it did before.

    Refusing a stamp outside ``CAPTURE_WRITE_VERSIONS`` is a contract change
    (dump spec §13.1), left to its own marked commit. This pins today's
    behaviour until that commit changes it on purpose.
    """
    undeclared = max(formats.CAPTURE_WRITE_VERSIONS) + 1
    Capture(tier="manual", product="app", base_commit="b" * 40, schema=undeclared).save(
        tmp_path / "capture.json"
    )
    assert json.loads((tmp_path / "capture.json").read_text())["schema"] == undeclared


@pytest.mark.parametrize("version", formats.CAPTURE_READ_VERSIONS)
def test_the_manual_store_rewrite_of_each_read_sample_keeps_it(version, tmp_path):
    """``write_manual_capture`` re-saves a loaded capture with the stamp it was read with."""
    written = write_manual_capture(Capture.load(_capture_sample(version)), tmp_path)
    assert json.loads(written.read_text())["schema"] == version
    CAPTURE_MEANING[version](Capture.load(written))


@pytest.mark.parametrize("version", formats.TICKETS_WRITE_VERSIONS)
def test_tickets_export_stamps_the_declared_write_version(version):
    payload = build_ticket_export(
        CoverageStore.load(_store_sample(8)),
        repo_root=Path("/work"),
        project="p",
        otto_version="0",
        generated="2026-07-01T00:00:00Z",
    )
    assert payload["format"] == version
    assert [(t["id"], [f["path"] for f in t["files"]]) for t in payload["tickets"]] == [
        ("PROJ-1", ["src/app.c"])
    ]
