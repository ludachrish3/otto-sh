"""How a lab's declared links compose across files and sources, pinned case by case.

:meth:`~otto.labs.CompositeLabRepository.load_lab_reported` merges the
sources' hosts, then resolves every source's ``links`` once, over the composed
lab (:func:`~otto.labs.composition.compose_links`): an endpoint resolves
through its own source's addressing first, so each source's own result
stands, then through the composed hosts and the other sources. The links merge
by id, the later source winning. Each case below builds its
sources under a scratch directory, loads lab :data:`LAB` and records
(:func:`outcome`) the resulting links as ``[id, a, b]`` (an endpoint is
``host[:interface][@ip]``), the lab's dangling ends (an endpoint host the lab
does not hold, :func:`~tests._fixtures.labdata.dangling_ends`) and, where a
case names it, the source that declares a link; or the error text, with the
case's directory written ``<case>``. The record is compared with
``tests/_fixtures/link_goldens.json``.

Generation and comparison run the SAME case builders. ``python -m
tests.unit.labs.test_link_composition_goldens`` rewrites the golden from the
code as it stands, so a red test here needs a ruling on the change, never a
regeneration to make it green.
"""

import dataclasses
import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import otto
from tests._fixtures.labdata import built_json_source, dangling_ends
from tests._fixtures.named_inventory import FakeViewSource, LinkedViewSource
from tests._fixtures.portable import portable

if TYPE_CHECKING:
    from otto.link.model import Link

ROOT = Path(otto.__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "_fixtures" / "link_goldens.json"

LAB = "site"
"""The lab every case loads."""

_CREDS = [{"login": "u", "password": "p"}]


# --------------------------------------------------------------------------- #
# Builders: lab documents, sources, composites.
# --------------------------------------------------------------------------- #


def _host(ip: str, **identity: Any) -> dict[str, Any]:
    return {"ip": ip, "os_type": "unix", "creds": _CREDS, **identity}


def _element(name: str, *hosts: dict[str, Any], labs: "list[str] | None" = None) -> dict[str, Any]:
    return {"name": name, "labs": labs or [LAB], "hosts": list(hosts)}


def _link(a: str, b: str, *, name: "str | None" = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"endpoints": [{"host": a}, {"host": b}]}
    if name is not None:
        entry["name"] = name
    return entry


def _write(path: Path, **sections: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sections, indent=2))
    return path


def _resolved_link(a: str, b: str, *, name: str) -> "Link":
    """A declared link between hosts *a* and *b*, as a backend's loaded lab carries it."""
    from otto.link.model import Link, LinkEndpoint

    return Link(a=LinkEndpoint(host=a), b=LinkEndpoint(host=b), name=name)


def _view_source(label: str, repository: Any) -> Any:
    from otto.labs import LabSource

    return LabSource(label=label, repository=repository, project_root=None)


@dataclasses.dataclass(frozen=True)
class _Loaded:
    """A case's loaded lab, and the source declaring each link the case names."""

    lab: Any
    declared_by: "dict[str, str]" = dataclasses.field(default_factory=dict)
    """Link id to the label of the source whose file declares it; a ``Link`` carries no source."""


def _composite_load(*sources: Any) -> Any:
    """Load :data:`LAB` from the sources, in order, through ``load_lab_reported``."""
    from otto.inventory.view import InventoryView
    from otto.labs import CompositeLabRepository

    composite = CompositeLabRepository(list(sources))
    view = InventoryView.of_default(inventory=None)
    return composite.load_lab_reported(
        LAB, view=view, report=composite.lab_status_report(view=view)
    )


# --------------------------------------------------------------------------- #
# The cases. Each returns the loaded lab; :func:`outcome` projects it.
# --------------------------------------------------------------------------- #


def case_a_link_across_two_files_of_one_source(case: Path) -> Any:
    """One source, two files: the hosts in one, the link between them in the other."""
    hosts = _write(
        case / "hosts.json",
        labs={LAB: {}},
        elements=[_element("alpha", _host("10.40.0.1")), _element("beta", _host("10.40.0.2"))],
    )
    links = _write(case / "links.json", links=[_link("alpha", "beta")])
    return _composite_load(built_json_source(case, "a", hosts, links))


