"""Shared subprocess harness for CLI e2e tests.

Provides :func:`run_otto`, which launches the ``otto`` binary as a subprocess
with subprocess-coverage wiring and a controlled environment, and the two env
builders behind it — :func:`coverage_subprocess_env` (the subprocess-coverage
dance alone, for plain-python child processes) and
:func:`otto_subprocess_env` (that plus the otto keys, for anything that spawns
the ``otto`` binary through its own machinery, e.g. a PTY driver).

This module is the ONE home of the ``COVERAGE_PROCESS_START`` env dance (gate
G8): every local copy that ever existed drifted, and every one of them dropped
the ``-p no:tach`` scar key first (issue #193). Import from here — the leading
underscore prevents pytest from collecting this as a test module.
"""

import atexit
import functools
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from otto import layout
from otto.logger.management import _LOG_DIR_NAME_RE
from tests._ambient_env import ambient
from tests._fixtures.paths import CHECKOUT_ROOT

PROJECT1 = CHECKOUT_ROOT / "tests" / "project1"
PROJECT2 = CHECKOUT_ROOT / "tests" / "project2"
PROJECT5 = CHECKOUT_ROOT / "tests" / "project5"
PROJECT_CTR = CHECKOUT_ROOT / "tests" / "project_ctr"
PROJECT_E2E = CHECKOUT_ROOT / "tests" / "project_e2e"
COVERAGERC = CHECKOUT_ROOT / ".coveragerc"
COVERAGE_BOOTSTRAP = CHECKOUT_ROOT / "tests" / "_coverage_bootstrap"
OTTO_BIN = Path(sys.executable).parent / "otto"


