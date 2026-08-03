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

from tests._ambient_env import AMBIENT_OPT_INS as AMBIENT_OPT_IN_NOTES
from tests._ambient_env import ambient, ambient_opt_ins

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Imported, never re-declared: a hand-copied second copy of the allowlist is
# what let issue #192 through — the copy here can agree with itself while the
# real strip in tests/conftest.py is missing an entry.
AMBIENT_OPT_INS = ambient_opt_ins()

# A distinct value per variable, so a probe assertion can't pass on a
# coincidental collision with some reader's default.
_PROBE_VALUES = {name: f"probe-{i}" for i, name in enumerate(sorted(AMBIENT_OPT_INS))}

# Deliberately NOT OTTO_-prefixed: the guard under test would strip it.
PROBE_FLAG = "_TEST_OTTO_HERMETICITY_PROBE"


@pytest.mark.skipif(PROBE_FLAG not in os.environ, reason="probe for the subprocess pin below")
def test_probe_ambient_otto_env_is_stripped():
    """Runs only as the single test of the pin's inner session, where the
    process env is known exactly. In a full run this assertion would be
    order-fragile: any earlier in-worker test that exports an ``OTTO_*``
    variable without cleanup would fail it spuriously — the guard strips
    the *ambient* env once at conftest import, not between tests."""
    leaked = [k for k in os.environ if k.startswith("OTTO_") and k not in AMBIENT_OPT_INS]
    assert leaked == [], (
        f"ambient otto configuration leaked into the test process: {leaked} "
        "(tests/conftest.py should have stripped these at import time)"
    )
    # Positive pin (the subprocess below sets every one of them): EVERY
    # declared harness opt-in must SURVIVE the strip. Each of these is read
    # from the ambient environment by harness code, and each fails SILENTLY
    # when stripped — the reader just sees its default and the run continues
    # green against the wrong venue/seed/depth. That is how issue #192
    # happened: OTTO_CHAOS_DOCKER was undeclared, so nightly's `loopback` job
    # silently targeted the bed host pepper instead.
    stripped = sorted(k for k in AMBIENT_OPT_INS if os.environ.get(k) != _PROBE_VALUES[k])
    assert stripped == [], (
        f"declared ambient opt-ins were stripped from the env: {stripped} — "
        "each reader would silently fall back to its default (see "
        "tests/_ambient_env.py for what each one drives)"
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
            # Product configuration: must be stripped.
            "OTTO_SUT_DIRS": "/somewhere/else/tests/repo1",
            "OTTO_XDIR": "/somewhere/else/xdir",
            # Every declared harness opt-in: must survive.
            **_PROBE_VALUES,
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


def test_reading_an_undeclared_opt_in_raises():
    """``ambient()`` must refuse a name the strip does not spare.

    This is the only loud moment available for the whole bug class. An
    undeclared opt-in is gone from ``os.environ`` before its reader runs, so
    every downstream symptom is a silent fallback to a default — a chaos run
    against the wrong venue, a "reproduce with this seed" that reseeds, a
    soak at the wrong depth. None of those fail anything. Raising here turns
    "forgot to declare it" into an immediate error at the first read.
    """
    with pytest.raises(KeyError, match="not a declared ambient harness opt-in"):
        ambient("OTTO_NOT_DECLARED")


def test_every_declared_opt_in_documents_what_it_drives():
    """A bare name in the registry is not enough to keep it honest.

    The note is what tells the next person touching the strip which lane
    breaks if they drop the entry — and what tells a reviewer whether a new
    entry is a genuine harness knob or otto product configuration that has
    no business surviving.
    """
    thin = sorted(name for name, note in AMBIENT_OPT_IN_NOTES.items() if len(note) < 40)
    assert thin == [], f"declared opt-ins with no usable note: {thin}"


def test_bootstrap_state_cannot_leak_between_tests():
    """Pin: a test that poisons ``otto.bootstrap``'s caches can't fail the next one.

    ``bootstrap()`` memoizes into module globals and folds the append-only
    ``_discovery_errors`` into every later result, so a test that drives the CLI
    with ``OTTO_SUT_DIRS`` pointed at a scratch repo leaves a framed discovery
    error behind after ``monkeypatch`` restores the variable. Every later test
    on that worker then hits ``fail_loud_on_bootstrap_errors()`` and exits 1
    before Click reports the missing ``--lab`` — the "must exit 2" tests below
    fail with a bare ``SystemExit(1)``. ``tests/conftest.py``'s
    ``_reset_bootstrap_state`` clears the caches between tests; this runs the
    exact historical pair, in order, in one process to prove it.

    Ordered pair, so ``-p no:randomly``: the leak is order-dependent by nature.
    """
    poisoner = "tests/unit/cli/test_init_prompts.py::test_epilogue_skips_sut_dirs_when_already_set"
    victim = (
        "tests/unit/cli/test_main.py::TestArgumentValidation"
        "::test_lab_needing_path_without_lab_reports_missing_option"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            poisoner,
            victim,
            "-n0",
            "-q",
            "--no-cov",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"bootstrap state leaked between tests (rc={result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    # A rename of either test would otherwise make this pass vacuously.
    assert "2 passed" in result.stdout, f"the ordered pair did not run:\n{result.stdout}"
