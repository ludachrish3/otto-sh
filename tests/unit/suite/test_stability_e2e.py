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
    - Vagrant test VM ``carrot`` must be running (lab ``veggies``).

Running::

    uv run pytest tests/unit/suite/test_stability_e2e.py \\
        -m integration -v --override-ini 'addopts='
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPO1_DIR = PROJECT_ROOT / "tests" / "repo1"
COVERAGERC = PROJECT_ROOT / ".coveragerc"
COVERAGE_BOOTSTRAP = PROJECT_ROOT / "tests" / "_coverage_bootstrap"
OTTO_BIN = Path(sys.executable).parent / "otto"


def _otto_env(xdir: Path) -> dict[str, str]:
    """Env for an ``otto`` subprocess with subprocess-coverage enabled."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OTTO_SUT_DIRS": str(REPO1_DIR),
        "OTTO_XDIR": str(xdir),
        "COVERAGE_PROCESS_START": str(COVERAGERC),
        "PYTHONPATH": os.pathsep.join(
            [str(COVERAGE_BOOTSTRAP), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }


def _run_otto(
    argv: list[str],
    *,
    xdir: Path,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run ``otto ARGV`` and return the result."""
    return subprocess.run(
        [str(OTTO_BIN), *argv],
        env=_otto_env(xdir),
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
    )


@pytest.mark.integration
@pytest.mark.xdist_group("stability_e2e")
class TestStabilityE2E:
    """Subprocess-based e2e: SSH connections survive stability iterations."""

    def test_ssh_connection_survives_iterations(self, tmp_path: Path):
        """Run TestStabilityFixture with --iterations 3; SSH must not break."""
        xdir = tmp_path / "xdir"
        xdir.mkdir()

        result = _run_otto(
            ["-l", "veggies", "test", "--iterations", "3", "TestStabilityFixture"],
            xdir=xdir,
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
        assert "STABLE" in result.stdout, (
            f"Expected STABLE in stability report:\n{result.stdout}"
        )

    def test_single_run_baseline(self, tmp_path: Path):
        """Sanity check: TestStabilityFixture works without stability mode."""
        xdir = tmp_path / "xdir"
        xdir.mkdir()

        result = _run_otto(
            ["-l", "veggies", "test", "TestStabilityFixture"],
            xdir=xdir,
        )

        assert result.returncode == 0, (
            f"otto test exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
