"""Completion synthesizes container ids in the shape registration actually uses.

`register_declared_container_hosts` takes the USE-CASE branch for any repo
that declares `[[docker.use_cases]]` and registers
``<parent>.<usecase>.<service>`` placeholders (spec §9); only a composes-only
repo keeps the legacy ``<parent>.<repo>.<service>`` shape. Completion has to
mirror that branch or it offers ids nothing registers.

The sample repos (`tests/repo1`, `tests/repo2`) name their use-cases after
themselves, so both shapes coincide there and a divergence is invisible. Every
test below therefore uses a use-case name that DIFFERS from its repo name —
that mismatch is the point, not incidental naming.
"""

from pathlib import Path
from types import SimpleNamespace

from otto.config.completion_cache import collect_host_ids
from otto.config.repo import DockerCompose, DockerSettings, DockerUseCase
from tests._fixtures.labdata import json_lab_sources, write_lab_json


def _repo(tmp_path: Path, *, name: str, docker: DockerSettings) -> SimpleNamespace:
    """A duck-typed Repo carrying one docker-capable host and a [docker] block."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    write_lab_json(
        labs_dir / "lab.json",
        [
            {
                "ip": "10.0.0.1",
                "element": "server",
                "element_id": 47,
                "labs": ["east"],
                "docker_capable": True,
                "creds": [{"login": "u", "password": "p"}],
            }
        ],
    )
    return SimpleNamespace(
        name=name,
        lab_sources=json_lab_sources(labs_dir.parent, [labs_dir]),
        sut_dir=labs_dir.parent,
        # `build_inventory` reads it on the enumeration path; without it the
        # enumeration is contained and offers no hosts at all.
        inventory_settings={},
        docker_settings=docker,
    )


def _compose(name: str, *services: str) -> DockerCompose:
    return DockerCompose(path=Path(f"/repo/docker/{name}.yml"), services=services, name=name)


def test_use_case_repo_completes_use_case_ids_not_repo_ids(tmp_path):
    """The masked case: repo ``acme`` declaring use-case ``integration``.

    `--list-hosts` shows ``server47.integration.api`` (that is what the
    placeholder walk registers); completion must offer the same string. The
    repo-named id is not merely redundant — it names nothing, so a user who
    tab-completes it gets a host error for an id otto handed them.
    """
    docker = DockerSettings(
        composes=(_compose("core", "api"),),
        use_cases=(DockerUseCase(name="integration", composes=("core",)),),
    )
    ids = set(collect_host_ids([_repo(tmp_path, name="acme", docker=docker)]))

    assert "server47.integration.api" in ids, "the id registration mints must complete"
    assert "server47.acme.api" not in ids, (
        "the repo-named id names nothing for a use-case repo — offering it "
        "sends the user to a host that does not exist"
    )


def test_a_composes_only_repo_keeps_the_legacy_repo_named_ids(tmp_path):
    """The legacy branch is unchanged: no fragments, so the middle is the repo."""
    docker = DockerSettings(composes=(_compose("core", "api"),), use_cases=())
    ids = set(collect_host_ids([_repo(tmp_path, name="acme", docker=docker)]))

    assert "server47.acme.api" in ids


def test_every_fragment_of_a_multi_use_case_repo_contributes_its_own_ids(tmp_path):
    """Two fragments, two use-cases, one shared compose handle → both ids.

    Placement is NOT mirrored here (this function has no lab), so the walk
    stays pessimistic: every declared use-case pairs with every docker-capable
    host, exactly as the legacy walk pairs every compose with every host.
    """
    docker = DockerSettings(
        composes=(_compose("core", "api", "db"),),
        use_cases=(
            DockerUseCase(name="integration", composes=("core",)),
            DockerUseCase(name="soak", composes=("core",)),
        ),
    )
    ids = set(collect_host_ids([_repo(tmp_path, name="acme", docker=docker)]))

    assert {
        "server47.integration.api",
        "server47.integration.db",
        "server47.soak.api",
        "server47.soak.db",
    } <= ids


def test_a_fragment_naming_an_unknown_handle_is_skipped_not_raised(tmp_path):
    """Completion never crashes on bad user data — it just offers less.

    The schema rejects an unresolvable handle, so this shape only reaches here
    from settings that were never validated; the fast path's contract is to
    stay silent about it rather than take the whole completion down.
    """
    docker = DockerSettings(
        composes=(_compose("core", "api"),),
        use_cases=(
            DockerUseCase(name="integration", composes=("nosuch",)),
            DockerUseCase(name="soak", composes=("core",)),
        ),
    )
    ids = set(collect_host_ids([_repo(tmp_path, name="acme", docker=docker)]))

    assert "server47.soak.api" in ids, "the resolvable fragment still completes"
    assert not any(".integration." in i for i in ids)
