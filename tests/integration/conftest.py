"""Fixtures for coverage integration tests.

These tests require:
- Vagrant test VMs (test1/test2) to be running
- gcc and lcov installed on the dev VM
"""

import os
from pathlib import Path

# Must be set before any otto imports -- configmodule reads OTTO_SUT_DIRS at
# import time to compute the module-level _repos singleton.
os.environ.setdefault('OTTO_SUT_DIRS', str(Path(__file__).parent.parent / 'repo1'))

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio

from otto.host.remoteHost import RemoteHost

_LAB_DATA = Path(__file__).parent.parent / "lab_data" / "tech1" / "hosts.json"

# Docker host the e2e/compose tests target (test VM "pepper" / test3).
_DOCKER_HOST_IP = "10.10.200.13"

# Compose-project name fragments that only ever appear in *disposable* test
# stacks: `fresh_suffix` yields ``e2e-<hex>`` and the unstarted-container test
# uses ``noexist-<hex>``. Each test run mints a fresh suffix, so an interrupted
# or crashed run leaves an orphan stack behind. Roughly 30 orphans exhaust
# docker's default address-pool ("all predefined address pools have been fully
# subnetted") and wedge every subsequent ``compose up``. Stacks matching these
# fragments are always safe to reap — they belong to no live developer session.
_ORPHAN_PROJECT_FRAGMENTS = ("-e2e-", "-noexist-")


async def _reap_orphan_docker_stacks() -> None:
    """Remove leaked ``otto-*-{e2e,noexist}-*`` containers and networks on the
    docker host so address-pool exhaustion can't accumulate across runs."""
    host = RemoteHost(
        ip=_DOCKER_HOST_IP,
        ne="pepper",
        creds={"vagrant": "vagrant"},
        board="seed",
        is_virtual=True,
        term="ssh",
        transfer="scp",
        docker_capable=True,
    )
    try:
        for frag in _ORPHAN_PROJECT_FRAGMENTS:
            containers = (
                await host.oneshot(f"docker ps -aq --filter 'name={frag}'", timeout=30)
            ).output.split()
            if containers:
                await host.oneshot(f"docker rm -f {' '.join(containers)}", timeout=60)
            networks = (
                await host.oneshot(
                    f"docker network ls -q --filter 'name={frag}'", timeout=30
                )
            ).output.split()
            if networks:
                await host.oneshot(
                    f"docker network rm {' '.join(networks)}", timeout=60
                )
    finally:
        await host.close()


@pytest.fixture(scope="session", autouse=True)
def reap_orphan_docker_stacks() -> None:
    """Sweep orphaned docker test stacks before the integration session.

    All tests in this directory drive docker on a shared host; a stack leaked
    by an earlier interrupted run would otherwise compound until the daemon
    runs out of network subnets. Best-effort — if the host is unreachable we
    let the individual tests report that themselves.
    """
    try:
        asyncio.run(_reap_orphan_docker_stacks())
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        pass


def _host_data(ne: str) -> dict[str, Any]:
    hosts = json.loads(_LAB_DATA.read_text())
    for host in hosts:
        if host["ne"] == ne:
            return host
    raise KeyError(f"NE {ne!r} not found in {_LAB_DATA}")


@pytest_asyncio.fixture
async def carrot():
    """RemoteHost for test1 (carrot) via SSH."""
    data = _host_data("carrot")
    h = RemoteHost(
        ip=data["ip"],
        ne=data["ne"],
        creds=data["creds"],
        board=data.get("board"),
        is_virtual=True,
        term="ssh",
        transfer="scp",
    )
    yield h
    await h.close()


@pytest_asyncio.fixture
async def tomato():
    """RemoteHost for test2 (tomato) via SSH."""
    data = _host_data("tomato")
    h = RemoteHost(
        ip=data["ip"],
        ne=data["ne"],
        creds=data["creds"],
        board=data.get("board"),
        is_virtual=True,
        term="ssh",
        transfer="scp",
    )
    yield h
    await h.close()
