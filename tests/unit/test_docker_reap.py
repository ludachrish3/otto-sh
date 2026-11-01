"""Pins for tests/_fixtures/docker_reap.py: the pool, the command lines, the reap and its checks."""

from dataclasses import dataclass, field

import pytest

from otto.result import CommandResult
from otto.utils import Status
from scripts.bed_lease import load_labs
from tests._fixtures import docker_reap
from tests._fixtures.bed_hygiene import ProbeFailedError
from tests._fixtures.docker_reap import (
    E2E_IMAGE_LABEL,
    ORPHAN_FRAGMENTS,
    docker_parents,
    image_ids_command,
    reap_docker,
    remove_labelled_images,
    remove_labelled_images_command,
)
from tests._fixtures.labdata import lab_data_path

pytestmark = pytest.mark.interpreter_agnostic


def test_the_parents_on_tech1_are_test1_and_test3_and_so_is_the_e2e_pool() -> None:
    """One rule for the pool and the reap: test2 is docker-capable but resolves to telnet."""
    hosts = load_labs(lab_data_path("tech1"), ["unix"]).hosts
    assert docker_parents(hosts.values()) == ["test1", "test3"]
    assert hosts["test2"].docker_capable, "the rule is the term, not the flag"
    from tests.e2e.docker.conftest import _DOCKER_POOL

    assert list(_DOCKER_POOL) == ["test1", "test3"]


def test_the_image_commands_select_by_label_only() -> None:
    assert image_ids_command("label=x") == "docker image ls -aq --no-trunc --filter label=x"
    line = remove_labelled_images_command(["label=a", "label=b=t"])
    assert line == (
        "for i in $(docker image ls -aq --no-trunc --filter label=a); do docker image rm -f -- $i; "
        "done; for i in $(docker image ls -aq --no-trunc --filter label=b=t); "
        "do docker image rm -f -- $i; done; true"
    )


@dataclass
class _Host:
    id: str = "test1"
    answers: dict = field(default_factory=dict)
    failing: str = ""
    commands: list = field(default_factory=list)

    async def exec(self, cmd: str, timeout: float, log: object) -> CommandResult:
        self.commands.append(cmd)
        status = Status.Failed if self.failing and self.failing in cmd else Status.Success
        return CommandResult(status=status, value=self.answers.get(cmd, ""), command=cmd)


@pytest.mark.asyncio
async def test_reap_docker_removes_containers_before_networks_per_fragment() -> None:
    host = _Host(
        answers={
            "docker ps -aq --filter 'name=-e2e-'": "c1\nc2\n",
            "docker network ls -q --filter 'name=-e2e-'": "n1\n",
        }
    )
    assert await reap_docker(host) == ["container c1", "container c2", "network n1"]
    assert host.commands == [
        "docker ps -aq --filter 'name=-e2e-'",
        "docker rm -f c1 c2",
        "docker network ls -q --filter 'name=-e2e-'",
        "docker network rm n1",
        "docker ps -aq --filter 'name=-noexist-'",
        "docker network ls -q --filter 'name=-noexist-'",
    ]
    assert ORPHAN_FRAGMENTS == ("-e2e-", "-noexist-")


@pytest.mark.asyncio
async def test_a_removal_that_fails_is_raised_host_named() -> None:
    host = _Host(answers={"docker ps -aq --filter 'name=-e2e-'": "c1"}, failing="docker rm")
    with pytest.raises(ProbeFailedError, match="test1"):
        await reap_docker(host)


@pytest.mark.asyncio
async def test_labelled_images_are_removed_by_id_and_verified_gone() -> None:
    listing = image_ids_command(f"label={E2E_IMAGE_LABEL}")
    host = _Host()
    host.answers = {listing: "sha256:a\nsha256:b"}
    removal = remove_labelled_images_command([f"label={E2E_IMAGE_LABEL}"])

    original = host.exec

    async def exec_then_empty(cmd, timeout, log):
        result = await original(cmd, timeout, log)
        if cmd == removal:
            host.answers[listing] = ""
        return result

    host.exec = exec_then_empty  # type: ignore[method-assign]
    assert await remove_labelled_images(host, E2E_IMAGE_LABEL) == ["sha256:a", "sha256:b"]
    assert host.commands == [listing, removal, listing]


@pytest.mark.asyncio
async def test_no_labelled_image_runs_no_removal() -> None:
    host = _Host()
    assert await remove_labelled_images(host, E2E_IMAGE_LABEL) == []
    assert len(host.commands) == 1


@pytest.mark.asyncio
async def test_an_image_still_listed_after_the_removal_is_raised() -> None:
    listing = image_ids_command(f"label={E2E_IMAGE_LABEL}")
    host = _Host(answers={listing: "sha256:stuck"})
    with pytest.raises(RuntimeError, match="still carry"):
        await remove_labelled_images(host, E2E_IMAGE_LABEL)


def test_the_hygiene_oracle_probes_the_same_label_and_fragments() -> None:
    """The reap's constants are the oracle's: one spelling of what is the suite's to remove."""
    from tests._fixtures import bed_hygiene

    assert bed_hygiene.E2E_IMAGE_LABEL is docker_reap.E2E_IMAGE_LABEL
    assert bed_hygiene.ORPHAN_FRAGMENTS is docker_reap.ORPHAN_FRAGMENTS
    assert image_ids_command(f"label={E2E_IMAGE_LABEL}") in bed_hygiene._DOCKER_IMAGES_PROBE
