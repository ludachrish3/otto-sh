"""Example OttoSuite demonstrating @register_suite(), inherited options,
suite-specific options, timeout, retry, parametrize, and stability testing.

Run with::

    otto test TestDevice --help
    otto test TestDevice --device-type switch --firmware 2.1
    otto test TestDevice --filter test_device_reachable
    otto test --iterations 10 --threshold 90 TestDevice
"""

from dataclasses import dataclass
from typing import Annotated

import pytest
import typer
from repo1_common.options import RepoOptions

from otto.suite import OttoSuite, register_suite


@dataclass
class _Options(RepoOptions):
    firmware: Annotated[str, typer.Option(
        help="Firmware version to validate against.",
    )] = "latest"

    check_interfaces: Annotated[bool, typer.Option(
        help="When True, verify all expected interfaces are up.",
    )] = True


@register_suite()
class TestDevice(OttoSuite[_Options]):
    """Validate device configuration and connectivity."""

    Options = _Options

    async def test_device_reachable(self, suite_options: _Options) -> None:
        """Verify the device responds to basic connectivity checks."""
        self.logger.info(
            f"[bold]Checking reachability[/bold] — "
            f"device_type={suite_options.device_type!r}  "
            f"lab_env={suite_options.lab_env!r}",
            extra={'markup': True},
        )
        # Placeholder: replace with real host connectivity check
        assert True

    @pytest.mark.timeout(30)
    async def test_firmware_version(self, suite_options: _Options) -> None:
        """Verify the running firmware matches the expected version."""
        self.logger.info(
            f"Checking firmware={suite_options.firmware!r} on {suite_options.device_type!r}",
        )
        # Placeholder: replace with real firmware query
        assert True

    @pytest.mark.retry(2)
    async def test_management_plane(self) -> None:
        """Verify management-plane access (retried up to 2 times on flaky links)."""
        self.logger.info("Testing management-plane connectivity")
        # Placeholder: replace with real management check
        assert True

    @pytest.mark.integration
    async def test_interface_state(self, suite_options: _Options) -> None:
        """Verify all expected interfaces are operationally up (requires live device)."""
        if not suite_options.check_interfaces:
            pytest.skip("Interface check disabled via --no-check-interfaces")
        self.logger.info("Checking interface state (integration)")
        # Placeholder: replace with real SNMP/SSH interface query
        assert True

    @pytest.mark.parametrize("interface", ["eth0", "eth1", "mgmt0"])
    async def test_interface_up(self, interface: str) -> None:
        """Parametrized test — runs once per interface name."""
        self.logger.info(f"Checking interface {interface}")
        # Placeholder: replace with real interface check
        assert True
