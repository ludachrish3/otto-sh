"""Host connections across pytest event loops, against the unix lab (spec §6.1, §6.6, §9 item 1).

Each case writes a scratch repo, runs a real ``otto --lab unix test``, and
reads the outcome. Unpinned, every test and async fixture runs on the one
session loop, so a host is shared by the whole run. Each loop closes the hosts
it owns before it ends, and a host used from the wrong live loop fails fast
with ``HostLoopError`` naming it.
"""

import re
import time
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.integration

_LAB_SOURCE = f'''
[[lab.sources]]
backend = "json"
paths = ["{PROJECT_ROOT / "tests/_fixtures/lab_data/tech1"}"]
'''


def _run(tmp_path: Path, body: str, *names: str, timeout: int = 90):
    repo = make_sut_repo(
        tmp_path / "probe",
        name="probe",
        tests=["tests"],
        extra=_LAB_SOURCE,
        files={"tests/test_probe.py": body},
    )
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    started = time.monotonic()
    result = run_otto(
        ["test", "--no-random", *names],
        sut_dirs=repo,
        lab="unix",
        xdir=xdir,
        timeout=timeout,
    )
    return result, time.monotonic() - started


_TWO_CLASSES_NO_CLOSE = """
import pytest_asyncio
from otto.lab import get_host

@pytest_asyncio.fixture(scope="session")
async def dut():
    host = get_host("test1")
    await host.run("true")
    yield host

class TestFirst:
    async def test_a(self, dut):
        assert (await dut.run("true")).only.status.is_ok

class TestSecond:
    async def test_b(self):
        assert (await get_host("test1").run("true")).only.status.is_ok

async def test_c(dut):
    assert (await dut.run("true")).only.status.is_ok
"""


def test_two_classes_and_a_function_share_an_unclosed_host_unpinned(tmp_path):
    """No pin anywhere: an unpinned session fixture's host serves every test on the session loop."""
    result, _ = _run(tmp_path, _TWO_CLASSES_NO_CLOSE, "test_a", "test_b", "test_c")
    assert result.returncode == 0, result.stdout


_SESSION_HOST_UNDER_A_CLASS_PIN = """
import pytest
import pytest_asyncio
from otto.lab import get_host

@pytest_asyncio.fixture(scope="session")
async def dut():
    host = get_host("test1")
    await host.run("true")
    yield host
    await host.close()

@pytest.mark.asyncio(loop_scope="class")
class TestPinned:
    async def test_uses_session_host(self, dut):
        await dut.run("true")
"""


def test_cross_loop_use_fails_fast_naming_the_host(tmp_path):
    """A class pinned to its own loop cannot drive a host the session loop owns."""
    result, elapsed = _run(tmp_path, _SESSION_HOST_UNDER_A_CLASS_PIN, "test_uses_session_host")
    assert result.returncode != 0
    assert re.search(r"HostLoopError[^\n]*\btest1\b", result.stdout), result.stdout
    # Whitespace-tolerant: a long log line may wrap in the captured output.
    both_loops = r"on\s+the\s+session's\s+loop\s+but\s+was\s+used\s+from\s+TestPinned's\s+loop"
    assert re.search(both_loops, result.stdout), result.stdout
    assert elapsed < 60, f"took {elapsed:.0f}s; a fail-fast error must not wait for command timeout"


_UNPINNED_FIXTURE_UNDER_PINNED_TESTS = """
import pytest
import pytest_asyncio
from otto.lab import get_host

pytestmark = pytest.mark.asyncio(loop_scope="module")

@pytest_asyncio.fixture
async def dut():
    host = get_host("test1")
    yield host
    await host.close()

async def test_pinned(dut):
    await dut.run("true")
"""


def test_unpinned_fixture_close_fails_fast(tmp_path):
    result, elapsed = _run(tmp_path, _UNPINNED_FIXTURE_UNDER_PINNED_TESTS, "test_pinned")
    assert re.search(r"HostLoopError[^\n]*\btest1\b", result.stdout), result.stdout
    assert elapsed < 60, f"took {elapsed:.0f}s"


_MODULE_PINNED = """
import pytest
import pytest_asyncio
from otto.lab import get_host

pytestmark = pytest.mark.asyncio(loop_scope="module")

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def dut():
    host = get_host("test1")
    await host.run("true")
    yield host
    await host.close()

class TestOne:
    async def test_one(self, dut):
        await dut.run("true")

class TestTwo:
    async def test_two(self, dut):
        await dut.run("true")

async def test_three(dut):
    await dut.run("true")
"""


def test_module_pinned_fixture_shares_one_connection(tmp_path):
    result, _ = _run(tmp_path, _MODULE_PINNED, "test_one", "test_two", "test_three")
    assert result.returncode == 0, result.stdout
