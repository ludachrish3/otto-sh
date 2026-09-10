"""Hostless e2e tests: schema export, run --list-instructions, per-group --help.

Every case here is purely informational (help / discovery / schema), so none may
create a per-invocation output dir — each test asserts that via
:func:`assert_no_output_dir`.
"""

import json
import re
from pathlib import Path

import pytest

from tests._fixtures.cli_registry import builtin_group_names
from tests.e2e._otto_subprocess import REPO_E2E, assert_no_output_dir, run_otto

pytestmark = pytest.mark.hostless

# The registry, not a hand-list: every built-in name whose registered object
# resolves to a Typer GROUP (see tests._fixtures.cli_registry.builtin_group_names
# for what that excludes — a single-command loader like `init`, or a Typer app
# that flattens to one bare leaf like `monitor`, which has its own dedicated
# flat-CLI coverage below).
GROUPS = builtin_group_names()

# Subcommand-level help across every group whose callback creates an output dir
# when a subcommand is pending. Each once crashed (bug#3): the root callback skips
# init_cli_logging on the help path, so the group callback's create_output_dir
# raised. All are hostless (no --lab needed — the help flag is lab-free).
#
# Kept as a literal: several of these groups (run, test, host) resolve their
# subcommands dynamically at dispatch time rather than listing them on the
# Typer object, so there is no static first-subcommand to derive. The
# completeness assertion below is what stops a new group with subcommands
# from going unchecked here.
SUBCOMMAND_HELP = [
    ["run", "noop", "--help"],
    ["test", "TestE2EFixture", "--help"],
    ["host", "local", "exists", "--help"],
    ["cov", "report", "--help"],
    ["docker", "up", "--help"],
    ["reservation", "check", "--help"],
    ["cache", "info", "--help"],
    ["env", "create", "--help"],
    ["inventory", "lookup", "--help"],
    ["link", "impair", "--help"],
    ["schema", "export", "--help"],
    ["tunnel", "add", "--help"],
]


def test_subcommand_help_covers_every_group_with_subcommands() -> None:
    """Every group in ``GROUPS`` must have at least one entry in ``SUBCOMMAND_HELP``.

    ``GROUPS`` is derived from the registry (see above); this is what makes a
    newly-added group with subcommands fail loudly here instead of silently
    going unchecked by ``SUBCOMMAND_HELP``, which stays a hand-written list.
    """
    covered = {argv[0] for argv in SUBCOMMAND_HELP}
    missing = [group for group in GROUPS if group not in covered]
    assert not missing, f"add a SUBCOMMAND_HELP entry for: {missing}"


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


@pytest.mark.parametrize("verb", ["put", "get"])
def test_host_transfer_help_lists_the_recursive_flag(tmp_path: Path, verb: str) -> None:
    r = run_otto(["host", "local", verb, "--help"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "--recursive" in r.stdout
    assert re.search(r"(?<![\w-])-r(?![\w-])", r.stdout), r.stdout
    assert_no_output_dir(tmp_path)
