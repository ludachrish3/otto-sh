"""Static, user-code-free collector behind ``--lab`` completion."""

from pathlib import Path

from otto.config.completion_cache import collect_lab_names
from tests._fixtures.fake_repo import fake_repo
from tests._fixtures.labdata import json_lab_sources, write_lab_json

_HOSTS = [
    {"ip": "10.0.0.1", "element": "r", "labs": ["tech1", "shared"]},
    {"ip": "10.0.0.2", "element": "s", "labs": ["tech2", "shared"]},
]


def _repo(*, labs: list[Path] | None = None):
    """A stand-in Repo whose only host source is a json one over `labs` —
    what a real repo without a custom backend compiles to."""
    sut_dir = (labs or [Path()])[0]
    return fake_repo(
        "stand-in",
        lab_sources=json_lab_sources(sut_dir, labs or []) if labs else [],
        sut_dir=sut_dir,
    )


def test_collect_lab_names_reads_declared_labs(tmp_path: Path) -> None:
    write_lab_json(tmp_path / "lab.json", _HOSTS)
    assert collect_lab_names([_repo(labs=[tmp_path])]) == ["shared", "tech1", "tech2"]


def test_collect_lab_names_empty_without_hosts_file(tmp_path: Path) -> None:
    assert collect_lab_names([_repo(labs=[tmp_path])]) == []
    assert collect_lab_names([]) == []
