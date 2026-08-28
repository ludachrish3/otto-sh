"""lab.json v2: declared labs, element membership by pattern, migration error (spec §2, §7, §9)."""

import json
from pathlib import Path

import pytest

from otto.labs import LabNotFoundError, LabRepositoryError
from otto.labs.json_repository import JsonFileLabRepository, parse_lab_sections

_CREDS = [{"login": "u", "password": "p"}]


def _write(tmp_path: Path, doc: dict, name: str = "lab.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return p


def _doc(*, labs=None, elements=(), links=()) -> dict:
    return {"labs": labs or {}, "elements": list(elements), "links": list(links)}


def _el(name, labs, hosts=None, **extra) -> dict:
    return {
        "name": name,
        "labs": labs,
        "hosts": hosts or [{"ip": "10.0.0.1", "creds": _CREDS}],
        **extra,
    }


def test_top_level_hosts_is_the_migration_error(tmp_path: Path) -> None:
    _write(tmp_path, {"hosts": [], "links": []})
    with pytest.raises(LabRepositoryError, match=r"'hosts'.*moved.*'elements'"):
        JsonFileLabRepository([tmp_path]).load_lab("unix")


def test_parse_lab_sections_shapes() -> None:
    out = parse_lab_sections({"$schema": "x", "_c": 1, "labs": {"unix": {}}, "elements": []}, "f")
    assert out == {"labs": {"unix": {}}, "elements": [], "links": []}
    with pytest.raises(LabRepositoryError, match="unknown section"):
        parse_lab_sections({"routes": []}, "f")
    with pytest.raises(LabRepositoryError, match="'labs' must be a JSON object"):
        parse_lab_sections({"labs": []}, "f")
    with pytest.raises(LabRepositoryError, match="'elements' must be a JSON array"):
        parse_lab_sections({"elements": {}}, "f")


def test_members_by_fullmatch_pattern_and_flattened_identity(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _doc(
            labs={"unix": {"resources": ["unix-bed"], "metadata": {"d": 1}}, "unix2": {}},
            elements=[
                _el("test1", ["unix"]),
                _el("dut", ["unix(\\..*)?"], id=3, hosts=[{"ip": "10.0.0.2", "creds": _CREDS}]),
                _el("other", ["unix2"], hosts=[{"ip": "10.0.0.3", "creds": _CREDS}]),
            ],
        ),
    )
    lab = JsonFileLabRepository([tmp_path]).load_lab("unix")
    assert set(lab.hosts) == {"test1", "dut3"}
    assert lab.hosts["dut3"].element == "dut"
    assert lab.hosts["dut3"].element_id == 3
    assert lab.resources == {"unix-bed"}
    assert lab.metadata == {"unix": {"d": 1}}


def test_element_metadata_copied_per_host(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _doc(
            labs={"l": {}},
            elements=[
                _el(
                    "dut",
                    ["l"],
                    metadata={"rack": "B4"},
                    hosts=[
                        {"ip": "10.0.0.1", "board": "a", "creds": _CREDS},
                        {"ip": "10.0.0.2", "board": "b", "creds": _CREDS},
                    ],
                )
            ],
        ),
    )
    lab = JsonFileLabRepository([tmp_path]).load_lab("l")
    a, b = lab.hosts["dut_a"], lab.hosts["dut_b"]
    assert a.element_metadata == {"rack": "B4"}
    assert b.element_metadata == {"rack": "B4"}
    a.element_metadata["rack"] = "Z"
    assert b.element_metadata == {"rack": "B4"}


def test_source_with_neither_members_nor_declaration_raises_not_found(tmp_path: Path) -> None:
    _write(tmp_path, _doc(labs={"unix": {}}, elements=[_el("t", ["unix"])]))
    with pytest.raises(LabNotFoundError):
        JsonFileLabRepository([tmp_path]).load_lab("embedded")


def test_declaration_only_and_members_only_contributions(tmp_path: Path) -> None:
    _write(tmp_path, _doc(labs={"unix": {"resources": ["r"]}}, elements=[_el("t", ["other"])]))
    declared_only = JsonFileLabRepository([tmp_path]).load_lab("unix")
    assert declared_only.hosts == {}
    assert declared_only.resources == {"r"}
    members_only = JsonFileLabRepository([tmp_path]).load_lab("other")
    assert set(members_only.hosts) == {"t"}
    assert members_only.resources == set()
    assert "other" not in members_only.metadata


def test_list_labs_is_the_declared_set_not_the_patterns(tmp_path: Path) -> None:
    _write(tmp_path, _doc(labs={"unix": {}, "b": {}}, elements=[_el("t", ["unix", "ghost"])]))
    assert JsonFileLabRepository([tmp_path]).list_labs() == ["b", "unix"]


def test_duplicate_element_key_errors(tmp_path: Path) -> None:
    _write(
        tmp_path, _doc(labs={"l": {}}, elements=[_el("dut", ["l"], id=1), _el("dut", ["l"], id=1)])
    )
    with pytest.raises(LabRepositoryError, match=r"duplicate element \('dut', 1\)"):
        JsonFileLabRepository([tmp_path]).load_lab("l")


def test_host_entry_error_names_element_and_index(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _doc(labs={"l": {}}, elements=[_el("dut", ["l"], hosts=[{"ipp": "x", "creds": _CREDS}])]),
    )
    # (?s): the pydantic report is multi-line — the offending key is on a later
    # line than the "element 'dut' hosts[0]" prefix this test is really pinning.
    with pytest.raises(LabRepositoryError, match=r"(?s)element 'dut' hosts\[0\].*ipp"):
        JsonFileLabRepository([tmp_path]).load_lab("l")


def test_provider_failure_is_wrapped_with_the_entry_that_caused_it(
    tmp_path: Path, monkeypatch
) -> None:
    """A SUT-repo provider blowing up on one host must not escape as a raw traceback.

    ``create_host_from_dict`` runs repo-owned code after validation
    (``to_host``, then the product and dev-tool providers), which can raise
    ANYTHING. The composite only absorbs ``LabNotFoundError``, so an unwrapped
    ``AttributeError`` reaches the CLI with none of the file/element/index
    context spec §9 exists to give — and with no clue which of a hundred hosts
    is at fault.
    """
    _write(tmp_path, _doc(labs={"l": {}}, elements=[_el("dut", ["l"])]))
    boom = AttributeError("provider is broken")

    def _explode(host: object) -> None:
        raise boom

    monkeypatch.setattr("otto.host.factory.apply_product_providers", _explode)

    with pytest.raises(LabRepositoryError, match=r"element 'dut' hosts\[0\]") as exc_info:
        JsonFileLabRepository([tmp_path]).load_lab("l")
    assert exc_info.value.__cause__ is boom  # chained, never swallowed
    assert "provider is broken" in str(exc_info.value)


def test_summaries_carry_patterns_and_resolved_labs(tmp_path: Path) -> None:
    _write(tmp_path, _doc(labs={"unix": {}, "unix.b4": {}}, elements=[_el("t", ["unix(\\..*)?"])]))
    (s,) = JsonFileLabRepository([tmp_path]).list_host_summaries()
    assert s.id == "t"
    assert s.lab_patterns == ["unix(\\..*)?"]
    assert s.labs == ["unix", "unix.b4"]


def test_links_still_resolve_against_flattened_ids(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _doc(
            labs={"l": {}},
            elements=[
                _el(
                    "a",
                    ["l"],
                    hosts=[{"ip": "10.0.0.1", "creds": _CREDS, "interfaces": {"e": "192.168.1.1"}}],
                ),
                _el(
                    "b",
                    ["l"],
                    hosts=[{"ip": "10.0.0.2", "creds": _CREDS, "interfaces": {"e": "192.168.1.2"}}],
                ),
            ],
            links=[
                {"endpoints": [{"host": "a", "interface": "e"}, {"host": "b", "interface": "e"}]}
            ],
        ),
    )
    lab = JsonFileLabRepository([tmp_path]).load_lab("l")
    assert len(lab.links) == 1
