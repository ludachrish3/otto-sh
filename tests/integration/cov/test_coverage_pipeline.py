"""Integration tests for the otto coverage pipeline.

These tests verify the fetch half of the coverage workflow:
1. Compile a C product with ``--coverage``
2. Deploy to multiple remote hosts
3. Run the product (generating ``.gcda`` files)
4. Fetch ``.gcda`` files using ``GcdaFetcher``

Capture and report from fetched counters are covered end to end by the
``tests/e2e/cov`` lanes.

**Prerequisites**:
- Vagrant test VMs ``test1`` and ``test2`` must be running
- ``gcc`` and ``lcov`` must be installed on the dev VM

Run with::

    uv run pytest tests/unit/cov/ -m integration
"""

from pathlib import Path

import pytest
import tomli

import otto.host.factory  # noqa: F401 — imported for its side effect: the factory module is what puts the built-in "file" product kind in PRODUCT_KINDS
from otto.coverage.fetcher.remote import GcdaFetcher
from otto.host.local_host import LocalHost
from otto.host.product import PRODUCT_KIND_BUILDER, stamp_cov_dir
from otto.host.unix_host import UnixHost
from otto.labs.loader import Lab
from otto.models.settings import DeclaredEntrySpec
from otto.utils import Status
from tests._fixtures.paths import TESTS_ROOT
from tests.conftest import active_context


def configured_hosts(*hosts):
    """Temporarily install an OttoContext exposing the given hosts via all_hosts().

    Used by integration tests that construct UnixHost instances directly
    (bypassing the lab loader) but need the new GcdaFetcher to see them.
    """
    lab = Lab(name="pipeline_test")
    lab.hosts = {h.id: h for h in hosts}
    return active_context(lab=lab)


PROJECT1 = TESTS_ROOT / "project1"
PRODUCT_DIR = PROJECT1 / "product"
REMOTE_INSTALL_DIR = "/opt/coverage_product"
PRODUCT_NAME = "product"


def _declared_entry():
    """project1's ``[[products]]`` entry for the C product, in runtime form.

    Read from the fixture project's own ``settings.toml`` rather than restated
    here. These tests build their hosts directly (no lab loader, so no ingest
    and no declared-product pass), and a second copy of ``cov_dir`` in this
    file is exactly the drift the per-product model exists to remove: the
    fetcher discovers counters under ``Product.cov_dir``, so if the constant
    here and the declaration there ever disagreed, the fetch would come back
    empty and every assertion below would read as a transport failure.
    """
    settings = tomli.loads((PROJECT1 / ".otto" / "settings.toml").read_text())
    raw = next(e for e in settings["products"] if e["name"] == PRODUCT_NAME)
    spec = DeclaredEntrySpec.model_validate(raw)
    return spec.to_runtime(owner="project1", base_dir=PROJECT1, seam="products")


def attach_product(host: UnixHost) -> None:
    """Give *host* project1's declared product, the way lab ingest would.

    ``PRODUCT_KIND_BUILDER.build`` applies the entry's match table, so a host these
    tests point at that project1 does not declare fails here — loudly — instead
    of silently fetching nothing.
    """
    built = PRODUCT_KIND_BUILDER.build([_declared_entry()], host)
    assert built, f"project1's {PRODUCT_NAME!r} entry does not match host {host.id}"
    for product in built:
        stamp_cov_dir(product)
    host.products.extend(built)


def _cov_dir(host: UnixHost) -> str:
    """The host-side coverage directory *host*'s product declares."""
    product = next(p for p in host.products if p.name == PRODUCT_NAME)
    assert product.cov_dir is not None
    return product.cov_dir


def _gcov_prefix_strip() -> int:
    """Compute GCOV_PREFIX_STRIP for the product build directory."""
    return len(PRODUCT_DIR.parts) - 1


async def _compile_product() -> None:
    """Compile the C product with --coverage."""
    localhost = LocalHost()
    try:
        result = await localhost.exec(f"make -C {PRODUCT_DIR} clean all", timeout=30)
        assert result.status == Status.Success, f"Compilation failed:\n{result.value}"
    finally:
        await localhost.close()


async def _install_on_host(host: UnixHost) -> None:
    """Attach the declared product, then deploy its binary to a remote host."""
    attach_product(host)
    cov_dir = _cov_dir(host)
    await host.exec(f"sudo mkdir -p {REMOTE_INSTALL_DIR} {cov_dir}", timeout=10)
    await host.exec(f"sudo chmod 777 {REMOTE_INSTALL_DIR} {cov_dir}", timeout=10)

    binary = PRODUCT_DIR / "product"
    res = await host.put(
        src_files=[binary],
        dest_dir=Path(REMOTE_INSTALL_DIR),
    )
    assert res.is_ok, f"Deploy to {host.id} failed: {res.msg}"
    await host.exec(f"chmod +x {REMOTE_INSTALL_DIR}/product", timeout=10)


async def _uninstall_from_host(host: UnixHost) -> None:
    """Remove the product and coverage data from a remote host."""
    await host.exec(f"sudo rm -rf {REMOTE_INSTALL_DIR} {_cov_dir(host)}", timeout=10)


async def _run_product(host: UnixHost, op: str, *args: int) -> str:
    """Run the product on a remote host with GCOV_PREFIX set."""
    strip = _gcov_prefix_strip()
    str_args = " ".join(str(a) for a in args)
    cmd = (
        f"GCOV_PREFIX={_cov_dir(host)} "
        f"GCOV_PREFIX_STRIP={strip} "
        f"{REMOTE_INSTALL_DIR}/product {op} {str_args}"
    )
    result = await host.exec(cmd, timeout=10)
    return result.value


@pytest.mark.xdist_group("coverage_e2e")
class TestCoverageFetch:
    """Test that .gcda files are correctly fetched from remote hosts."""

    @pytest.mark.asyncio
    async def test_fetch_gcda_files(self, test1, test2, tmp_path):
        """Deploy, run, and fetch .gcda files from two hosts."""
        hosts = [test1, test2]

        # Setup
        await _compile_product()
        for host in hosts:
            await _install_on_host(host)

        try:
            # Run the product on each host with different operations
            await _run_product(test1, "add", 1, 2)
            await _run_product(test2, "sub", 5, 3)

            # Fetch .gcda files — the fetcher takes no remote directory: each
            # product names its own (`Product.cov_dir`), which is what makes a
            # host with two products two separate staging dirs.
            cov_dir = tmp_path / "cov"
            with configured_hosts(*hosts):
                fetcher = GcdaFetcher(cov_dir)
                product_dirs = await fetcher.fetch_all(failures={})

            # Verify we got .gcda files from both hosts
            assert len(product_dirs) == 2, f"Expected 2 (host, product) keys, got {product_dirs}"

            # Verify the staging tree is keyed by (host.id, product name)
            assert (test1.id, PRODUCT_NAME) in product_dirs
            assert (test2.id, PRODUCT_NAME) in product_dirs

            # ...and that each key's dir is the two-level cov/<host>/<product>/
            for (host_id, product), product_dir in product_dirs.items():
                assert product_dir == cov_dir / host_id / product
                gcda_files = list(product_dir.glob("**/*.gcda"))
                assert len(gcda_files) > 0, f"No .gcda files found for {product} on {host_id}"

        finally:
            for host in hosts:
                await _uninstall_from_host(host)
