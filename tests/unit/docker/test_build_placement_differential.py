"""THE PARENT GUARD: for the same use-case, parent and provide, compose_build builds on
exactly the host deploy deploys to. It turns red the day either verb grows a parent
rule of its own (issue #494 was that day, once, for the placement rule this replaced).

Verified red when written: temporarily changing compose_build's
``deployment.resolve_use_case(use_case, parent=parent, provide=provide)`` call to
``deployment.resolve_use_case(use_case, parent=None, provide=provide)`` and re-running this
file failed the ``named_over_rank`` and ``named_other_lab`` rows (compose_build then built
on the ranked ``alt2`` while ``deploy`` deployed on the named host), confirming the
differential depends on compose_build sharing deploy's parent rather than merely
resembling it.
"""

from unittest.mock import AsyncMock, patch

import pytest

from otto.docker import build_verbs as verbs_mod
from otto.docker import deployment as deploy_mod
from otto.docker.build_verbs import compose_build
from otto.docker.deployment import deploy

from .test_deploy import _compose_file, _frag, _host, _install, _lab, _repo, _wire


def _sub(tmp, name):
    """A fresh subdirectory of *tmp* for a second repo's compose file."""
    path = tmp / name
    path.mkdir()
    return path


def _ranked(host_id, ip, priority):
    host = _host(host_id, ip)
    host.docker_priority = priority
    return host


def _alt3_host():
    """A host stamped from a lab OTHER than the active ``unix`` lab.

    Carries the #494 shape: a parent named from another lab of the session
    must resolve to that host as-is, its own ``source_lab`` intact.
    """
    host = _host("alt3", "10.10.200.23")
    host.source_lab = "unix_alt"
    return host


def _two_repos(tmp):
    return [
        _repo("a", _frag(), composes=[_compose_file(tmp, "core")], images=("api",)),
        _repo("b", _frag(), composes=[_compose_file(_sub(tmp, "b"), "core")], images=("db",)),
    ]


LAYOUTS = {
    "only_host": lambda tmp: (
        _two_repos(tmp),
        [_wire(_host("test3", "10.10.200.13"))],
        {},
    ),
    "ranked": lambda tmp: (
        _two_repos(tmp),
        [
            _wire(_host("test3", "10.10.200.13")),
            _wire(_ranked("alt2", "10.10.200.22", 10)),
        ],
        {},
    ),
    "provider": lambda tmp: (
        [
            _repo(
                "real",
                _frag(provides="db", priority=1),
                composes=[_compose_file(tmp, "core")],
                images=("db",),
            ),
            _repo(
                "mock",
                _frag(provides="db", priority=0),
                composes=[_compose_file(_sub(tmp, "m"), "core")],
                images=("mockdb",),
            ),
            _repo(
                "app",
                _frag(),
                composes=[_compose_file(_sub(tmp, "app"), "core")],
                images=("api",),
            ),
        ],
        [
            _wire(_host("test3", "10.10.200.13")),
            _wire(_ranked("alt2", "10.10.200.22", 10)),
        ],
        {"provide": {"db": "mock"}},
    ),
    "named_over_rank": lambda tmp: (
        _two_repos(tmp),
        [
            _wire(_host("test3", "10.10.200.13")),
            _wire(_ranked("alt2", "10.10.200.22", 10)),
        ],
        {"parent": "test3"},
    ),
    "named_other_lab": lambda tmp: (
        _two_repos(tmp),
        [
            _wire(_alt3_host()),
            _wire(_ranked("alt2", "10.10.200.22", 10)),
        ],
        {"parent": "alt3"},
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", sorted(LAYOUTS), ids=str)
async def test_compose_build_and_deploy_land_on_one_parent(layout, tmp_path):
    repos, hosts, kw = LAYOUTS[layout](tmp_path)
    lab = _lab(*hosts)
    built: list[tuple[str, str]] = []

    async def _spy(repo, parent, *, image_names=None, options=None):
        built.append((repo.name, parent.id))
        return {}

    with (
        _install(lab, repos),
        patch.object(verbs_mod, "build_images", AsyncMock(side_effect=_spy)),
    ):
        await compose_build("integration", **kw)
    deployed_on: set[tuple[str, str]] = set()

    async def _deploy_spy(repo, parent, **_kw):
        deployed_on.add((repo.name, parent.id))
        return {}

    with (
        _install(lab, repos),
        patch.object(deploy_mod, "build_images", AsyncMock(side_effect=_deploy_spy)),
    ):
        await deploy("integration", build=True, **kw)
    assert built, layout
    assert len({host for _, host in built}) == 1, f"{layout}: one use-case, one parent: {built}"
    assert set(built) == deployed_on, (
        f"{layout}: compose_build {sorted(built)} vs deploy {sorted(deployed_on)}"
    )
