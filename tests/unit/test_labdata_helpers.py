"""The shared v2 fixture writers in ``tests/_fixtures/labdata.py``.

These helpers let every test keep writing FLAT host dicts while the file they
produce is v2, so they sit between the fixtures and the loader — a silent bug
here reshapes what other tests believe they asserted.
"""

import json
import os
from pathlib import Path

import pytest

from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from tests._fixtures.labdata import (
    element_for,
    element_of,
    entry_of,
    flat_hosts,
    flatten_lab_doc,
    host_data,
    lab_json_v2,
    write_lab_json,
)

_CREDS = [{"login": "u", "password": "p"}]


def _host(element: str, board: str, labs: list[str], **extra) -> dict:
    return {
        "ip": "10.0.0.1",
        "element": element,
        "board": board,
        "creds": _CREDS,
        "labs": labs,
        **extra,
    }


def test_hosts_of_one_element_group_into_one_element_entry() -> None:
    doc = lab_json_v2([_host("dut", "a", ["unix"]), _host("dut", "b", ["unix"])])
    (element,) = doc["elements"]
    assert element["name"] == "dut"
    assert element["labs"] == ["unix"]
    assert [h["board"] for h in element["hosts"]] == ["a", "b"]
    assert doc["labs"] == {"unix": {"resources": []}}


def test_lab_json_v2_still_hoists_v1_host_resources_to_the_lab() -> None:
    """Spec 2026-08-28 three-level-reservations §2: the helper migrates v1 fixtures.

    ``resources`` is a host field again, so a writer that simply forwarded the
    key would be defensible — and would silently empty the ``labs`` table under
    every migrated fixture, leaving each ``lab.resources`` assertion downstream
    to pass for the wrong reason. This helper's input is v1-shaped, and in v1 a
    host's resources aggregated into the lab, so it keeps doing exactly that; a
    test that wants element- or host-level resources writes the v2 shape
    directly.
    """
    doc = lab_json_v2([_host("a", "b", ["l"], resources=["r"])])
    assert doc["labs"]["l"]["resources"] == ["r"]
    assert "resources" not in doc["elements"][0]["hosts"][0]


def test_hosts_of_one_element_disagreeing_on_labs_is_loud() -> None:
    """v2 assigns membership per ELEMENT, so this fixture has no v2 spelling.

    Taking the first host's ``labs`` and dropping the rest would silently
    delete ``dut-b`` from ``embedded`` — and every "host X is absent from lab
    Y" assertion downstream would then pass for the wrong reason.
    """
    with pytest.raises(ValueError, match=r"'dut'.*\['unix'\].*\['embedded'\]"):
        lab_json_v2([_host("dut", "a", ["unix"]), _host("dut", "b", ["embedded"])])


def test_element_id_separates_the_groups_so_membership_may_differ() -> None:
    """Same name, different ``element_id`` = different elements, so no conflict."""
    doc = lab_json_v2(
        [
            _host("dut", "a", ["unix"], element_id=1),
            _host("dut", "b", ["embedded"], element_id=2),
        ]
    )
    assert [(e["id"], e["labs"]) for e in doc["elements"]] == [(1, ["unix"]), (2, ["embedded"])]


def test_declare_labs_false_writes_a_member_only_document(tmp_path) -> None:
    """A member file of a multi-file source declares nothing (spec §2.4)."""
    path = write_lab_json(tmp_path / "e.json", [_host("dut", "a", ["unix"])], declare_labs=False)
    doc = json.loads(path.read_text())
    assert "labs" not in doc
    assert [e["name"] for e in doc["elements"]] == ["dut"]


def test_flatten_lab_doc_round_trips_lab_json_v2() -> None:
    """The reader half undoes the writer half — minus the hoisted keys.

    Asserted as a round trip rather than against a hand-written expectation:
    the two helpers are what keeps every migrated test's flat dicts meaning
    what they meant on v1, so they have to agree with each other by
    construction, not by two copies of the same guess.
    """
    hosts = [_host("dut", "a", ["unix"], element_id=1), _host("srv", "b", ["unix", "east"])]
    flat = flatten_lab_doc(lab_json_v2(hosts))

    assert [(h["element"], h.get("element_id"), h["board"]) for h in flat] == [
        ("dut", 1, "a"),
        ("srv", None, "b"),
    ]
    # Hoisted away, because a v2 host entry may not carry them and the factory
    # would refuse the dict if it did.
    assert all("labs" not in h and "resources" not in h for h in flat)


def test_flatten_lab_doc_resolves_membership_only_when_asked() -> None:
    """``with_labs`` re-resolves the element's PATTERNS against the declared names."""
    doc = lab_json_v2([_host("dut", "a", ["unix"])])
    doc["labs"]["unix2"] = {"resources": []}
    doc["elements"][0]["labs"] = ["unix"]

    (flat,) = flatten_lab_doc(doc, with_labs=True)
    # fullmatch, not search: `unix` must not admit `unix2` (spec §7).
    assert flat["labs"] == ["unix"]


def test_flatten_lab_doc_tolerates_a_document_with_no_elements() -> None:
    """Every section is optional per file (spec §2.4), including ``elements``.

    The tree-walking readers (``tests/conformance/_cells.py``) hand this
    whatever ``lab.json`` they find, and a labs-only declaration file is a
    legal one — it must flatten to nothing, not raise.
    """
    assert flatten_lab_doc({"labs": {"unix": {"resources": []}}}) == []


