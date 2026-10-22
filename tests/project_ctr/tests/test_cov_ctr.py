"""Tests driving project_ctr's shell product inside its compose container.

The stack (``otto docker compose up covctr``) must already be up: its one
container is ``test3.covctr.app``. Product install walks never cover
container hosts, so the class fixture calls the product's own verbs on the
container host itself — after creating the absolute stage directory and a
writable ``cov_dir`` there — exercises the binary once with ``GCOV_PREFIX``
set and no ``GCOV_PREFIX_STRIP`` (the counters keep their ``/build/...``
object paths under ``cov_dir``), and uninstalls in teardown. Uninstall
removes the binary only: the counters stay in ``cov_dir`` for ``otto test
--cov``'s post-run collection and any later ``otto cov get``.
"""

import re
import shlex

import pytest
import pytest_asyncio

from otto.host import DockerContainerHost
from otto.lab import all_hosts

_CONTAINER = re.compile(r"test3\.covctr\.app")
_PRODUCT = "counter"
_BINARY = "/opt/cov-ctr/bin/counter"


async def _sh(host: DockerContainerHost, cmd: str) -> str:
    result = await host.exec(cmd, timeout=30)
    if not result.status.is_ok:
        raise RuntimeError(f"{host.id}: {cmd} failed: {result.value}")
    return result.value.strip()


async def _install(host: DockerContainerHost) -> None:
    product = next(p for p in host.products if p.name == _PRODUCT)
    cov_dir = shlex.quote(str(product.cov_dir))
    stage_dir = shlex.quote(str(product.stage_dir))
    await _sh(host, f"mkdir -p {stage_dir} {cov_dir} && chmod 777 {cov_dir}")
    result = await product.stage(host)
    if result.is_ok:
        result = await product.install(host)
    if not result.is_ok:
        raise RuntimeError(f"{host.id}: installing {_PRODUCT} failed: {result.msg}")


async def _uninstall(host: DockerContainerHost) -> None:
    product = next(p for p in host.products if p.name == _PRODUCT)
    result = await product.uninstall(host)
    if not result.is_ok:
        raise RuntimeError(f"{host.id}: uninstalling {_PRODUCT} failed: {result.msg}")


class TestCovCtr:
    @pytest_asyncio.fixture(autouse=True, scope="class")
    @classmethod
    async def _container(cls):
        (cls._host,) = all_hosts(_CONTAINER, include_containers=True)
        product = next(p for p in cls._host.products if p.name == _PRODUCT)
        cls._cov_dir = str(product.cov_dir)
        await _install(cls._host)
        prefix = f"GCOV_PREFIX={shlex.quote(cls._cov_dir)}"
        cls._added = await _sh(cls._host, f"{prefix} {_BINARY} add 2 3")
        yield
        await _uninstall(cls._host)

    @pytest.mark.integration
    async def test_the_product_ran_in_the_container(self) -> None:
        assert self._added == "5"

    @pytest.mark.integration
    async def test_counters_keep_their_build_paths_under_cov_dir(self) -> None:
        listing = await _sh(self._host, f"find {shlex.quote(self._cov_dir)} -name '*.gcda'")
        assert sorted(listing.split()) == [
            f"{self._cov_dir}/build/build/main.gcda",
            f"{self._cov_dir}/build/build/ops.gcda",
        ]