def coverage_subprocess_env(
    *,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """The subprocess-coverage env dance, and nothing else.

    ``PATH``/``HOME`` passthrough plus ``COVERAGE_PROCESS_START`` and the
    ``sitecustomize`` bootstrap on ``PYTHONPATH`` — enough for any plain
    ``python`` child process to fold its lines into the combined report.
    Use :func:`otto_subprocess_env` for children that run ``otto`` itself.

    ``PYTHONPYCACHEPREFIX`` IS A PASSTHROUGH, for the same reason ``PATH`` and
    ``HOME`` are: this env is built from scratch, so a variable not named here
    does not reach the child. ``tests/conftest.py`` exports that prefix for the
    whole session precisely so no test process writes ``__pycache__`` into the
    editable ``src/otto`` tree — a directory created there bumps its mtime, and
    CPython's ``FileFinder`` then re-lists it in every other process importing
    from ``src/otto``, moving the import-budget child's audited I/O counters by
    one (#321, #343, #360, #361). These children import otto, so dropping the
    prefix here put the writes straight back: measured, one ``-n 4`` run of
    ``tests/unit/host`` against a cold tree created six of them. Absent when
    nothing set one (a bare ``python -m pytest`` outside this conftest, or a
    direct call), which leaves the child's default behaviour untouched.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "COVERAGE_PROCESS_START": str(COVERAGERC),
        "PYTHONPATH": os.pathsep.join(
            [str(COVERAGE_BOOTSTRAP), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }
    pycache_prefix = os.environ.get("PYTHONPYCACHEPREFIX")
    if pycache_prefix:
        env["PYTHONPYCACHEPREFIX"] = pycache_prefix
    if extra_env:
        env.update(extra_env)
    return env


@functools.cache
def _fallback_otto_home() -> Path:
    """One isolated ``OTTO_HOME`` per test process, for callers with no xdir.

    Created lazily (and cached) so importing this module allocates nothing.
    Shared within a process (one dir per xdist worker) rather than fresh per
    call: the workspace key inside it already separates project-root sets, and a
    fresh home per call would make every child permanently cold, masking
    warm-path regressions the same way pointing at the real ``~/.otto``
    masked cold ones. Removed at interpreter exit — without that, one
    scratch home per worker per session would accumulate in /tmp forever:
    the exact unbounded growth pinning OTTO_HOME exists to stop, relocated.
    """
    path = tempfile.mkdtemp(prefix="otto-e2e-home-")
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return Path(path)


_INSTALLER_INDEX_VARS = (
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_INDEX",
    "PIP_FIND_LINKS",
    "UV_FIND_LINKS",
    "UV_CONFIG_FILE",
)


def _isolate_installer_config(env: dict[str, str]) -> None:
    """Cut *env* off from every installer index source but the command line.

    The env fixture's version (``0.1.0+otto.fixture``) is one PyPI cannot
    serve, but a private index in user, global or site installer config could.
    So the config files are switched off (``PIP_CONFIG_FILE=/dev/null``,
    ``UV_NO_CONFIG=1``) and the index overrides dropped, leaving
    ``--find-links`` as the only way a resolver can find the fixture.

    The drop list exists even though the env is built from scratch: it is
    defence in depth for when ``extra_env`` or a future change copies the
    ambient environment.
    """
    env["PIP_CONFIG_FILE"] = os.devnull
    env["UV_NO_CONFIG"] = "1"
    for var in _INSTALLER_INDEX_VARS:
        env.pop(var, None)


def otto_subprocess_env(
    *,
    xdir: Path | None = None,
    roots: Path | None = PROJECT_E2E,
    term: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the controlled environment for an ``otto`` child process.

    The coverage dance plus the otto keys: ``OTTOPATH`` (omitted when
    *roots* is None), ``OTTO_XDIR`` (omitted when *xdir* is None), ``TERM``
    (omitted when *term* is None — PTY drivers need one), ``OTTO_HOME`` (see
    below), and the #193 scar:

    the child's in-process pytest sessions must never load tach's pytest11
    plugin — its Rust extension sets a C-level Ctrl-C handler at import and
    panics (``MultipleHandlers``) when ``otto test`` runs consecutive sessions
    in one process. The dev venv can carry tach (lint group), so block it here
    rather than trust the env.

    ``OTTO_HOME``: every child gets a PRIVATE user-level home. The completion
    caches live in the workspace home — ``~/.otto`` without this — so an
    unpinned child reads the developer's real cache state (making test
    outcomes depend on what ran on the machine before) and writes fixture
    corpora back into it on every invocation. Beside the xdir when there is
    one (``<xdir>/otto-home`` — per-test, so a test's second run is
    legitimately WARM), a per-process scratch home otherwise. *extra_env*
    merges last, so tests probing home resolution itself still override.

    ``OTTO_INIT_TIMEOUT`` passes through when set, so a slow machine's raised
    readiness deadline reaches the otto under test, not only the test process.

    Installer config is isolated (:func:`_isolate_installer_config`), so a
    ``pip``/``uv`` the child spawns resolves from ``--find-links`` alone.
    """
    env = coverage_subprocess_env()
    env["PYTEST_ADDOPTS"] = "-p no:tach"
    env["OTTO_HOME"] = str(xdir / "otto-home" if xdir is not None else _fallback_otto_home())
    if roots is not None:
        env["OTTOPATH"] = str(roots)
    if xdir is not None:
        env["OTTO_XDIR"] = str(xdir)
    if term is not None:
        env["TERM"] = term
    init_timeout = ambient("OTTO_INIT_TIMEOUT")
    if init_timeout:
        env["OTTO_INIT_TIMEOUT"] = init_timeout
    _isolate_installer_config(env)
    if extra_env:
        env.update(extra_env)
    return env


def run_otto(
    argv: list[str],
    *,
    xdir: Path | None = None,
    roots: Path | None = PROJECT_E2E,
    lab: str | None = None,
    extra_argv_prefix: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
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
    roots:
        Path to the project root passed as ``OTTOPATH``.  Defaults to
        the dedicated e2e fixture project :data:`PROJECT_E2E`; ``None`` omits the
        variable entirely.
    lab:
        Lab token prepended as ``--lab <lab>`` before *argv*.  When ``None``
        no ``--lab`` flag is inserted.
    extra_argv_prefix:
        Root-level flags inserted between ``otto`` and any ``--lab`` token
        (e.g. ``["-R"]`` to bypass the reservation gate).
    extra_env:
        Additional environment variables merged *last* (overriding defaults).
    cwd:
        Working directory for the child (default: the project root).
    timeout:
        Subprocess timeout in seconds (default 60).

    Returns
    -------
    subprocess.CompletedProcess[str]
        The completed process result (``check=False``).
    """
    cmd: list[str] = [str(OTTO_BIN)]
    if extra_argv_prefix:
        cmd += extra_argv_prefix
    if lab:
        cmd += ["--lab", lab]
    cmd += argv

    return subprocess.run(
        cmd,
        env=otto_subprocess_env(xdir=xdir, roots=roots, extra_env=extra_env),
        capture_output=True,
        text=True,
        cwd=str(cwd if cwd is not None else CHECKOUT_ROOT),
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# Output-directory assertions
# ---------------------------------------------------------------------------
#
# otto writes each real invocation's output dir as
# ``<xdir>/otto-output/<command>/<timestamp>_<subcommand>/`` (see
# ``otto.logger.management.create_output_dir``; the command's directory comes
# from ``otto.layout.command_output_dir`` on both sides).  The e2e policy is that a
# command creates such a dir only when it does real work on a host — help /
# discovery / schema and the no-remote commands (cov, reservation) must not.
# These helpers let every e2e test assert the presence or absence of that dir.
#
# Everything under ``otto-output/`` is otto's own, so no command list is kept
# here: a test's scratch dirs elsewhere in the xdir (e.g. ``back/`` / ``dest/``
# in transfer tests) can never register, and a run of any command does.


def output_dirs(xdir: Path, command: str | None = None) -> list[Path]:
    """Return the per-invocation output dirs otto created under *xdir*.

    With *command* given, only that command's dirs; otherwise every command's.
    """
    if command is not None:
        roots = [layout.command_output_dir(xdir, command)]
    else:
        out = layout.output_root(xdir)
        roots = sorted(p for p in out.iterdir() if p.is_dir()) if out.is_dir() else []
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            # Match only real per-invocation dirs (``YYYYMMDD_HHMMSS_mmm[_sub]``)
            # against otto's own naming rule.
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
    return max(found)
