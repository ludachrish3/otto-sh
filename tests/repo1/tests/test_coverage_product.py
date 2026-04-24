"""OttoSuite for the sample C coverage product.

Compiles the product with ``--coverage``, deploys to remote hosts,
runs operations that exercise different code paths, and cleans up
on teardown.  Designed to be run with ``otto test --cov`` to collect
``.gcda`` files for coverage reporting.

Usage::

    otto test --cov TestCoverageProduct
    otto cov report <output_dir> --report ./report

The product exercises different code paths on each host so that
merged coverage across hosts is greater than any single host's
coverage alone.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import pytest
import pytest_asyncio
import typer

from otto.configmodule.configmodule import (
    all_hosts,
    do_for_all_hosts,
)
from otto.host import LocalHost
from otto.host.remoteHost import RemoteHost
from otto.logger import getOttoLogger
from otto.suite import OttoSuite, register_suite
from otto.suite.plugin import otto_cov_key
from otto.utils import Status

logger = getOttoLogger()

PRODUCT_DIR = Path(__file__).resolve().parent.parent / "product"
REMOTE_INSTALL_DIR = "/opt/coverage_product"
GCDA_REMOTE_DIR = "/var/coverage/product"


@dataclass
class _Options:
    pass


async def _compile_product() -> None:
    """Compile the C product with --coverage on the local host."""
    localhost = LocalHost()
    try:
        result = await localhost.oneshot(f"make -C {PRODUCT_DIR} clean all", timeout=30)
        if result.status != Status.Success:
            raise RuntimeError(f"Product compilation failed:\n{result.output}")
        logger.info("Product compiled successfully")
    finally:
        await localhost.close()


async def _install_on_host(host: RemoteHost) -> None:
    """Deploy the compiled product binary to a remote host."""
    # Create directories on remote
    await host.oneshot(f"sudo mkdir -p {REMOTE_INSTALL_DIR} {GCDA_REMOTE_DIR}", timeout=10)
    await host.oneshot(f"sudo chmod 777 {REMOTE_INSTALL_DIR} {GCDA_REMOTE_DIR}", timeout=10)

    # Upload the binary
    binary = PRODUCT_DIR / "product"
    status, msg = await host.put(
        src_files=[binary],
        dest_dir=Path(REMOTE_INSTALL_DIR),
    )
    if not status.is_ok:
        raise RuntimeError(f"Failed to deploy to {host.id}: {msg}")

    await host.oneshot(f"chmod +x {REMOTE_INSTALL_DIR}/product", timeout=10)
    logger.info("Installed product on %s", host.id)


async def _uninstall_from_host(host: RemoteHost) -> None:
    """Remove the product and coverage data from a remote host."""
    await host.oneshot(f"sudo rm -rf {REMOTE_INSTALL_DIR} {GCDA_REMOTE_DIR}", timeout=10)
    logger.info("Uninstalled product from %s", host.id)


async def _run_product(host: RemoteHost, op: str, *args: int) -> str:
    """Run the product on a remote host with GCOV_PREFIX set.

    Returns the stdout output from the product.
    """
    # Compute GCOV_PREFIX_STRIP: strip all path components from the
    # build directory so .gcda files land flat in GCDA_REMOTE_DIR.
    strip = len(PRODUCT_DIR.parts) - 1  # -1 for root '/'

    str_args = " ".join(str(a) for a in args)
    cmd = (
        f"GCOV_PREFIX={GCDA_REMOTE_DIR} "
        f"GCOV_PREFIX_STRIP={strip} "
        f"{REMOTE_INSTALL_DIR}/product {op} {str_args}"
    )
    result = await host.oneshot(cmd, timeout=10)
    if result.status != Status.Success:
        raise RuntimeError(
            f"Product run failed on {host.id}: {result.output}"
        )
    return result.output.strip()


@register_suite()
@pytest.mark.asyncio(loop_scope="class")
class TestCoverageProduct(OttoSuite[_Options]):
    """Exercise the sample C product across multiple hosts for coverage testing.

    Different hosts exercise different code paths so that merged
    coverage is greater than any individual host's coverage.
    """

    Options = _Options

    @pytest_asyncio.fixture(autouse=True, scope="class", loop_scope="class")
    async def _deploy_product(self, request):
        """Compile and deploy the product to all remote hosts; uninstall on teardown."""
        await _compile_product()

        request.cls._hosts = list(all_hosts())

        await do_for_all_hosts(_install_on_host)

        yield

        cov_active = request.config.stash.get(otto_cov_key, False)
        if cov_active:
            # Only remove the binary; leave .gcda files for post-test fetch.
            await do_for_all_hosts(
                RemoteHost.oneshot,
                f"sudo rm -rf {REMOTE_INSTALL_DIR}",
                timeout=10,
            )
        else:
            await do_for_all_hosts(_uninstall_from_host)

    @staticmethod
    def _assert_all(
        results: dict[str, str | BaseException],
        expected: str,
    ) -> None:
        """Fail the test if any host didn't produce the expected output."""
        for host_id, output in results.items():
            assert not isinstance(output, BaseException), (
                f"Error running product on {host_id}: {output}"
            )
            assert output == expected, (
                f"Expected {expected}, got {output!r} on {host_id}"
            )

    @pytest.mark.integration
    async def test_add(self) -> None:
        """Run 'add' on all hosts — exercises add() and the 'add' branch in main."""
        results = await do_for_all_hosts(_run_product, "add", 2, 3)
        self._assert_all(results, "5")

    @pytest.mark.integration
    async def test_subtract(self) -> None:
        """Run 'sub' on all hosts — exercises subtract() and the 'sub' branch."""
        results = await do_for_all_hosts(_run_product, "sub", 10, 4)
        self._assert_all(results, "6")

    @pytest.mark.integration
    async def test_multiply(self) -> None:
        """Run 'mul' on all hosts — exercises multiply() and the 'mul' branch."""
        results = await do_for_all_hosts(_run_product, "mul", 3, 7)
        self._assert_all(results, "21")

    @pytest.mark.integration
    async def test_divide(self) -> None:
        """Run 'div' on all hosts — exercises divide() success path."""
        results = await do_for_all_hosts(_run_product, "div", 20, 4)
        self._assert_all(results, "5")

    @pytest.mark.integration
    async def test_divide_by_zero(self) -> None:
        """Run 'div 1 0' on the first host only — exercises divide() error branch.

        Only running on one host means this branch is only covered
        when coverage from multiple hosts is merged.
        """
        host = self._hosts[0]
        result = await host.oneshot(
            f"GCOV_PREFIX={GCDA_REMOTE_DIR} "
            f"GCOV_PREFIX_STRIP={len(PRODUCT_DIR.parts) - 1} "
            f"{REMOTE_INSTALL_DIR}/product div 1 0",
            timeout=10,
        )
        # The program should exit with code 1 and print an error
        assert "Division by zero" in result.output

    @pytest.mark.integration
    async def test_clamp(self) -> None:
        """Run 'clamp' on the last host only — exercises clamp() branches.

        Only running on one host means clamp coverage is host-specific
        until merged.
        """
        host = self._hosts[-1]
        # value < lo
        output = await _run_product(host, "clamp", 1, 5, 10)
        assert output == "5"
        # value > hi
        output = await _run_product(host, "clamp", 15, 5, 10)
        assert output == "10"
        # value in range
        output = await _run_product(host, "clamp", 7, 5, 10)
        assert output == "7"
