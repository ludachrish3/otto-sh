"""The test suite must be hermetic against ambient otto configuration.

A developer shell with ``OTTO_SUT_DIRS`` exported (say, pointing at another
checkout's ``tests/repo1``) leaks into every test that exercises a CLI path
whose callback calls ``bootstrap()`` without monkeypatching the env: the
ambient repo's suites get registered into the process-wide ``SUITES``
registry under foreign file paths, which later collide with the real
``tests/repo1`` imports in ``test_repo.py``'s bootstrap test (three
``BootstrapError: test suite ... is already registered`` failures, worker-
order dependent under xdist). ``tests/conftest.py`` therefore strips all
``OTTO_*`` variables (minus explicit harness toggles) at import time; these
tests pin that guard.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Harness opt-ins legitimately read from the ambient environment; everything
# else OTTO_-prefixed is otto *product* configuration and must not leak in.
ALLOWED_AMBIENT = {"OTTO_DETECT_ASYNCIO_LEAKS", "OTTO_TS_COVERAGE"}

# Deliberately NOT OTTO_-prefixed: the guard under test would strip it.
PROBE_FLAG = "_TEST_OTTO_HERMETICITY_PROBE"


@pytest.mark.skipif(PROBE_FLAG not in os.environ, reason="probe for the subprocess pin below")
def test_probe_ambient_otto_env_is_stripped():
    """Runs only as the single test of the pin's inner session, where the
    process env is known exactly. In a full run this assertion would be
    order-fragile: any earlier in-worker test that exports an ``OTTO_*``
    variable without cleanup would fail it spuriously — the guard strips
    the *ambient* env once at conftest import, not between tests."""
    leaked = [k for k in os.environ if k.startswith("OTTO_") and k not in ALLOWED_AMBIENT]
    assert leaked == [], (
        f"ambient otto configuration leaked into the test process: {leaked} "
        "(tests/conftest.py should have stripped these at import time)"
    )
    # Positive pin (the subprocess below sets it): an allowlisted harness
    # opt-in must SURVIVE the strip, or `make dashboard`'s OTTO_TS_COVERAGE gate
    # silently collects nothing and `make coverage-ts` fails with an opaque
    # empty-coverage error far downstream.
    assert os.environ.get("OTTO_TS_COVERAGE") == "1", (
        "allowlisted OTTO_TS_COVERAGE was stripped from the ambient env — the "
        "browser TS-coverage gate would no-op"
    )


def test_ambient_otto_env_cannot_leak_into_a_pytest_run():
    """End-to-end pin: a pytest run started from a polluted shell is hermetic."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{Path(__file__)}::test_probe_ambient_otto_env_is_stripped",
            "-n0",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
        env={
            **os.environ,
            PROBE_FLAG: "1",
            "OTTO_SUT_DIRS": "/somewhere/else/tests/repo1",
            "OTTO_XDIR": "/somewhere/else/xdir",
            "OTTO_TS_COVERAGE": "1",
        },
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"inner pytest saw ambient OTTO_* env (rc={result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    # Guard against silently passing on a deselected/skipped probe.
    assert "1 passed" in result.stdout, f"probe did not run:\n{result.stdout}"
