"""One spelling rule for OTTOPATH's roots, and a repeated root collapses.

Spec ``docs/superpowers/specs/2026-10-17-649-project-vocabulary-design.md``
(A repeated root collapses). Every case starts from the ORIGINAL ``OTTOPATH``
value and runs through every reader: the env settings, discovery, both
child-environment builders (the shim's, stdlib only, and the product's), and
the workspace each side keys (the shim's ``locate_cache`` and the product's
``workspace_home``). All of them must agree. The kept spelling expands ``~``
and joins a relative entry to the cwd, and nothing else: ``<link>/..`` stays as
written, because ``abspath`` would fold it lexically to a different directory
than the filesystem names.
"""

import os
from pathlib import Path

import pytest

from otto import _shim_complete as sc
from otto.bootstrap import invalidate
from otto.config import completion_cache as cc
from otto.config.env import load_otto_env
from otto.config.home import workspace_home, workspace_key
from otto.config.project import discover
from otto.models.settings import OttoEnvSettings
from tests._fixtures.sutrepo import make_sut_repo


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Projects a, b, c; ``link`` -> a; ``inner-link`` -> a/inner; cwd and HOME at tmp_path."""
    for name in ("a", "b", "c"):
        make_sut_repo(tmp_path / name, name=name)
    (tmp_path / "a" / "inner").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "a")
    (tmp_path / "inner-link").symlink_to(tmp_path / "a" / "inner")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    for var in ("OTTOPATH", "OTTO_XDIR", "_OTTO_COMPLETE", "COMP_WORDS", "COMP_CWORD"):
        monkeypatch.delenv(var, raising=False)
    invalidate()  # discovery and every cached root key
    yield tmp_path
    invalidate()


def _discovered() -> list[str]:
    invalidate()
    try:
        result = discover()
        assert result.errors == []
        return [str(p.root) for p in result.projects]
    finally:
        invalidate()


def _views(world: Path) -> dict[str, list[str]]:
    settings = [str(p) for p in OttoEnvSettings().ottopath]
    shim = sc._child_environ(dict(os.environ))["OTTOPATH"].split(os.pathsep)
    command = cc._collect_child_command(world / ".home")
    assert command is not None, "the venv has an otto console script"
    product = command.env["OTTOPATH"].split(os.pathsep)
    return {
        "settings": settings,
        "discovery": _discovered(),
        "shim child": shim,
        "product child": product,
    }


CASES = {
    "a symlink and its target": ("{w}/link:{w}/a", ["{w}/link"]),
    "a symlink followed by ..": ("inner-link/..:a", ["{w}/inner-link/.."]),
    "relative and absolute spellings": ("b:{w}/b", ["{w}/b"]),
    "tilde and its expansion": ("~/c:{w}/c", ["{w}/c"]),
    "interleaved repeats": ("a:b:a:c:b", ["{w}/a", "{w}/b", "{w}/c"]),
}


def _original(case: str, world: Path) -> str:
    return CASES[case][0].replace(":", os.pathsep).format(w=world)


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_reader_keeps_the_first_spelling_of_each_root(world, monkeypatch, case):
    monkeypatch.setenv("OTTOPATH", _original(case, world))
    want = [e.format(w=world) for e in CASES[case][1]]
    assert _views(world) == {
        "settings": want,
        "discovery": want,
        "shim child": want,
        "product child": want,
    }


@pytest.mark.parametrize("case", sorted(CASES))
def test_both_sides_key_one_workspace_from_the_original_value(world, monkeypatch, case):
    original = _original(case, world)
    monkeypatch.setenv("OTTOPATH", original)
    shim = sc.locate_cache({"OTTOPATH": original, "OTTO_HOME": str(world / "home")})
    assert shim == str(workspace_home() / sc.CACHE_FILENAME)
    kept = [Path(e.format(w=world)) for e in CASES[case][1]]
    assert Path(shim).parent.name == workspace_key(kept) == sc.workspace_key([str(p) for p in kept])


@pytest.mark.parametrize("var", ["OTTO_XDIR", "OTTO_HOME"])
def test_a_symlink_dotdot_home_or_xdir_keeps_its_spelling_in_both_children(world, monkeypatch, var):
    monkeypatch.setenv("OTTOPATH", "a")
    monkeypatch.setenv(var, "inner-link/..")
    want = f"{world}/inner-link/.."
    command = cc._collect_child_command(world / ".home")
    assert command is not None
    assert (sc._child_environ(dict(os.environ))[var], command.env[var]) == (want, want)
    # what the filesystem names, which abspath's lexical fold (to the world) would not
    assert os.path.realpath(want) == os.path.realpath(world / "a")


def test_an_empty_workspace_is_named_no_projects():
    assert workspace_key([]).endswith("-no-projects")
    assert sc.workspace_key([]).endswith("-no-projects")


def test_empty_entries_drop_and_an_unknown_user_tilde_stays_literal(world, monkeypatch):
    monkeypatch.setenv("OTTOPATH", os.pathsep.join(["a", "", "b", ""]))
    assert OttoEnvSettings().ottopath == [world / "a", world / "b"]
    monkeypatch.setenv("OTTOPATH", "~nosuchuser-649/x")
    assert OttoEnvSettings().ottopath == [world / "~nosuchuser-649" / "x"]
    with pytest.raises(FileNotFoundError, match="~nosuchuser-649/x"):
        load_otto_env()
