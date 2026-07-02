"""Real ``$?`` contracts for the Result family (spec 2026-07-01).

These exercise the built-in ``local`` host end-to-end (no bed required) to
verify that the CLI's exit code faithfully reflects the layer that produced
it: ``run`` passes through the shell command's own return code
(:class:`~otto.result.CommandResult.exit_code`), while scalar verbs map
:class:`~otto.utils.Status` to a small fixed set of codes
(:attr:`~otto.result.Result.exit_code`).
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import REPO_E2E, assert_output_dir, run_otto

pytestmark = pytest.mark.hostless


def test_run_passes_through_command_retcode(tmp_path: Path) -> None:
    """``run`` on the local host exits with the command's own retcode (here, 42).

    Uses ``sh -c 'exit 42'`` rather than a bare top-level ``exit 42``: the
    latter terminates *otto's own persistent shell session* before its
    sentinel-based retcode protocol can report back (a pre-existing,
    session-lifecycle characteristic of any persistent-shell backend,
    orthogonal to this design's retcode-passthrough contract). Running the
    exit inside a subshell keeps the session alive to report the real
    ``CommandResult.exit_code`` passthrough this test targets.
    """
    r = run_otto(
        ["host", "local", "run", "sh -c 'exit 42'"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="veggies",
    )
    assert r.returncode == 42, r.stderr
    assert_output_dir(tmp_path, "host")


def test_bare_exit_kills_session_and_exits_255(tmp_path: Path) -> None:
    """A bare top-level ``exit`` pins the session-lifecycle contract: 255.

    The persistent shell dies before the sentinel-retcode protocol reports,
    so the command surfaces as never-ran (``retcode == -1`` → exit 255,
    matching ssh's connection-error convention). Pinned so a change in
    session EOF handling can't silently alter the CLI contract.
    """
    r = run_otto(
        ["host", "local", "run", "exit 42"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="veggies",
    )
    assert r.returncode == 255, r.stderr


def test_run_success_exits_zero(tmp_path: Path) -> None:
    """``run 'true'`` on the local host exits 0."""
    r = run_otto(
        ["host", "local", "run", "true"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="veggies",
    )
    assert r.returncode == 0, r.stderr
    assert_output_dir(tmp_path, "host")


def test_failing_get_maps_status(tmp_path: Path) -> None:
    """``get`` of a nonexistent path exits 2 (``Status.Error``), with a clean diagnostic."""
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    r = run_otto(
        ["host", "local", "get", "/no/such/file", str(dest_dir)],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="veggies",
    )
    assert r.returncode == 2, r.stderr  # Status.Error
    assert "no/such/file" in r.stdout + r.stderr
