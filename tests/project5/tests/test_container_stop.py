"""Suites driving the container products through a stop, on the daemon host (test3).

``tests/e2e/cov/test_docker_image_coverage_e2e.py`` runs each class by name, in
its own ``otto test`` run, around its own CLI steps; none of them is part of
``TestCovContainer``'s run. Every daemon check reads a raw ``host.exec`` result,
never ``otto host … exec`` stdout, which echoes the command it ran.
"""

import re
import shlex

import pytest
import pytest_asyncio
from project5_containers import PRODUCTS, install_products, uninstall_products

from otto.fleet import all_hosts
from otto.host.unix_host import UnixHost

_HOST = re.compile(r"test3")


def _test3() -> UnixHost:
    host = next(iter(all_hosts(_HOST)))
    assert isinstance(host, UnixHost), host
    return host


class TestContainerReinstall:
    """Each product's own install hook replaces its stopped container (#783)."""

    @pytest.mark.integration
    async def test_install_over_stopped_containers_succeeds(self) -> None:
        host = _test3()
        await install_products(host, PRODUCTS)
        stopped = await host.exec(f"docker stop {' '.join(PRODUCTS)}", timeout=120)
        assert stopped.status.is_ok, stopped.value
        for name in PRODUCTS:
            state = await host.exec(
                f"docker container inspect --format '{{{{.State.Status}}}}' {shlex.quote(name)}",
                timeout=60,
            )
            assert state.status.is_ok, state.value
            assert state.value == "exited", (name, state.value)
        await install_products(host, PRODUCTS)
        for name in PRODUCTS:
            product = next(p for p in host.products if p.name == name)
            assert await product.is_installed(host), name


class TestContainerStopCleanup:
    """Removes what the stop suites installed; run last, in its own ``otto test``."""

    @pytest_asyncio.fixture(autouse=True, scope="class")
    @classmethod
    async def _uninstalled(cls):
        await uninstall_products(_test3(), PRODUCTS)
        yield

    @pytest.mark.integration
    async def test_no_cov_container_remains(self) -> None:
        listed = await _test3().exec(
            "docker ps -a --filter name=cov_container --format '{{.Names}}'", timeout=60
        )
        assert listed.status.is_ok, listed.value
        assert listed.value == ""
