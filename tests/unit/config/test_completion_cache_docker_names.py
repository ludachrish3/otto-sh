"""The declared docker names the cache offers without a lab or a bootstrap."""

from otto.config import completion_cache as cc
from tests._fixtures.project_scaffold import make_project

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
"""The second project re-declares project one's names (dedup), adds an image that sorts FIRST
(`aaa`), and its `integration` fragment names `cache` (a service project one lacks) but NOT
`edge` (so `proxy` comes only from project one): neither project alone equals the union, so a
last-write-wins or unsorted implementation fails the cross-project tests."""

FILES = {
    "docker/Dockerfile": "FROM scratch\n",
    "docker/compose.yml": "services: {}\n",
    "docker/edge.yml": "services: {}\n",
    "docker/cache.yml": "services: {}\n",
}


def _projects(tmp_path, monkeypatch, *, second=False):
    from otto.config.project import discover

    sut = make_project(tmp_path / "one", name="one", extra=DOCKER, files=FILES)
    dirs = [sut]
    if second:
        other = make_project(
            tmp_path / "two",
            name="two",
            extra=SECOND_DOCKER,
            files=FILES,
        )
        dirs.append(other)
    monkeypatch.setenv("OTTOPATH", ":".join(str(d) for d in dirs))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OTTO_LAB", raising=False)
    return discover().projects


def test_image_names_are_sorted_and_deduped_across_projects(tmp_path, monkeypatch):
    projects = _projects(tmp_path, monkeypatch, second=True)
    assert cc.collect_docker_image_names(projects) == ["aaa", "api", "ghcr.io/me/db"]


def test_services_by_use_case_union_the_composes_each_fragment_names(tmp_path, monkeypatch):
    projects = _projects(tmp_path, monkeypatch)
    assert cc.collect_docker_services_by_use_case(projects) == {
        "integration": ["api", "db", "proxy"],
        "smoke": ["api", "db"],
    }


def test_services_by_use_case_merge_the_same_use_case_across_projects(tmp_path, monkeypatch):
    projects = _projects(tmp_path, monkeypatch, second=True)
    by_use_case = cc.collect_docker_services_by_use_case(projects)
    assert by_use_case["integration"] == ["api", "cache", "db", "proxy"]
    assert by_use_case["smoke"] == ["api", "db"]
    assert set(by_use_case) == {"integration", "smoke", "nightly"}


def test_project_names_are_sorted(tmp_path, monkeypatch):
    projects = _projects(tmp_path, monkeypatch, second=True)
    assert cc.collect_active_project_names(projects) == ["one", "two"]


def test_a_project_without_docker_contributes_nothing(tmp_path, monkeypatch):
    from otto.config.project import discover

    sut = make_project(tmp_path / "plain", name="plain")
    monkeypatch.setenv("OTTOPATH", str(sut))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    projects = discover().projects
    assert cc.collect_docker_image_names(projects) == []
    assert cc.collect_docker_services_by_use_case(projects) == {}
    assert cc.collect_active_project_names(projects) == ["plain"]


def test_the_names_section_carries_the_three_keys_and_read_cache_serves_them(tmp_path, monkeypatch):
    from otto.cli.main import _completion_collectors
    from otto.config.cache_sections import _collect_names

    projects = _projects(tmp_path, monkeypatch)
    payload = _collect_names(projects, _completion_collectors())
    assert payload["docker_images"] == ["api", "ghcr.io/me/db"]
    assert payload["docker_services_by_use_case"]["smoke"] == ["api", "db"]
    assert payload["active_projects"] == ["one"]
    cc.write_cache(
        projects,
        payload["instructions"],
        payload["hosts"],
        docker_images=payload["docker_images"],
        docker_services_by_use_case=payload["docker_services_by_use_case"],
        active_projects=payload["active_projects"],
    )
    view = cc.read_cache(projects)
    assert view is not None
    assert view["docker_images"] == ["api", "ghcr.io/me/db"]
    assert view["docker_services_by_use_case"] == payload["docker_services_by_use_case"]
    assert view["active_projects"] == ["one"]
