"""Every id otto composes from the shipped lab data is kebab-case (spec 2026-10-03 §2)."""

import json
import re
from pathlib import Path

import pytest

from otto.config.lab import load_lab
from otto.host.builtin_hosts import builtin_host_ids
from otto.host.command_frame import FRAME_CLASSES, register_command_frame
from otto.host.docker_host import DockerContainerHost
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.labs.json_repository import JsonFileLabRepository
from otto.logger.mode import LogMode
from tests._fixtures.gs_example import import_gs_example
from tests._fixtures.paths import PROJECT_ROOT, ensure_custom_hosts_on_path

ensure_custom_hosts_on_path()

_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_CONTAINER_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*\.[a-z0-9_-]+\.[a-z0-9._-]+$")
_LABS = [
    PROJECT_ROOT / "tests/_fixtures/lab_data/tech1/lab.json",
    PROJECT_ROOT / "docs/examples/getting-started/lab_data/lab.json",
]


@pytest.fixture(autouse=True)
def _zephyr_inline_frame() -> None:
    """Register the frame ``tech1``'s zephyr hosts declare (see test_pinned_identities)."""
    from custom_hosts.zephyr_inline import ZephyrInlineRetcodeFrame

    if ZephyrInlineRetcodeFrame.type_name not in FRAME_CLASSES:
        register_command_frame(ZephyrInlineRetcodeFrame.type_name, ZephyrInlineRetcodeFrame)


def _composed_ids(path: Path) -> list[str]:
    """The id of every host the REAL loader builds from *path*, across all its labs.

    Through :func:`~otto.config.lab.load_lab`, so inventory resolution,
    ``ElementSpec.to_element`` and the ``os_profile`` merge all run exactly as
    they do for a user. The example project's init module is imported first
    because its hosts name the session setups it registers.
    """
    import_gs_example()
    search = [path.parent]
    labs = JsonFileLabRepository(search_paths=search).list_labs()
    builtins = set(builtin_host_ids())
    ids: set[str] = set()
    for lab in labs:
        ids |= set(load_lab(lab, search_paths=search).hosts) - builtins
    return sorted(ids)


@pytest.mark.parametrize("path", _LABS, ids=lambda p: p.relative_to(PROJECT_ROOT).as_posix())
def test_every_shipped_lab_id_is_kebab(path):
    ids = _composed_ids(path)
    assert ids, f"{path} composes no ids — the loader built no hosts"
    bad = [i for i in ids if not _ID.fullmatch(i)]
    assert not bad, bad


@pytest.mark.parametrize("path", _LABS, ids=lambda p: p.relative_to(PROJECT_ROOT).as_posix())
def test_every_link_endpoint_names_a_composed_id(path):
    doc = json.loads(path.read_text())
    ids = set(_composed_ids(path))
    named = {ep["host"] for link in doc.get("links", []) for ep in link["endpoints"]}
    assert named, f"{path} declares no link endpoints — the walker lost the schema"
    assert named <= ids, f"link endpoints name ids no host composes: {sorted(named - ids)}"


def test_the_container_shape_is_what_docker_host_builds():
    parent = UnixHost(
        ip="10.10.200.11",
        element=Element("test1"),
        creds=[Cred(login="u", password="p")],
        board="bb",
        slot=0,
        log=LogMode.QUIET,
    )
    assert _ID.fullmatch(parent.id), "the parent is a kebab id"
    container = DockerContainerHost(
        parent=parent,
        container_id="abc123def456",
        project="bench-integration-e2e-ab12cd34",
        service="api_v2",
        compose_project="otto-bench-integration-e2e-ab12cd34-vagrant",
    )
    assert container.id.startswith(f"{parent.id}.")
    assert _CONTAINER_ID.fullmatch(container.id), container.id
