"""Old coverage metadata is refused, whatever order the inputs come in.

Spec ``docs/superpowers/specs/2026-10-17-649-project-vocabulary-design.md``
(Old coverage metadata is refused). Every input directory's metadata is
validated, not just the first, so an old directory can neither crash on a
missing key nor slip in silently.
"""

import json
from pathlib import Path

import pytest

from otto.coverage.errors import CoverageInputError
from otto.coverage.reporter import read_cov_source_root

NEW = {"project_name": "fw", "project_root": "/src/fw"}
OLD = {"repo_name": "fw", "sut_dir": "/src/fw"}
BOTH = {**NEW, **OLD}
"""A file carrying the current keys AND a stale one: refused, never read past."""


def _cov_dir(path: Path, meta: "dict[str, str]") -> Path:
    path.mkdir(parents=True)
    body = {**meta, "toolchains": {}, "source_roots": {}}
    (path / ".otto_cov_meta.json").write_text(json.dumps(body))
    return path


@pytest.mark.parametrize("order", ["old-first", "new-first"])
def test_old_metadata_is_refused_in_either_input_order(tmp_path, order):
    old = _cov_dir(tmp_path / "old", OLD)
    new = _cov_dir(tmp_path / "new", NEW)
    dirs = [old, new] if order == "old-first" else [new, old]
    with pytest.raises(CoverageInputError) as excinfo:
        read_cov_source_root(dirs)
    message = str(excinfo.value)
    assert excinfo.value.field == "cov_dirs"
    assert str(old) in message
    assert "'repo_name'" in message
    assert "re-collect" in message


@pytest.mark.parametrize("order", ["both-first", "new-first"])
def test_metadata_carrying_current_and_old_keys_is_refused(tmp_path, order):
    both = _cov_dir(tmp_path / "both", BOTH)
    new = _cov_dir(tmp_path / "new", NEW)
    with pytest.raises(CoverageInputError) as excinfo:
        read_cov_source_root([both, new] if order == "both-first" else [new, both])
    assert str(both) in str(excinfo.value)
    assert "'repo_name'" in str(excinfo.value)


def test_metadata_missing_a_current_key_is_refused(tmp_path):
    half = _cov_dir(tmp_path / "half", {"project_name": "fw"})
    with pytest.raises(CoverageInputError, match="'project_root'"):
        read_cov_source_root([half])


def test_current_metadata_reads_its_project_root(tmp_path):
    assert read_cov_source_root([_cov_dir(tmp_path / "new", NEW)]) == Path("/src/fw")


def test_a_directory_without_metadata_is_still_tolerated(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert read_cov_source_root([bare, _cov_dir(tmp_path / "new", NEW)]) == Path("/src/fw")
