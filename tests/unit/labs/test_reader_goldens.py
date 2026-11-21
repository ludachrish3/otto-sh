"""The four readers of a lab document, pinned case by case.

Four sites read a ``lab.json``, all through the one lab-document reader
(:func:`otto.labs.documents.read_lab_document`), each with its own tolerance:

* the loader, ``_JsonFileLabRepository._read_source``, whose sections are
  the reader's;
* the ``otto init`` doctor, :func:`otto.init.doctor._parse_lab_documents`;
* completion's links read, :func:`otto.config.completion_cache._read_lab_links`;
* the Next steps panel's :func:`otto.init.next_steps.declares_lab`.

Each case under ``tests/_fixtures/reader_cases/`` is read by all four, and the
four verdicts (:class:`ReaderVerdicts`) are compared with
``tests/_fixtures/reader_goldens.json``. Where the readers disagree today, the
golden records the disagreement as it is; a change that brings them together
updates the golden deliberately, line by line, in its commit body.

Generation and comparison run the SAME projection functions. ``python -m
tests.unit.labs.test_reader_goldens`` rewrites the golden from the code as it
stands, so a red test here needs a ruling on the change, never a regeneration
to make it green.

Every recorded string names a case file as ``<case>`` and the doctor's
scratch project as ``<project>``, so the golden is portable across checkouts.
"""

import dataclasses
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

import otto
from tests._fixtures.portable import portable

ROOT = Path(otto.__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "_fixtures"
CASES_DIR = FIXTURES / "reader_cases"
GOLDEN = FIXTURES / "reader_goldens.json"

CASES = (
    "valid.json",
    "bom.json",
    "latin1.json",
    "malformed.json",
    "not_object.json",
    "v1_hosts.json",
    "unknown_section.json",
    "schema_key.json",
    "links_not_list.json",
    "secret_inline.json",
)
"""Every reader case, one file each under :data:`CASES_DIR`."""

LAB_NAME = "case"
"""The lab every case that declares one declares; :func:`declares_lab` is asked for it."""

SECRET_PLAINTEXTS = (
    "plain-host-secret",
    "plain-key-secret",
    "plain-peer-secret",
    "plain-link-secret",
)
"""The secrets ``secret_inline.json`` plants: two hosts' creds, an ssh passphrase, a link's."""


@dataclasses.dataclass(frozen=True)
class ReaderVerdicts:
    """What each of the four readers makes of one case file, projected portably."""

    loader: str
    """``ok: <sections>`` from the loader, or ``<ExceptionType>: <message>``."""

    doctor: str
    """``ok: <documents>`` from the doctor, its problems one per line, or the exception."""

    links: "list[Any] | str"
    """Completion's links read, or ``<ExceptionType>: <message>`` when it raises."""

    declares: bool
    """Whether the Next steps panel finds the lab :data:`LAB_NAME` declared."""


# --------------------------------------------------------------------------- #
# Projections, shared by generation and comparison.
# --------------------------------------------------------------------------- #


def _jsonable(value: Any) -> Any:
    """*value* as plain JSON data: a ``SecretStr`` is ``"<secret>"``, a set is sorted."""
    if isinstance(value, SecretStr):
        return "<secret>"
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, Path):
        return str(value)
    return value


