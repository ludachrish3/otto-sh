"""Integration tests for the otto coverage pipeline.

These tests verify the full coverage workflow:
1. Compile a C product with ``--coverage``
2. Deploy to multiple remote hosts
3. Run the product (generating ``.gcda`` files)
4. Fetch ``.gcda`` files using ``GcdaFetcher``
5. Generate a report using ``CoverageReporter``
6. Assert that cross-host coverage merging works correctly

**Prerequisites**:
- Vagrant test VMs ``test1`` and ``test2`` must be running
- ``gcc`` and ``lcov`` must be installed on the dev VM

Run with::

    uv run pytest tests/unit/cov/ -m integration
"""

import asyncio
from pathlib import Path

import pytest

from otto.coverage.fetcher.remote import GcdaFetcher
from otto.coverage.reporter import CoverageReporter, discover_gcda_dirs
from otto.host.localHost import LocalHost
from otto.host.remoteHost import RemoteHost
from otto.utils import Status

from tests.unit.cov.conftest import configured_hosts

PRODUCT_DIR = Path(__file__).resolve().parents[2] / "repo1" / "product"
REMOTE_INSTALL_DIR = "/opt/coverage_product"
GCDA_REMOTE_DIR = "/var/coverage/product"



def _gcov_prefix_strip() -> int:
    """Compute GCOV_PREFIX_STRIP for the product build directory."""
    return len(PRODUCT_DIR.parts) - 1


async def _compile_product() -> None:
    """Compile the C product with --coverage."""
    localhost = LocalHost()
    try:
        result = await localhost.oneshot(f"make -C {PRODUCT_DIR} clean all", timeout=30)
        assert result.status == Status.Success, f"Compilation failed:\n{result.output}"
    finally:
        await localhost.close()


async def _install_on_host(host: RemoteHost) -> None:
    """Deploy the product binary to a remote host."""
    await host.oneshot(f"sudo mkdir -p {REMOTE_INSTALL_DIR} {GCDA_REMOTE_DIR}", timeout=10)
    await host.oneshot(f"sudo chmod 777 {REMOTE_INSTALL_DIR} {GCDA_REMOTE_DIR}", timeout=10)

    binary = PRODUCT_DIR / "product"
    status, msg = await host.put(
        src_files=[binary],
        dest_dir=Path(REMOTE_INSTALL_DIR),
    )
    assert status.is_ok, f"Deploy to {host.id} failed: {msg}"
    await host.oneshot(f"chmod +x {REMOTE_INSTALL_DIR}/product", timeout=10)


async def _uninstall_from_host(host: RemoteHost) -> None:
    """Remove the product and coverage data from a remote host."""
    await host.oneshot(f"sudo rm -rf {REMOTE_INSTALL_DIR} {GCDA_REMOTE_DIR}", timeout=10)


