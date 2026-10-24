"""build_example_archives: one reproducible archive of tracked files per example track."""

import tarfile
from pathlib import Path

import pytest

from scripts import build_example_archives as bea
from tests._fixtures.gitrepo import TmpGitRepo

pytestmark = pytest.mark.interpreter_agnostic


@pytest.fixture
def repo(tmp_path):
    """A git checkout with one track, one other example, and untracked build output."""
    git = TmpGitRepo(tmp_path / "repo")
    repo = git.root
    track = repo / "docs" / "examples" / "quickstart-shell"
    (track / "src").mkdir(parents=True)
    (track / "src" / "main.cpp").write_text("int main() { return 0; }\n")
    (track / "CMakeLists.txt").write_text("project(x)\n")
    run = track / "run.sh"
    run.write_text("#!/bin/sh\n")
    run.chmod(0o755)
    (repo / "docs" / "examples" / "getting-started").mkdir()
    (repo / "docs" / "examples" / "getting-started" / "a.txt").write_text("a\n")
    git.commit("x")
    (track / "build").mkdir()
    (track / "build" / "main.gcno").write_text("untracked build output")
    return repo


def _members(archive: Path) -> "list[tarfile.TarInfo]":
    with tarfile.open(archive) as tar:
        return sorted(tar.getmembers(), key=lambda m: m.name)


def test_one_archive_per_track_holding_only_tracked_files(repo, tmp_path):
    built = bea.build(repo / "docs" / "examples", tmp_path / "out", checkout=repo)
    assert [p.name for p in built] == ["quickstart-shell.tar.gz"]
    assert [m.name for m in _members(built[0])] == [
        "quickstart-shell/CMakeLists.txt",
        "quickstart-shell/run.sh",
        "quickstart-shell/src/main.cpp",
    ]


def test_an_executable_stays_executable_and_nothing_names_its_builder(repo, tmp_path):
    built = bea.build(repo / "docs" / "examples", tmp_path / "out", checkout=repo)
    members = {m.name: m for m in _members(built[0])}
    assert members["quickstart-shell/run.sh"].mode == 0o755
    assert members["quickstart-shell/src/main.cpp"].mode == 0o644
    assert {(m.uid, m.gid, m.uname, m.gname, m.mtime) for m in members.values()} == {
        (0, 0, "", "", 0)
    }


def test_two_builds_are_byte_identical(repo, tmp_path):
    examples = repo / "docs" / "examples"
    first = bea.build(examples, tmp_path / "a", checkout=repo)[0].read_bytes()
    (repo / "docs" / "examples" / "quickstart-shell" / "src" / "main.cpp").touch()
    second = bea.build(examples, tmp_path / "b", checkout=repo)[0].read_bytes()
    assert first == second


def test_no_track_builds_nothing(tmp_path):
    root = tmp_path / "examples"
    root.mkdir()
    assert bea.build(root, tmp_path / "out", checkout=tmp_path) == []
    assert not (tmp_path / "out").exists()
