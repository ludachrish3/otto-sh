"""A link naming a host whose record was skipped says so, and why (#1305).

A lab source skips a host record it cannot read when it computes the
addressing declared links resolve against: the record may belong to another
lab, whose mistake must not fail this lab's load. A link of the loaded lab
that names such a host used to fail with "unknown host ... (no such host in
any lab file)", for a host that is in the file.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from otto.inventory import InventoryView
from otto.labs import CompositeLabRepository, LabRepositoryError, SkippedHostRecord
from otto.link.derive import DeclaredLinkEntry, HostAddressing, resolve_declared_links
from tests._fixtures.labdata import built_json_source

LAB = "bench"
_CREDS = [{"login": "u", "password": "p"}]
BAD_FIELD = "a_field_no_release_reads"


def _host(ip: str, **fields: Any) -> dict[str, Any]:
    return {"ip": ip, "os_type": "unix", "creds": _CREDS, **fields}


def _element(name: str, *hosts: dict[str, Any], lab: str = LAB) -> dict[str, Any]:
    return {"name": name, "labs": [lab], "hosts": list(hosts)}


def _link(a: str, b: str) -> dict[str, Any]:
    return {"endpoints": [{"host": a}, {"host": b}]}


def _write(path: Path, **sections: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sections))
    return path


def _sections(*, links: list[dict[str, Any]], bad: dict[str, Any] | None = None) -> dict[str, Any]:
    """Lab `bench` holds `dut` and `peer`; element `far` of lab `other` has an unreadable record."""
    far = _host("10.60.0.9", **(bad if bad is not None else {BAD_FIELD: True}))
    return {
        "labs": {LAB: {}, "other": {}},
        "elements": [
            _element("dut", _host("10.60.0.1")),
            _element("peer", _host("10.60.0.2")),
            _element("far", far, lab="other"),
        ],
        "links": links,
    }


# --------------------------------------------------------------------------- #
# Through the json source.
# --------------------------------------------------------------------------- #


def test_a_link_to_a_skipped_host_names_the_record_and_the_reason(tmp_path: Path):
    lab_file = _write(tmp_path / "lab.json", **_sections(links=[_link("dut", "far")]))
    with pytest.raises(LabRepositoryError) as caught:
        built_json_source(tmp_path, "p", lab_file).repository.load_lab(LAB)
    message = str(caught.value)
    assert message.startswith(
        f"Invalid link in {lab_file} at index 0: host 'far' was not loaded: "
        f"the record of element 'far' in {lab_file} was skipped: "
    )
    assert BAD_FIELD in message, "the reason the record was skipped is in the error"
    assert "unknown host" not in message
    assert "no such host in any lab file" not in message


def test_a_skipped_record_is_logged_by_its_keys_never_its_values(tmp_path: Path, caplog):
    bad = {"inventory": {"name": "physical", "key": "far", "password": "hunter2"}}
    lab_file = _write(tmp_path / "lab.json", **_sections(links=[_link("dut", "peer")], bad=bad))
    caplog.set_level("DEBUG", logger="otto")
    built_json_source(tmp_path, "p", lab_file).repository.load_lab(LAB)
    skipped = [r.getMessage() for r in caplog.records if "Skipping unresolvable" in r.getMessage()]
    assert skipped, "the skipped record is logged"
    assert all("'inventory'" in line for line in skipped)
    assert "hunter2" not in caplog.text


def test_a_skipped_record_no_link_names_stays_quiet(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """The skip is deliberate containment: another lab's bad record does not fail or warn here.

    The link between two good hosts makes the source read every record, the
    unreadable one included (the debug line shows it did).
    """
    lab_file = _write(tmp_path / "lab.json", **_sections(links=[_link("dut", "peer")]))
    with caplog.at_level(logging.DEBUG, logger="otto.labs.json_repository"):
        lab = built_json_source(tmp_path, "p", lab_file).repository.load_lab(LAB)
    assert set(lab.hosts) == {"dut", "peer"}
    assert len(lab.links) == 1
    mentions = [r for r in caplog.records if BAD_FIELD in r.getMessage()]
    assert mentions, "the record was read and skipped"
    assert {r.levelno for r in mentions} == {logging.DEBUG}


def test_a_link_to_a_host_no_record_holds_is_still_an_unknown_host(tmp_path: Path):
    lab_file = _write(tmp_path / "lab.json", **_sections(links=[_link("dut", "ghost")]))
    with pytest.raises(
        LabRepositoryError, match=r"unknown host 'ghost' \(no such host in any lab file\)"
    ):
        built_json_source(tmp_path, "p", lab_file).repository.load_lab(LAB)


def test_a_link_of_one_source_names_a_host_another_source_skipped(tmp_path: Path):
    a = _write(
        tmp_path / "a" / "lab.json",
        labs={LAB: {}},
        elements=[_element("dut", _host("10.60.0.1"))],
        links=[_link("dut", "far")],
    )
    b = _write(
        tmp_path / "b" / "lab.json",
        labs={"other": {}},
        elements=[_element("far", _host("10.60.0.9", **{BAD_FIELD: True}), lab="other")],
    )
    composite = CompositeLabRepository(
        [built_json_source(tmp_path, "a", a), built_json_source(tmp_path, "b", b)]
    )
    view = InventoryView.of_default(inventory=None)
    with pytest.raises(LabRepositoryError) as caught:
        composite.load_lab_reported(LAB, view=view, report=composite.lab_status_report(view=view))
    message = str(caught.value)
    assert f"Invalid link in {a} at index 0: host 'far' was not loaded" in message
    assert f"the record of element 'far' in {b} was skipped: " in message


# --------------------------------------------------------------------------- #
# The resolution itself.
# --------------------------------------------------------------------------- #

HOSTS = {"dut": HostAddressing(ip="10.60.0.1", interfaces={})}


def _resolve(named: str, *skipped: SkippedHostRecord, pass_skipped: bool = True) -> None:
    entries = [DeclaredLinkEntry(origin=Path("lab.json"), index=0, entry=_link("dut", named))]
    kwargs: dict[str, Any] = {"skipped": lambda: skipped} if pass_skipped else {}
    resolve_declared_links(entries, HOSTS, loaded_ids={"dut"}, selection=frozenset({LAB}), **kwargs)


def _skip(element: str, reason: str = "boom", origin: str = "far.json") -> SkippedHostRecord:
    return SkippedHostRecord(element=element, origin=Path(origin), reason=reason)


@pytest.mark.parametrize("named", ["bb1310", "bb1310-qemu", "bb1310-qemu-2"])
def test_a_skipped_record_is_matched_by_its_element_name(named: str):
    """Host ids are ``<element>-<board>[-<slot>]``; the record's id itself is not known."""
    with pytest.raises(ValueError, match="boom") as caught:
        _resolve(named, _skip("bb1310"))
    assert str(caught.value) == (
        f"Invalid link in lab.json at index 0: host {named!r} was not loaded: "
        "the record of element 'bb1310' in far.json was skipped: boom"
    )


def test_the_element_name_is_matched_as_its_slug():
    with pytest.raises(ValueError, match=r"the record of element 'BB 1310' in far\.json"):
        _resolve("bb-1310-qemu", _skip("BB 1310"))


@pytest.mark.parametrize("element", ["bb1", "bb1310-qemu-extra", "other"])
def test_a_skipped_record_of_another_element_is_not_blamed(element: str):
    with pytest.raises(ValueError, match=r"unknown host 'bb1310-qemu' \(no such host in any"):
        _resolve("bb1310-qemu", _skip(element))


def test_every_matching_skipped_record_is_listed():
    with pytest.raises(ValueError, match="was not loaded") as caught:
        _resolve("far-a", _skip("far", "first"), _skip("far", "second", origin="g.json"))
    assert str(caught.value).endswith(
        "host 'far-a' was not loaded: the record of element 'far' in far.json was skipped: "
        "first; the record of element 'far' in g.json was skipped: second"
    )


def test_without_skipped_records_a_miss_is_an_unknown_host():
    """A caller, or a custom source, that reports no skips gets today's message."""
    with pytest.raises(ValueError, match=r"unknown host 'far' \(no such host in any lab file\)$"):
        _resolve("far", pass_skipped=False)
