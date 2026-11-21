"""The one lab-document reader and the one source read every lab-file reader goes through.

:func:`otto.labs.documents.read_lab_document` decodes one lab file and
:func:`otto.labs.documents.read_source` reads one source's files into
documents and problems. The loader, the ``otto init`` doctor, completion's
links read and the Next steps panel's ``declares_lab`` all reach the reader,
and the loader and the doctor reach the source read; the spies below prove it
by patching the module attribute each caller resolves at call time.
"""

import dataclasses
import json
import pathlib
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import otto
from otto.labs.documents import LabDocumentSource, read_lab_document, read_source
from otto.labs.errors import LabRepositoryError

CASES_DIR = Path(otto.__file__).resolve().parents[2] / "tests" / "_fixtures" / "reader_cases"

VALID = (CASES_DIR / "valid.json").read_text()
"""A lab file every reader accepts: lab ``case``, elements ``alpha`` and ``beta``, one link."""

SECRET_CASE = (CASES_DIR / "secret_inline.json").read_text()
"""A lab file with a password in a host's creds (and three more secrets elsewhere)."""

LAB_NAME = "case"

CALLERS = {
    "_read_source": "loader",
    "_parse_lab_documents": "doctor",
    "_read_lab_links": "links",
    "declares_lab": "declares_lab",
}
"""The function each reader runs the read from, and the name the spy records it under."""


@dataclasses.dataclass(frozen=True)
class Call:
    """One call a spy saw: which reader made it, and its arguments."""

    caller: str | None
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


def _caller() -> str | None:
    """The nearest reader (:data:`CALLERS`) on the current stack, or ``None``."""
    for frame in reversed(traceback.extract_stack()):
        if frame.name in CALLERS:
            return CALLERS[frame.name]
    return None


