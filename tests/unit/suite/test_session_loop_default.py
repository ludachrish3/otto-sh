"""Under ``otto test`` every test and async fixture runs on one session-wide loop (spec §6.6).

Each test runs a real inner pytest session with ``otto test``'s own loop-scope
arguments, through ``tests/unit/suite/_inner.py``. The hosts are real
``LocalHost`` shells (``FAKE_HOSTS``), which claim the running loop on first
use and so register with that loop with no test wiring.
"""

import pytest

from tests.unit.suite._inner import FAKE_HOSTS, run_inner

pytest_plugins = ["pytester"]


def test_unpinned_classes_and_a_function_share_one_host_connection(pytester, otto_plugins):
    """Two classes and a plain function, none pinned: one loop, one connection, one close."""
    pytester.makepyfile(fakehost=FAKE_HOSTS)
    result = run_inner(
        pytester,
        otto_plugins,
        test_shared=(
            "import asyncio\n"
            "from fakehost import RecordingHost, CLOSED\n"
            "HOST = RecordingHost('dut1')\n"
            "SEEN = []\n"
            "async def _use():\n"
            "    await HOST.run('true')\n"
            "    SEEN.append((id(asyncio.get_running_loop()), id(HOST._session_mgr)))\n"
            "class TestFirst:\n"
            "    async def test_a(self):\n"
            "        await _use()\n"
            "class TestSecond:\n"
            "    async def test_b(self):\n"
            "        assert CLOSED == []\n"
            "        await _use()\n"
            "async def test_plain():\n"
            "    assert CLOSED == []\n"
            "    await _use()\n"
            "    assert len(set(SEEN)) == 1, SEEN\n"
        ),
    )
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(
        [
            "*recorded close of dut1, loop closed: False*",
            "*closed 1 host at end of the session's loop: dut1*",
        ]
    )
    closes = [line for line in result.outlines if "recorded close of dut1" in line]
    assert len(closes) == 1, closes


def test_unpinned_class_and_module_async_fixtures_run_on_the_session_loop(pytester, otto_plugins):
    """A class- and a module-scoped async fixture with no ``loop_scope`` work unpinned."""
    pytester.makepyfile(fakehost=FAKE_HOSTS)
    result = run_inner(
        pytester,
        otto_plugins,
        test_fixtures=(
            "import asyncio, pytest_asyncio\n"
            "from fakehost import RecordingHost, CLOSED\n"
            "LOOPS = set()\n"
            "@pytest_asyncio.fixture(scope='module')\n"
            "async def mod_dut():\n"
            "    LOOPS.add(id(asyncio.get_running_loop()))\n"
            "    h = RecordingHost('mod1')\n"
            "    await h.run('true')\n"
            "    yield h\n"
            "@pytest_asyncio.fixture(scope='class')\n"
            "async def cls_dut():\n"
            "    LOOPS.add(id(asyncio.get_running_loop()))\n"
            "    h = RecordingHost('cls1')\n"
            "    await h.run('true')\n"
            "    yield h\n"
            "    await h.close()\n"
            "class TestA:\n"
            "    async def test_a(self, mod_dut, cls_dut):\n"
            "        LOOPS.add(id(asyncio.get_running_loop()))\n"
            "        await mod_dut.run('true')\n"
            "        await cls_dut.run('true')\n"
            "class TestB:\n"
            "    async def test_b(self, mod_dut, cls_dut):\n"
            "        await mod_dut.run('true')\n"
            "        await cls_dut.run('true')\n"
            "async def test_plain(mod_dut):\n"
            "    LOOPS.add(id(asyncio.get_running_loop()))\n"
            "    await mod_dut.run('true')\n"
            "    assert len(LOOPS) == 1, LOOPS\n"
            "    assert [c[0] for c in CLOSED] == ['cls1', 'cls1']\n"
        ),
    )
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(["*closed 1 host at end of the session's loop: mod1*"])


