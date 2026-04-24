"""Fixtures for coverage unit tests.

These tests require:
- Vagrant test VMs (test1/test2) to be running
- ``gcc`` and ``lcov`` installed on the dev VM
"""

import os
from pathlib import Path

# Must be set before any otto imports -- configmodule reads OTTO_SUT_DIRS at
# import time to compute the module-level _repos singleton.
os.environ.setdefault("OTTO_SUT_DIRS", str(Path(__file__).resolve().parents[2] / "repo1"))

from contextlib import contextmanager
from unittest.mock import PropertyMock, patch

import pytest_asyncio

from otto.configmodule.configmodule import ConfigModule, ConfigModuleManager
from otto.configmodule.lab import Lab
from tests.unit.conftest import make_host


@contextmanager
def configured_hosts(*hosts):
    """Temporarily install a ConfigModule exposing the given hosts via all_hosts().

    Used by integration tests that construct RemoteHost instances directly
    (bypassing the lab loader) but need the new GcdaFetcher to see them.
    """
    lab = Lab(name="pipeline_test")
    lab.hosts = {h.id: h for h in hosts}
    cm = ConfigModule(repos=[], lab=lab)
    with patch(
        "otto.configmodule.configmodule._manager",
        spec=ConfigModuleManager,
    ) as mock_mgr:
        type(mock_mgr).configModule = PropertyMock(return_value=cm)
        yield


@pytest_asyncio.fixture
async def carrot():
    """RemoteHost for test1 (carrot) via SSH."""
    h = make_host("carrot", term="ssh", transfer="scp")
    yield h
    await h.close()


@pytest_asyncio.fixture
async def tomato():
    """RemoteHost for test2 (tomato) via SSH."""
    h = make_host("tomato", term="ssh", transfer="scp")
    yield h
    await h.close()