def case_b_an_unrelated_labs_malformed_link(case: Path) -> Any:
    """A link between two hosts of another lab, malformed, beside this lab's good one."""
    lab = _write(
        case / "lab.json",
        labs={LAB: {}, "other": {}},
        elements=[
            _element("alpha", _host("10.40.1.1")),
            _element("beta", _host("10.40.1.2")),
            _element("gamma", _host("10.40.1.3"), labs=["other"]),
            _element("delta", _host("10.40.1.4"), labs=["other"]),
        ],
        links=[
            # One endpoint, where a link needs two: malformed, and touching only ``other``.
            {"endpoints": [{"host": "gamma"}], "protocol": "tcp"},
            _link("alpha", "beta"),
        ],
    )
    return _composite_load(built_json_source(case, "a", lab))


def case_c_a_link_with_an_unknown_host(case: Path) -> Any:
    lab = _write(
        case / "lab.json",
        labs={LAB: {}},
        elements=[_element("alpha", _host("10.40.2.1"))],
        links=[_link("alpha", "ghost")],
    )
    return _composite_load(built_json_source(case, "a", lab))


def case_d_a_link_in_b_to_a_host_only_a_declares(case: Path) -> Any:
    """Source B links its ``beta`` to ``alpha``, which only source A declares."""
    first = _write(
        case / "a" / "lab.json", labs={LAB: {}}, elements=[_element("alpha", _host("10.40.3.1"))]
    )
    second = _write(
        case / "b" / "lab.json",
        elements=[_element("beta", _host("10.40.3.2"))],
        links=[_link("beta", "alpha")],
    )
    return _composite_load(
        built_json_source(case, "a", first), built_json_source(case, "b", second)
    )


def case_e_one_link_id_in_two_sources(case: Path) -> Any:
    """``uplink`` is declared by A between its hosts and by B between its own; B is later."""
    first = _write(
        case / "a" / "lab.json",
        labs={LAB: {}},
        elements=[_element("alpha", _host("10.40.4.1")), _element("beta", _host("10.40.4.2"))],
        links=[_link("alpha", "beta", name="uplink")],
    )
    second = _write(
        case / "b" / "lab.json",
        elements=[_element("gamma", _host("10.40.4.3")), _element("delta", _host("10.40.4.4"))],
        links=[_link("gamma", "delta", name="uplink")],
    )
    return _composite_load(
        built_json_source(case, "a", first), built_json_source(case, "b", second)
    )


def case_f_a_link_only_source(case: Path) -> Any:
    """Source B holds only a link (``extra``): no elements, no ``labs`` declaration."""
    first = _write(
        case / "a" / "lab.json",
        labs={LAB: {}},
        elements=[_element("alpha", _host("10.40.5.1")), _element("beta", _host("10.40.5.2"))],
        links=[_link("alpha", "beta")],
    )
    second = _write(case / "b" / "lab.json", links=[_link("alpha", "beta", name="extra")])
    return _composite_load(
        built_json_source(case, "a", first), built_json_source(case, "b", second)
    )


def case_g_a_link_to_a_slot_a_later_source_removed(case: Path) -> Any:
    """The link to ``chassis-lc-2`` is declared in source A, the earlier, replaced one.

    A declares the two-slot element ``chassis`` and the link
    ``chassis-lc-2--peer``, which resolves against A's own hosts. B, later,
    replaces ``chassis`` with a one-slot entry, so ``chassis-lc-2`` is not in
    the merged lab; the endpoint still resolves through A's own addressing, as
    A resolved it alone, so the merged lab holds a link to a host it does not
    have.
    """
    first = _write(
        case / "a" / "lab.json",
        labs={LAB: {}},
        elements=[
            _element(
                "chassis",
                _host("10.40.6.1", board="lc", slot=1),
                _host("10.40.6.2", board="lc", slot=2),
            ),
            _element("peer", _host("10.40.6.9")),
        ],
        links=[_link("chassis-lc-2", "peer")],
    )
    second = _write(
        case / "b" / "lab.json",
        elements=[_element("chassis", _host("10.40.6.1", board="lc", slot=1))],
    )
    declaring = built_json_source(case, "a", first)
    return _Loaded(
        lab=_composite_load(declaring, built_json_source(case, "b", second)),
        declared_by={"chassis-lc-2--peer": declaring.label},
    )


def case_h_a_legacy_source_host_as_a_link_endpoint(case: Path) -> Any:
    """A third-party source declares ``legacy``; the json source links ``alpha`` to it."""
    legacy = FakeViewSource({LAB: [{"name": "legacy", "hosts": [_host("10.40.7.1")]}]})
    lab = _write(
        case / "lab.json",
        elements=[_element("alpha", _host("10.40.7.2"))],
        links=[_link("alpha", "legacy")],
    )
    return _composite_load(_view_source("legacy/fake", legacy), built_json_source(case, "a", lab))


