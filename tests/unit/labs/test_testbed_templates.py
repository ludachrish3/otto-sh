"""The testbed's lab configs template their Ubuntu VMs (spec 2026-11-18 lab-templating §9).

``tests/_fixtures/lab_data/tech1/lab.jsonnet`` is the bed's cell: its four
Ubuntu VMs come from one Jsonnet function, ``testN`` at ``10.10.200.(10+N)``.
It must render exactly the lab its hand-written JSON file declared, frozen
as :data:`tests._fixtures.paths.TECH1_GOLDEN` (an edit to the bed edits
both). ``tech1-inventory`` keeps its hand-written files
(``docs/configuration/inventory.md``'s worked example) and gains a templated
twin: ``inventory.jsonnet`` renders the three records from one function, and
``lab.jsonnet`` lays the labs out over them with ``otto.libsonnet``. The twin
composes the same labs as the hand-written pair.

No test or script reads the cell as raw JSON: every reader goes through
otto (``scripts/_lab_document.py``, ``tests/_fixtures/labdata.py``), so the
cell stays one file with one reading.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import tomli

from otto.labs.documents import read_lab_document
from scripts import _lab_document
from tests._fixtures.gitrepo import git_env
from tests._fixtures.labdata import lab_data_dir, lab_data_path, tech1_document
from tests._fixtures.paths import CHECKOUT_ROOT, TECH1_GOLDEN
from tests.unit.templating._lab_project import (
    json_source,
    jsonnet_source,
    lab_facts,
    project,
    table,
)
from tests.unit.templating.conftest import jsonnet_binding_under_test, otto_home  # noqa: F401

TWIN = lab_data_dir() / "tech1-inventory"

_RAW_CELL = re.compile(r"tech1(?:/|[\"']\s*[/,]\s*[\"'])lab\.json(?!net)")
"""A path to the cell's lab file as raw JSON: ``tech1/lab.json``, never ``tech1/lab.jsonnet``."""


def test_tech1_template_renders_todays_lab():
    """The rendered cell's sections equal the hand-written lab.json's, read by the same reader."""
    rendered = _lab_document.read_cell_document(lab_data_path("tech1"))
    assert rendered == read_lab_document(TECH1_GOLDEN).sections


def _code(entry: Path) -> str:
    """*entry*'s Jsonnet with its ``//`` comments dropped: what it computes, not what it says."""
    return "\n".join(re.sub(r"\s*//.*$", "", line) for line in entry.read_text().splitlines())


def test_ubuntu_vms_come_from_one_function():
    """Each entry file spells the management prefix once: inside the one function."""
    for entry in (lab_data_path("tech1"), TWIN / "inventory.jsonnet"):
        assert _code(entry).count("10.10.200.") == 1, entry
    assert "10.10.200." not in _code(TWIN / "lab.jsonnet")  # machine facts are the records'
    vms = {
        e["name"]: e["hosts"][0]["ip"]
        for e in tech1_document()["elements"]
        if e["name"][:4] == "test"
    }
    assert vms == {f"test{n}": f"10.10.200.{10 + n}" for n in (1, 2, 3, 4)}


def _twin_settings(*, templated: bool) -> str:
    """The worked example's settings, its user file's tables in the project, by absolute path.

    The hand-written project reads ``lab.json`` over ``inventory.json``; the
    templated one reads ``lab.jsonnet`` over ``inventory.jsonnet``, from a
    source declaring ``inventories = ["default"]``.
    """
    user = tomli.loads((TWIN / "user-settings.toml").read_text())
    records = TWIN / ("inventory.jsonnet" if templated else "inventory.json")
    tables = table(
        "[inventory]",
        backend=user["inventory"]["backend"],
        path=str(records),
        supplies=user["inventory"]["supplies"],
    ) + table("[creds]", backend=user["creds"]["backend"], path=str(TWIN / user["creds"]["path"]))
    if templated:
        return tables + jsonnet_source(paths=[str(TWIN / "lab.jsonnet")], inventories=["default"])
    return tables + json_source(TWIN / "lab.json")


def _facts(sut: Any, name: str) -> dict[str, Any]:
    """:func:`lab_facts`, with each reference's name and key but not its spelling.

    The hand-written lab writes the bare key and the template the qualified
    reference, on purpose; both name the same record.
    """
    facts = lab_facts(sut, name)
    for host in facts["hosts"].values():
        host["ref"] = host["ref"][:2]
    return facts


