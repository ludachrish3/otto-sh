"""Fixtures for coverage integration tests.

These tests require:
- Vagrant test VMs (test1/test2) to be running
- gcc and lcov installed on the dev VM
"""

import asyncio
import contextlib
import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from otto.config.env import SUT_DIRS_ENV_VAR
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from tests._fixtures.labdata import lab_data_path
from tests._fixtures.paths import default_sut_dir

_INTEGRATION_ROOT = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``integration`` marker to every test under this tree.

    The ``tests/integration/`` directory is the single source of truth for the
    integration tier (Spec §5.1): tests here drive the real Vagrant/QEMU bed via
    otto's Python API. Stamping the marker from the path lets the marker-based
    gates (``coverage-unix`` = ``-m "integration and not embedded"``, etc.)
    select this tree without each test repeating ``@pytest.mark.integration``.
    Idempotent and additive — explicit ``embedded``/``hops``/``stability`` stay.

    NOTE (lane consequence, recorded at Wave 16): because only explicit marks
    survive on top of the path stamp, a subtree with NO ``embedded`` marks
    rides the unix lane wholesale — ``tests/integration/cov/`` is the live
    case: every module there lands in ``M_UNIX`` (``integration and not
    embedded ...``) and is invisible to ``make coverage-embedded``, including
    the pure-git/tmp_path modules that need no bed at all. That is currently
    intended (the cov integration tier exercises host tooling, not Zephyr),
    but it is a *default*, not a decision each file re-makes — a future
    embedded-coverage integration test must OPT IN with an explicit
    ``@pytest.mark.embedded`` or it will silently run (and fail) in the unix
    lane. Only ``tests/integration/host/`` distinguishes the lanes today,
    via per-param marks.
    """
    for item in items:
        if _INTEGRATION_ROOT in item.path.parents:
            item.add_marker("integration")


def _default_sut_dirs_env_impl():
    """``OTTO_SUT_DIRS`` -> the ``repo1`` fixture SUT for this tree, at RUNTIME.

    History: this was a module-scope ``ensure_sut_dirs()`` call justified by
    a "config reads OTTO_SUT_DIRS at import time" comment that stopped being
    true — every reader (``bootstrap()``/``OttoEnvSettings``, spawned otto
    subprocesses) reads the env lazily at call time, which a session-start
    write fully precedes. Import-time env writes are banned (G11): they run
    behind the root conftest's hermeticity strip's back, invisible to
    monkeypatch and to any pin that never imports this tree's conftest —
    which is how this one went uncertified for a year. ``setdefault`` +
    restore keeps a pre-set value (another harness layer) authoritative.

    Plain generator (the ``_hygiene_bracket_impl`` pattern) so the unit-lane
    pin can drive set-and-restore directly.
    """
    prior = os.environ.get(SUT_DIRS_ENV_VAR)
    os.environ.setdefault(SUT_DIRS_ENV_VAR, default_sut_dir())
    yield
    if prior is None:
        os.environ.pop(SUT_DIRS_ENV_VAR, None)
    else:
        os.environ[SUT_DIRS_ENV_VAR] = prior


@pytest.fixture(scope="session", autouse=True)
def _default_sut_dirs_env():
    yield from _default_sut_dirs_env_impl()


_LAB_DATA = lab_data_path()

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
    host = UnixHost(
        ip=_DOCKER_HOST_IP,
        element="pepper",
        creds=[Cred(login="vagrant", password="vagrant")],
        board="seed",
        is_virtual=True,
        term="ssh",
        transfer="scp",
        docker_capable=True,
    )
    try:
        for frag in _ORPHAN_PROJECT_FRAGMENTS:
            containers = (
                await host.exec(f"docker ps -aq --filter 'name={frag}'", timeout=30)
            ).value.split()
            if containers:
                await host.exec(f"docker rm -f {' '.join(containers)}", timeout=60)
            networks = (
                await host.exec(f"docker network ls -q --filter 'name={frag}'", timeout=30)
            ).value.split()
            if networks:
                await host.exec(f"docker network rm {' '.join(networks)}", timeout=60)
    finally:
        await host.close()


@pytest.fixture(scope="session", autouse=True)
def reap_orphan_docker_stacks() -> None:
    """Sweep orphaned docker test stacks before the integration session.

    All tests in this directory drive docker on a shared host; a stack leaked
    by an earlier interrupted run would otherwise compound until the daemon
    runs out of network subnets. Best-effort — if the host is unreachable we
    let the individual tests report that themselves.

    Hermetic venues (e.g. the GitHub ``chaos-tier2`` nightly job) have no
    route to the bed at all — unlike a reachable-but-docker-broken host,
    connecting to an unroutable address can hang on SYN retries well past
    the point where the `contextlib.suppress` below would ever catch
    anything, stalling the whole session before a single test runs. A short
    TCP preflight distinguishes "no route" (skip the reap silently — it's a
    lab-only courtesy, not a test, so nothing here fails or skips a test)
    from "host up, something else went wrong" (still attempt the reap and
    suppress as before).
    """
    try:
        with socket.create_connection((_DOCKER_HOST_IP, 22), timeout=2):
            pass
    except OSError:
        return
    with contextlib.suppress(Exception):
        asyncio.run(_reap_orphan_docker_stacks())


def _host_data(ne: str) -> dict[str, Any]:
    hosts = json.loads(_LAB_DATA.read_text())["hosts"]
    for host in hosts:
        if host["element"] == ne:
            return host
    raise KeyError(f"NE {ne!r} not found in {_LAB_DATA}")


@pytest_asyncio.fixture
async def carrot():
    """UnixHost for test1 (carrot) via SSH."""
    data = _host_data("carrot")
    h = UnixHost(
        ip=data["ip"],
        element=data["element"],
        creds=[Cred(**c) for c in data["creds"]],
        board=data.get("board"),
        is_virtual=True,
        term="ssh",
        transfer="scp",
    )
    yield h
    await h.close()


@pytest_asyncio.fixture
async def tomato():
    """UnixHost for test2 (tomato) via SSH."""
    data = _host_data("tomato")
    h = UnixHost(
        ip=data["ip"],
        element=data["element"],
        creds=[Cred(**c) for c in data["creds"]],
        board=data.get("board"),
        is_virtual=True,
        term="ssh",
        transfer="scp",
    )
    yield h
    await h.close()
