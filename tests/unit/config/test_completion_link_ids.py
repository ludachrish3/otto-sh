"""collect_link_ids offers the declared static links `otto link` can act on.

Two properties, both held against the real loaded lab rather than a
hand-written list: every offered id resolves through ``find_link`` AND is
actually impairable, and a lab's links do not leak into another lab's
completion.

Implicit links are deliberately absent even though ``find_link`` accepts
them — see the negative test at the bottom for why.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.config.completion_cache import collect_link_ids
from otto.labs import build_lab_sources
from otto.link.placement import BOTH_DIRECTIONS, endpoint_placements, ensure_not_local_link
from tests._fixtures.labdata import json_lab_sources

_TEST1 = {
    "ip": "1.1.1.1",
    "element": "test1",
    "creds": [{"login": "u", "password": "p"}],
    "interfaces": {"eth1": "10.0.0.1"},
}
_TEST2 = {
    "ip": "1.1.1.2",
    "element": "test2",
    "creds": [{"login": "u", "password": "p"}],
    "interfaces": {"eth1": "10.0.0.2"},
}


def _repo_with_lab(
    tmp_path: Path, hosts: list[dict], links: list[dict], *, lab: str = "unix"
) -> SimpleNamespace:
    """A fake Repo whose single lab search path holds *hosts* and *links*."""
    lab_dir = tmp_path / "lab"
    lab_dir.mkdir(parents=True, exist_ok=True)
    hosts = [{**h, "labs": h.get("labs", [lab])} for h in hosts]
    (lab_dir / "lab.json").write_text(json.dumps({"hosts": hosts, "links": links}))
    return SimpleNamespace(
        lab_sources=json_lab_sources(tmp_path, [lab_dir]),
        sut_dir=tmp_path,
        name="fake",
    )


def _impairable(link) -> bool:
    """Exactly what `otto link impair` requires beyond find_link resolving.

    Deliberately NOT `impairment_refusal`: this is the oracle for a property
    of `collect_link_ids`, and re-deriving it from the functions that actually
    refuse keeps it independent of the predicate. `impairment_refusal`'s own
    agreement with these two is pinned in `tests/unit/link/test_impairability`.
    """
    try:
        ensure_not_local_link(link)
        endpoint_placements(link, BOTH_DIRECTIONS)
    except ValueError:
        return False
    return True


def test_collect_link_ids_names_and_derived(tmp_path: Path) -> None:
    repo = _repo_with_lab(
        tmp_path,
        hosts=[_TEST1, _TEST2],
        links=[
            {"endpoints": [{"host": "test1"}, {"host": "test2"}], "name": "edge"},
            {"endpoints": [{"host": "test1"}, {"host": "test2"}]},
        ],
    )
    assert collect_link_ids([repo]) == ["edge", "test1--test2"]


def test_collect_link_ids_empty_without_links(tmp_path: Path) -> None:
    repo = _repo_with_lab(tmp_path, hosts=[_TEST1], links=[])
    assert collect_link_ids([repo]) == []


def test_collect_link_ids_no_repos() -> None:
    assert collect_link_ids([]) == []


def test_collect_link_ids_skips_malformed_entries(tmp_path: Path) -> None:
    repo = _repo_with_lab(
        tmp_path,
        hosts=[_TEST1],
        links=[
            "not-a-dict",
            {"endpoints": "not-a-list"},
            {"endpoints": [{"host": "test1"}]},  # only one endpoint
            {"endpoints": [{"host": "test1"}, {"not_host": "x"}]},  # missing host key
            {"endpoints": [{"host": "test1"}, {"host": ""}]},  # empty host
        ],
    )
    assert collect_link_ids([repo]) == []


def test_collect_link_ids_dedupes_and_sorts(tmp_path: Path) -> None:
    repo = _repo_with_lab(
        tmp_path,
        hosts=[_TEST1, _TEST2],
        links=[
            {"endpoints": [{"host": "test2"}, {"host": "test1"}]},
            {"endpoints": [{"host": "test1"}, {"host": "test2"}]},
            {"endpoints": [{"host": "test1"}, {"host": "test2"}], "name": "edge"},
        ],
    )
    assert collect_link_ids([repo]) == ["edge", "test1--test2"]


# ── The two properties, against a real loaded lab ────────────────────────────


def test_every_offered_id_resolves_and_is_impairable(tmp_path: Path) -> None:
    """Offering an id `otto link impair` refuses is worse than offering nothing.

    This is why implicit links are excluded: they resolve through find_link
    but can never be impaired, so an acceptance-only assertion would pass
    while the shell suggested guaranteed errors.
    """
    repo = _repo_with_lab(
        tmp_path,
        hosts=[_TEST1, _TEST2],
        links=[
            {
                "endpoints": [
                    {"host": "test1", "interface": "eth1"},
                    {"host": "test2", "interface": "eth1"},
                ],
                "name": "edge",
            }
        ],
    )
    lab = build_lab_sources([repo]).load_lab("unix")
    by_ident = {link.id: link for link in lab.static_links()}
    by_ident.update({link.name: link for link in lab.static_links() if link.name})

    offered = collect_link_ids([repo])
    assert offered, "positive control: something must be offered"
    for ident in offered:
        assert ident in by_ident, f"{ident} is offered but find_link would not resolve it"
        assert _impairable(by_ident[ident]), f"{ident} is offered but cannot be impaired"


def test_implicit_links_are_offered_by_nobody_because_nothing_can_impair_them(
    tmp_path: Path,
) -> None:
    """Pins the reason for the exclusion, so a future widening has to face it."""
    repo = _repo_with_lab(tmp_path, hosts=[_TEST1, {**_TEST2, "hop": "test1"}], links=[])
    lab = build_lab_sources([repo]).load_lab("unix")

    implicit = [link for link in lab.static_links() if link.provenance.value == "implicit"]
    assert implicit, "positive control: the lab must have implicit links"
    assert not any(_impairable(link) for link in implicit), (
        "an implicit link became impairable — completion should now offer it"
    )
    assert collect_link_ids([repo]) == []


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        (None, ["alt1--alt2-link", "test1--test2"]),
        ({"test1", "test2"}, ["test1--test2"]),
        ({"alt1", "alt2"}, ["alt1--alt2-link"]),
    ],
)
def test_loaded_ids_scopes_to_the_selected_lab(
    tmp_path: Path, selected: set[str] | None, expected: list[str]
) -> None:
    """Under `-l <lab>`, a sibling lab's links are not offered.

    `find_link` only ever sees the loaded lab's links (json_repository filters
    declared links to those touching its hosts), so an unscoped completer
    offers ids that dispatch refuses.
    """
    unix_alt = [
        {**_TEST1, "element": "alt1", "board": None, "labs": ["unix_alt"]},
        {**_TEST2, "element": "alt2", "board": None, "labs": ["unix_alt"]},
    ]
    repo = _repo_with_lab(
        tmp_path,
        hosts=[_TEST1, _TEST2, *unix_alt],
        links=[
            {"endpoints": [{"host": "test1"}, {"host": "test2"}]},
            {"endpoints": [{"host": "alt1"}, {"host": "alt2"}], "name": "alt1--alt2-link"},
        ],
    )
    assert collect_link_ids([repo], loaded_ids=selected) == expected