def _raised(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _doctor_project(case: Path, project: Path) -> Path:
    """A project at *project* whose one json lab source reads *case*, by its absolute path."""
    from tests._fixtures.project_scaffold import make_project

    source = f'[[lab.sources]]\nbackend = "json"\npaths = [{json.dumps(str(case))}]\n'
    return make_project(project, name="reader-case", version="0.1.0", extra=source)


def _read_loader(case: Path) -> Any:
    """The loader's sections for *case*: the one reader's (raises what it raises)."""
    from otto.labs.documents import read_lab_document

    return read_lab_document(case).sections


def _read_doctor(project: Path) -> Any:
    from otto.init.doctor import _parse_lab_documents

    return _parse_lab_documents(project)


def _read_links(case: Path) -> list[Any]:
    from otto.config.completion_cache import _read_lab_links

    return _read_lab_links(case)


def _loader_verdict(case: Path) -> str:
    try:
        sections = _read_loader(case)
    except Exception as e:  # noqa: BLE001 — the verdict records whatever the reader raises
        return _raised(e)
    return "ok: " + json.dumps(_jsonable(sections), sort_keys=True)


def _doctor_verdict(project: Path) -> str:
    try:
        parsed = _read_doctor(project)
    except Exception as e:  # noqa: BLE001 — the verdict records whatever the reader raises
        return _raised(e)
    if parsed.problems:
        return "\n".join(parsed.problems)
    documents = [
        {
            "path": str(d.path),
            "labs": sorted(d.entries),
            "elements": [e.name for e in d.elements],
            "links": _jsonable(d.links),
        }
        for d in parsed.documents
    ]
    return "ok: " + json.dumps(documents, sort_keys=True)


def _links_verdict(case: Path) -> "list[Any] | str":
    try:
        return _jsonable(_read_links(case))
    except Exception as e:  # noqa: BLE001 — the verdict records whatever the reader raises
        return _raised(e)


def _portable_links(links: "list[Any] | str", *, places: "dict[str, Path]") -> "list[Any] | str":
    if isinstance(links, str):
        return portable(links, places=places)
    return json.loads(portable(json.dumps(links), places=places))


def verdicts(case: Path, *, scratch: Path) -> ReaderVerdicts:
    """Every reader's verdict on *case*; the doctor's project is written under *scratch*."""
    from otto.init.next_steps import declares_lab

    project = _doctor_project(case, scratch / "project")
    places = {"<case>": case, "<project>": project}
    return ReaderVerdicts(
        loader=portable(_loader_verdict(case), places=places),
        doctor=portable(_doctor_verdict(project), places=places),
        links=_portable_links(_links_verdict(case), places=places),
        declares=declares_lab(project, LAB_NAME),
    )


def all_verdicts(scratch: Path) -> dict[str, dict[str, Any]]:
    """Every case's verdicts, as the golden holds them."""
    return {
        name: dataclasses.asdict(verdicts(CASES_DIR / name, scratch=scratch / Path(name).stem))
        for name in CASES
    }


# --------------------------------------------------------------------------- #
# The tests.
# --------------------------------------------------------------------------- #


def test_the_cases_are_exactly_the_golden_ones():
    assert sorted(p.name for p in CASES_DIR.iterdir()) == sorted(CASES)
    assert sorted(json.loads(GOLDEN.read_text())) == sorted(CASES)


@pytest.mark.parametrize("name", CASES)
def test_every_reader_verdict_matches_its_golden(name, tmp_path):
    got = dataclasses.asdict(verdicts(CASES_DIR / name, scratch=tmp_path))
    for spelled in {str(tmp_path), str(tmp_path.resolve())}:
        assert spelled not in json.dumps(got), f"{spelled} leaked into a recorded verdict"
    assert got == json.loads(GOLDEN.read_text())[name]


def _strings_and_secrets(value: Any) -> "tuple[list[str], int]":
    """Every ``str`` reachable from *value*, and how many ``SecretStr`` it holds."""
    strings: list[str] = []
    secrets = 0
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, SecretStr):
            secrets += 1
        elif isinstance(node, str):
            strings.append(node)
        elif isinstance(node, dict):
            pending.extend(node.keys())
            pending.extend(node.values())
        elif isinstance(node, (list, tuple, set, frozenset)):
            pending.extend(node)
        elif isinstance(node, BaseModel):
            pending.extend(vars(node).values())
        elif dataclasses.is_dataclass(node) and not isinstance(node, type):
            pending.extend(getattr(node, f.name) for f in dataclasses.fields(node))
    return strings, secrets


def test_a_wrapped_secret_never_reaches_a_reader_result_as_str(tmp_path):
    """Each reader wraps the secrets it parsed before its result leaves it.

    ``secret_inline.json`` plants a password in each of two hosts' creds, a
    passphrase in one's ssh options and a password in a link's comment key.
    The loader's sections and the doctor's documents hold all four, and
    completion's links the link's one, as ``SecretStr`` (the count shows the
    walk reached them), never as ``str``.
    """
    case = CASES_DIR / "secret_inline.json"
    project = _doctor_project(case, tmp_path / "project")
    parsed = _read_doctor(project)
    assert not parsed.problems
    results = {
        "loader": (_read_loader(case), 4),
        "doctor": (parsed.documents, 4),
        "links": (_read_links(case), 1),
    }
    for reader, (result, planted) in results.items():
        strings, secrets = _strings_and_secrets(result)
        leaked = [s for s in strings if any(p in s for p in SECRET_PLAINTEXTS)]
        assert not leaked, f"the {reader} result holds a secret as str: {leaked}"
        assert secrets == planted, f"the {reader} result holds {secrets} SecretStr, not {planted}"


def _generate() -> None:
    """Write the golden from the code as it stands; see the module docstring."""
    with tempfile.TemporaryDirectory(prefix="otto-reader-goldens-") as scratch:
        got = all_verdicts(Path(scratch))
    GOLDEN.write_text(json.dumps(got, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stdout.write(f"wrote {GOLDEN.relative_to(ROOT)}\n")


if __name__ == "__main__":
    _generate()
