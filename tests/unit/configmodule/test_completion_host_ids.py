"""collect_host_ids surfaces the built-in `local` host for tab completion."""

import json
from pathlib import Path
from types import SimpleNamespace

from otto.configmodule.completion_cache import (
    collect_docker_capable_host_ids,
    collect_host_ids,
    collect_host_ids_by_lab,
)
from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID


def _repo_with_hosts(tmp_path: Path, hosts: list[dict]) -> SimpleNamespace:
    """A fake Repo whose single lab search path holds *hosts* in lab.json."""
    lab = tmp_path / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    (lab / "lab.json").write_text(json.dumps({"hosts": hosts}))
    return SimpleNamespace(labs=[lab])


_CARROT = {
    "ip": "1.1.1.1",
    "element": "carrot",
    "board": "seed",
    "creds": [{"login": "u", "password": "p"}],
    "labs": ["veggies"],
}
_TOMATO = {
    "ip": "1.1.1.2",
    "element": "tomato",
    "board": "seed",
    "creds": [{"login": "u", "password": "p"}],
    "labs": ["veggies"],
}
_APPLE = {
    "ip": "1.1.1.3",
    "element": "apple",
    "board": "seed",
    "creds": [{"login": "u", "password": "p"}],
    "labs": ["fruits"],
}


def test_collect_host_ids_includes_builtin_local() -> None:
    # No repos → no lab.json hosts, but the built-in local must still appear.
    ids = collect_host_ids([])
    assert BUILTIN_LOCAL_HOST_ID in ids


def test_docker_capable_excludes_builtin_local() -> None:
    ids = collect_docker_capable_host_ids([])
    assert BUILTIN_LOCAL_HOST_ID not in ids


# ── lab_names filter ─────────────────────────────────────────────────────────


def test_lab_names_filter_restricts_to_membership(tmp_path: Path) -> None:
    """With lab_names, only hosts tagged with a named lab survive (plus local)."""
    repo = _repo_with_hosts(tmp_path, [_CARROT, _TOMATO, _APPLE])

    assert collect_host_ids([repo], lab_names=["veggies"]) == [
        "carrot_seed",
        "local",
        "tomato_seed",
    ]


def test_lab_names_filter_unknown_lab_yields_only_builtin(tmp_path: Path) -> None:
    """A lab no host belongs to still resolves the always-present built-in host."""
    repo = _repo_with_hosts(tmp_path, [_CARROT, _APPLE])

    assert collect_host_ids([repo], lab_names=["ghosts"]) == ["local"]


def test_lab_names_filter_unions_multiple_labs(tmp_path: Path) -> None:
    repo = _repo_with_hosts(tmp_path, [_CARROT, _TOMATO, _APPLE])

    assert collect_host_ids([repo], lab_names=["veggies", "fruits"]) == [
        "apple_seed",
        "carrot_seed",
        "local",
        "tomato_seed",
    ]


def test_lab_names_none_returns_all_hosts(tmp_path: Path) -> None:
    """Regression: the default (no filter) still enumerates every host."""
    repo = _repo_with_hosts(tmp_path, [_CARROT, _APPLE])

    assert collect_host_ids([repo]) == ["apple_seed", "carrot_seed", "local"]


# ── collect_host_ids_by_lab ──────────────────────────────────────────────────


def test_collect_host_ids_by_lab_groups_by_membership(tmp_path: Path) -> None:
    """Each lab maps to its member host IDs — pure membership, no built-ins."""
    repo = _repo_with_hosts(tmp_path, [_CARROT, _TOMATO, _APPLE])

    by_lab = collect_host_ids_by_lab([repo])

    assert by_lab == {
        "veggies": ["carrot_seed", "tomato_seed"],
        "fruits": ["apple_seed"],
    }
    # The built-in `local` is added by the completer, not stored per-lab.
    for ids in by_lab.values():
        assert BUILTIN_LOCAL_HOST_ID not in ids


def test_collect_host_ids_by_lab_host_in_two_labs(tmp_path: Path) -> None:
    """A host tagged with two labs appears in both buckets."""
    shared = {
        "ip": "9.9.9.9",
        "element": "shared",
        "board": "seed",
        "creds": [{"login": "u", "password": "p"}],
        "labs": ["veggies", "fruits"],
    }
    repo = _repo_with_hosts(tmp_path, [shared])

    by_lab = collect_host_ids_by_lab([repo])

    assert by_lab == {
        "veggies": ["shared_seed"],
        "fruits": ["shared_seed"],
    }


def test_collect_host_ids_by_lab_empty_without_hosts() -> None:
    assert collect_host_ids_by_lab([]) == {}


# ── lab-scoped docker container synthesis ────────────────────────────────────


def _repo_with_docker(tmp_path: Path, hosts: list[dict], compose) -> SimpleNamespace:
    repo = _repo_with_hosts(tmp_path, hosts)
    repo.name = "myrepo"
    repo.docker_settings = SimpleNamespace(composes=[compose])
    return repo


_CARROT_DOCKER = {**_CARROT, "docker_capable": True}


def test_lab_filter_synthesizes_container_when_default_host_in_lab(tmp_path: Path) -> None:
    """A compose default_host that survives the lab filter yields its container."""
    compose = SimpleNamespace(default_host="carrot_seed", services=("api",))
    repo = _repo_with_docker(tmp_path, [_CARROT_DOCKER], compose)

    assert collect_host_ids([repo], lab_names=["veggies"]) == [
        "carrot_seed",
        "carrot_seed.myrepo.api",
        "local",
    ]


def test_lab_filter_drops_container_when_default_host_outside_lab(tmp_path: Path) -> None:
    """A default_host filtered out by the lab must not synthesize a container.

    Guards the leak the old code had: it synthesized default_host containers
    regardless of which lab was selected.
    """
    compose = SimpleNamespace(default_host="carrot_seed", services=("api",))
    repo = _repo_with_docker(tmp_path, [_CARROT_DOCKER, _APPLE], compose)

    # carrot (and thus its container) belongs to veggies, not fruits.
    assert collect_host_ids([repo], lab_names=["fruits"]) == ["apple_seed", "local"]
