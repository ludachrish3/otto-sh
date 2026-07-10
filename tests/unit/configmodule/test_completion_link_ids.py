"""collect_link_ids surfaces static-link completion ids: name if set, else a--b."""

import json
from pathlib import Path
from types import SimpleNamespace

from otto.configmodule.completion_cache import collect_link_ids

_CARROT = {
    "ip": "1.1.1.1",
    "element": "carrot",
    "board": "seed",
    "creds": [{"login": "u", "password": "p"}],
}
_TOMATO = {
    "ip": "1.1.1.2",
    "element": "tomato",
    "board": "seed",
    "creds": [{"login": "u", "password": "p"}],
}


def _repo_with_lab(tmp_path: Path, hosts: list[dict], links: list[dict]) -> SimpleNamespace:
    """A fake Repo whose single lab search path holds *hosts* and *links* in lab.json."""
    lab = tmp_path / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    (lab / "lab.json").write_text(json.dumps({"hosts": hosts, "links": links}))
    return SimpleNamespace(labs=[lab])


def test_collect_link_ids_names_and_derived(tmp_path: Path) -> None:
    # lab.json with two links: one named ("edge"), one unnamed (id = "a_seed--b_seed")
    repo = _repo_with_lab(
        tmp_path,
        hosts=[_CARROT, _TOMATO],
        links=[
            {"endpoints": [{"host": "carrot_seed"}, {"host": "tomato_seed"}], "name": "edge"},
            {"endpoints": [{"host": "carrot_seed"}, {"host": "tomato_seed"}]},
        ],
    )
    assert collect_link_ids([repo]) == ["carrot_seed--tomato_seed", "edge"]


def test_collect_link_ids_empty_without_links(tmp_path: Path) -> None:
    repo = _repo_with_lab(tmp_path, hosts=[_CARROT], links=[])
    assert collect_link_ids([repo]) == []


def test_collect_link_ids_no_repos() -> None:
    assert collect_link_ids([]) == []


def test_collect_link_ids_skips_malformed_entries(tmp_path: Path) -> None:
    repo = _repo_with_lab(
        tmp_path,
        hosts=[_CARROT],
        links=[
            "not-a-dict",
            {"endpoints": "not-a-list"},
            {"endpoints": [{"host": "carrot_seed"}]},  # only one endpoint
            {"endpoints": [{"host": "carrot_seed"}, {"not_host": "x"}]},  # missing host key
        ],
    )
    assert collect_link_ids([repo]) == []


def test_collect_link_ids_dedupes_and_sorts(tmp_path: Path) -> None:
    repo = _repo_with_lab(
        tmp_path,
        hosts=[_CARROT, _TOMATO],
        links=[
            {"endpoints": [{"host": "tomato_seed"}, {"host": "carrot_seed"}]},
            {"endpoints": [{"host": "carrot_seed"}, {"host": "tomato_seed"}]},
            {"endpoints": [{"host": "carrot_seed"}, {"host": "tomato_seed"}], "name": "edge"},
        ],
    )
    assert collect_link_ids([repo]) == ["carrot_seed--tomato_seed", "edge"]
