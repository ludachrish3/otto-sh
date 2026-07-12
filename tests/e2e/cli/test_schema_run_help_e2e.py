"""Hostless e2e tests: schema export, run --list-instructions, per-group --help.

Every case here is purely informational (help / discovery / schema), so none may
create a per-invocation output dir — each test asserts that via
:func:`assert_no_output_dir`.
"""

import json
from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import REPO_E2E, assert_no_output_dir, run_otto

pytestmark = pytest.mark.hostless

GROUPS = ["run", "test", "monitor", "cov", "host", "docker", "reservation", "schema"]

# Subcommand-level help across every group whose callback creates an output dir
# when a subcommand is pending. Each once crashed (bug#3): the root callback skips
# init_cli_logging on the help path, so the group callback's create_output_dir
# raised. All are hostless (no --lab needed — the help flag is lab-free).
SUBCOMMAND_HELP = [
    ["run", "noop", "--help"],
    ["test", "TestE2EFixture", "--help"],
    ["host", "local", "exists", "--help"],
    ["cov", "report", "--help"],
    ["docker", "up", "--help"],
    ["reservation", "check", "--help"],
]


def test_schema_export_writes_json_files(tmp_path: Path) -> None:
    # schema export writes *.schema.json into --out DIR; it does NOT print JSON.
    out = tmp_path / "schemas"
    r = run_otto(["schema", "export", "--out", str(out)], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    written = list(out.glob("*.schema.json"))
    assert written, f"no schema files written to {out}"
    json.loads((out / "lab.schema.json").read_text())  # one is valid JSON
    assert_no_output_dir(tmp_path)  # schema is informational — no run dir


def test_run_list_instructions(tmp_path: Path) -> None:
    # NO --lab: Task 2.5 makes --list-instructions lab-free (this guards that fix).
    r = run_otto(["run", "--list-instructions"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "noop" in r.stdout
    assert_no_output_dir(tmp_path)  # discovery is informational — no run dir


@pytest.mark.parametrize("group", GROUPS)
def test_group_help_dispatches(group: str, tmp_path: Path) -> None:
    # NO --lab: Task 2.5 makes subcommand --help lab-free for all groups (this guards that fix).
    r = run_otto([group, "--help"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, f"{group} --help failed:\n{r.stderr}"
    assert r.stdout.strip() != ""
    assert_no_output_dir(tmp_path)  # group --help is informational — no run dir


def test_monitor_help_shows_flat_options(tmp_path: Path) -> None:
    # Regression: monitor_app is a single-command Typer app; flattening keeps its
    # documented flat CLI so `otto monitor --help` surfaces --live / --hosts
    # directly instead of a spurious nested `monitor` subcommand (which broke
    # `otto monitor --live` with exit-2 "No such option").
    r = run_otto(["monitor", "--help"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "--live" in r.stdout, r.stdout
    assert "--hosts" in r.stdout, r.stdout
    assert_no_output_dir(tmp_path)


@pytest.mark.parametrize("argv", SUBCOMMAND_HELP, ids=[" ".join(a) for a in SUBCOMMAND_HELP])
def test_subcommand_help_no_crash_no_dir(argv: list[str], tmp_path: Path) -> None:
    # Regression (bug#3): a subcommand-level --help must show the subcommand's help,
    # exit 0, and create NO output dir. The group-level `<group> --help` test above
    # never reached the group callback body, so it never caught this crash.
    r = run_otto(argv, xdir=tmp_path, sut_dirs=REPO_E2E)
    joined = " ".join(argv)
    assert r.returncode == 0, f"{joined} failed:\n{r.stdout}\n{r.stderr}"
    assert "RuntimeError" not in (r.stdout + r.stderr), f"{joined} crashed:\n{r.stdout}\n{r.stderr}"
    assert "Usage" in r.stdout, f"{joined} did not print help:\n{r.stdout}"
    # A help path must never spam the lab-context probe's "Missing option" error
    # to stderr (regression: HostGroup's class-scoping probe printed it once per
    # exposed verb during `otto host --help` formatting).
    assert "Missing option" not in r.stderr, f"{joined} leaked lab-probe error:\n{r.stderr}"
    assert_no_output_dir(tmp_path)


def test_host_help_is_stderr_clean_and_lab_free(tmp_path: Path) -> None:
    # Regression: `otto host --help` (no --lab, no host id) must render clean help
    # without probing the lab — no "Missing option '--lab'" spam on stderr, one
    # line per exposed verb or otherwise.
    r = run_otto(["host", "--help"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "Usage" in r.stdout
    assert "Missing option" not in r.stderr, f"host --help leaked lab-probe error:\n{r.stderr}"
    assert_no_output_dir(tmp_path)


def test_test_suite_discovery_flag_no_crash(tmp_path: Path) -> None:
    # Regression (bug#3): a discovery flag after a suite name must fail cleanly, never
    # with a create_output_dir RuntimeError traceback. `--list-suites` is a group-level
    # flag, so targeting a specific suite is a usage error — but a *clean* one.
    r = run_otto(["test", "TestE2EFixture", "--list-suites"], xdir=tmp_path, sut_dirs=REPO_E2E)
    combined = r.stdout + r.stderr
    assert "RuntimeError" not in combined, f"discovery flag after suite crashed:\n{combined}"
    assert "init_cli_logging" not in combined, f"create_output_dir ran before init:\n{combined}"
    assert_no_output_dir(tmp_path)
