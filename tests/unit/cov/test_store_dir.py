"""Manual capture store: naming, round-trip, listing."""

import re
from pathlib import Path

import pytest

from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.capture.store_dir import (
    load_manual_captures,
    manual_store_dir,
    write_manual_capture,
)


def _capture(ticket: str = "PROJ-123") -> Capture:
    return Capture(
        tier="manual",
        base_commit="0" * 40,
        captured_at="2026-07-02T18:40:00Z",
        tester={"name": "chris", "email": "c@x"},
        ticket=ticket,
        labs=["lab1"],
        board="Board One",
        files={"f.c": CaptureFileCov(lines={1: 1})},
    )


def test_write_and_load(tmp_path: Path) -> None:
    p = write_manual_capture(_capture(), tmp_path)
    assert p.parent == manual_store_dir(tmp_path)
    assert p.name == "20260702T184000Z-proj-123-board-one.json"
    caps = load_manual_captures(tmp_path)
    assert len(caps) == 1
    assert caps[0].ticket == "PROJ-123"


def test_missing_dir_is_empty(tmp_path: Path) -> None:
    assert load_manual_captures(tmp_path) == []


def test_malformed_names_file(tmp_path: Path) -> None:
    d = manual_store_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{nope")
    with pytest.raises(ValueError, match=re.escape("bad.json")):
        load_manual_captures(tmp_path)
