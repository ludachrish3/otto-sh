"""Lab.add_host refuses two declarations that compose one id, naming both."""

import pytest

from otto.config.lab import Lab
from otto.host.builtin_hosts import make_builtin_local_host
from otto.host.docker_host import DockerContainerHost
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.labs.errors import LabRepositoryError
from otto.logger.mode import LogMode


def make_lab(name: str) -> Lab:
    return Lab(name=name)


def make_remote_host(*, element: str, board: str | None, slot: int | None, ip: str) -> UnixHost:
    return UnixHost(
        ip=ip,
        element=Element(element),
        creds=[Cred(login="u", password="p")],
        board=board,
        slot=slot,
        log=LogMode.QUIET,
    )


def make_container_host(*, parent: UnixHost, project: str, service: str) -> DockerContainerHost:
    return DockerContainerHost(
        parent=parent,
        container_id="abc123def456",
        project=project,
        service=service,
        compose_project=f"otto-{project}-vagrant",
    )


def test_two_declarations_one_id_is_refused_naming_both():
    lab = make_lab("bench")
    lab.add_host(make_remote_host(element="edge-node", board="line", slot=None, ip="10.10.200.11"))
    with pytest.raises(LabRepositoryError) as exc:
        lab.add_host(
            make_remote_host(element="edge", board="node-line", slot=None, ip="10.10.200.12")
        )
    text = str(exc.value)
    assert "host id 'edge-node-line' in lab 'bench'" in text
    assert "element 'edge-node' board 'line'" in text
    assert "10.10.200.11" in text
    assert "element 'edge' board 'node-line'" in text
    assert "10.10.200.12" in text
    assert "Give the elements distinct names, or set board/slot" in text


def test_board_with_trailing_number_vs_slot_is_refused():
    lab = make_lab("bench")
    lab.add_host(make_remote_host(element="a", board="b-0", slot=None, ip="10.0.0.1"))
    with pytest.raises(LabRepositoryError) as exc:
        lab.add_host(make_remote_host(element="a", board="b", slot=0, ip="10.0.0.2"))
    text = str(exc.value)
    assert "host id 'a-b-0'" in text
    assert "element 'a' board 'b-0' (10.0.0.1)" in text
    assert "slot 0" in text


def test_a_container_registered_twice_is_refused_naming_it():
    lab = make_lab("bench")
    parent = make_remote_host(element="test3", board=None, slot=None, ip="10.10.200.13")
    lab.add_host(parent)
    # A dotted ELEMENT name slugs to hyphens: a different id from the container's
    # dotted id, so it is not a collision (pins the seam argument).
    lab.add_host(make_remote_host(element="test3.repo1.api", board=None, slot=None, ip="10.0.0.9"))
    lab.add_host(make_container_host(parent=parent, project="repo1", service="api"))
    with pytest.raises(LabRepositoryError) as exc:
        lab.add_host(make_container_host(parent=parent, project="repo1", service="api"))
    text = str(exc.value)
    assert "container repo1/api on test3 collides with container repo1/api on test3" in text
    assert "Give the elements" not in text


def test_a_host_that_is_neither_remote_nor_container_is_named_by_type():
    lab = make_lab("bench")
    lab.add_host(make_builtin_local_host())
    with pytest.raises(LabRepositoryError) as exc:
        lab.add_host(make_builtin_local_host())
    text = str(exc.value)
    assert "host 'local' (LocalHost) collides with host 'local' (LocalHost)." in text
    assert "Give the elements" not in text
