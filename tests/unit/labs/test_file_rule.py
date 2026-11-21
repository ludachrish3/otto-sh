"""The entry-to-files rule is a per-backend fact: each source's facts name the rule it reads by."""

from dataclasses import dataclass
from pathlib import Path

from otto.config.cache_sections import lab_key_paths
from otto.config.project import Project
from otto.host.os_profile import ProfileContext
from otto.init.areas import lab_files
from otto.labs.documents import expand_lab_files
from otto.labs.json_repository import JsonLabSourceConfig
from otto.labs.registry import register_lab_repository
from otto.labs.sources import (
    JSON_FILE_RULE,
    LabFileRule,
    LabSourceFacts,
    compile_lab_sources,
    prepare_lab_sources,
)
from otto.models.settings import LabConfigSpec
from tests._fixtures.project_scaffold import make_project

_RULE = LabFileRule(suffix=".lab", directory_file="main.lab")
"""A rule no built-in backend uses, so a test passing on it cannot be reading the json rule."""


def _touch(*paths: Path) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")


def _state(tmp_path: Path, backend: str, **options: object):
    (state,) = prepare_lab_sources(
        compile_lab_sources(
            LabConfigSpec.model_validate({"sources": [{"backend": backend, **options}]}),
            project_name="r",
            root=tmp_path,
        ),
        profiles=ProfileContext.empty(),
    )
    return state


def test_rule_selects_suffix_and_directory_file(tmp_path: Path) -> None:
    """The rule's directory file, its suffix for a file entry, and only its suffix from a glob."""
    d = tmp_path / "d"
    single = tmp_path / "one.lab"
    _touch(d / "main.lab", d / "b.lab", d / "a.lab", d / "lab.json", d / "c.json", single)
    (d / "notes.md").write_text("not a lab file")

    found = expand_lab_files([d, d / "*", single, tmp_path / "x.json"], rule=_RULE)
    assert found == [d / "main.lab", d / "a.lab", d / "b.lab", single]

    assert expand_lab_files([d, d / "*"], rule=JSON_FILE_RULE) == [d / "lab.json", d / "c.json"]


def test_the_rule_marks_every_directory_it_enters(tmp_path: Path) -> None:
    d = tmp_path / "d"
    _touch(d / "main.lab", d / "sub" / "x.lab")
    visited: set[Path] = set()
    found = expand_lab_files([d, d / "sub" / "*.lab"], rule=_RULE, visited=visited)
    assert found == [d / "main.lab", d / "sub" / "x.lab"]
    assert visited == {d, d / "sub"}


def test_the_json_source_reads_the_json_rule_and_names_no_key_roots(tmp_path: Path) -> None:
    _touch(tmp_path / "lab_data" / "lab.json")
    state = _state(tmp_path, "json", paths=["lab_data"])
    assert state.prepared is not None
    assert state.prepared.facts.file_rule == JSON_FILE_RULE
    assert state.prepared.facts.key_roots == ()
    assert state.file_rule() == JSON_FILE_RULE
    assert state.key_paths() == state.lab_files() == [tmp_path / "lab_data" / "lab.json"]


@dataclass(frozen=True)
class _OlderFacts:
    """A third-party facts object written before the rule existed: ``file_inputs`` only."""

    file_inputs: tuple[Path, ...]


class _OlderConfig(JsonLabSourceConfig):
    """A third-party subclass of the json config whose facts carry no rule."""

    def prepared_facts(self) -> _OlderFacts:  # type: ignore[override]  # the point: an older shape
        return _OlderFacts(file_inputs=self.paths)


def test_third_party_facts_without_a_rule_read_json(tmp_path: Path) -> None:
    _touch(tmp_path / "lab_data" / "lab.json", tmp_path / "lab_data" / "main.lab")
    register_lab_repository("older", config=_OlderConfig, factory=lambda c: None)
    state = _state(tmp_path, "older", paths=["lab_data", "lab_data/*"])
    assert state.is_file_backed()
    assert state.file_rule() == JSON_FILE_RULE
    assert state.lab_files() == [tmp_path / "lab_data" / "lab.json"]
    visited: set[Path] = set()
    assert state.key_paths(visited=visited) == [tmp_path / "lab_data" / "lab.json"]
    assert visited == {tmp_path / "lab_data"}


class _RuleConfig(JsonLabSourceConfig):
    """A backend reading ``.lab`` files: the json options, another rule."""

    def prepared_facts(self) -> LabSourceFacts:
        return LabSourceFacts(file_inputs=self.paths, file_rule=_RULE)


def test_cache_key_paths_and_doctor_see_the_same_files(tmp_path: Path) -> None:
    """The cache writer's key paths and the ``otto init`` doctor read one source's rule."""
    register_lab_repository("dot-lab", config=_RuleConfig, factory=lambda c: None)
    root = tmp_path / "sut"
    make_project(
        root, name="r", extra='[[lab.sources]]\nbackend = "dot-lab"\npaths = ["labs", "labs/*"]\n'
    )
    labs = root / "labs"
    _touch(labs / "main.lab", labs / "a.lab", labs / "lab.json")

    doctor = lab_files(root)
    assert doctor == [labs / "main.lab", labs / "a.lab"]
    assert lab_key_paths([Project(root=root)]) == [*doctor, labs]


_YAML_RULE = LabFileRule(suffix=".yaml", directory_file="lab.yaml")
"""The rule ``docs/extend/lab-source-backends.md`` teaches, for a format otto does not decode."""


class _YamlConfig(JsonLabSourceConfig):
    """A backend reading ``lab.yaml`` files, as the backend guide's recipe writes it."""

    def prepared_facts(self) -> LabSourceFacts:
        return LabSourceFacts(file_inputs=self.paths, file_rule=_YAML_RULE)


def test_a_file_otto_does_not_decode_is_left_to_its_backend(tmp_path: Path, monkeypatch) -> None:
    """The doctor, the Next steps panel and completion's links read no ``lab.yaml`` as JSON.

    The file is still one of the source's files (fingerprinted, "has lab
    data"); only the readers outside the backend skip it.
    """
    from otto.config.completion_cache import collect_link_ids
    from otto.init.doctor import check_project
    from otto.init.next_steps import declares_lab
    from otto.labs import documents

    register_lab_repository("yamlish", config=_YamlConfig, factory=lambda c: None)
    root = tmp_path / "sut"
    make_project(root, name="r", extra='[[lab.sources]]\nbackend = "yamlish"\npaths = ["labs"]\n')
    lab_yaml = root / "labs" / "lab.yaml"
    lab_yaml.parent.mkdir(parents=True, exist_ok=True)
    lab_yaml.write_text("labs:\n  bench: {}\nlinks:\n  - endpoints: [{host: a}, {host: b}]\n")
    assert lab_files(root) == [lab_yaml], "positive control: the file is the source's"
    assert lab_key_paths([Project(root=root)]) == [lab_yaml, lab_yaml.parent]

    verdict = check_project(root).verdict(name="lab")
    assert verdict.problems == []

    real = documents.read_lab_document
    read: list[Path] = []

    def recording(path: Path) -> documents.LabDocumentSource:
        read.append(path)
        return real(path)

    monkeypatch.setattr(documents, "read_lab_document", recording)
    assert declares_lab(root, "bench") is False
    assert collect_link_ids([Project(root=root)]) == []
    assert read == []
