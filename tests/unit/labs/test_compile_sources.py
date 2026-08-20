"""compile_lab_sources: labels, ordering, json validation, anchoring."""

from pathlib import Path

import pytest

from otto.labs.sources import CompiledLabSource, compile_lab_sources
from otto.models.settings import LabConfigSpec

SUT = Path("/repo")


def _cfg(*entries: dict) -> LabConfigSpec:
    return LabConfigSpec.model_validate({"sources": list(entries)})


def _compile(cfg) -> list[CompiledLabSource]:
    return compile_lab_sources(cfg, repo_name="r1", sut_dir=SUT)


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


def test_json_paths_required_nonempty() -> None:
    with pytest.raises(ValueError, match="paths"):
        _compile(_cfg({"backend": "json"}))
    with pytest.raises(ValueError, match="paths"):
        _compile(_cfg({"backend": "json", "paths": []}))


def test_json_unknown_key_rejected() -> None:
    with pytest.raises(ValueError, match="server"):
        _compile(_cfg({"backend": "json", "paths": ["lab"], "server": "nope"}))


def test_json_paths_anchored_relative_absolute_passthrough() -> None:
    (src,) = _compile(_cfg({"backend": "json", "paths": ["lab", "/abs/global.json"]}))
    assert src.paths == [SUT / "lab", Path("/abs/global.json")]


def test_lab_files_file_vs_directory() -> None:
    (src,) = _compile(_cfg({"backend": "json", "paths": ["lab", "/abs/global.json"]}))
    assert src.lab_files() == [SUT / "lab" / "lab.json", Path("/abs/global.json")]


def test_custom_backend_kwargs_passthrough_untouched() -> None:
    (src,) = _compile(_cfg({"backend": "cmdb", "server": "db", "paths": ["not-anchored"]}))
    assert src.kwargs == {"server": "db", "paths": ["not-anchored"]}  # custom kwargs verbatim
    assert src.paths == []  # anchoring is json-only; a custom backend's paths stay raw
    assert src.repo_dir == SUT


def test_no_config_no_sources() -> None:
    """No ``[lab]`` table at all — the only way to have zero sources, now that
    an empty one is a settings error (see test_settings.py)."""
    assert _compile(None) == []
