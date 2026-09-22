"""Real ``$?`` contracts for the Result family (spec 2026-07-01).

These exercise the built-in ``local`` host end-to-end (no bed required) to
verify that the CLI's exit code faithfully reflects the layer that produced
it: ``exec`` passes through the shell command's own return code
(:class:`~otto.result.CommandResult.exit_code`), while scalar verbs map
:class:`~otto.utils.Status` to a small fixed set of codes
(:attr:`~otto.result.Result.exit_code`).
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import REPO_E2E, assert_output_dir, run_otto

pytestmark = pytest.mark.hostless


def test_run_passes_through_command_retcode(tmp_path: Path) -> None:
    """``exec`` on the local host exits with the command's own retcode (here, 42).

    Uses ``sh -c 'exit 42'`` rather than a bare top-level ``exit 42``: the
    latter terminates *otto's own persistent shell session* before its
    sentinel-based retcode protocol can report back (a pre-existing,
    session-lifecycle characteristic of any persistent-shell backend,
    orthogonal to this design's retcode-passthrough contract). Running the
    exit inside a subshell keeps the session alive to report the real
    ``CommandResult.exit_code`` passthrough this test targets.
    """
    r = run_otto(
        ["host", "local", "exec", "sh -c 'exit 42'"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="unix",
    )
    assert r.returncode == 42, r.stderr
    assert_output_dir(tmp_path, "host")


def test_a_command_that_never_completes_exits_255(tmp_path: Path) -> None:
    """255 is ssh's connection-error code: the command never returned a retcode.
    A timeout is the one way to get there on a stateless exec."""
    r = run_otto(
        ["host", "local", "exec", "sleep 5", "--timeout", "0.2"], xdir=tmp_path, lab="unix"
    )
    assert r.returncode == 255, r.stderr


def test_run_success_exits_zero(tmp_path: Path) -> None:
    """``exec 'true'`` on the local host exits 0."""
    r = run_otto(
        ["host", "local", "exec", "true"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="unix",
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
        lab="unix",
    )
    assert r.returncode == 2, r.stderr  # Status.Error
    assert "no/such/file" in r.stdout + r.stderr
