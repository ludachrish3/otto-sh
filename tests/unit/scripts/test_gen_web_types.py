"""Pins for scripts/gen_web_types.sh's any-stderr rule and npm notifier switch.

json-schema-to-typescript writes nothing to stderr on a clean run, so the
script fails on any stderr output. npm's weekly "New major version of npm
available!" notice is npx's own stderr, so without the update notifier
switched off that notice alone would fail `make web` on unchanged code.

Each case runs a COPY of the script from a throwaway root (the script ``cd``s
to its own parent directory), with a no-op fake ``uv`` (the schema export)
and a fake ``npx`` first on PATH, so neither otto nor Node ever runs.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_SCRIPT = PROJECT_ROOT / "scripts" / "gen_web_types.sh"

# The fake npx: records the update-notifier setting it was handed, then writes
# the scripted stderr and exits 0.
_FAKE_NPX = """#!/usr/bin/env bash
printf '%s' "${npm_config_update_notifier-unset}" > "$FAKE_NPX_NOTIFIER"
printf '%b' "$FAKE_NPX_STDERR" >&2
exit 0
"""


def _fake(bin_dir: Path, name: str, body: str) -> None:
    tool = bin_dir / name
    tool.write_text(body)
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)


def _run(tmp_path: Path, *, stderr: str = "") -> subprocess.CompletedProcess[str]:
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "web").mkdir()
    shutil.copy(_SCRIPT, root / "scripts" / _SCRIPT.name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "npx", _FAKE_NPX)
    _fake(bin_dir, "uv", "#!/usr/bin/env bash\nexit 0\n")
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


def test_clean_generation_passes(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_any_npx_stderr_fails_and_is_reprinted(tmp_path: Path) -> None:
    result = _run(tmp_path, stderr="warning: unsupported schema construct\\n")
    assert result.returncode != 0, "stderr output from json2ts must fail the script"
    assert "unsupported schema construct" in result.stderr, (
        "the failure message must reprint the captured stderr"
    )


def test_npm_update_notifier_is_switched_off(tmp_path: Path) -> None:
    """The update notice is npx's own stderr, so npx must be handed the
    notifier switched off, or a fresh npm cache fails type generation."""
    _run(tmp_path)
    assert (tmp_path / "npx-notifier").read_text() == "false", (
        "scripts/gen_web_types.sh must run npx with npm_config_update_notifier=false"
    )
