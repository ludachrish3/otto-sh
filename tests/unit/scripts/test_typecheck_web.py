"""Pins for scripts/typecheck_web.sh's any-stderr rule and npm notifier switch.

tsc prints its diagnostics on stdout, so anything on stderr is Node or npx
talking -- a deprecation warning, or npm's weekly "New major version of npm
available!" notice. The script fails on any stderr output, which makes the
notice a false failure on unchanged code unless the script switches npm's
update notifier off (it did not, and ``npm_config_cache=$(mktemp -d)
scripts/typecheck_web.sh`` exited 1 on the notice alone).

Each case runs a COPY of the script from a throwaway root holding a minimal
``web/untitledui.lock.json`` (the script resolves web/ from its own location),
with a fake ``npx`` first on PATH, so no real tsc ever runs.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_SCRIPT = PROJECT_ROOT / "scripts" / "typecheck_web.sh"

# The fake npx: records the update-notifier setting it was handed, then writes
# the scripted stderr and exits 0 with no diagnostics on stdout (a clean tsc).
_FAKE_NPX = """#!/usr/bin/env bash
printf '%s' "${npm_config_update_notifier-unset}" > "$FAKE_NPX_NOTIFIER"
printf '%b' "$FAKE_NPX_STDERR" >&2
exit 0
"""


def _run(tmp_path: Path, *, stderr: str = "") -> subprocess.CompletedProcess[str]:
    if shutil.which("jq") is None:
        pytest.fail("scripts/typecheck_web.sh needs `jq` on PATH; install it")
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "web").mkdir()
    shutil.copy(_SCRIPT, root / "scripts" / _SCRIPT.name)
    (root / "web" / "untitledui.lock.json").write_text('{"paths": ["src/components/**"]}\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npx = bin_dir / "npx"
    npx.write_text(_FAKE_NPX)
    npx.chmod(npx.stat().st_mode | stat.S_IXUSR)
    env = {
        # A notifier setting inherited from the caller must not decide the
        # outcome: the script has to switch it off itself.
        **{k: v for k, v in os.environ.items() if k.lower() != "npm_config_update_notifier"},
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_NPX_NOTIFIER": str(tmp_path / "npx-notifier"),
        "FAKE_NPX_STDERR": stderr,
    }
    return subprocess.run(
        ["bash", str(root / "scripts" / _SCRIPT.name)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_clean_tsc_passes(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "typecheck_web: OK" in result.stdout
    assert result.stderr == ""


def test_any_npx_stderr_fails_and_is_reprinted(tmp_path: Path) -> None:
    result = _run(tmp_path, stderr="(node:1) DeprecationWarning: something\\n")
    assert result.returncode != 0, "stderr output from the tsc pass must fail the gate"
    assert "DeprecationWarning: something" in result.stderr, (
        "the failure message must reprint the captured stderr"
    )


def test_npm_update_notifier_is_switched_off(tmp_path: Path) -> None:
    """The update notice is npx's own stderr, so npx must be handed the
    notifier switched off, or a fresh npm cache fails the type-check."""
    _run(tmp_path)
    assert (tmp_path / "npx-notifier").read_text() == "false", (
        "scripts/typecheck_web.sh must run npx with npm_config_update_notifier=false"
    )
