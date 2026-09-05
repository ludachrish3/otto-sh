"""The static scan sees every marker the corpus spells, in the pass that finds test names."""

from pathlib import Path

from otto.config import completion_cache as cc
from tests._fixtures.sutrepo import make_sut_repo

_BODY = """
import pytest

pytestmark = [pytest.mark.slow, pytest.mark.module_level("x")]


@pytest.mark.smoke
def test_a():
    pass


class TestB:
    @pytest.mark.deep(1)
    async def test_b(self):
        pass


def test_c():
    pass
"""
_NESTED = "from pytest import mark\n\n\n@mark.nested\ndef test_y():\n    pass\n"


def _repo(tmp_path: Path):
    from otto.config.repo import Repo

    root = make_sut_repo(
        tmp_path / "sut",
        tests=["tests"],
        files={"tests/test_x.py": _BODY, "tests/sub/test_y.py": _NESTED},
    )
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers = ["declared: from pyproject", "deep(n): depth"]\n'
    )
    return Repo(sut_dir=root)


def test_scan_yields_names_and_markers_in_one_pass(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    calls = 0
    real = cc._match_py_files

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(cc, "_match_py_files", counting)
    scan = cc.scan_test_corpus([repo])
    assert scan.names == ["TestB::test_b", "test_a", "test_b", "test_c", "test_y"]
    assert scan.markers == ["deep", "module_level", "nested", "slow", "smoke"]
    assert calls == 1


def test_collect_marker_names_unions_declared_builtin_and_scanned(tmp_path):
    from otto.suite.markers import OTTO_MARKERS

    repo = _repo(tmp_path)
    names = cc.collect_marker_names([repo])
    assert {"declared", "deep", "smoke", "nested", *OTTO_MARKERS} <= set(names)
    assert names == sorted(names)


def test_collect_test_names_is_unchanged_by_the_refactor(tmp_path):
    repo = _repo(tmp_path)
    assert cc.collect_test_names([repo]) == cc.scan_test_corpus([repo]).names
