"""Lab sources: the envelope at compile (labels, order), the json options at preparation."""

from pathlib import Path

import pytest

from otto.host.os_profile import ProfileContext
from otto.labs.errors import LabRepositoryError, LabSourceConstructionError
from otto.labs.sources import PendingLabSource, compile_lab_sources, prepare_lab_sources
from otto.models.settings import LabConfigSpec

SUT = Path("/repo")


def _cfg(*entries: dict) -> LabConfigSpec:
    return LabConfigSpec.model_validate({"sources": list(entries)})


def _compile(cfg) -> list[PendingLabSource]:
    return compile_lab_sources(cfg, repo_name="r1", sut_dir=SUT)


def _prepared(entry: dict, sut_dir: Path = SUT):
    (state,) = prepare_lab_sources(
        compile_lab_sources(_cfg(entry), repo_name="r1", sut_dir=sut_dir),
        profiles=ProfileContext.empty(),
    )
    return state


def test_order_labels_and_default_names() -> None:
    out = _compile(
        _cfg(
            {"backend": "cmdb", "server": "db.example.com"},
            {"backend": "json", "paths": ["lab"]},
        )
    )
    assert [s.label for s in out] == ["r1/cmdb#1", "r1/json#2"]
    assert [s.backend for s in out] == ["cmdb", "json"]


def test_explicit_name_used_in_label() -> None:
    (src,) = _compile(_cfg({"backend": "json", "name": "global", "paths": ["lab"]}))
    assert src.label == "r1/global"


def test_duplicate_labels_within_repo_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        _compile(
            _cfg(
                {"backend": "json", "name": "x", "paths": ["a"]},
                {"backend": "json", "name": "x", "paths": ["b"]},
            )
        )


def test_compile_keeps_the_options_raw_and_names_the_settings_file() -> None:
    (src,) = _compile(_cfg({"backend": "cmdb", "server": "db", "paths": ["not-anchored"]}))
    assert src.raw.thaw_json() == {"server": "db", "paths": ["not-anchored"]}
    assert src.repo_dir == SUT
    assert src.origin == str(SUT / ".otto" / "settings.toml")


def test_compile_accepts_a_json_source_without_paths() -> None:
    """Only the envelope is checked at compile; the options wait for preparation."""
    (src,) = _compile(_cfg({"backend": "json"}))
    assert src.raw.thaw_json() == {}


def test_json_paths_required_nonempty() -> None:
    with pytest.raises(LabSourceConstructionError, match="paths"):
        _prepared({"backend": "json"})
    with pytest.raises(LabSourceConstructionError, match="paths"):
        _prepared({"backend": "json", "paths": []})
    with pytest.raises(LabSourceConstructionError, match="paths"):
        _prepared({"backend": "json", "paths": [""]})


def test_json_unknown_key_rejected() -> None:
    with pytest.raises(LabSourceConstructionError, match="server"):
        _prepared({"backend": "json", "paths": ["lab"], "server": "nope"})


def test_json_paths_anchored_relative_absolute_passthrough() -> None:
    state = _prepared({"backend": "json", "paths": ["lab", "/abs/global.json"]})
    assert state.prepared is not None
    assert state.prepared.facts.file_inputs == (SUT / "lab", Path("/abs/global.json"))


def test_lab_files_file_vs_directory(tmp_path: Path) -> None:
    """A directory entry contributes its lab.json; a .json entry IS the file.

    Written against a real tree because ``lab_files()`` lists the files a
    source READS (spec §2.4: globs expand, entries resolving to nothing are
    skipped), not a path mapping that may name absent files.
    """
    (tmp_path / "lab").mkdir()
    (tmp_path / "lab" / "lab.json").write_text("{}")
    absolute = tmp_path / "global.json"
    absolute.write_text("{}")
    state = _prepared({"backend": "json", "paths": ["lab", str(absolute)]}, tmp_path)
    assert state.lab_files() == [tmp_path / "lab" / "lab.json", absolute]


def test_lab_files_expands_globs(tmp_path: Path) -> None:
    """A glob entry lists exactly the sorted .json files it matches (spec §2.4)."""
    lab_data = tmp_path / "lab_data"
    lab_data.mkdir()
    for name in ("b.json", "a.json", "notes.md"):
        (lab_data / name).write_text("{}")
    (lab_data / "nested").mkdir()
    (lab_data / "nested" / "c.json").write_text("{}")  # not matched by a single-level glob
    state = _prepared({"backend": "json", "paths": ["lab_data/*.json"]}, tmp_path)
    assert state.lab_files() == [lab_data / "a.json", lab_data / "b.json"]


def test_lab_files_skips_entries_that_resolve_to_nothing(tmp_path: Path) -> None:
    """An absent directory, an absent file and a glob matching nothing contribute nothing."""
    state = _prepared({"backend": "json", "paths": ["gone", "gone.json", "gone/*.json"]}, tmp_path)
    assert state.lab_files() == []


def test_an_unknown_backend_is_unknown_and_has_no_files_to_ask_for() -> None:
    state = _prepared({"backend": "cmdb", "server": "db"})
    assert not state.is_known()
    assert not state.is_file_backed()
    with pytest.raises(LabRepositoryError, match="not registered"):
        state.lab_files()


def test_no_config_no_sources() -> None:
    """No ``[lab]`` table at all — the only way to have zero sources, now that
    an empty one is a settings error (see test_settings.py)."""
    assert _compile(None) == []
