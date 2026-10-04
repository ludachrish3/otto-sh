"""The `__docker_observed__` namespace: what the daemon last said, per host, two TTLs."""

import json
import time

import pytest

from otto.config import completion_cache as cc
from tests._fixtures.sutrepo import make_sut_repo


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """One bootstrapped repo whose cache file lives under tmp_path."""
    from otto.bootstrap import discover

    sut = make_sut_repo(tmp_path / "sut", name="sut")
    monkeypatch.setenv("OTTO_SUT_DIRS", str(sut))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OTTO_LAB", raising=False)
    return discover().repos


def _raw(path):
    return json.loads(path.read_text())


def test_record_then_read_returns_what_was_recorded(repos):
    cc.record_docker_images(repos, "test3", refs=["repo1-api:latest"], ids=["sha256:ab"])
    cc.record_docker_containers(repos, "test3", names=["unix-x-api-1"], ids=["3f9a"])
    state = cc.read_docker_observed(repos, "test3")
    assert state == cc.ObservedDockerState(
        image_refs=["repo1-api:latest"],
        image_ids=["sha256:ab"],
        container_names=["unix-x-api-1"],
        container_ids=["3f9a"],
    )
    assert cc.read_docker_observed_hosts(repos) == ["test3"]


def test_a_cold_host_reads_empty_and_lists_no_hosts(repos):
    assert cc.read_docker_observed(repos, "nowhere") == cc.ObservedDockerState([], [], [], [])
    assert cc.read_docker_observed_hosts(repos) == []


def test_each_sub_entry_expires_on_its_own_ttl(repos):
    cc.record_docker_images(repos, "test3", refs=["a:1"], ids=["i"])
    cc.record_docker_containers(repos, "test3", names=["c"], ids=["x"])
    later = time.time() + 16 * 60  # containers gone, images (a day) still served
    state = cc.read_docker_observed(repos, "test3", now=later)
    assert state.image_refs == ["a:1"]
    assert state.container_names == []
    assert state.container_ids == []
    assert cc.read_docker_observed_hosts(repos, now=later) == ["test3"]
    much_later = time.time() + 25 * 60 * 60
    assert cc.read_docker_observed(repos, "test3", now=much_later) == cc.ObservedDockerState(
        [], [], [], []
    )
    assert cc.read_docker_observed_hosts(repos, now=much_later) == []


def test_the_ttl_boundary_is_inclusive_and_a_future_stamp_is_fresh(repos, monkeypatch):
    """Exactly ``ttl`` old is still served; one second more is not; a stamp from the future
    (clock skew) is served. ``otto._shim_complete`` mirrors this exact rule — change both
    or neither."""
    stamp = 1_800_000_000
    monkeypatch.setattr(cc.time, "time", lambda: float(stamp))
    cc.record_docker_containers(repos, "test3", names=["c"], ids=["x"])
    ttl = cc.DOCKER_OBSERVED_CONTAINERS_TTL_SECONDS
    assert cc.read_docker_observed(repos, "test3", now=stamp + ttl).container_names == ["c"]
    assert cc.read_docker_observed(repos, "test3", now=stamp + ttl + 1).container_names == []
    assert cc.read_docker_observed(repos, "test3", now=stamp - 60).container_names == ["c"]


def test_a_writer_replaces_its_own_sub_entry_and_leaves_the_other(repos):
    cc.record_docker_images(repos, "test3", refs=["a:1"], ids=["i1"])
    cc.record_docker_containers(repos, "test3", names=["c1"], ids=["x1"])
    cc.record_docker_containers(repos, "test3", names=["c2"], ids=["x2"])
    state = cc.read_docker_observed(repos, "test3")
    assert state.image_refs == ["a:1"]
    assert state.container_names == ["c2"]
    assert state.container_ids == ["x2"]


def test_lists_are_cut_at_the_cap_in_the_daemons_order(repos):
    names = [f"c{i}" for i in range(250)]
    cc.record_docker_containers(repos, "test3", names=names, ids=[f"x{i}" for i in range(250)])
    state = cc.read_docker_observed(repos, "test3")
    assert state.container_names == names[:200]
    assert state.container_ids == [f"x{i}" for i in range(200)]


def test_hosts_are_independent(repos):
    cc.record_docker_images(repos, "test3", refs=["a:1"], ids=["i"])
    cc.record_docker_images(repos, "alt2", refs=["b:1"], ids=["j"])
    assert cc.read_docker_observed(repos, "alt2").image_refs == ["b:1"]
    assert cc.read_docker_observed_hosts(repos) == ["alt2", "test3"]


def test_the_namespace_sits_beside_the_other_reserved_keys_and_keeps_them(repos):
    cc.record_tunnel_ids(repos, ["t1"])
    cc.record_docker_images(repos, "test3", refs=["a:1"], ids=["i"])
    raw = _raw(cc._cache_path())
    assert raw[cc.DYNAMIC_TUNNELS_KEY]
    entry = raw[cc.DOCKER_OBSERVED_KEY]
    assert entry["schema_version"] == cc.DOCKER_OBSERVED_SCHEMA_VERSION
    assert set(entry["hosts"]["test3"]["images"]) == {"observed_at", "refs", "ids"}
    assert cc.read_tunnel_ids(repos) == ["t1"]


@pytest.mark.parametrize(
    "broken",
    [
        "not a dict",
        {
            "schema_version": 99,
            "hosts": {"test3": {"images": {"observed_at": 1, "refs": ["a"], "ids": ["i"]}}},
        },
        {"schema_version": 1, "hosts": {"test3": {"images": {"refs": ["a"], "ids": ["i"]}}}},
        {
            "schema_version": 1,
            "hosts": {"test3": {"images": {"observed_at": "soon", "refs": ["a"], "ids": ["i"]}}},
        },
        {
            "schema_version": 1,
            "hosts": {"test3": {"images": {"observed_at": 1e12, "refs": "a", "ids": ["i"]}}},
        },
    ],
    ids=["not-a-dict", "other-schema", "no-stamp", "string-stamp", "refs-not-a-list"],
)
def test_a_malformed_entry_reads_empty_and_never_raises(repos, broken):
    cc.record_docker_images(repos, "seed", refs=["s:1"], ids=["s"])  # creates the file
    path = cc._cache_path()
    raw = _raw(path)
    raw[cc.DOCKER_OBSERVED_KEY] = broken
    path.write_text(json.dumps(raw))
    assert cc.read_docker_observed(repos, "test3") == cc.ObservedDockerState([], [], [], [])


def test_an_ephemeral_fingerprint_writes_nothing(repos, monkeypatch):
    monkeypatch.setattr(cc, "_fingerprint_is_ephemeral", lambda _repos: True)
    cc.record_docker_images(repos, "test3", refs=["a:1"], ids=["i"])
    path = cc._cache_path()
    assert not path.is_file() or cc.DOCKER_OBSERVED_KEY not in _raw(path)


def test_the_two_ttls_and_the_cap_are_the_specs(repos):
    assert cc.DOCKER_OBSERVED_CONTAINERS_TTL_SECONDS == 15 * 60
    assert cc.DOCKER_OBSERVED_IMAGES_TTL_SECONDS == 24 * 60 * 60
    assert cc.DOCKER_OBSERVED_CAP == 200
    assert cc.DOCKER_OBSERVED_KEY == "__docker_observed__"
