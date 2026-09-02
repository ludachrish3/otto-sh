"""End-to-end tests for otto root action flags (lab-free).

Verifies that ``--version``, ``--list-labs``, ``--list-hosts``, and the
``otto cache`` group (successor to the removed ``--clear-autocomplete-cache``
root flag, 0.10.0 breaking) work correctly as subprocess invocations. All
tests carry the ``hostless`` marker so they run without a live lab.
"""

from pathlib import Path

import pytest

from otto.config.home import workspace_key
from tests.e2e._otto_subprocess import REPO_E2E, assert_no_output_dir, run_otto

pytestmark = pytest.mark.hostless


def test_version(tmp_path: Path) -> None:
    """--version exits 0 and prints 'otto version:' to stdout."""
    r = run_otto(["--version"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0
    assert "otto version:" in r.stdout
    assert_no_output_dir(tmp_path)  # root action flag — no subcommand, no run dir


def test_list_labs(tmp_path: Path) -> None:
    """--list-labs exits 0 and lists the concrete lab names from tech1 fixture data."""
    r = run_otto(["--list-labs"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    # repo_e2e → tech1 fixture → two labs: embedded and unix
    assert "unix" in r.stdout
    assert "embedded" in r.stdout
    assert_no_output_dir(tmp_path)  # informational listing — no run dir


def test_list_hosts(tmp_path: Path) -> None:
    """--list-hosts with --lab unix exits 0 and lists a concrete host id."""
    r = run_otto(["--lab", "unix", "--list-hosts"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    # tech1 lab.json: test1/test2/test3 all belong to lab "unix"
    assert "test1" in r.stdout
    assert_no_output_dir(tmp_path)  # queries lab state but runs no subcommand — no run dir


def test_cache_clear(tmp_path: Path) -> None:
    """`otto cache clear` removes the completion cache and reports what it removed.

    Successor to the removed ``--clear-autocomplete-cache`` root flag (see
    ``test_clear_autocomplete_cache_flag_no_longer_exists`` below for proof the
    old spelling is gone). No ``OTTO_HOME`` override needed: passing *xdir*
    already pins the harness's per-test home at ``<xdir>/otto-home``.
    """
    # Seed a warm completion cache first — an unseeded workspace reports
    # "nothing to remove", which contains neither "removed" nor "cache".
    seed = run_otto(["--help"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert seed.returncode == 0, seed.stderr

    r = run_otto(["cache", "clear"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "removed" in r.stdout.lower()
    assert "cache" in r.stdout.lower()
    assert_no_output_dir(tmp_path)  # lab-free command — no subcommand run dir


def test_cache_prune_dry_run(tmp_path: Path) -> None:
    """`otto cache prune --dry-run` reports its plan without touching the filesystem."""
    seed = run_otto(["--help"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert seed.returncode == 0, seed.stderr

    # <xdir>/otto-home is the harness's own pinned layout (_otto_subprocess.py's
    # otto_subprocess_env) -- derived here rather than read back from an
    # OTTO_HOME override, so there is nothing to drift if that layout ever
    # changes an override would silently keep pointing at.
    cache_file = tmp_path / "otto-home" / workspace_key([REPO_E2E]) / "completion_cache.json"
    assert cache_file.exists()

    # --age 0: the default 60-day cutoff would exempt a just-seeded cache
    # entirely, and the plan would list nothing.
    r = run_otto(
        ["cache", "prune", "--dry-run", "--age", "0"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
    )
    assert r.returncode == 0, r.stderr
    assert str(cache_file) in r.stdout
    assert cache_file.exists()  # dry-run must never touch the filesystem
    assert_no_output_dir(tmp_path)  # lab-free command — no subcommand run dir


def test_clear_autocomplete_cache_flag_no_longer_exists(tmp_path: Path) -> None:
    """The removed root flag is Typer's unknown-option error, not a silent no-op."""
    r = run_otto(["--clear-autocomplete-cache"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode != 0
    assert "no such option" in r.stderr.lower()


def test_list_hosts_includes_builtin_local(tmp_path: Path) -> None:
    """The built-in `local` host appears in --list-hosts for any lab (no custom repo)."""
    r = run_otto(["--lab", "unix", "--list-hosts"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "local" in r.stdout
    assert_no_output_dir(tmp_path)
