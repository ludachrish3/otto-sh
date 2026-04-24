"""Fixtures for coverage integration tests.

These tests require:
- Vagrant test VMs (test1/test2) to be running
- gcc and lcov installed on the dev VM
"""

import os
from pathlib import Path

# Must be set before any otto imports -- configmodule reads OTTO_SUT_DIRS at
# import time to compute the module-level _repos singleton.
os.environ.setdefault('OTTO_SUT_DIRS', str(Path(__file__).parent.parent / 'repo1'))

import json
from typing import Any

import pytest_asyncio

from src.otto.host.remoteHost import RemoteHost

_LAB_DATA = Path(__file__).parent.parent / "lab_data" / "tech1" / "hosts.json"


def _host_data(ne: str) -> dict[str, Any]:
    hosts = json.loads(_LAB_DATA.read_text())
    for host in hosts:
        if host["ne"] == ne:
            return host
    raise KeyError(f"NE {ne!r} not found in {_LAB_DATA}")


@pytest_asyncio.fixture
async def carrot():
    """RemoteHost for test1 (carrot) via SSH."""
    data = _host_data("carrot")
    h = RemoteHost(
        ip=data["ip"],
        ne=data["ne"],
        creds=data["creds"],
        board=data.get("board"),
        is_virtual=True,
        term="ssh",
        transfer="scp",
    )
    yield h
    await h.close()


@pytest_asyncio.fixture
async def tomato():
    """RemoteHost for test2 (tomato) via SSH."""
    data = _host_data("tomato")
    h = RemoteHost(
        ip=data["ip"],
        ne=data["ne"],
        creds=data["creds"],
        board=data.get("board"),
        is_virtual=True,
        term="ssh",
        transfer="scp",
    )
    yield h
    await h.close()
