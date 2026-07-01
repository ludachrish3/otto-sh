"""Hostless e2e tests: schema export, run --list-instructions, per-group --help."""

import json
from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import REPO_E2E, run_otto

pytestmark = pytest.mark.hostless

GROUPS = ["run", "test", "monitor", "cov", "host", "docker", "reservation", "schema"]


def test_schema_export_writes_json_files(tmp_path: Path) -> None:
    # schema export writes *.schema.json into --out DIR; it does NOT print JSON.
    out = tmp_path / "schemas"
    r = run_otto(["schema", "export", "--out", str(out)], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    written = list(out.glob("*.schema.json"))
    assert written, f"no schema files written to {out}"
    json.loads((out / "hosts.schema.json").read_text())  # one is valid JSON


def test_run_list_instructions(tmp_path: Path) -> None:
    # NO --lab: Task 2.5 makes --list-instructions lab-free (this guards that fix).
    r = run_otto(["run", "--list-instructions"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "noop" in r.stdout


@pytest.mark.parametrize("group", GROUPS)
def test_group_help_dispatches(group: str, tmp_path: Path) -> None:
    # NO --lab: Task 2.5 makes subcommand --help lab-free for all groups (this guards that fix).
    r = run_otto([group, "--help"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, f"{group} --help failed:\n{r.stderr}"
    assert r.stdout.strip() != ""