def test_host_data_returns_what_the_factory_takes_and_element_for_the_rest() -> None:
    """The two halves of one fixture entry: the host dict and its ``Element``.

    ``host_data`` strips the element keys the flat v1 shape carries — the
    factory's specs are ``extra='forbid'``, so leaving either in would make
    every caller's build fail — and ``element_for`` is where they went.
    """
    entry = host_data("test1")
    assert "element" not in entry
    assert "element_id" not in entry
    element = element_for("test1")
    assert isinstance(element, Element)
    assert element.name == "test1"
    # Together they build the host, and its id is composed from the element.
    host = create_host_from_dict(entry, element=element)
    assert host.element is element
    assert host.id.startswith("test1")


def test_element_for_and_host_data_read_the_same_flat_entry() -> None:
    """``element_of``/``entry_of`` split one flat dict; the two lookups agree."""
    flat = next(h for h in flat_hosts("tech1") if h["element"] == "test1")
    assert entry_of(flat) == host_data("test1")
    assert element_of(flat) == element_for("test1")


@pytest.mark.parametrize("lookup", [host_data, element_for])
def test_an_unknown_element_names_the_file(lookup) -> None:
    with pytest.raises(KeyError, match=r"lab\.json"):
        lookup("no-such-element")


_IMPORTING_CELL = """\
local host = import 'host.libsonnet';
function(otto) {
  labs: { cell: {} },
  elements: [{ name: 'h1', labs: ['cell'], hosts: [host] }],
}
"""
"""A cell whose one host is its library's: an edit to the library alone moves its document."""


def _library_host(address: str) -> str:
    return json.dumps({"ip": address, "os_type": "unix", "creds": _CREDS})


@pytest.fixture
def importing_cell(tmp_path, monkeypatch) -> "tuple[Path, Path]":
    """A ``cell`` tech whose ``lab.jsonnet`` imports ``lab_data/lib/host.libsonnet``.

    The working directory holds the import root (``load_lab``'s, anchored at
    the current directory) and the otto home is empty. Returns the library
    file and the user settings file.
    """
    from otto.config.user_settings import clear_user_settings_memo
    from tests._fixtures import labdata

    data = tmp_path / "techs"
    (data / "cell").mkdir(parents=True)
    (data / "cell" / "lab.jsonnet").write_text(_IMPORTING_CELL)
    monkeypatch.setattr(labdata, "_LAB_DATA_DIR", data)
    work = tmp_path / "work"
    library = work / "lab_data" / "lib" / "host.libsonnet"
    library.parent.mkdir(parents=True)
    library.write_text(_library_host("192.0.2.1"))
    monkeypatch.chdir(work)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OTTO_HOME", str(home))
    clear_user_settings_memo()
    yield library, home / "settings.toml"
    clear_user_settings_memo()


def _cell_address() -> str:
    from tests._fixtures.labdata import lab_document

    return lab_document("cell")["elements"][0]["hosts"][0]["ip"]


def _bump(path: Path, text: str) -> None:
    """Rewrite *path* with *text*, its mtime moved on: an edit the freshness rule sees."""
    before = path.stat().st_mtime_ns
    path.write_text(text)
    os.utime(path, ns=(before + 1_000_000_000, before + 1_000_000_000))


def test_lab_document_sees_an_edit_to_an_imported_file_alone(importing_cell) -> None:
    """The cell's entry is unchanged; its document follows the library it imports."""
    library, _settings = importing_cell
    assert _cell_address() == "192.0.2.1"
    _bump(library, _library_host("192.0.2.2"))
    assert _cell_address() == "192.0.2.2"


def test_lab_document_sees_a_changed_protection_context(importing_cell) -> None:
    """The user settings file now names the library as protected: the render is refused."""
    from otto.config.user_settings import clear_user_settings_memo
    from otto.labs.errors import LabRepositoryError

    library, settings = importing_cell
    assert _cell_address() == "192.0.2.1"
    records = settings.parent / "usr.json"
    records.write_text("{}")
    settings.write_text(
        "[[inventories]]\n"
        'name = "usr"\nbackend = "json"\n'
        f'path = "{records}"\n'
        f'creds = {{ backend = "json", path = "{library}" }}\n'
    )
    clear_user_settings_memo()
    with pytest.raises(LabRepositoryError, match=r"refused \(a protected file\)"):
        _cell_address()


def test_a_checked_in_cell_renders_once_per_process(monkeypatch) -> None:
    """``tech1``'s entry is rendered at its first read; every later read is served by path."""
    from otto.labs import documents
    from tests._fixtures import labdata

    monkeypatch.setattr(labdata, "_CHECKED_IN_TEXT", {})
    real = documents.rendered_data
    renders: list[Path] = []

    def counting(path, render):
        renders.append(path)
        return real(path, render)

    monkeypatch.setattr(documents, "rendered_data", counting)
    first = labdata.lab_document("tech1")
    first["elements"].clear()
    again = [labdata.lab_document("tech1") for _ in range(3)]
    assert renders == [labdata.lab_data_path("tech1")]
    assert all(doc == again[0] and doc["elements"] for doc in again), "each read is its own copy"
