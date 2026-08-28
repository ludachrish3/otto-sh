"""Wiring tests: [[lab.sources]] drives lab construction end to end."""

import json
import logging
from pathlib import Path

import pytest

from otto.config.lab import load_lab
from otto.config.repo import Repo
from otto.labs import (
    CompositeLabRepository,
    JsonFileLabRepository,
    LabNotFoundError,
    LabRepositoryError,
    build_lab_sources,
    register_lab_repository,
)
from otto.labs.registry import LAB_REPOSITORIES
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo


def _lab_json(path: Path, hosts: list[dict]) -> None:
    write_lab_json(path, hosts)


def _host(element: str, ip: str) -> dict:
    return {
        "ip": ip,
        "element": element,
        "creds": [{"login": "u", "password": "p"}],
        "resources": [element],
        "labs": ["merged"],
    }


def _repo(root: Path, name: str, sources_toml: str, labfiles: dict[str, list[dict]]) -> Repo:
    sut = make_sut_repo(root, name=name, extra=sources_toml)
    for rel, hosts in labfiles.items():
        _lab_json(root / rel, hosts)
    return Repo(sut)


def test_single_json_source_still_goes_through_the_composite(tmp_path: Path) -> None:
    """ONE source is a composite too (spec §14, Ruling R15).

    The composite owns lab existence and the declared-but-memberless rule, so
    returning the bare backend here — which is what this seam used to do —
    silently exempted the commonest setup of all (one repo, one json source)
    from both. The wrapped backend is still the registry's, unchanged.
    """
    repo = _repo(
        tmp_path / "r1",
        "r1",
        '[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n',
        {"lab/lab.json": [_host("alt1", "10.0.0.1")]},
    )
    repository = build_lab_sources([repo])
    assert isinstance(repository, CompositeLabRepository)
    assert [s.label for s in repository.sources] == ["r1/json#1"]
    assert isinstance(repository.sources[0].repository, JsonFileLabRepository)
    assert set(load_lab("merged", repository=repository).hosts) == {"alt1", "local"}


def test_one_source_gets_the_declared_but_memberless_rule(tmp_path: Path) -> None:
    """The rule R15 exists for: it must fire through ``build_lab_sources``.

    ``lab.json`` declares ``ghost`` and no element matches it. With the bare
    backend this loaded an empty lab (the json source contributes its
    declaration and no members, and nothing above it objected); through the
    composite it is the loud definition mistake spec §9 requires.
    """
    repo = _repo(
        tmp_path / "r1",
        "r1",
        '[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n',
        {"lab/lab.json": [_host("alt1", "10.0.0.1")]},
    )
    lab_file = tmp_path / "r1" / "lab" / "lab.json"
    doc = json.loads(lab_file.read_text())
    doc["labs"]["ghost"] = {}
    lab_file.write_text(json.dumps(doc))

    with pytest.raises(LabRepositoryError, match=r"declared.*no element"):
        build_lab_sources([repo]).load_lab("ghost")


def test_two_repos_all_sources_live(tmp_path: Path) -> None:
    r1 = _repo(
        tmp_path / "r1",
        "r1",
        '[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n',
        {"lab/lab.json": [_host("alt1", "10.0.0.1")]},
    )
    r2 = _repo(
        tmp_path / "r2",
        "r2",
        '[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n',
        {"lab/lab.json": [_host("test2", "10.0.0.2")]},
    )
    repository = build_lab_sources([r1, r2])
    assert isinstance(repository, CompositeLabRepository)
    assert set(load_lab("merged", repository=repository).hosts) == {"alt1", "test2", "local"}


def test_later_repo_overrides_earlier_with_warning(tmp_path: Path, caplog) -> None:
    r1 = _repo(
        tmp_path / "r1",
        "r1",
        '[[lab.sources]]\nname = "global"\nbackend = "json"\npaths = ["lab"]\n',
        {"lab/lab.json": [_host("alt1", "10.0.0.1")]},
    )
    r2 = _repo(
        tmp_path / "r2",
        "r2",
        '[[lab.sources]]\nname = "override"\nbackend = "json"\npaths = ["lab"]\n',
        {"lab/lab.json": [_host("alt1", "10.9.9.9")]},
    )
    with caplog.at_level(logging.WARNING, logger="otto.labs.composite"):
        lab = build_lab_sources([r1, r2]).load_lab("merged")
    assert lab.hosts["alt1"].ip == "10.9.9.9"
    # The ELEMENT warning specifically. Both repos also DECLARE `merged`, so a
    # labs-entry warning naming the same two labels is emitted too — an
    # assertion on the labels alone would be satisfied by that one, and
    # deleting the element warning outright would still pass.
    assert any(
        "('alt1', None)" in r.getMessage()
        and "r2/override" in r.getMessage()
        and "r1/global" in r.getMessage()
        for r in caplog.records
    )


def test_custom_backend_source_kwargs_inline(tmp_path: Path) -> None:
    class DictRepo:
        def __init__(self, repo_dir, names=None):
            self.repo_dir = repo_dir
            self._names = names or []

        def load_lab(self, name, preferences=None):
            from otto.config.lab import Lab

            return Lab(name=name)

        def list_labs(self):
            return list(self._names)

    register_lab_repository("dict-wiring-test", DictRepo)
    try:
        repo = _repo(
            tmp_path / "r1",
            "r1",
            '[[lab.sources]]\nbackend = "dict-wiring-test"\nnames = ["from-custom"]\n',
            {},
        )
        repository = build_lab_sources([repo])
        # The composite wraps it (R15); the CONSTRUCTED backend is still the
        # registered class, with the entry's kwargs passed verbatim.
        built = repository.sources[0].repository
        assert isinstance(built, DictRepo)
        assert built.repo_dir == repo.sut_dir
        assert repository.list_labs() == ["from-custom"]
    finally:
        LAB_REPOSITORIES.unregister("dict-wiring-test")


def test_reregistering_json_takes_effect(tmp_path: Path) -> None:
    """The built-in name is resolved through the REGISTRY, not hardcoded.

    A repo may replace ``"json"`` (``overwrite=True``) with a class honouring
    the same ``search_paths=`` constructor contract; the composite must build
    the replacement. Ported from the deleted build_lab_repository suite — the
    bypass it guards against is just as reachable through this seam.
    """

    class ReplacementJsonRepo:
        def __init__(self, search_paths=None):
            self.search_paths = list(search_paths or [])

        def load_lab(self, name, preferences=None):
            raise NotImplementedError

        def list_labs(self):
            return []

    repo = _repo(
        tmp_path / "r1",
        "r1",
        '[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n',
        {},
    )
    register_lab_repository("json", ReplacementJsonRepo, overwrite=True)
    try:
        repository = build_lab_sources([repo])
        built = repository.sources[0].repository  # the composite wraps it (R15)
        assert isinstance(built, ReplacementJsonRepo)
        assert built.search_paths == [repo.sut_dir / "lab"]
    finally:
        register_lab_repository("json", JsonFileLabRepository, overwrite=True)


def test_unknown_backend_raises(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r1", "r1", '[[lab.sources]]\nbackend = "nope"\n', {})
    with pytest.raises(LabRepositoryError, match="Unknown lab repository backend"):
        build_lab_sources([repo])


def test_no_sources_anywhere_fails_loud_on_demand(tmp_path: Path) -> None:
    repo = Repo(make_sut_repo(tmp_path / "bare", name="bare"))
    repository = build_lab_sources([repo])
    assert repository.list_labs() == []
    with pytest.raises(LabNotFoundError, match=r"\[\[lab\.sources\]\]"):
        repository.load_lab("anything")
