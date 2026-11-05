"""Tests driving the two container-image products on the daemon host.

Installs both through their verbs (tarball first: it loads the image the
reference form runs), exercises the product inside each container with
`docker exec`, and uninstalls in reverse in teardown.

``otto test`` never calls ``Host.uninstall()``/``get_product_logs()`` itself
— those are project-CLI-only actions (``otto uninstall``/``otto cleanup``),
so a suite that tears its own products down (as this one must, to uninstall
in the reverse of install order — the tarball's ``docker rmi`` fails while
the reference form's container still runs off that image) never gets a
docker container's logs hauled unless it asks for them itself, and it must
ask BEFORE removing the container: ``docker logs`` needs it to still exist.

Both containers are exercised once per host inside the class-scoped
``_containers`` fixture, right after install, rather than as their own
tests: ``test_counters_landed_flat_under_each_cov_dir`` needs the .gcda
files the exec calls produce, and pytest-randomly is free to run the test
methods in any order, so that dependency cannot live in a test method (the
same reasoning as project5's kmod suite's COMMON mix).
"""

import logging
import re
import shlex

import pytest
import pytest_asyncio
from project5_containers import PRODUCTS, exec_in, install_products, uninstall_products

from otto.fleet import all_hosts, do_for_all_hosts
from otto.host.unix_host import UnixHost

logger = logging.getLogger(__name__)

_HOST = re.compile(r"test3")


async def _install(host: UnixHost) -> None:
    await install_products(host, "project5")


async def _exercise(host: UnixHost) -> dict[str, str]:
    """Run each container's product once, returning every read-back output."""
    return {
        "tarball_add": await exec_in(host, "cov_container", "add", "2", "3"),
        "tarball_div": await exec_in(host, "cov_container", "div", "20", "5"),
        "ref_sub": await exec_in(host, "cov_container_ref", "sub", "10", "4"),
        "ref_clamp": await exec_in(host, "cov_container_ref", "clamp", "15", "5", "10"),
    }


async def _uninstall(host: UnixHost) -> None:
    await uninstall_products(host, PRODUCTS)


class TestCovContainer:
    @pytest_asyncio.fixture(autouse=True, scope="class")
    @classmethod
    async def _containers(cls):
        cls._host = next(iter(all_hosts(_HOST)))
        failed = {
            h: r
            for h, r in (await do_for_all_hosts(_install, pattern=_HOST)).items()
            if isinstance(r, BaseException)
        }
        if failed:
            raise RuntimeError(f"container install failed on: {failed}")
        cls._outputs = await do_for_all_hosts(_exercise, pattern=_HOST)
        failed = {h: r for h, r in cls._outputs.items() if isinstance(r, BaseException)}
        if failed:
            raise RuntimeError(f"exercising the containers failed on: {failed}")
        yield
        failed = {
            h: r
            for h, r in (await do_for_all_hosts(_uninstall, pattern=_HOST)).items()
            if isinstance(r, BaseException)
        }
        if failed:
            raise RuntimeError(f"container uninstall failed on: {failed}")

    @pytest.mark.integration
    async def test_tarball_container_ran_add_and_div(self) -> None:
        outputs = self._outputs[self._host.id]
        assert outputs["tarball_add"] == "5"
        assert outputs["tarball_div"] == "4"

    @pytest.mark.integration
    async def test_reference_container_ran_sub_and_clamp(self) -> None:
        outputs = self._outputs[self._host.id]
        assert outputs["ref_sub"] == "6"
        assert outputs["ref_clamp"] == "10"

    @pytest.mark.integration
    async def test_counters_landed_flat_under_each_cov_dir(self) -> None:
        for name in PRODUCTS:
            product = next(p for p in self._host.products if p.name == name)
            listing = await self._host.exec(f"ls {shlex.quote(product.cov_dir)}", timeout=30)
            assert "product-main.gcda" in listing.value, f"{name}: {listing.value}"
            assert "product-math_ops.gcda" in listing.value, f"{name}: {listing.value}"
