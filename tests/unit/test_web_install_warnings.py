"""Pins for the Makefile's web-install recipe: an `npm warn` line is a failure.

npm ci exits 0 after printing `npm warn deprecated ...`, EBADENGINE and the
like, and web-install's quiet mode used to discard those lines with the rest
of the log. The recipe now fails on any of them, reprints them, and deletes
the install stamp so the next make run retries the install.

Each case runs the REAL Makefile's web-install target (``make -f``) from a
throwaway directory, with a fake ``npm`` first on PATH, so no install ever
runs and nothing under the repo's own web/ is touched.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_MAKEFILE = PROJECT_ROOT / "Makefile"

# Directories the Makefile `find`s at parse time; created empty so the parse
# is quiet in the throwaway directory.
_PARSE_TIME_DIRS = [
    "web/src",
    "docs",
    "src/otto",
    "tests/e2e/monitor/dashboard",
    "tests/e2e/cov/report_browser",
]

# The fake npm: behaves like a successful `npm ci` -- writes the install
# stamp, prints the scripted output, exits 0.
_FAKE_NPM = """#!/usr/bin/env bash
mkdir -p node_modules && : > node_modules/.package-lock.json
printf '%b' "$FAKE_NPM_OUTPUT"
exit 0
"""


def _run_web_install(tmp_path: Path, output: str) -> subprocess.CompletedProcess[str]:
    make = shutil.which("make")
    if make is None:
        pytest.fail("the web-install pins need `make` on PATH")
    for rel in _PARSE_TIME_DIRS:
        (tmp_path / rel).mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(_FAKE_NPM)
    npm.chmod(npm.stat().st_mode | stat.S_IXUSR)
    env = {
        key: value
        for key, value in os.environ.items()
        # An outer make's flags (-n, -j, ...) must not ride into this one.
        if key not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"}
    }
    env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    env["FAKE_NPM_OUTPUT"] = output
    return subprocess.run(
        [make, "--no-print-directory", "-f", str(_MAKEFILE), "web-install"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_clean_install_passes_and_keeps_the_stamp(tmp_path: Path) -> None:
    result = _run_web_install(tmp_path, "added 5 packages in 1s\\n")
    assert result.returncode == 0, result.stderr
    assert "added 5 packages" in result.stdout, "the one-line tally must be shown"
    assert (tmp_path / "web" / "node_modules" / ".package-lock.json").exists()


@pytest.mark.parametrize(
    "warning",
    [
        "npm warn deprecated x@1.0.0: no longer supported",
        "npm WARN EBADENGINE Unsupported engine",
    ],
)
def test_npm_warn_fails_reprints_and_drops_the_stamp(tmp_path: Path, warning: str) -> None:
    result = _run_web_install(tmp_path, f"{warning}\\nadded 5 packages in 1s\\n")
    assert result.returncode != 0, "an `npm warn` line from npm ci must fail web-install"
    assert warning in result.stderr, "the warning must be reprinted on stderr"
    assert "warnings are errors" in result.stderr
    assert not (tmp_path / "web" / "node_modules" / ".package-lock.json").exists(), (
        "a warned-about install must not leave the stamp that marks it good"
    )
