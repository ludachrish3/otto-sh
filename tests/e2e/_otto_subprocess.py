"""Shared subprocess harness for CLI e2e tests.

Provides :func:`run_otto`, which launches the ``otto`` binary as a subprocess
with subprocess-coverage wiring and a controlled environment.  Import this
module from test files in ``tests/e2e/`` — the leading underscore prevents
pytest from collecting it as a test module.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO1 = PROJECT_ROOT / "tests" / "repo1"
REPO_E2E = PROJECT_ROOT / "tests" / "repo_e2e"
COVERAGERC = PROJECT_ROOT / ".coveragerc"
COVERAGE_BOOTSTRAP = PROJECT_ROOT / "tests" / "_coverage_bootstrap"
OTTO_BIN = Path(sys.executable).parent / "otto"


def run_otto(
    argv: list[str],
    *,
    xdir: Path | None = None,
    sut_dirs: Path = REPO_E2E,
    lab: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run ``otto ARGV`` as a subprocess and return the result.

    Coverage is wired via ``COVERAGE_PROCESS_START`` so that subprocess runs
    are folded into the combined coverage report.

    Parameters
    ----------
    argv:
        Arguments to pass after the ``otto`` binary (and after any ``--lab``
        flag if *lab* is given).
    xdir:
        Path to an existing directory for otto's ``--xdir`` output.  When
        ``None`` the ``OTTO_XDIR`` variable is omitted.
    sut_dirs:
        Path to the SUT repo root passed as ``OTTO_SUT_DIRS``.  Defaults to
        the dedicated e2e fixture repo :data:`REPO_E2E`.
    lab:
        Lab token prepended as ``--lab <lab>`` before *argv*.  When ``None``
        no ``--lab`` flag is inserted.
    extra_env:
        Additional environment variables merged *last* (overriding defaults).
    timeout:
        Subprocess timeout in seconds (default 60).

    Returns
    -------
    subprocess.CompletedProcess[str]
        The completed process result (``check=False``).
    """
    cmd: list[str] = [str(OTTO_BIN)]
    if lab:
        cmd += ["--lab", lab]
    cmd += argv

    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OTTO_SUT_DIRS": str(sut_dirs),
        "COVERAGE_PROCESS_START": str(COVERAGERC),
        "PYTHONPATH": os.pathsep.join(
            [str(COVERAGE_BOOTSTRAP), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }
    if xdir is not None:
        env["OTTO_XDIR"] = str(xdir)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
        check=False,
    )
