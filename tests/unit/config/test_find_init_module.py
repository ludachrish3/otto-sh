"""Init modules resolve through importlib's own finder, never a hand-rolled layout check (#499)."""

import os
import sys
from pathlib import Path

import pytest

from otto.config.repo import find_init_module


def _lib(tmp_path: Path) -> Path:
    lib = tmp_path / "pylib"
    lib.mkdir()
    return lib


def test_a_package_resolves(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    (lib / "foo").mkdir()
    (lib / "foo" / "__init__.py").write_text("")
    assert find_init_module("foo", [lib]) is not None


def test_a_single_file_module_resolves(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    (lib / "foo.py").write_text("")
    spec = find_init_module("foo", [lib])
    assert spec is not None
    assert spec.origin == str(lib / "foo.py")


def test_a_dotted_name_resolves_through_its_parent_package(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    (lib / "pkg" / "sub").mkdir(parents=True)
    (lib / "pkg" / "__init__.py").write_text("")
    (lib / "pkg" / "sub" / "__init__.py").write_text("")
    assert find_init_module("pkg.sub", [lib]) is not None


def test_a_namespace_package_resolves(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    (lib / "ns").mkdir()
    (lib / "ns" / "mod.py").write_text("")
    assert find_init_module("ns", [lib]) is not None
    assert find_init_module("ns.mod", [lib]) is not None


def test_a_module_only_on_sys_path_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "site"
    elsewhere.mkdir()
    (elsewhere / "only_on_path_xyz.py").write_text("")
    monkeypatch.syspath_prepend(str(elsewhere))
    assert find_init_module("only_on_path_xyz", []) is not None


@pytest.mark.parametrize("name", ["missing_xyz", "foo.bar", "foo.", "", ".foo"])
def test_unresolvable_names_return_none(tmp_path: Path, name: str) -> None:
    lib = _lib(tmp_path)
    (lib / "foo.py").write_text("")  # a plain module: `foo.bar` has no parent package
    assert find_init_module(name, [lib]) is None


def test_finding_executes_no_code(tmp_path: Path) -> None:
    lib = _lib(tmp_path)
    (lib / "boom_xyz.py").write_text("raise RuntimeError('executed')\n")
    assert find_init_module("boom_xyz", [lib]) is not None
    assert "boom_xyz" not in sys.modules


def test_a_module_written_after_a_lookup_is_found(tmp_path: Path) -> None:
    """``otto init`` scaffolds a module then checks it in one process.

    importlib's FileFinder caches a directory's listing keyed on its mtime,
    which a fast write can leave unchanged; the mtime is pinned back here so
    the stale listing is guaranteed, not timing-dependent.
    """
    lib = _lib(tmp_path)
    before = lib.stat()
    assert find_init_module("late_xyz", [lib]) is None
    (lib / "late_xyz.py").write_text("")
    os.utime(lib, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert find_init_module("late_xyz", [lib]) is not None
