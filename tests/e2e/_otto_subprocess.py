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

from otto.logger.management import _LOG_DIR_NAME_RE

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


# ---------------------------------------------------------------------------
# Output-directory assertions
# ---------------------------------------------------------------------------
#
# otto writes each real invocation's output dir as
# ``<xdir>/<command>/<timestamp>_<subcommand>/`` (see
# ``otto.logger.management.create_output_dir``).  The e2e policy is that a
# command creates such a dir only when it does real work on a host — help /
# discovery / schema and the no-remote commands (cov, reservation) must not.
# These helpers let every e2e test assert the presence or absence of that dir.
#
# Scoped to the known command names so a test's own ``xdir`` scratch dirs
# (e.g. ``back/`` / ``dest/`` in transfer tests) never register as output dirs.

OUTPUT_COMMANDS: tuple[str, ...] = (
    "run",
    "test",
    "host",
    "monitor",
    "cov",
    "docker",
    "reservation",
)


def output_dirs(xdir: Path, command: str | None = None) -> list[Path]:
    """Return the per-invocation output dirs otto created under *xdir*.

    With *command* given, only that command's dirs; otherwise every command's.
    """
    commands = (command,) if command is not None else OUTPUT_COMMANDS
    found: list[Path] = []
    for cmd in commands:
        root = xdir / cmd
        if root.is_dir():
            # Match only real per-invocation dirs (``YYYYMMDD_HHMMSS_mmm[_sub]``)
            # against otto's own naming rule, so a test's same-named scratch dir
            # (e.g. a ``--cov-dir`` at ``tmp_path/"cov"``) can never register.
            found.extend(p for p in root.iterdir() if p.is_dir() and _LOG_DIR_NAME_RE.match(p.name))
    return found


def assert_no_output_dir(xdir: Path) -> None:
    """Assert otto created NO per-invocation output dir under *xdir*.

    Use for help / discovery / schema and the no-remote commands (cov,
    reservation) — anything purely informational or that never touches a host.
    """
    found = output_dirs(xdir)
    assert not found, f"expected no output dir under {xdir}, found: {found}"


def assert_output_dir(xdir: Path, command: str) -> Path:
    """Assert otto created at least one *command* output dir under *xdir*.

    Returns the most recently created one (timestamped names sort lexically).
    Use for commands that do real work on a host (run/test suites, host verbs,
    monitor, docker).
    """
    found = output_dirs(xdir, command)
    assert found, f"expected a {command!r} output dir under {xdir}, found none"
    return sorted(found)[-1]
