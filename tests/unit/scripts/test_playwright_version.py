"""Pins for scripts/playwright_version.py: the one reader of the locked Playwright version."""

import re
from pathlib import Path

import pytest
import tomli

from scripts import playwright_version as pv
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic


def _lock(tmp_path: Path, *versions: str) -> Path:
    body = "".join(f'[[package]]\nname = "playwright"\nversion = "{v}"\n\n' for v in versions)
    path = tmp_path / "uv.lock"
    path.write_text('version = 1\n\n[[package]]\nname = "other"\nversion = "9.9.9"\n\n' + body)
    return path


def test_reads_the_locked_version_from_this_lock() -> None:
    lock = CHECKOUT_ROOT / "uv.lock"
    version = pv.locked_version(lock)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    packages = tomli.loads(lock.read_text())["package"]
    assert [p["version"] for p in packages if p["name"] == "playwright"] == [version]


def test_refuses_a_lock_without_playwright(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no playwright package"):
        pv.locked_version(_lock(tmp_path))


def test_refuses_two_playwright_versions(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=r"more than one playwright version.*1\.60\.0.*1\.61\.0"):
        pv.locked_version(_lock(tmp_path, "1.61.0", "1.60.0"))


def test_refuses_a_non_numeric_version(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=r"not N\.N\.N"):
        pv.locked_version(_lock(tmp_path, "1.61.0rc1"))


def test_image_for_spells_the_noble_python_image() -> None:
    assert pv.image_for("1.61.0") == "mcr.microsoft.com/playwright/python:v1.61.0-noble"


def test_main_writes_both_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert pv.main([str(_lock(tmp_path, "1.61.0"))]) == 0
    assert out.read_text().splitlines() == [
        "version=1.61.0",
        "image=mcr.microsoft.com/playwright/python:v1.61.0-noble",
    ]