def test_an_unpinned_class_fixture_that_leaves_its_host_open_is_swept_once_at_session_end(
    pytester, otto_plugins
):
    """No close in the fixture: the host stays open across classes; the session sweep closes it."""
    pytester.makepyfile(fakehost=FAKE_HOSTS)
    result = run_inner(
        pytester,
        otto_plugins,
        test_noclose=(
            "import pytest_asyncio\n"
            "from fakehost import RecordingHost, CLOSED\n"
            "HOST = RecordingHost('cls1')\n"
            "@pytest_asyncio.fixture(scope='class')\n"
            "async def cls_dut():\n"
            "    await HOST.run('true')\n"
            "    yield HOST\n"
            "class TestA:\n"
            "    async def test_a(self, cls_dut):\n"
            "        await cls_dut.run('true')\n"
            "class TestB:\n"
            "    async def test_b(self, cls_dut):\n"
            "        assert CLOSED == []\n"
            "        await cls_dut.run('true')\n"
        ),
    )
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        [
            "*recorded close of cls1, loop closed: False*",
            "*closed 1 host at end of the session's loop: cls1*",
        ]
    )
    closes = [line for line in result.outlines if "recorded close of cls1" in line]
    assert len(closes) == 1, closes


def test_a_class_pinned_to_its_own_loop_closes_its_host_when_the_class_ends(pytester, otto_plugins):
    pytester.makepyfile(fakehost=FAKE_HOSTS)
    result = run_inner(
        pytester,
        otto_plugins,
        test_pinned=(
            "import pytest\n"
            "from fakehost import RecordingHost, CLOSED\n"
            "@pytest.mark.asyncio(loop_scope='class')\n"
            "class TestPinned:\n"
            "    async def test_a(self):\n"
            "        await RecordingHost('own1').run('true')\n"
            "async def test_after():\n"
            "    assert [(c[0], c[2]) for c in CLOSED] == [('own1', False)]\n"
        ),
    )
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*closed 1 host at end of TestPinned's loop: own1*"])


def test_a_class_pinned_narrower_cannot_use_a_host_the_session_loop_owns(pytester, otto_plugins):
    """The pinned class fails fast with a HostLoopError naming both loops."""
    pytester.makepyfile(fakehost=FAKE_HOSTS)
    result = run_inner(
        pytester,
        otto_plugins,
        test_cross=(
            "import pytest\n"
            "from fakehost import RecordingHost\n"
            "from otto.host.loop_owner import HostLoopError\n"
            "HOST = RecordingHost('dut1')\n"
            "async def test_opens_on_the_session_loop():\n"
            "    await HOST.run('true')\n"
            "@pytest.mark.asyncio(loop_scope='class')\n"
            "class TestPinned:\n"
            "    async def test_uses_it(self):\n"
            "        with pytest.raises(HostLoopError) as err:\n"
            "            await HOST.run('true')\n"
            "        assert str(err.value).startswith(\n"
            "            \"host 'dut1' is connected on the session's loop\"\n"
            '            " but was used from TestPinned\'s loop."\n'
            "        ), err.value\n"
            '        assert "stays on the session\'s loop for the whole run" in str(err.value)\n'
        ),
    )
    result.assert_outcomes(passed=2)


_ENSURE_ON_SESSION_LOOP = """
import asyncio, pytest
from otto import project
from otto.result import Result, Status

SEEN = {}

async def fake_ensure_installed(source):
    SEEN["ensure"] = id(asyncio.get_running_loop())
    return Result(Status.Success, msg="installed")

project.ensure_installed = fake_ensure_installed

class TestPlain:
    @pytest.mark.ensure("installed")
    async def test_x(self, _session_scoped_runner):
        assert id(asyncio.get_running_loop()) == id(_session_scoped_runner.get_loop())
        assert SEEN["ensure"] == id(asyncio.get_running_loop())
"""


@pytest.mark.usefixtures("_restore_ensure_installed")
def test_an_unpinned_test_and_its_converge_run_on_the_session_loop(pytester, otto_plugins):
    run_inner(pytester, otto_plugins, test_e=_ENSURE_ON_SESSION_LOOP).assert_outcomes(passed=1)
