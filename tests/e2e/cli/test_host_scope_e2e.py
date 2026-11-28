"""``otto host`` against a declared scope, end to end: the target is scoped, ``--hop`` is not.

The workspace is :func:`tests.e2e._scope_workspace.scoped_roots` with both
projects declaring: under ``-l alpha`` the run targets ``a-dut`` (``project_a``)
and ``b-dut`` (``project_b``), and no project targets ``a-jump``. Every case
here is refused before any connection, or is a dry run, so none dials a host.
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import run_otto
from tests.e2e._scope_workspace import scoped_roots

pytestmark = pytest.mark.hostless


def _otto(tmp_path: Path, argv: list[str]):
    return run_otto(
        argv,
        xdir=tmp_path / "x",
        roots=None,
        lab="alpha",
        extra_argv_prefix=["-R"],
        extra_env={"OTTOPATH": scoped_roots(tmp_path, declare_a=True, hosts=True)},
    )


def _flat(r) -> str:
    return " ".join((r.stdout + r.stderr).split())


def _settings(tmp_path: Path, project: str) -> str:
    return f"{tmp_path / project}/.otto/settings.toml"


def test_e2e_host_out_of_scope_refused(tmp_path: Path) -> None:
    r = _otto(tmp_path, ["host", "a-jump", "exec", "true"])
    blob = _flat(r)
    assert r.returncode == 1, blob
    assert (
        "host 'a-jump' is in lab 'alpha' but outside this run's scope: "
        "no declared project targets it. To reach it, add its id to [project] "
        "host_patterns (or its lab to lab_patterns) in "
        f"{_settings(tmp_path, 'project_a')}, {_settings(tmp_path, 'project_b')}."
    ) in blob
    assert "No host with ID" not in blob


def test_e2e_dry_run_hop_out_of_scope_resolves(tmp_path: Path) -> None:
    """A hop needs only lab membership: ``a-jump`` carries an in-scope target's dry run."""
    r = _otto(tmp_path, ["-n", "host", "--hop", "a-jump", "a-dut", "exec", "true"])
    blob = _flat(r)
    assert r.returncode == 0, blob
    assert "references resolve: host 'a-dut'" in blob


def test_e2e_exclude_refuses_switched_off_host(tmp_path: Path) -> None:
    r = _otto(tmp_path, ["-E", "project_b", "host", "b-dut", "exec", "true"])
    blob = _flat(r)
    assert r.returncode == 1, blob
    assert (
        "host 'b-dut' is in lab 'alpha' but outside this run's scope: only switched-off "
        "projects target it (project_b). Drop -E project_b to reach it."
    ) in blob


@pytest.mark.parametrize(
    "argv",
    [["host", "a-jump", "--help"], ["host", "a-jump", "exec", "--help"]],
    ids=["menu", "verb"],
)
def test_e2e_host_help_out_of_scope_refused(tmp_path: Path, argv: list[str]) -> None:
    r = _otto(tmp_path, argv)
    blob = _flat(r)
    assert r.returncode == 1, blob
    assert "host 'a-jump' is in lab 'alpha' but outside this run's scope" in blob
    assert "Usage" not in blob


@pytest.mark.parametrize("argv", [["--list-hosts"], ["host", "--list-hosts"]], ids=["root", "host"])
def test_e2e_list_hosts_omits_out_of_scope_host(tmp_path: Path, argv: list[str]) -> None:
    """``--list-hosts`` lists what the run may target; ``a-jump`` is in the lab, no project's."""
    r = _otto(tmp_path, argv)
    assert r.returncode == 0, _flat(r)
    listed = [line for line in r.stdout.splitlines() if line.startswith("\u2022")]
    assert listed == ["\u2022 a-dut", "\u2022 b-dut", "\u2022 local"], r.stdout
