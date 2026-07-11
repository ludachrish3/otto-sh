"""End-to-end test for the ``bootstrap()``/``run_suite()`` library path.

Drives a plain Python script — no ``otto`` CLI involved at all — that mirrors
the "Running suites from Python" walkthrough in
``docs/guide/library-usage.md``: ``bootstrap()``, ``find_suite()``,
``run_suite()``. This is the flow that caught the extraction's only shipped
bug (``run_suite`` assuming a CLI-installed context was already active); this
test regresses it directly, with the CLI layer entirely out of the picture.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import COVERAGE_BOOTSTRAP, COVERAGERC, PROJECT_ROOT, REPO_E2E

pytestmark = pytest.mark.hostless

# Script run via `sys.executable <path>` (not `otto`): bootstrap the
# composition root, look the suite up by name (as a config-driven caller
# would), run it, and report back everything the parent test needs as one
# JSON line on stdout.
_SCRIPT = """
import json
import os
from pathlib import Path

from otto.bootstrap import bootstrap
from otto.context import try_get_context
from otto.suite import find_suite, run_suite

output_dir = Path(os.environ["OTTO_TEST_OUTPUT_DIR"])

bootstrap()
cls = find_suite("TestE2EFixture")
r = run_suite(cls, options=cls.Options(), output_dir=output_dir)

print(json.dumps({
    "passed": r.passed,
    "exit_code": r.exit_code,
    "junit_exists": r.junit_paths[0].exists(),
    "context_none_after": try_get_context() is None,
}))
"""


def _run_library_script(
    tmp_path: Path,
    *,
    output_dir: Path,
    extra_env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run :data:`_SCRIPT` as a plain ``python`` subprocess (no otto binary).

    Follows the same env conventions as :func:`tests.e2e._otto_subprocess.run_otto`
    (``OTTO_SUT_DIRS``, subprocess-coverage wiring) but drives ``sys.executable``
    directly against a script file, since this is a library-only flow with no
    CLI entry point involved.
    """
    script_path = tmp_path / "run_suite_script.py"
    script_path.write_text(_SCRIPT)

    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OTTO_SUT_DIRS": str(REPO_E2E),
        "OTTO_TEST_OUTPUT_DIR": str(output_dir),
        "COVERAGE_PROCESS_START": str(COVERAGERC),
        "PYTHONPATH": os.pathsep.join(
            [str(COVERAGE_BOOTSTRAP), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [sys.executable, str(script_path)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
        check=False,
    )


def test_library_run_suite_pass(tmp_path: Path) -> None:
    """``bootstrap()`` -> ``find_suite()`` -> ``run_suite()`` on a passing suite.

    The driving script exits 0; the printed :class:`SuiteRunResult` reports
    ``passed``, a real JUnit file was written, and the library-installed
    context was torn down (``try_get_context()`` is ``None`` again).
    """
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = _run_library_script(tmp_path, output_dir=output_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["passed"] is True
    assert payload["exit_code"] == 0
    assert payload["junit_exists"] is True
    assert payload["context_none_after"] is True


def test_library_run_suite_fail(tmp_path: Path) -> None:
    """``OTTO_E2E_FAIL=1`` fails the suite — but the driving SCRIPT still exits 0.

    ``run_suite`` never raises for a red suite; it reports the failure in the
    returned :class:`SuiteRunResult`. Only the plain Python script's own logic
    (absent here) would turn that into a nonzero process exit — mirroring how
    a real caller decides whether/how to propagate ``r.exit_code``.
    """
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = _run_library_script(tmp_path, output_dir=output_dir, extra_env={"OTTO_E2E_FAIL": "1"})

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["passed"] is False
    assert payload["exit_code"] == 1
    assert payload["junit_exists"] is True
    assert payload["context_none_after"] is True