@pytest.mark.usefixtures("otto_home")
def test_tech1_inventory_twin_composes_the_same_labs(tmp_path):
    hand = project(tmp_path / "hand", _twin_settings(templated=False), name="hand")
    templated = project(tmp_path / "templated", _twin_settings(templated=True), name="templated")
    for name in ("unix", "busybox"):
        assert _facts(templated, name) == _facts(hand, name), name
    unix = _facts(templated, "unix")
    assert sorted(unix["hosts"]) == ["test1", "test2", "test3"]
    assert unix["hosts"]["test2"]["console"] == ("test1", 4001)
    assert {h["ref"] for h in unix["hosts"].values()} == {
        ("default", f"test{n}") for n in (1, 2, 3)
    }
    assert unix["hosts"]["test1"]["extra"] == {"asset_tag": "VM-0011"}


def test_reach_prints_addresses_only(capsys):
    """``reach`` prints each host's management address, one per line, and nothing else."""
    assert _lab_document.main(["reach", str(lab_data_path("tech1"))]) == 0
    out = capsys.readouterr().out
    expected = [h["ip"] for e in tech1_document()["elements"] for h in e["hosts"]]
    assert out.splitlines() == expected
    assert "password" not in out
    assert "vagrant" not in out
    assert "{" not in out


_SCANNED = ("tests/", "scripts/", "Makefile", "docs/contributing.md")
"""Where the cell is read or named: the tests, the scripts, the Makefile, the contributor guide."""

_HISTORY = {
    "tests/project3/docs/feasibility.md": "a dated fix record: the file it names existed then",
}
"""Tracked files that name the old cell file on purpose, each with why."""


_CELL_NAMED = "tech1(/|[\"'][[:space:]]*[/,][[:space:]]*[\"'])lab\\.json"
""":data:`_RAW_CELL` in git's extended regex, less its ``(?!net)``: it finds ``lab.jsonnet`` too."""


def _cell_lines(home: Path) -> "list[tuple[str, int, str]]":
    """Every tracked ``(path, line number, line)`` under :data:`_SCANNED` naming the cell's file.

    One ``git grep`` over the tracked files, binaries skipped: the raw JSON
    spelling and the ``lab.jsonnet`` one both, which :data:`_RAW_CELL` tells apart.
    """
    proc = subprocess.run(
        ["git", "grep", "-n", "-I", "--null", "-E", _CELL_NAMED, "--", *_SCANNED],
        cwd=CHECKOUT_ROOT,
        env=git_env(home),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode in (0, 1), proc.stderr  # 1: no line matched
    found = []
    for hit in proc.stdout.splitlines():
        rel, number, line = hit.split("\0", 2)
        found.append((rel, int(number), line))
    return found


def _offenders(lines: "list[tuple[str, int, str]]") -> list[str]:
    """Every ``path:line`` of *lines* naming the cell as raw JSON, bar this file and history."""
    own = Path(__file__).resolve().relative_to(CHECKOUT_ROOT).as_posix()
    return [
        f"{rel}:{number}"
        for rel, number, line in lines
        if rel != own and rel not in _HISTORY and _RAW_CELL.search(line)
    ]


def test_the_raw_cell_pattern_matches_every_path_spelling():
    """A string path, a ``/``-joined pathlib path and a joined argument list all count."""
    for spelled in (
        "tests/_fixtures/lab_data/tech1/lab.json",
        'lab_data_dir() / "tech1" / "lab.json"',
        "root / 'tech1' / 'lab.json'",
        'os.path.join(root, "tech1", "lab.json")',
    ):
        assert _RAW_CELL.search(spelled), spelled
    for fine in (
        "tech1/lab.jsonnet",
        '"tech1" / "lab.jsonnet"',
        "tech2/lab.json",
        "tech1_lab.json",
    ):
        assert not _RAW_CELL.search(fine), fine


def test_no_test_or_script_reads_the_cell_as_raw_json(tmp_path):
    """The cell's raw JSON file is gone, and no tracked test, script or guide line names it."""
    lines = _cell_lines(tmp_path)
    named = {rel for rel, _, _ in lines}
    # Not vacuous: the grep sees the tree, where these name the cell as lab.jsonnet.
    assert "scripts/bed_lease.py" in named
    assert "Makefile" in named
    for rel in _HISTORY:
        hits = [line for path, _, line in lines if path == rel and _RAW_CELL.search(line)]
        assert hits, f"stale history exemption: {rel}"
    offenders = _offenders(lines)
    assert not offenders, offenders
    assert not (lab_data_dir() / "tech1" / "lab.json").exists()
    assert json.loads(TECH1_GOLDEN.read_text())["labs"], "the frozen copy is still there"
