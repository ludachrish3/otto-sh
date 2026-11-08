"""Suites driving the container products through a stop, on the daemon host (test3).

``tests/e2e/cov/test_docker_image_coverage_e2e.py`` runs each class by name, in
its own ``otto test`` run, around its own CLI steps; none of them is part of
``TestCovContainer``'s run. Every daemon check reads a raw ``host.exec`` result,
never ``otto host … exec`` stdout, which echoes the command it ran.
"""

import json
import os
import re
import shlex
from pathlib import Path

import pytest
import pytest_asyncio
from project5_containers import (
    BIND_SRC,
    PRODUCTS,
    STOP_PRODUCTS,
    exec_in,
    install_products,
    project5_root,
    uninstall_products,
)

from otto.coverage.objects import ObjectList, ObjectListCache
from otto.fleet import all_hosts
from otto.host.docker_cli import SCAN_LABEL, inspect_container
from otto.host.unix_host import UnixHost

_HOST = re.compile(r"test3")

_CONTAINERD_STORE = ["driver-type", "io.containerd.snapshotter.v1"]
"""The ``DriverStatus`` pair docker reports when its image store is containerd's snapshotter."""


def _test3() -> UnixHost:
    host = next(iter(all_hosts(pattern=_HOST)))
    assert isinstance(host, UnixHost), host
    return host


async def _ok(host: UnixHost, cmd: str, timeout: float = 120) -> str:
    """Run *cmd* on *host* as one raw ``host.exec``; assert it succeeded and return its value."""
    result = await host.exec(cmd, timeout=timeout)
    assert result.status.is_ok, (cmd, result.value)
    return result.value


class TestContainerReinstall:
    """Each product's own install hook replaces its stopped container (#783)."""

    @pytest.mark.integration
    async def test_install_over_stopped_containers_succeeds(self) -> None:
        host = _test3()
        await install_products(host, "project5")
        stopped = await host.exec(f"docker stop {' '.join(PRODUCTS)}", timeout=120)
        assert stopped.status.is_ok, stopped.value
        for name in PRODUCTS:
            inspected = await inspect_container(host, name)
            assert inspected.state is not None, (name, inspected.result)
            assert inspected.state.status == "exited", (name, inspected.state)
        await install_products(host, "project5")
        for name in PRODUCTS:
            product = next(p for p in host.products if p.name == name)
            assert await product.is_installed(host), name


class TestContainerStopFlow:
    """Install the three stop products, exercise one, then stop them all: the flush.

    The bind-mounted build goes to test3 first. The class teardown is the
    stop that flushes each instrumented product's ``serve`` counters; the
    report that follows (its own CLI step) reads the stopped containers.
    """

    @pytest_asyncio.fixture(autouse=True, scope="class")
    @classmethod
    async def _installed_then_stopped(cls):
        host = _test3()
        bind = shlex.quote(BIND_SRC)
        await _ok(host, f"sudo mkdir -p {bind} && sudo chmod 777 {bind}")
        build = project5_root() / "docker" / "build-bind" / "product"
        put = await host.put(build, Path(BIND_SRC), mode=0o755, show_progress=False)
        assert put.is_ok, put.msg
        await install_products(host, None)  # STOP_PRODUCTS: project5 and the bind overlay
        await exec_in(host, "cov_container_ref", "add", "2", "3")
        yield
        for name in STOP_PRODUCTS:
            await _ok(host, f"docker stop {shlex.quote(name)}")

    @pytest.mark.integration
    async def test_a_stopped_bind_mounted_build_is_listed(self) -> None:
        host = _test3()
        await _ok(host, "docker stop cov_container_bind")
        product = next(p for p in host.products if p.name == "cov_container_bind")
        root = project5_root()
        listed = await ObjectListCache(root).read_container(product, host)
        assert isinstance(listed, ObjectList), listed
        bind_build = (root / "docker" / "build-bind").as_posix() + "/"
        image_build = (root / "docker" / "build").as_posix() + "/"
        assert listed.paths, listed
        assert all(str(p).startswith(bind_build) for p in listed.paths), listed.paths
        assert not any(str(p).startswith(image_build) for p in listed.paths), listed.paths


class TestScanLeftovers:
    """The stopped-container report left nothing behind on test3's daemon.

    Each check is its own raw ``host.exec``, and each resource list is
    asserted empty on its own.
    """

    @pytest.mark.integration
    async def test_no_labelled_scan_container_remains(self) -> None:
        assert await _ok(_test3(), f"docker ps -aq --filter label={SCAN_LABEL}=1") == ""

    @pytest.mark.integration
    async def test_no_labelled_scan_image_remains(self) -> None:
        assert await _ok(_test3(), f"docker image ls -aq --filter label={SCAN_LABEL}=1") == ""

    @pytest.mark.integration
    async def test_no_otto_scan_tag_remains(self) -> None:
        assert await _ok(_test3(), "docker image ls -q otto-scan") == ""

    @pytest.mark.integration
    @pytest.mark.parametrize("name", PRODUCTS)
    async def test_the_products_stopped_through_their_handler(self, name: str) -> None:
        # 0 is serve's own return after its SIGTERM handler; a SIGKILL after
        # docker stop's timeout would read 137.
        code = await _ok(
            _test3(), f"docker container inspect --format '{{{{.State.ExitCode}}}}' {name}"
        )
        assert code == "0", (name, code)

    @pytest.mark.integration
    async def test_the_image_store_is_containerd(self) -> None:
        record = os.environ.get("OTTO_E2E_IMAGE_STORE_RECORD")
        assert record, "OTTO_E2E_IMAGE_STORE_RECORD is unset: the e2e passes the record path"
        host = _test3()
        driver = await _ok(host, "docker info --format '{{.Driver}}'")
        status = json.loads(await _ok(host, "docker info --format '{{json .DriverStatus}}'"))
        Path(record).write_text(json.dumps({"driver": driver, "driver_status": status}))
        assert driver, "docker info reported no storage driver"
        assert _CONTAINERD_STORE in status, (driver, status)


class TestContainerStopCleanup:
    """Removes what the stop suites installed; run last, in its own ``otto test``."""

    @pytest_asyncio.fixture(autouse=True, scope="class")
    @classmethod
    async def _uninstalled(cls):
        host = _test3()
        await uninstall_products(host, STOP_PRODUCTS)
        await _ok(host, f"sudo rm -rf {shlex.quote(BIND_SRC)}")
        yield

    @pytest.mark.integration
    async def test_no_cov_container_remains(self) -> None:
        listed = await _test3().exec(
            "docker ps -a --filter name=cov_container --format '{{.Names}}'", timeout=60
        )
        assert listed.status.is_ok, listed.value
        assert listed.value == ""
