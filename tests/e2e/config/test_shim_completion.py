"""Spec §6 end to end, through the installed binary: cold writes, warm answers, edit hands over."""

import json
import os
from pathlib import Path

import pytest

from otto.config.cache_maintenance import MARKER_FILENAMES
from otto.config.completion_cache import CACHE_FILENAME
from otto.config.home import workspace_key
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.shim_repo import CREDS, HOSTS, LINKS, make_shim_repo
from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.hostless


def _tab(repo: Path, xdir: Path, words: str, cword: int) -> str:
    result = run_otto(
        [],
        xdir=xdir,
        sut_dirs=repo,
        extra_env={
            "_OTTO_COMPLETE": "complete_bash",
            "COMP_WORDS": words,
            "COMP_CWORD": str(cword),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    return result.stdout


def _cache(repo: Path, xdir: Path) -> Path:
    return xdir / "otto-home" / workspace_key([repo]) / CACHE_FILENAME


def test_cold_writes_warm_answers_edit_hands_over(tmp_path: Path) -> None:
    repo = make_shim_repo(tmp_path)
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    (xdir / "otto-home").mkdir()
    cache = _cache(repo, xdir)
    marker = cache.parent / MARKER_FILENAMES["names"]

    cold = _tab(repo, xdir, "otto host ", 2)
    assert {"dut1", "dut2", "box", "local"} <= set(cold.split())
    assert "shim" in json.loads(cache.read_text())["sections"]
    written = cache.stat().st_mtime_ns
    assert not marker.exists(), "no TAB has validated the entry yet"

    warm = _tab(repo, xdir, "otto host ", 2)
    assert warm == cold
    assert cache.stat().st_mtime_ns == written, "the warm TAB rewrote the cache"
    assert marker.stat().st_mtime_ns >= written

    again = _tab(repo, xdir, "otto host dut2 ", 3)
    assert "blink" in again.split()
    assert cache.stat().st_mtime_ns == written

    # An edit INSIDE the marker window is served stale by design (spec §1
    # decision 8): add a host; the marker still vouches, so this TAB neither
    # sees the host nor rewrites the entry.
    lab = repo / "lab" / "lab.json"
    new_host = {"ip": "10.0.0.4", "element": "new", "labs": ["west"], "creds": CREDS}
    write_lab_json(lab, [*HOSTS, new_host], links=LINKS)
    inside = _tab(repo, xdir, "otto host ", 2)
    assert inside == cold
    assert "new" not in inside.split()
    assert cache.stat().st_mtime_ns == written, "inside the window nothing is re-validated"

    # Once the marker is older than the window the stat pass runs, the edited
    # lab file hands over, and the full path rewrites the entry with the host.
    os.utime(marker, ns=(0, 0))
    after_edit = _tab(repo, xdir, "otto host ", 2)
    assert "new" in after_edit.split()
    assert cache.stat().st_mtime_ns > written, (
        "the edited lab file must have handed over and rewritten"
    )
    assert marker.stat().st_mtime_ns < cache.stat().st_mtime_ns, "the old marker no longer vouches"

    served = _tab(repo, xdir, "otto host ", 2)
    assert served == after_edit
    assert marker.stat().st_mtime_ns >= cache.stat().st_mtime_ns
