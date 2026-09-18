"""Pins for scripts/build_web_no_warnings.sh — the web build's warnings gate.

The gate once failed only on the ``(!)`` / `` WARN `` markers in the combined
build log. Two warnings slipped straight past it: Vite's html-plugin notice
(``<script src=...> can't be bundled without type="module"``) carries neither
marker, and Tailwind's CSS-optimiser warning is a bare ``console.warn`` that
never touches Vite's logger at all. Both went to STDERR, so the rule is now
format-independent: any stderr output from the build is a failure.

Each case runs the real script against a fake ``npm`` placed first on PATH,
from a throwaway directory holding an empty ``web/`` (the script ``cd``s into
``web`` relative to its working directory), so no real build ever runs.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_SCRIPT = PROJECT_ROOT / "scripts" / "build_web_no_warnings.sh"

# The fake npm: records its argv + cwd and the update-notifier setting it was
# handed, then writes the scripted stdout and stderr and exits with the
# scripted status.
_FAKE_NPM = """#!/usr/bin/env bash
printf '%s\\n' "$PWD" "$@" > "$FAKE_NPM_RECORD"
printf '%s' "${npm_config_update_notifier-unset}" > "$FAKE_NPM_NOTIFIER"
printf '%b' "$FAKE_NPM_STDOUT"
printf '%b' "$FAKE_NPM_STDERR" >&2
exit "$FAKE_NPM_EXIT"
"""


def _run_gate(
    tmp_path: Path, *, stdout: str = "", stderr: str = "", exit_code: int = 0
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(_FAKE_NPM)
    npm.chmod(npm.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "web").mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_NPM_RECORD": str(tmp_path / "npm-record"),
        "FAKE_NPM_NOTIFIER": str(tmp_path / "npm-notifier"),
        "FAKE_NPM_STDOUT": stdout,
        "FAKE_NPM_STDERR": stderr,
        "FAKE_NPM_EXIT": str(exit_code),
    }
    return subprocess.run(
        ["bash", str(_SCRIPT), "build:covapp"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_clean_build_passes_and_streams_its_output(tmp_path: Path) -> None:
    result = _run_gate(tmp_path, stdout="vite v8 building\\nbuilt in 1.2s\\n")
    assert result.returncode == 0, result.stderr
    assert "built in 1.2s" in result.stdout, "the build output must stream live"
    assert result.stderr == ""
    record = (tmp_path / "npm-record").read_text().splitlines()
    assert record == [str(tmp_path / "web"), "run", "build:covapp"], (
        "the gate must run `npm run <script>` inside web/"
    )


def test_npm_update_notifier_is_switched_off(tmp_path: Path) -> None:
    """npm's "new version" notice goes to stderr and depends on the registry,
    not on the build, so the gate must not let it through to fail a build."""
    _run_gate(tmp_path, stdout="built\\n")
    assert (tmp_path / "npm-notifier").read_text() == "false", (
        "the gate must run npm with npm_config_update_notifier=false"
    )


def test_any_stderr_output_fails_and_is_reprinted(tmp_path: Path) -> None:
    """The two formats that slipped the marker grep: neither has a marker."""
    warning = (
        '<script src="./cov_data/index.js"> in "/covapp.html" can\'t be '
        'bundled without type="module" attribute\\n'
        "Found 1 warning while optimizing generated CSS:\\n"
    )
    result = _run_gate(tmp_path, stdout="built in 1.2s\\n", stderr=warning)
    assert result.returncode != 0, (
        "stderr output from the build must fail the gate, whatever its format"
    )
    live, sep, tail = result.stderr.partition("build_web_no_warnings:")
    assert sep, f"no gate failure message in stderr: {result.stderr!r}"
    assert "can't be bundled without" in live, (
        "the build's stderr must stream live on stderr, ahead of the verdict"
    )
    assert "can't be bundled without" in tail, (
        "the failure message must reprint the captured stderr"
    )
    assert "while optimizing generated CSS" in tail
    assert "can't be bundled without" not in result.stdout, (
        "the build's stderr must stay on stderr, not leak into stdout"
    )


def test_vite_chunk_size_warning_fails(tmp_path: Path) -> None:
    """Vite 8 reports a chunkSizeWarningLimit overrun on STDERR, so it is the
    any-stderr rule, not the stdout marker scan, that enforces the budget."""
    warning = (
        "[plugin builtin:vite-reporter] \\n"
        "(!) Some chunks are larger than 2000 kB after minification. Consider:\\n"
        "- Using dynamic import() to code-split the application\\n"
    )
    result = _run_gate(tmp_path, stdout="built in 1.2s\\n", stderr=warning)
    assert result.returncode != 0, "a chunk-size overrun must fail the gate"
    assert "Some chunks are larger" in result.stderr.partition("build_web_no_warnings:")[2], (
        "the failure message must reprint the chunk warning"
    )


def test_stdout_warning_marker_fails(tmp_path: Path) -> None:
    result = _run_gate(tmp_path, stdout="(!) some tool's notice\\nbuilt\\n")
    assert result.returncode != 0, "a '(!)' line on stdout must fail the gate"
    assert "(!)" in result.stdout, "the warning must still stream live"


def test_stdout_rolldown_warn_marker_fails(tmp_path: Path) -> None:
    result = _run_gate(tmp_path, stdout="[plugin x]  WARN  something odd\\n")
    assert result.returncode != 0, "a ' WARN ' line on stdout must fail the gate"


@pytest.mark.parametrize("stderr", ["", "error: boom\\n"])
def test_failed_build_propagates_its_exit_code(tmp_path: Path, stderr: str) -> None:
    result = _run_gate(tmp_path, stdout="building\\n", stderr=stderr, exit_code=7)
    assert result.returncode == 7, (
        f"a failed build's own exit code must propagate, got {result.returncode}"
    )
