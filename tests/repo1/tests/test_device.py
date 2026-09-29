"""Example tests demonstrating repo-wide options, test-only options, timeout,
retry, parametrize, and stability testing.

``repo1_instructions`` registers ``RepoOptions`` for ``otto run`` and
``otto test`` and ``DeviceTestOptions`` for ``otto test``; the tests read both
through ``ctx.options(...)``.

Run with::

    otto test --help
    otto test TestDevice --device-type switch --firmware 2.1
    otto test TestDevice::test_device_reachable
    otto test --iterations 10 --threshold 90 TestDevice
"""

import logging

import pytest
from repo1_common.options import DeviceTestOptions, RepoOptions

logger = logging.getLogger(__name__)


class TestDevice:
    """Validate device configuration and connectivity."""

    async def test_device_reachable(self, ctx) -> None:
        """Verify the device responds to basic connectivity checks."""
        repo = ctx.options(RepoOptions)
        logger.info(
            f"[bold]Checking reachability[/bold] — "
            f"device_type={repo.device_type!r}  "
            f"lab_env={repo.lab_env!r}",
            extra={"markup": True},
        )
        # Placeholder: replace with real host connectivity check
        assert True

    @pytest.mark.timeout(30)
    async def test_firmware_version(self, ctx) -> None:
        """Verify the running firmware matches the expected version."""
        firmware = ctx.options(DeviceTestOptions).firmware
        device_type = ctx.options(RepoOptions).device_type
        logger.info(f"Checking firmware={firmware!r} on {device_type!r}")
        # Placeholder: replace with real firmware query
        assert True

    @pytest.mark.retry(2)
    async def test_management_plane(self) -> None:
        """Verify management-plane access (2 total attempts on flaky links).

        ``retry(n)`` re-runs only the test body — fixtures keep the failed
        attempt's state — so a retried test's body must be idempotent: no
        appends, counters, or one-shot consumption that a second run would
        double. Reserve it for environmental flake (a management-plane blip
        on real hardware), not for racy test logic. Reruns are recorded: a
        ``retry_attempts`` property in JUnit XML, WARNING logs per failed
        attempt, and a terminal summary of retried tests.
        """
        logger.info("Testing management-plane connectivity")
        # Placeholder: replace with real management check
        assert True

    @pytest.mark.integration
    async def test_interface_state(self, ctx) -> None:
        """Verify all expected interfaces are operationally up (requires live device)."""
        if not ctx.options(DeviceTestOptions).check_interfaces:
            pytest.skip("Interface check disabled via --no-check-interfaces")
        logger.info("Checking interface state (integration)")
        # Placeholder: replace with real SNMP/SSH interface query
        assert True

    @pytest.mark.parametrize("interface", ["eth0", "eth1", "mgmt0"])
    async def test_interface_up(self, interface: str) -> None:
        """Parametrized test — runs once per interface name."""
        logger.info(f"Checking interface {interface}")
        # Placeholder: replace with real interface check
        assert True
