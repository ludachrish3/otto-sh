"""End-to-end test for stability testing with class-scoped async fixtures.

Runs ``otto test --iterations 3 TestStabilityFixture`` as a subprocess to
verify that stability iterations do not tear down class-scoped async
fixtures.  The ``TestStabilityFixture`` suite in repo1 establishes a real
SSH connection during class setup and reuses it on every iteration.

This catches the bug where ``runtestprotocol`` (called in a loop) destroyed
class-scoped fixtures between iterations, breaking SSH connections bound
to the original event loop with::

    RuntimeError: Task got Future attached to a different loop

Prerequisites:
    - Vagrant test VM ``test1`` must be running (lab ``unix``).

Running::

    uv run pytest tests/unit/suite/test_stability_e2e.py \\
        -m integration -v --override-ini 'addopts='
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import REPO1, assert_output_dir, run_otto


@pytest.mark.integration
@pytest.mark.xdist_group("stability_e2e")
class TestStabilityE2E:
    """Subprocess-based e2e: SSH connections survive stability iterations."""

    def test_ssh_connection_survives_iterations(self, tmp_path: Path):
        """Run TestStabilityFixture with --iterations 3; SSH must not break."""
        xdir = tmp_path / "xdir"
        xdir.mkdir()

        result = run_otto(
            ["test", "--iterations", "3", "TestStabilityFixture"],
            xdir=xdir,
            sut_dirs=REPO1,
            lab="unix",
        )

        combined = result.stdout + result.stderr

        # Must not contain the event-loop mismatch error that occurs when
        # class-scoped fixtures are torn down between iterations
        assert "attached to a different loop" not in combined, (
            f"Event loop mismatch detected — class-scoped fixture was "
            f"torn down between iterations:\n{combined}"
        )

        assert result.returncode == 0, (
            f"otto test exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )

        # Stability report should show all iterations passed
        assert "STABLE" in result.stdout, f"Expected STABLE in stability report:\n{result.stdout}"
        # a real suite run produces results → test output dir created
        assert_output_dir(xdir, "test")

    def test_single_run_baseline(self, tmp_path: Path):
        """Sanity check: TestStabilityFixture works without stability mode."""
        xdir = tmp_path / "xdir"
        xdir.mkdir()

        result = run_otto(
            ["test", "TestStabilityFixture"],
            xdir=xdir,
            sut_dirs=REPO1,
            lab="unix",
        )

        assert result.returncode == 0, (
            f"otto test exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
        # a real suite run produces results → test output dir created
        assert_output_dir(xdir, "test")
