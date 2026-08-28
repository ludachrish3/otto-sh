"""The shared v2 fixture writers in ``tests/_fixtures/labdata.py``.

These helpers let every test keep writing FLAT host dicts while the file they
produce is v2, so they sit between the fixtures and the loader — a silent bug
here reshapes what other tests believe they asserted.
"""

import json

import pytest

from tests._fixtures.labdata import flatten_lab_doc, lab_json_v2, write_lab_json

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


def test_hosts_of_one_element_disagreeing_on_labs_is_loud() -> None:
    """v2 assigns membership per ELEMENT, so this fixture has no v2 spelling.

    Taking the first host's ``labs`` and dropping the rest would silently
    delete ``dut_b`` from ``embedded`` — and every "host X is absent from lab
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
