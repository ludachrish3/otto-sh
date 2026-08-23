"""collect_host_ids surfaces the built-in `local` host for tab completion."""

import json
from pathlib import Path
from types import SimpleNamespace

from otto.config.completion_cache import (
    collect_docker_capable_host_ids,
    collect_host_ids,
    collect_host_ids_by_lab,
)
from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from tests._fixtures.labdata import json_lab_sources


def _repo_with_hosts(tmp_path: Path, hosts: list[dict]) -> SimpleNamespace:
    """A fake Repo whose single lab search path holds *hosts* in lab.json."""
    lab = tmp_path / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    (lab / "lab.json").write_text(json.dumps({"hosts": hosts}))
    return SimpleNamespace(
        lab_sources=json_lab_sources(tmp_path, [lab]),
        sut_dir=tmp_path,
    )


_TEST1 = {
    "ip": "1.1.1.1",
    "element": "test1",
    "creds": [{"login": "u", "password": "p"}],
    "labs": ["unix"],
}
_TEST2 = {
    "ip": "1.1.1.2",
    "element": "test2",
    "creds": [{"login": "u", "password": "p"}],
    "labs": ["unix"],
}
_ALT2 = {
    "ip": "1.1.1.3",
    "element": "alt2",
    "creds": [{"login": "u", "password": "p"}],
    "labs": ["unix_alt"],
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
    repo = _repo_with_hosts(tmp_path, [_TEST1, _TEST2, _ALT2])

    assert collect_host_ids([repo], lab_names=["unix"]) == [
        "local",
        "test1",
        "test2",
    ]


def test_lab_names_filter_unknown_lab_yields_only_builtin(tmp_path: Path) -> None:
    """A lab no host belongs to still resolves the always-present built-in host."""
    repo = _repo_with_hosts(tmp_path, [_TEST1, _ALT2])

    assert collect_host_ids([repo], lab_names=["ghosts"]) == ["local"]


def test_lab_names_filter_unions_multiple_labs(tmp_path: Path) -> None:
    repo = _repo_with_hosts(tmp_path, [_TEST1, _TEST2, _ALT2])

    assert collect_host_ids([repo], lab_names=["unix", "unix_alt"]) == [
        "alt2",
        "local",
        "test1",
        "test2",
    ]


def test_lab_names_none_returns_all_hosts(tmp_path: Path) -> None:
    """Regression: the default (no filter) still enumerates every host."""
    repo = _repo_with_hosts(tmp_path, [_TEST1, _ALT2])

    assert collect_host_ids([repo]) == ["alt2", "local", "test1"]


# ── collect_host_ids_by_lab ──────────────────────────────────────────────────


def test_collect_host_ids_by_lab_groups_by_membership(tmp_path: Path) -> None:
    """Each lab maps to its member host IDs — pure membership, no built-ins."""
    repo = _repo_with_hosts(tmp_path, [_TEST1, _TEST2, _ALT2])

    by_lab = collect_host_ids_by_lab([repo])

    assert by_lab == {
        "unix": ["test1", "test2"],
        "unix_alt": ["alt2"],
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
        "labs": ["unix", "unix_alt"],
    }
    repo = _repo_with_hosts(tmp_path, [shared])

    by_lab = collect_host_ids_by_lab([repo])

    assert by_lab == {
        "unix": ["shared_seed"],
        "unix_alt": ["shared_seed"],
    }


def test_collect_host_ids_by_lab_empty_without_hosts() -> None:
    assert collect_host_ids_by_lab([]) == {}


# ── lab-scoped docker container synthesis ────────────────────────────────────


def _repo_with_docker(tmp_path: Path, hosts: list[dict], compose) -> SimpleNamespace:
    repo = _repo_with_hosts(tmp_path, hosts)
    repo.name = "myrepo"
    repo.docker_settings = SimpleNamespace(composes=[compose])
    return repo


_TEST1_DOCKER = {**_TEST1, "docker_capable": True}


def test_lab_filter_synthesizes_container_when_default_host_in_lab(tmp_path: Path) -> None:
    """A compose default_host that survives the lab filter yields its container."""
    compose = SimpleNamespace(default_host="test1", services=("api",))
    repo = _repo_with_docker(tmp_path, [_TEST1_DOCKER], compose)

    assert collect_host_ids([repo], lab_names=["unix"]) == [
        "local",
        "test1",
        "test1.myrepo.api",
    ]


def test_lab_filter_drops_container_when_default_host_outside_lab(tmp_path: Path) -> None:
    """A default_host filtered out by the lab must not synthesize a container.

    Guards the leak the old code had: it synthesized default_host containers
    regardless of which lab was selected.
    """
    compose = SimpleNamespace(default_host="test1", services=("api",))
    repo = _repo_with_docker(tmp_path, [_TEST1_DOCKER, _ALT2], compose)

    # test1 (and thus its container) belongs to unix, not unix_alt.
    assert collect_host_ids([repo], lab_names=["unix_alt"]) == ["alt2", "local"]