def spy(monkeypatch: pytest.MonkeyPatch, target: str) -> list[Call]:
    """Wrap the function at the dotted *target*; return the calls it sees, in order."""
    module_name, _, attribute = target.rpartition(".")
    module = __import__(module_name, fromlist=[attribute])
    real: Callable[..., Any] = getattr(module, attribute)
    calls: list[Call] = []

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append(Call(caller=_caller(), args=args, kwargs=kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, attribute, wrapper)
    return calls


def spy_open(monkeypatch: pytest.MonkeyPatch) -> list[Call]:
    """Record every ``Path.open`` call; ``args[0]`` is the path opened."""
    real = pathlib.Path.open
    calls: list[Call] = []

    def wrapper(self: Path, *args: Any, **kwargs: Any) -> Any:
        calls.append(Call(caller=_caller(), args=(self, *args), kwargs=kwargs))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", wrapper)
    return calls


def write(root: Path, text: str, *, name: str = "lab.json") -> Path:
    """Write *text* as the lab file *name* under *root*; return its path."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def find_password(value: Any) -> Any:
    """The first ``password`` value anywhere under *value*."""
    pending = [value]
    while pending:
        node = pending.pop(0)
        if isinstance(node, dict):
            if "password" in node:
                return node["password"]
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    raise AssertionError("no password found")


def _project(root: Path, lab_file: Path) -> Path:
    """A project at *root* whose one json lab source reads *lab_file*."""
    from tests._fixtures.project_scaffold import make_project

    source = f'[[lab.sources]]\nbackend = "json"\npaths = [{json.dumps(str(lab_file))}]\n'
    return make_project(root, name="documents", version="0.1.0", extra=source)


def exercise_loader(tmp_path: Path) -> None:
    from otto.labs.json_repository import _JsonFileLabRepository

    lab_dir = tmp_path / "loader"
    write(lab_dir, VALID)
    lab = _JsonFileLabRepository([lab_dir]).load_lab(LAB_NAME)
    assert sorted(lab.hosts) == ["alpha", "beta"]


def exercise_doctor(tmp_path: Path) -> None:
    from otto.init.doctor import _parse_lab_documents

    lab_file = write(tmp_path / "doctor-lab", VALID)
    parsed = _parse_lab_documents(_project(tmp_path / "doctor", lab_file))
    assert not parsed.problems
    assert [d.path for d in parsed.documents] == [lab_file]


def exercise_link_completion(tmp_path: Path) -> None:
    from otto.config.completion_cache import _read_lab_links

    assert len(_read_lab_links(write(tmp_path / "links", VALID))) == 1


def exercise_declares_lab(tmp_path: Path) -> None:
    from otto.init.next_steps import declares_lab

    lab_file = write(tmp_path / "declares-lab", VALID)
    assert declares_lab(_project(tmp_path / "declares", lab_file), LAB_NAME)


# --------------------------------------------------------------------------- #
# The reader.
# --------------------------------------------------------------------------- #


def test_reader_wraps_secrets_before_returning(tmp_path):
    doc = read_lab_document(write(tmp_path, SECRET_CASE))
    assert isinstance(find_password(doc.sections), SecretStr)


def test_reader_returns_the_validated_sections_and_its_origin(tmp_path):
    path = write(tmp_path, VALID)
    doc = read_lab_document(path)
    assert isinstance(doc, LabDocumentSource)
    assert doc.origin == path
    assert sorted(doc.sections) == ["elements", "labs", "links"]
    assert doc.provenance is None


def test_reader_names_the_file_on_malformed_json(tmp_path):
    with pytest.raises(LabRepositoryError, match=r"Lab file '.*bad.json' contains malformed JSON"):
        read_lab_document(write(tmp_path, "{", name="bad.json"))


def test_reader_names_the_file_on_undecodable_bytes(tmp_path):
    """A byte the locale cannot decode is a lab-file error, never a bare UnicodeDecodeError."""
    path = tmp_path / "latin1.json"
    path.write_bytes((CASES_DIR / "latin1.json").read_bytes())
    with pytest.raises(LabRepositoryError, match=r"Lab file '.*latin1.json' could not be read: "):
        read_lab_document(path)


def test_reader_names_the_file_it_cannot_open(tmp_path):
    with pytest.raises(LabRepositoryError, match=r"Lab file '.*missing.json' could not be read: "):
        read_lab_document(tmp_path / "missing.json")


def test_reader_applies_the_section_contract(tmp_path):
    with pytest.raises(LabRepositoryError, match="top-level 'hosts' has moved"):
        read_lab_document(write(tmp_path, '{"hosts": []}'))


def test_json_reader_opens_with_the_loaders_encoding_rule(monkeypatch, tmp_path):
    # the narrow seam: read_lab_document opens .json through otto.models.base.read_json_file,
    # which calls path.open() with encoding left to the locale, as the old loader did; the
    # spy asserts the arguments, never claiming to change the process locale
    path = write(tmp_path, VALID)
    calls = spy_open(monkeypatch)
    read_lab_document(path)
    opened = [c for c in calls if c.args[0] == path]
    assert opened
    assert opened[-1].kwargs.get("encoding") is None
    assert opened[-1].args[1:] in {(), ("r",)}


# --------------------------------------------------------------------------- #
# The source read.
# --------------------------------------------------------------------------- #


def test_source_read_reports_every_bad_file_and_never_raises(tmp_path):
    good = write(tmp_path / "a", VALID)
    bad = write(tmp_path / "b", "{", name="bad.json")
    hosts = write(tmp_path / "c", '{"hosts": []}', name="v1.json")
    read = read_source([bad, good, hosts])
    assert [d.path for d in read.documents] == [good]
    assert [p.where for p in read.problems] == [str(bad), str(hosts)]
    assert "contains malformed JSON" in read.problems[0].reason
    assert "top-level 'hosts' has moved" in read.problems[1].reason


def test_source_read_threads_duplicates_across_its_files(tmp_path):
    """Within one read a repeated lab is a problem naming both files; a new read starts clean."""
    first = write(tmp_path / "a", VALID)
    second = write(tmp_path / "b", VALID)
    read = read_source([first, second])
    assert [d.path for d in read.documents] == [first]
    (problem,) = read.problems
    assert problem.where == str(second)
    assert str(first) in problem.reason
    assert str(second) in problem.reason
    assert not read_source([second]).problems


def test_source_read_uses_the_reader_it_is_given(tmp_path):
    path = write(tmp_path, VALID)
    seen: list[Path] = []

    def reader(p: Path) -> LabDocumentSource:
        seen.append(p)
        return read_lab_document(p)

    assert [d.path for d in read_source([path], read=reader).documents] == [path]
    assert seen == [path]


# --------------------------------------------------------------------------- #
# One reader, one source read, every caller.
# --------------------------------------------------------------------------- #


def test_doctor_and_loader_share_one_source_read(monkeypatch, tmp_path):
    calls = spy(monkeypatch, "otto.labs.documents.read_source")
    exercise_loader(tmp_path)
    exercise_doctor(tmp_path)
    assert len(calls) == 2
    assert [c.caller for c in calls] == ["loader", "doctor"]


def test_the_four_readers_reach_one_function(monkeypatch, tmp_path):
    calls = spy(monkeypatch, "otto.labs.documents.read_lab_document")
    exercise_loader(tmp_path)
    exercise_doctor(tmp_path)
    exercise_link_completion(tmp_path)
    exercise_declares_lab(tmp_path)
    assert {c.caller for c in calls} == {"loader", "doctor", "links", "declares_lab"}
