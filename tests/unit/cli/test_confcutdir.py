"""_repo_confcutdir maps a suite file to its owning repo root."""

from pathlib import Path
from types import SimpleNamespace

from otto.suite.run import _repo_confcutdir


def test_file_inside_repo_maps_to_sut_dir(tmp_path: Path) -> None:
    repo = SimpleNamespace(sut_dir=tmp_path / "repo_a")
    suite = repo.sut_dir / "tests" / "sub" / "test_x.py"
    suite.parent.mkdir(parents=True)
    suite.touch()
    assert _repo_confcutdir(str(suite), [repo]) == repo.sut_dir  # type: ignore[arg-type]


def test_file_outside_all_repos_falls_back_to_parent(tmp_path: Path) -> None:
    repo = SimpleNamespace(sut_dir=tmp_path / "repo_a")
    stray = tmp_path / "elsewhere" / "test_y.py"
    stray.parent.mkdir(parents=True)
    stray.touch()
    assert _repo_confcutdir(str(stray), [repo]) == stray.parent  # type: ignore[arg-type]


def test_first_matching_repo_wins(tmp_path: Path) -> None:
    outer = SimpleNamespace(sut_dir=tmp_path)
    inner = SimpleNamespace(sut_dir=tmp_path / "nested")
    suite = inner.sut_dir / "tests" / "test_z.py"
    suite.parent.mkdir(parents=True)
    suite.touch()
    # repos are checked in order; list inner first for the tighter root
    assert _repo_confcutdir(str(suite), [inner, outer]) == inner.sut_dir  # type: ignore[arg-type]
