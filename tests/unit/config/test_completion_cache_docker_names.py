"""The declared docker names the cache offers without a lab or a bootstrap."""

from otto.config import completion_cache as cc
from tests._fixtures.sutrepo import make_sut_repo

DOCKER = """
[[docker.images]]
name = "api"
dockerfile = "docker/Dockerfile"
context = "docker"

[[docker.images]]
name = "ghcr.io/me/db"
dockerfile = "docker/Dockerfile"
context = "docker"

[[docker.composes]]
name = "core"
path = "docker/compose.yml"
services = ["api", "db"]

[[docker.composes]]
name = "edge"
path = "docker/edge.yml"
services = ["proxy"]

[[docker.use_cases]]
name = "integration"
composes = ["core", "edge"]

[[docker.use_cases]]
name = "smoke"
composes = ["core"]
"""

SECOND_DOCKER = (
    DOCKER.replace("smoke", "nightly").replace(
        'composes = ["core", "edge"]', 'composes = ["core", "cache"]'
    )
    + """
[[docker.images]]
name = "aaa"
dockerfile = "docker/Dockerfile"
context = "docker"

[[docker.composes]]
name = "cache"
path = "docker/cache.yml"
services = ["cache"]
"""
)
"""The second repo re-declares repo one's names (dedup), adds an image that sorts FIRST
(`aaa`), and its `integration` fragment names `cache` (a service repo one lacks) but NOT
`edge` (so `proxy` comes only from repo one): neither repo alone equals the union, so a
last-write-wins or unsorted implementation fails the cross-repo tests."""

FILES = {
    "docker/Dockerfile": "FROM scratch\n",
    "docker/compose.yml": "services: {}\n",
    "docker/edge.yml": "services: {}\n",
    "docker/cache.yml": "services: {}\n",
}


def _repos(tmp_path, monkeypatch, *, second=False):
    from otto.bootstrap import discover

    sut = make_sut_repo(tmp_path / "one", name="one", extra=DOCKER, files=FILES)
    dirs = [sut]
    if second:
        other = make_sut_repo(
            tmp_path / "two",
            name="two",
            extra=SECOND_DOCKER,
            files=FILES,
        )
        dirs.append(other)
    monkeypatch.setenv("OTTO_SUT_DIRS", ":".join(str(d) for d in dirs))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OTTO_LAB", raising=False)
    return discover().repos


def test_image_names_are_sorted_and_deduped_across_repos(tmp_path, monkeypatch):
    repos = _repos(tmp_path, monkeypatch, second=True)
    assert cc.collect_docker_image_names(repos) == ["aaa", "api", "ghcr.io/me/db"]


def test_services_by_use_case_union_the_composes_each_fragment_names(tmp_path, monkeypatch):
    repos = _repos(tmp_path, monkeypatch)
    assert cc.collect_docker_services_by_use_case(repos) == {
        "integration": ["api", "db", "proxy"],
        "smoke": ["api", "db"],
    }


def test_services_by_use_case_merge_the_same_use_case_across_repos(tmp_path, monkeypatch):
    repos = _repos(tmp_path, monkeypatch, second=True)
    by_use_case = cc.collect_docker_services_by_use_case(repos)
    assert by_use_case["integration"] == ["api", "cache", "db", "proxy"]
    assert by_use_case["smoke"] == ["api", "db"]
    assert set(by_use_case) == {"integration", "smoke", "nightly"}


def test_repo_names_are_sorted(tmp_path, monkeypatch):
    repos = _repos(tmp_path, monkeypatch, second=True)
    assert cc.collect_repo_names(repos) == ["one", "two"]


def test_a_repo_without_docker_contributes_nothing(tmp_path, monkeypatch):
    from otto.bootstrap import discover

    sut = make_sut_repo(tmp_path / "plain", name="plain")
    monkeypatch.setenv("OTTO_SUT_DIRS", str(sut))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    repos = discover().repos
    assert cc.collect_docker_image_names(repos) == []
    assert cc.collect_docker_services_by_use_case(repos) == {}
    assert cc.collect_repo_names(repos) == ["plain"]


def test_the_names_section_carries_the_three_keys_and_read_cache_serves_them(tmp_path, monkeypatch):
    from otto.config.cache_sections import _collect_names

    repos = _repos(tmp_path, monkeypatch)
    payload = _collect_names(repos)
    assert payload["docker_images"] == ["api", "ghcr.io/me/db"]
    assert payload["docker_services_by_use_case"]["smoke"] == ["api", "db"]
    assert payload["repos"] == ["one"]
    cc.write_cache(
        repos,
        payload["instructions"],
        payload["hosts"],
        docker_images=payload["docker_images"],
        docker_services_by_use_case=payload["docker_services_by_use_case"],
        repos_names=payload["repos"],
    )
    view = cc.read_cache(repos)
    assert view is not None
    assert view["docker_images"] == ["api", "ghcr.io/me/db"]
    assert view["docker_services_by_use_case"] == payload["docker_services_by_use_case"]
    assert view["repos"] == ["one"]


def test_schema_is_25():
    from otto import _shim_complete as sc

    assert cc.SCHEMA_VERSION == 25 == sc.SCHEMA
