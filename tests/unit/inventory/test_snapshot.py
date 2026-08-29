"""The stage-1 document writer/reader and the record diff (spec §9.1, §9.5, §11)."""

import json
import stat

import pytest

from otto.inventory.snapshot import (
    RecordDifference,
    diff_records,
    document_hash,
    document_to_records,
    records_to_document,
    write_document_atomically,
)
from otto.models.inventory import FILLABLE_INVENTORY_FIELDS, InventoryRecord


def _rec(**kw):
    return InventoryRecord.model_validate({"ip": "10.0.0.1", **kw})


def test_document_is_sorted_unstated_free_and_creds_free():
    doc = records_to_document(
        {
            "b": _rec(site="x", creds=[{"login": "u", "password": "p"}]),
            "a": _rec(shelf=2),
        }
    )
    assert list(doc) == ["a", "b"]
    assert doc["a"] == {"ip": "10.0.0.1", "shelf": 2}  # unstated fields and Nones omitted
    assert "creds" not in doc["b"]


def test_a_stated_default_survives_the_round_trip():
    """``exclude_unset``, never ``exclude_defaults``: a STATED default is a statement.

    ``exclude_defaults`` compares VALUES, so a backend that says
    ``is_virtual: false`` about a bare-metal device would write a document the
    reader could not tell from silence — and ``model_fields_set`` is precisely
    what :func:`otto.inventory.resolve_host_entry` and the conformance suite
    read to decide what an inventory supplied.
    """
    doc = records_to_document({"stated": _rec(is_virtual=False), "silent": _rec()})
    assert doc == {
        "silent": {"ip": "10.0.0.1"},
        "stated": {"ip": "10.0.0.1", "is_virtual": False},
    }
    back = document_to_records(doc, source="mem", supplies=FILLABLE_INVENTORY_FIELDS)
    assert "is_virtual" in back["stated"].model_fields_set
    assert "is_virtual" not in back["silent"].model_fields_set


def test_round_trip_and_hash(tmp_path):
    records = {
        "k": _rec(
            interfaces={"eth0": {"ip": "192.168.1.1", "subnet": "192.168.1.0/24"}},
            extra={"a": 1},
        )
    }
    doc = records_to_document(records)
    back = document_to_records(doc, source="mem", supplies=FILLABLE_INVENTORY_FIELDS)
    assert back == records
    assert document_hash(doc) == document_hash(json.loads(json.dumps(doc)))
    assert document_hash(doc) != document_hash({**doc, "k": {**doc["k"], "shelf": 1}})
    path = tmp_path / "snap.json"
    write_document_atomically(path, doc)
    assert json.loads(path.read_text()) == doc


def test_the_write_leaves_no_temporary_behind_and_creates_the_parent(tmp_path):
    path = tmp_path / "inventory-cache" / "snap.json"
    write_document_atomically(path, {"k": {"ip": "10.0.0.1"}})
    write_document_atomically(path, {"k": {"ip": "10.0.0.2"}})
    assert json.loads(path.read_text()) == {"k": {"ip": "10.0.0.2"}}
    # The whole directory, not a glob: a temp file under any name is a leak.
    assert [p.name for p in path.parent.iterdir()] == ["snap.json"]


def test_a_failed_write_leaves_the_previous_document_and_no_temporary(tmp_path):
    """The rename is the commit — a serialisation that blows up must leave nothing behind."""
    path = tmp_path / "snap.json"
    write_document_atomically(path, {"k": {"ip": "10.0.0.1"}})
    with pytest.raises(TypeError, match="not JSON serializable"):
        write_document_atomically(path, {"k": {"ip": object()}})
    assert json.loads(path.read_text()) == {"k": {"ip": "10.0.0.1"}}
    assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]


def test_diff_renders_a_quoted_string_readably_and_a_number_as_json():
    """The cell is the VALUE, not a JSON dump with its outer quotes shaved off.

    ``json.dumps(...).strip('"')`` left the escapes in place and ate a trailing
    backslash with the closing quote, so a site name containing a quote reached
    the operator as ``say \\"hi\\``.
    """
    left = {"k": _rec(os_name='say "hi"', shelf=2)}
    right = {"k": _rec(os_name="plain", shelf=3)}
    assert diff_records(left, right) == [
        RecordDifference(key="k", field="os_name", left='say "hi"', right="plain"),
        RecordDifference(key="k", field="shelf", left="2", right="3"),
    ]


def test_diff_escapes_the_whitespace_controls_that_would_break_a_row():
    """A diff is a table; a raw newline in a cell breaks the row under it.

    Targeted, though: a site or rack name in the operator's own script must
    come through intact, which ``encode("unicode_escape")`` would not manage.
    """
    assert diff_records({"k": _rec(site="a\nb\tc")}, {"k": _rec(site="plain")}) == [
        RecordDifference(key="k", field="site", left="a\\nb\\tc", right="plain")
    ]
    assert diff_records({"k": _rec(site="lab-é")}, {"k": _rec(site="plain")}) == [
        RecordDifference(key="k", field="site", left="lab-é", right="plain")
    ]


def test_the_document_is_written_private_to_the_owner(tmp_path):
    """0600 by virtue of ``mkstemp``; a refactor to ``write_text`` would widen it."""
    path = tmp_path / "snap.json"
    write_document_atomically(path, {"k": {"ip": "10.0.0.1"}})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_diff_reports_missing_keys_and_changed_fields_never_creds():
    left = {"only-left": _rec(), "both": _rec(site="a", creds=[{"login": "u", "password": "p"}])}
    right = {"only-right": _rec(), "both": _rec(site="b", shelf=1)}
    assert diff_records(left, right) == [
        RecordDifference(key="both", field="shelf", left=None, right="1"),
        RecordDifference(key="both", field="site", left="a", right="b"),
        RecordDifference(key="only-left", field=None, left="present", right=None),
        RecordDifference(key="only-right", field=None, left=None, right="present"),
    ]
    assert diff_records(left, left) == []


def test_diff_renders_a_structured_field_as_canonical_json():
    left = {"k": _rec(extra={"b": 2, "a": 1})}
    right = {"k": _rec(extra={"a": 1})}
    assert diff_records(left, right) == [
        RecordDifference(key="k", field="extra", left='{"a": 1, "b": 2}', right='{"a": 1}')
    ]
