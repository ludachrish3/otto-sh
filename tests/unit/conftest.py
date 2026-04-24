"""
Fixtures for integration tests.

These fixtures load host configuration from the lab JSON files so that tests
always reflect the current lab definitions — IP addresses, credentials, and
host names are never hard-coded in the tests themselves.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from otto.host.host import setDryRun
from otto.host.localHost import LocalHost
from otto.host.remoteHost import RemoteHost


@pytest.fixture(autouse=True)
def _reset_dry_run():
    """Ensure the global dry-run flag is off before and after every test.

    Without this, tests in test_dry_run.py that call ``setDryRun(True)``
    can leak into other tests when pytest-xdist runs them in the same worker.
    """
    setDryRun(False)
    yield
    setDryRun(False)

_LAB_DATA = Path(__file__).parent.parent / "lab_data" / "tech1" / "hosts.json"


def host_data(ne: str) -> dict[str, str]:
    """Return the raw host dict for a given NE name from the lab JSON."""
    hosts = json.loads(_LAB_DATA.read_text())
    for host in hosts:
        if host["ne"] == ne:
            return host
    raise KeyError(f"NE {ne!r} not found in {_LAB_DATA}")


def make_host(ne: str, **kwargs: Any) -> RemoteHost:
    """Build a RemoteHost from lab data with optional field overrides."""
    data = host_data(ne)
    return RemoteHost(
        ip=data["ip"],
        ne=data["ne"],
        creds=data["creds"],
        board=data.get("board"),
        is_virtual=data.get("is_virtual", False),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Parameterized fixtures (driven by @pytest.mark.parametrize + indirect)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def host1(request):
    """Integration host, parameterized by host type ('ssh', 'telnet', or 'local')."""
    if request.param == "local":
        h = LocalHost()
        yield h
        await h.close()
        return
    term = request.param
    kwargs: dict[str, str] = {"term": term}
    if term == "telnet":
        kwargs["transfer"] = "ftp"
    h = make_host("carrot", **kwargs)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def host2(request):
    """Integration host2, parameterized by term type ('ssh' or 'telnet')."""
    term = request.param
    kwargs: dict[str, str] = {"term": term}
    if term == "telnet":
        kwargs["transfer"] = "ftp"
    h = make_host("tomato", **kwargs)
    yield h
    await h.close()

@pytest_asyncio.fixture
async def host3(request):
    """Integration host2, parameterized by term type ('ssh' or 'telnet')."""
    term = request.param
    kwargs: dict[str, str] = {"term": term}
    if term == "ssh":
        kwargs["transfer"] = "scp"
    h = make_host("pepper", **kwargs)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def hop_host(request):
    """Integration host reached through one or two SSH hops.

    Parameterized by ``(ne, hop_ne, term, transfer)`` tuples — e.g.
    ``("tomato", "carrot", "ssh", "scp")`` means "reach tomato through carrot".

    For two-hop chains, *hop_ne* is the first hop and the intermediate host
    must itself have a hop configured at fixture construction time.
    """
    ne, hop_ne, term, transfer = request.param
    target_data = host_data(ne)
    hop_data = host_data(hop_ne)
    hop_id = f"{hop_data['ne']}_{hop_data.get('board', 'seed')}"
    h = RemoteHost(
        ip=target_data["ip"],
        ne=target_data["ne"],
        creds=target_data["creds"],
        board=target_data.get("board"),
        is_virtual=target_data.get("is_virtual", False),
        term=term,
        transfer=transfer,
        hop=hop_id,
    )
    yield h
    await h.close()


@pytest_asyncio.fixture
async def transfer_host(request):
    """Integration host, parameterized by transfer type ('scp', 'sftp', 'ftp', 'nc').

    Accepts either a plain transfer string (uses default ssh term) or a
    ``(transfer, term)`` tuple for explicit term control — e.g. ``('nc', 'telnet')``.
    """
    param = request.param
    if isinstance(param, tuple):
        transfer, term = param
        h = make_host("carrot", transfer=transfer, term=term)
    else:
        h = make_host("carrot", transfer=param)
    yield h
    await h.close()
