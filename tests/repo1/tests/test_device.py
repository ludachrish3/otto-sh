"""Example OttoSuite demonstrating Test*-prefixed auto-registration, inherited
options, suite-specific options, timeout, retry, parametrize, and stability testing.

Run with::

    otto test TestDevice --help
    otto test TestDevice --device-type switch --firmware 2.1
    otto test TestDevice --filter test_device_reachable
    otto test --iterations 10 --threshold 90 TestDevice
"""

import logging
from typing import Annotated

import pytest
import typer
from repo1_common.options import RepoOptions

from otto import options
from otto.suite import OttoSuite

logger = logging.getLogger(__name__)


@options
class _Options(RepoOptions):
    firmware: Annotated[
        str,
        typer.Option(
            help="Firmware version to validate against.",
        ),
    ] = "latest"

    check_interfaces: Annotated[
        bool,
        typer.Option(
            help="When True, verify all expected interfaces are up.",
        ),
    ] = True


class TestDevice(OttoSuite):
    """Validate device configuration and connectivity."""

    Options = _Options

    async def test_device_reachable(self, suite_options: _Options) -> None:
        """Verify the device responds to basic connectivity checks."""
        logger.info(
            f"[bold]Checking reachability[/bold] — "
            f"device_type={suite_options.device_type!r}  "
            f"lab_env={suite_options.lab_env!r}",
            extra={"markup": True},
        )
        # Placeholder: replace with real host connectivity check
        assert True

    @pytest.mark.timeout(30)
    async def test_firmware_version(self, suite_options: _Options) -> None:
        """Verify the running firmware matches the expected version."""
        logger.info(
            f"Checking firmware={suite_options.firmware!r} on {suite_options.device_type!r}",
        )
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
    async def test_interface_state(self, suite_options: _Options) -> None:
        """Verify all expected interfaces are operationally up (requires live device)."""
        if not suite_options.check_interfaces:
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
