"""THE PLACEMENT GUARD: for the same use-case, on and provide, compose_build builds on
exactly the hosts deploy deploys to. It turns red the day either verb grows a placement
rule of its own (issue #494 was that day, once).

Verified red when written: temporarily changing compose_build's
``deployment._resolve(use_case, on=on, provide=provide)`` call to
``deployment._resolve(use_case, on=None, provide=provide)`` and re-running this file failed
the ``collapse`` row (compose_build then built ``b`` on ``alt2`` instead of collapsing it onto
``test3`` the way ``deploy`` did), confirming the differential actually depends on
compose_build sharing deploy's placement rather than merely resembling it.
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


def _alt3_host():
    """A host stamped from a lab OTHER than the active ``unix`` lab.

    Carries the #494 shape: a placement pin lab-qualified as
    ``unix_alt:alt3`` must resolve against this host's own
    ``source_lab``, not the active lab's name — the old CLI stripped the
    lab qualifier down to the bare host id before it ever reached
    resolution.
    """
    host = _host("alt3", "10.10.200.23", roles=("edge",))
    host.source_lab = "unix_alt"
    return host


LAYOUTS = {
    "pinned": lambda tmp: (
        [
            _repo(
                "a",
                _frag(placement={"edge": "test3"}, role="edge"),
                composes=[_compose_file(tmp, "core")],
                images=("api",),
            )
        ],
        [
            _wire(_host("test3", "10.10.200.13", roles=("edge",))),
            _wire(_host("alt2", "10.10.200.22", roles=("data",))),
        ],
        {},
    ),
    "roles": lambda tmp: (
        [
            _repo("a", _frag(role="edge"), composes=[_compose_file(tmp, "core")], images=("api",)),
            _repo(
                "b",
                _frag(role="data"),
                composes=[_compose_file(_sub(tmp, "b"), "core")],
                images=("db",),
            ),
        ],
        [
            _wire(_host("test3", "10.10.200.13", roles=("edge",))),
            _wire(_host("alt2", "10.10.200.22", roles=("data",))),
        ],
        {},
    ),
    "provider": lambda tmp: (
        [
            _repo(
                "real",
                _frag(role="data", provides="db", priority=1),
                composes=[_compose_file(tmp, "core")],
                images=("db",),
            ),
            _repo(
                "mock",
                _frag(role="data", provides="db", priority=0),
                composes=[_compose_file(_sub(tmp, "m"), "core")],
                images=("mockdb",),
            ),
            _repo(
                "app",
                _frag(role="edge"),
                composes=[_compose_file(_sub(tmp, "app"), "core")],
                images=("api",),
            ),
        ],
        [
            _wire(_host("test3", "10.10.200.13", roles=("edge",))),
            _wire(_host("alt2", "10.10.200.22", roles=("data",))),
        ],
        {"provide": {"db": "mock"}},
    ),
    "lab_qualified_pin": lambda tmp: (
        [
            _repo(
                "a",
                _frag(placement={"edge": "unix_alt:alt3"}, role="edge"),
                composes=[_compose_file(tmp, "core")],
                images=("api",),
            )
        ],
        [
            _wire(_alt3_host()),
            _wire(_host("alt2", "10.10.200.22", roles=("data",))),
        ],
        {},
    ),
    "collapse": lambda tmp: (
        [
            _repo("a", _frag(role="edge"), composes=[_compose_file(tmp, "core")], images=("api",)),
            _repo(
                "b",
                _frag(role="data"),
                composes=[_compose_file(_sub(tmp, "b"), "core")],
                images=("db",),
            ),
        ],
        [
            _wire(_host("test3", "10.10.200.13", roles=("edge",))),
            _wire(_host("alt2", "10.10.200.22", roles=("data",))),
        ],
        {"on": "test3"},
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", sorted(LAYOUTS), ids=str)
async def test_compose_build_and_deploy_place_identically(layout, tmp_path):
    repos, hosts, kw = LAYOUTS[layout](tmp_path)
    lab = _lab(*hosts)
    built: list[tuple[str, str]] = []

    async def _spy(repo, parent, *, image_names=None, rebuild=False):
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
        await deploy("integration", **kw)
    assert built, layout
    assert set(built) == deployed_on, (
        f"{layout}: compose_build {sorted(built)} vs deploy {sorted(deployed_on)}"
    )