def case_i_legacy_and_json_interleaved_override_one_id(case: Path) -> Any:
    """json A, a third-party source, then json B, each declaring ``uplink``; B is last."""
    first = _write(
        case / "a" / "lab.json",
        labs={LAB: {}},
        elements=[_element("alpha", _host("10.40.8.1")), _element("beta", _host("10.40.8.2"))],
        links=[_link("alpha", "beta", name="uplink"), _link("alpha", "beta", name="a-only")],
    )
    legacy = LinkedViewSource(
        {
            LAB: [
                {"name": "gamma", "hosts": [_host("10.40.8.3")]},
                {"name": "delta", "hosts": [_host("10.40.8.4")]},
            ]
        },
        links=[
            _resolved_link("gamma", "delta", name="uplink"),
            _resolved_link("gamma", "delta", name="legacy-only"),
        ],
    )
    second = _write(
        case / "b" / "lab.json",
        elements=[
            _element("epsilon", _host("10.40.8.5")),
            _element("zeta", _host("10.40.8.6")),
        ],
        links=[_link("epsilon", "zeta", name="uplink")],
    )
    return _composite_load(
        built_json_source(case, "a", first),
        _view_source("legacy/fake", legacy),
        built_json_source(case, "b", second),
    )


def case_j_the_json_backends_direct_load(case: Path) -> Any:
    """The registered json backend's own ``load_lab``, no composite."""
    lab = _write(
        case / "lab.json",
        labs={LAB: {}},
        elements=[_element("alpha", _host("10.40.9.1")), _element("beta", _host("10.40.9.2"))],
        links=[_link("alpha", "beta"), _link("beta", "alpha", name="back")],
    )
    return built_json_source(case, "a", lab).repository.load_lab(LAB)


CASES: "dict[str, Callable[[Path], Any]]" = {
    "a": case_a_link_across_two_files_of_one_source,
    "b": case_b_an_unrelated_labs_malformed_link,
    "c": case_c_a_link_with_an_unknown_host,
    "d": case_d_a_link_in_b_to_a_host_only_a_declares,
    "e": case_e_one_link_id_in_two_sources,
    "f": case_f_a_link_only_source,
    "g": case_g_a_link_to_a_slot_a_later_source_removed,
    "h": case_h_a_legacy_source_host_as_a_link_endpoint,
    "i": case_i_legacy_and_json_interleaved_override_one_id,
    "j": case_j_the_json_backends_direct_load,
}


# --------------------------------------------------------------------------- #
# The projection, shared by generation and comparison.
# --------------------------------------------------------------------------- #


def _endpoint(endpoint: Any) -> str:
    text = endpoint.host
    if endpoint.interface is not None:
        text += f":{endpoint.interface}"
    if endpoint.ip:
        text += f"@{endpoint.ip}"
    return text


def outcome(name: str, *, scratch: Path) -> "dict[str, Any] | str":
    """Case *name*'s links, dangling ends and declaring sources, or its error.

    The links are ``[id, a, b]`` entries; the error is ``<Type>: <message>``.
    """
    case = scratch / name
    case.mkdir(parents=True)
    try:
        result = CASES[name](case)
    except Exception as e:  # noqa: BLE001 — the golden records whatever the load raises
        return portable(f"{type(e).__name__}: {e}", places={"<case>": case})
    loaded = result if isinstance(result, _Loaded) else _Loaded(lab=result)
    return {
        "links": [[link.id, _endpoint(link.a), _endpoint(link.b)] for link in loaded.lab.links],
        "dangling": dangling_ends(loaded.lab),
        "declared_by": loaded.declared_by,
    }


def all_outcomes(scratch: Path) -> "dict[str, dict[str, Any] | str]":
    return {name: outcome(name, scratch=scratch) for name in CASES}


# --------------------------------------------------------------------------- #
# The tests.
# --------------------------------------------------------------------------- #


def test_the_cases_are_exactly_the_golden_ones():
    assert sorted(json.loads(GOLDEN.read_text())) == sorted(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_link_composition_matches_golden(name, tmp_path):
    got = outcome(name, scratch=tmp_path)
    for spelled in {str(tmp_path), str(tmp_path.resolve())}:
        assert spelled not in json.dumps(got), f"{spelled} leaked into a recorded outcome"
    assert got == json.loads(GOLDEN.read_text())[name]


def _generate() -> None:
    """Write the golden from the code as it stands; see the module docstring."""
    with tempfile.TemporaryDirectory(prefix="otto-link-goldens-") as scratch:
        got = all_outcomes(Path(scratch))
    GOLDEN.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(f"wrote {GOLDEN.relative_to(ROOT)}\n")


if __name__ == "__main__":
    _generate()
