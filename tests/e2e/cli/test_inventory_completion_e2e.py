"""The user-file inventory route, end to end: a ``[inventory]`` in ``$OTTO_HOME/settings.toml``
plus a ``hosts.json`` supplement, through the real binary's TAB and ``otto cache info``.

The deployment shape reported on 2026-09-03 ("JSON inventory with a hosts.json
supplement, no hosts come up"): the inventory declared in the USER file, the
repo carrying only references. Nothing below the binary had ever driven that
route — the unit pins build the inventory from repo stand-ins — and the
process-wide resolution it depends on (``build_inventory`` over ALL repos,
falling back to the user file) is exactly what completion had got wrong.

Cold TAB, warm TAB (the cache entry written by the cold one), and the
outlet: ``otto cache info`` naming the hosts offered and the entry dropped.
"""

import json
from pathlib import Path

import pytest

from otto.config.completion_cache import CACHE_FILENAME
from otto.config.home import workspace_key
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.hostless

_CREDS = [{"login": "u", "password": "p"}]

SETTINGS = """\
[[lab.sources]]
name = "local"
backend = "json"
paths = ["lab", "hosts.json"]
"""

USER_SETTINGS = """\
[inventory]
backend = "json"
path = "{inventory_file}"
supplies = ["ip"]
"""


def _workspace(tmp_path: Path, *, references: list[str]) -> tuple[Path, Path]:
    """Build the repo referencing *references* and an xdir whose home declares the inventory.

    ``run_otto`` pins ``OTTO_HOME`` at ``<xdir>/otto-home``, so the user file
    written there is the one the binary resolves the inventory from.
    """
    repo = make_sut_repo(tmp_path / "invsut", name="invsut", extra=SETTINGS)
    write_lab_json(
        repo / "lab" / "lab.json",
        [{"ip": "10.0.0.9", "element": "plain", "creds": _CREDS, "labs": ["site"]}],
        declare_labs=True,
    )
    write_lab_json(
        repo / "hosts.json",
        [
            {"inventory": key, "element": f"dut{i}", "creds": _CREDS, "labs": ["site"]}
            for i, key in enumerate(references, start=1)
        ],
        declare_labs=False,
    )
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    home = xdir / "otto-home"
    home.mkdir()
    inventory_file = tmp_path / "inventory.json"
    inventory_file.write_text(json.dumps({"dut-1": {"ip": "10.0.0.1"}}))
    (home / "settings.toml").write_text(  # sutrepo-exempt: the USER file is the route under test
        USER_SETTINGS.format(inventory_file=inventory_file)
    )
    return repo, xdir


def _tab(repo: Path, xdir: Path) -> set[str]:
    """``otto -l site host <TAB>`` through the real binary; the candidate names."""
    result = run_otto(
        [],
        xdir=xdir,
        sut_dirs=repo,
        extra_env={
            "_OTTO_COMPLETE": "complete_bash",
            "COMP_WORDS": "otto -l site host ",
            "COMP_CWORD": "4",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    return {line.split(",", 1)[-1] for line in result.stdout.splitlines() if line}


def _cache_file(repo: Path, xdir: Path) -> Path:
    return xdir / "otto-home" / workspace_key([repo]) / CACHE_FILENAME


def test_a_user_file_inventory_with_a_supplement_completes_cold_and_warm(tmp_path: Path) -> None:
    repo, xdir = _workspace(tmp_path, references=["dut-1"])

    cold = _tab(repo, xdir)
    assert {"dut1", "plain"} <= cold, cold
    cache_file = _cache_file(repo, xdir)
    assert cache_file.is_file(), "the cold TAB must have written the cache"
    written = cache_file.stat().st_mtime_ns

    warm = _tab(repo, xdir)
    assert {"dut1", "plain"} <= warm, warm
    # Served FROM the entry, not rebuilt behind an identical candidate list:
    # a rebuild rewrites the file.
    assert cache_file.stat().st_mtime_ns == written, "the warm TAB rewrote the cache"

    info = run_otto(["cache", "info"], xdir=xdir, sut_dirs=repo)
    assert info.returncode == 0, info.stdout + info.stderr
    assert "completion names: fresh" in info.stdout
    assert "hosts offered: 3 — dut1 local plain" in info.stdout  # `local` is the builtin host
    assert "dropped: none" in info.stdout


def test_cache_info_names_the_reference_that_did_not_build(tmp_path: Path) -> None:
    """The outlet: the ghost reference is not offered, and `cache info` says which and why."""
    repo, xdir = _workspace(tmp_path, references=["dut-1", "ghost"])

    offered = _tab(repo, xdir)
    assert "dut1" in offered, offered
    assert "dut2" not in offered, offered

    info = run_otto(["cache", "info"], xdir=xdir, sut_dirs=repo)
    assert info.returncode == 0, info.stdout + info.stderr
    assert "hosts offered: 3 — dut1 local plain" in info.stdout
    assert "dropped: 1 — not offered, and why:" in info.stdout
    assert f"[invsut] {repo / 'hosts.json'}: element 'dut2' hosts[0]: " in info.stdout
    assert "ghost" in info.stdout