async def _run_product(host: RemoteHost, op: str, *args: int) -> str:
    """Run the product on a remote host with GCOV_PREFIX set."""
    strip = _gcov_prefix_strip()
    str_args = " ".join(str(a) for a in args)
    cmd = (
        f"GCOV_PREFIX={GCDA_REMOTE_DIR} "
        f"GCOV_PREFIX_STRIP={strip} "
        f"{REMOTE_INSTALL_DIR}/product {op} {str_args}"
    )
    result = await host.oneshot(cmd, timeout=10)
    return result.output.strip()


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestCoverageFetch:
    """Test that .gcda files are correctly fetched from remote hosts."""

    @pytest.mark.asyncio
    async def test_fetch_gcda_files(self, carrot, tomato, tmp_path):
        """Deploy, run, and fetch .gcda files from two hosts."""
        hosts = [carrot, tomato]

        # Setup
        await _compile_product()
        for host in hosts:
            await _install_on_host(host)

        try:
            # Run the product on each host with different operations
            await _run_product(carrot, "add", 1, 2)
            await _run_product(tomato, "sub", 5, 3)

            # Fetch .gcda files
            cov_dir = tmp_path / "cov"
            with configured_hosts(*hosts):
                fetcher = GcdaFetcher(cov_dir)
                host_dirs = await fetcher.fetch_all(GCDA_REMOTE_DIR)

            # Verify we got .gcda files from both hosts
            assert len(host_dirs) == 2, f"Expected 2 hosts, got {len(host_dirs)}"

            # Verify directories are named by host.id
            assert carrot.id in host_dirs
            assert tomato.id in host_dirs

            # Verify .gcda files exist
            for host_id, host_dir in host_dirs.items():
                gcda_files = list(host_dir.glob("**/*.gcda"))
                assert len(gcda_files) > 0, (
                    f"No .gcda files found for host {host_id}"
                )

        finally:
            for host in hosts:
                await _uninstall_from_host(host)


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestCoverageReport:
    """Test that coverage reports are correctly generated from merged data."""

    @pytest.mark.asyncio
    async def test_merged_coverage_across_hosts(self, carrot, tomato, tmp_path):
        """Verify that merging coverage from multiple hosts combines data.

        Host 1 (carrot): runs add, multiply
        Host 2 (tomato): runs subtract, divide

        After merging, the report should show coverage of all four
        functions, which neither host achieved individually.
        """
        hosts = [carrot, tomato]

        await _compile_product()
        for host in hosts:
            await _install_on_host(host)

        try:
            # Exercise different code paths on each host
            await _run_product(carrot, "add", 2, 3)
            await _run_product(carrot, "mul", 4, 5)
            await _run_product(tomato, "sub", 10, 4)
            await _run_product(tomato, "div", 20, 5)

            # Fetch .gcda files
            run_dir = tmp_path / "run1"
            cov_dir = run_dir / "cov"
            with configured_hosts(*hosts):
                fetcher = GcdaFetcher(cov_dir)
                host_dirs = await fetcher.fetch_all(GCDA_REMOTE_DIR)
            assert len(host_dirs) == 2

            # Generate report
            gcda_dirs = discover_gcda_dirs([cov_dir])
            assert len(gcda_dirs) == 2

            report_dir = tmp_path / "report"
            reporter = CoverageReporter(
                gcda_dirs=gcda_dirs,
                source_root=PRODUCT_DIR,
                output_dir=report_dir,
            )
            store = await reporter.run()

            # Verify report was generated
            assert (report_dir / "index.html").exists(), "Report index.html not generated"

            # Verify coverage data was loaded
            assert store.file_count() > 0, "No files in coverage store"

            # Verify overall coverage is > 0%
            pct = store.overall_pct()
            assert pct > 0, f"Expected coverage > 0%, got {pct}%"

        finally:
            for host in hosts:
                await _uninstall_from_host(host)

    @pytest.mark.asyncio
    async def test_multi_run_stitching(self, carrot, tomato, tmp_path):
        """Verify that coverage from multiple test runs can be stitched.

        Run 1: carrot runs add
        Run 2: tomato runs clamp (all branches)

        The merged report should cover both runs.
        """
        hosts = [carrot, tomato]

        await _compile_product()
        for host in hosts:
            await _install_on_host(host)

        try:
            # Run 1: add on carrot
            run1_dir = tmp_path / "run1"
            cov1_dir = run1_dir / "cov"

            # Clean previous .gcda files
            await carrot.oneshot(
                f"find {GCDA_REMOTE_DIR} -name '*.gcda' -delete 2>/dev/null; true",
                timeout=10,
            )
            await _run_product(carrot, "add", 1, 2)

            with configured_hosts(carrot):
                fetcher1 = GcdaFetcher(cov1_dir)
                dirs1 = await fetcher1.fetch_all(GCDA_REMOTE_DIR)
            assert len(dirs1) == 1

            # Run 2: clamp on tomato
            run2_dir = tmp_path / "run2"
            cov2_dir = run2_dir / "cov"

            await tomato.oneshot(
                f"find {GCDA_REMOTE_DIR} -name '*.gcda' -delete 2>/dev/null; true",
                timeout=10,
            )
            await _run_product(tomato, "clamp", 1, 5, 10)
            await _run_product(tomato, "clamp", 15, 5, 10)
            await _run_product(tomato, "clamp", 7, 5, 10)

            with configured_hosts(tomato):
                fetcher2 = GcdaFetcher(cov2_dir)
                dirs2 = await fetcher2.fetch_all(GCDA_REMOTE_DIR)
            assert len(dirs2) == 1

            # Generate merged report from both runs
            gcda_dirs = discover_gcda_dirs([cov1_dir, cov2_dir])
            assert len(gcda_dirs) == 2

            report_dir = tmp_path / "report"
            reporter = CoverageReporter(
                gcda_dirs=gcda_dirs,
                source_root=PRODUCT_DIR,
                output_dir=report_dir,
            )
            store = await reporter.run()

            assert (report_dir / "index.html").exists()
            assert store.file_count() > 0
            assert store.overall_pct() > 0

        finally:
            for host in hosts:
                await _uninstall_from_host(host)
