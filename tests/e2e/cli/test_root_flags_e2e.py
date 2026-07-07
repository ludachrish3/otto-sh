"""End-to-end tests for otto root action flags (lab-free).

Verifies that ``--version``, ``--list-labs``, ``--list-hosts``, and
``--clear-autocomplete-cache`` work correctly as subprocess invocations.
All tests carry the ``hostless`` marker so they run without a live lab.
"""

from pathlib import Path

import pytest

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
    # repo_e2e → tech1 fixture → two labs: embedded and veggies
    assert "veggies" in r.stdout
    assert "embedded" in r.stdout
    assert_no_output_dir(tmp_path)  # informational listing — no run dir


def test_list_hosts(tmp_path: Path) -> None:
    """--list-hosts with --lab veggies exits 0 and lists a concrete host id."""
    r = run_otto(["--lab", "veggies", "--list-hosts"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    # tech1 lab.json: carrot/tomato/pepper all belong to lab "veggies"
    assert "carrot_seed" in r.stdout
    assert_no_output_dir(tmp_path)  # queries lab state but runs no subcommand — no run dir


def test_clear_autocomplete_cache(tmp_path: Path) -> None:
    """--clear-autocomplete-cache exits 0 and prints a message containing 'cache'."""
    r = run_otto(["--clear-autocomplete-cache"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0
    # message varies by cache state; all forms contain "cache"
    assert "cache" in r.stdout.lower()
    assert_no_output_dir(tmp_path)  # root action flag — no subcommand, no run dir


def test_list_hosts_includes_builtin_local(tmp_path: Path) -> None:
    """The built-in `local` host appears in --list-hosts for any lab (no custom repo)."""
    r = run_otto(["--lab", "veggies", "--list-hosts"], xdir=tmp_path, sut_dirs=REPO_E2E)
    assert r.returncode == 0, r.stderr
    assert "local" in r.stdout
    assert_no_output_dir(tmp_path)
