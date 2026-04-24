"""Tests for otto.configmodule.configmodule — all_hosts and host utility functions."""

import re
from unittest.mock import AsyncMock, patch, PropertyMock

import pytest

from otto.configmodule.configmodule import (
    all_hosts,
    ConfigModuleManager,
    ConfigModule,
    do_for_all_hosts,
    run_on_all_hosts,
)
from otto.configmodule.lab import Lab
from otto.utils import CommandStatus, Status

from tests.unit.conftest import make_host


@pytest.fixture()
def three_hosts():
    """Set up a ConfigModule with three hosts: carrot_seed, tomato_seed, pepper_seed."""
    hosts = {
        "carrot_seed": make_host("carrot"),
        "tomato_seed": make_host("tomato"),
        "pepper_seed": make_host("pepper"),
    }
    lab = Lab(name="test_lab")
    lab.hosts = hosts
    cm = ConfigModule(repos=[], lab=lab)
    with patch(
        "otto.configmodule.configmodule._manager",
        spec=ConfigModuleManager,
    ) as mock_mgr:
        type(mock_mgr).configModule = PropertyMock(return_value=cm)
        yield hosts


class TestAllHosts:

    def test_no_pattern_yields_all(self, three_hosts):
        """Default (None) returns every host."""
        result = list(all_hosts())
        assert len(result) == 3

    def test_pattern_matches_subset(self, three_hosts):
        """A pattern that matches some host IDs filters correctly."""
        pat = re.compile(r"tomato")
        result = list(all_hosts(pattern=pat))
        assert len(result) == 1
        assert result[0].id == "tomato_seed"

    def test_pattern_matches_multiple(self, three_hosts):
        """A pattern matching multiple hosts returns all matches."""
        pat = re.compile(r"(carrot|pepper)")
        result = list(all_hosts(pattern=pat))
        ids = {h.id for h in result}
        assert ids == {"carrot_seed", "pepper_seed"}

    def test_pattern_matches_none(self, three_hosts):
        """A pattern matching no hosts yields nothing."""
        pat = re.compile(r"nonexistent")
        result = list(all_hosts(pattern=pat))
        assert result == []

    def test_pattern_uses_search_not_fullmatch(self, three_hosts):
        """pattern.search is used, so partial matches work."""
        pat = re.compile(r"seed$")
        result = list(all_hosts(pattern=pat))
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Helpers for do_for_all_hosts / run_on_all_hosts tests
# ---------------------------------------------------------------------------

async def _echo_id(host) -> str:
    """Trivial async callable that returns the host's ID."""
    return host.id


async def _raise_for_tomato(host) -> str:
    """Raises for the tomato host, returns ID otherwise."""
    if "tomato" in host.id:
        raise RuntimeError("tomato error")
    return host.id


class TestDoForAllHosts:

    @pytest.mark.asyncio
    async def test_serial_calls_all(self, three_hosts):
        """Serial mode calls method on every host and returns a dict keyed by ID."""
        result = await do_for_all_hosts(_echo_id, concurrent=False)
        assert set(result.keys()) == {"carrot_seed", "tomato_seed", "pepper_seed"}
        for host_id, value in result.items():
            assert value == host_id

    @pytest.mark.asyncio
    async def test_concurrent_calls_all(self, three_hosts):
        """Concurrent mode returns the same results as serial."""
        result = await do_for_all_hosts(_echo_id, concurrent=True)
        assert set(result.keys()) == {"carrot_seed", "tomato_seed", "pepper_seed"}
        for host_id, value in result.items():
            assert value == host_id

    @pytest.mark.asyncio
    async def test_pattern_filters_hosts(self, three_hosts):
        """Only matching hosts are included in the result."""
        pat = re.compile(r"carrot")
        result = await do_for_all_hosts(_echo_id, pattern=pat)
        assert set(result.keys()) == {"carrot_seed"}

    @pytest.mark.asyncio
    async def test_serial_exception_captured(self, three_hosts):
        """In serial mode, a per-host exception is stored in the result."""
        result = await do_for_all_hosts(_raise_for_tomato, concurrent=False)
        assert isinstance(result["tomato_seed"], RuntimeError)
        assert result["carrot_seed"] == "carrot_seed"
        assert result["pepper_seed"] == "pepper_seed"

    @pytest.mark.asyncio
    async def test_concurrent_exception_captured(self, three_hosts):
        """In concurrent mode, a per-host exception is stored in the result."""
        result = await do_for_all_hosts(_raise_for_tomato, concurrent=True)
        assert isinstance(result["tomato_seed"], RuntimeError)
        assert result["carrot_seed"] == "carrot_seed"
        assert result["pepper_seed"] == "pepper_seed"

    @pytest.mark.asyncio
    async def test_args_and_kwargs_forwarded(self, three_hosts):
        """Positional and keyword arguments are forwarded to the method."""

        async def _method(host, cmd, timeout=None):
            return (host.id, cmd, timeout)

        result = await do_for_all_hosts(
            _method, "uname -a", concurrent=False, timeout=5.0,
        )
        for host_id, value in result.items():
            assert value == (host_id, "uname -a", 5.0)


class TestRunOnAllHosts:

    @pytest.mark.asyncio
    async def test_serial(self, three_hosts):
        """run_on_all_hosts delegates to run and returns status tuples."""
        expected = (Status.Success, [CommandStatus("ls", "out", Status.Success, 0)])
        with patch(
            "otto.host.remoteHost.RemoteHost.run",
            new_callable=AsyncMock,
            return_value=expected,
        ):
            result = await run_on_all_hosts("ls", concurrent=False)

        assert set(result.keys()) == {"carrot_seed", "tomato_seed", "pepper_seed"}
        for value in result.values():
            assert value == expected

    @pytest.mark.asyncio
    async def test_concurrent(self, three_hosts):
        """run_on_all_hosts works in concurrent mode."""
        expected = (Status.Success, [CommandStatus("ls", "out", Status.Success, 0)])
        with patch(
            "otto.host.remoteHost.RemoteHost.run",
            new_callable=AsyncMock,
            return_value=expected,
        ):
            result = await run_on_all_hosts("ls", concurrent=True)

        assert set(result.keys()) == {"carrot_seed", "tomato_seed", "pepper_seed"}
        for value in result.values():
            assert value == expected

    @pytest.mark.asyncio
    async def test_pattern_filters(self, three_hosts):
        """run_on_all_hosts respects the pattern filter."""
        expected = (Status.Success, [CommandStatus("ls", "out", Status.Success, 0)])
        with patch(
            "otto.host.remoteHost.RemoteHost.run",
            new_callable=AsyncMock,
            return_value=expected,
        ):
            result = await run_on_all_hosts(
                "ls", pattern=re.compile(r"pepper"), concurrent=False,
            )

        assert set(result.keys()) == {"pepper_seed"}
