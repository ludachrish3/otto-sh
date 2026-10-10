"""Every pytest-asyncio loop closes the hosts it owns before it closes (spec §6.2, §6.3).

Each test runs a real inner pytest session under otto's plugins. The hosts are
``LocalHost`` subclasses: they claim the running loop on first use, as every
host does, so they register with that loop without any test wiring. Each
close is logged with whether its loop was still open, which is what lets a
clean shutdown flush a device's coverage data and free a single-client console.
"""

import pytest

from tests.unit.suite._inner import FAKE_HOSTS as _FAKE
from tests.unit.suite._inner import run_inner

pytest_plugins = ["pytester"]

_CLASS_LOOPS = "import pytest\npytestmark = pytest.mark.asyncio(loop_scope='class')\n"
"""Pins a generated file's tests to their class loops; ``otto test`` defaults to the session's."""


def test_a_class_loop_closes_its_hosts_before_the_next_class(pytester, otto_plugins):
    pytester.makepyfile(fakehost=_FAKE)
    result = run_inner(
        pytester,
        otto_plugins,
        test_two=(
            _CLASS_LOOPS + "import asyncio\n"
            "from fakehost import RecordingHost, CLOSED\n"
            "HOST = RecordingHost('dut1')\n"
            "FIRST_LOOP = []\n"
            "class TestFirst:\n"
            "    async def test_a(self):\n"
            "        FIRST_LOOP.append(id(asyncio.get_running_loop()))\n"
            "        await HOST.run('true')\n"
            "class TestSecond:\n"
            "    async def test_b(self):\n"
            "        assert CLOSED == [(HOST.id, FIRST_LOOP[0], False)]\n"
            "        await HOST.run('true')\n"
        ),
    )
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*closed 1 host at end of TestFirst's loop: dut1*"])


def test_a_module_fixture_keeps_its_host_across_classes(pytester, otto_plugins):
    pytester.makepyfile(fakehost=_FAKE)
    result = run_inner(
        pytester,
        otto_plugins,
        test_mod=(
            "import pytest, pytest_asyncio\n"
            "from fakehost import RecordingHost, CLOSED\n"
            "pytestmark = pytest.mark.asyncio(loop_scope='module')\n"
            "@pytest_asyncio.fixture(scope='module', loop_scope='module')\n"
            "async def dut():\n"
            "    h = RecordingHost('dut1')\n"
            "    await h.run('true')\n"
            "    yield h\n"
            "class TestOne:\n"
            "    async def test_one(self, dut):\n"
            "        await dut.run('true')\n"
            "class TestTwo:\n"
            "    async def test_two(self, dut):\n"
            "        assert CLOSED == []\n"
            "        await dut.run('true')\n"
        ),
    )
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        [
            "*recorded close of dut1, loop closed: False*",
            "*closed 1 host at end of test_mod.py's loop: dut1*",
        ]
    )


def test_a_session_fixture_closes_at_session_end_on_an_open_loop(pytester, otto_plugins):
    pytester.makepyfile(fakehost=_FAKE)
    pytester.makeconftest(
        "import pytest, pytest_asyncio\n"
        "from pytest_asyncio import is_async_test\n"
        "from fakehost import RecordingHost\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        if is_async_test(item):\n"
        "            item.add_marker(pytest.mark.asyncio(loop_scope='session'), append=False)\n"
        "@pytest_asyncio.fixture(scope='session', loop_scope='session')\n"
        "async def dut():\n"
        "    h = RecordingHost('dut1')\n"
        "    await h.run('true')\n"
        "    yield h\n"
    )
    result = run_inner(
        pytester,
        otto_plugins,
        test_a="async def test_a(dut):\n    await dut.run('true')\n",
        test_b=(
            "from fakehost import CLOSED\n"
            "async def test_b(dut):\n"
            "    assert CLOSED == []\n"
            "    await dut.run('true')\n"
        ),
    )
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        [
            "*recorded close of dut1, loop closed: False*",
            "*closed 1 host at end of the session's loop: dut1*",
        ]
    )


def test_a_function_loop_is_named_after_its_test(pytester, otto_plugins):
    pytester.makepyfile(fakehost=_FAKE)
    result = run_inner(
        pytester,
        otto_plugins,
        test_f=(
            "import pytest\n"
            "from fakehost import RecordingHost\n"
            "@pytest.mark.asyncio(loop_scope='function')\n"
            "async def test_own_loop():\n"
            "    await RecordingHost('dut1').run('true')\n"
        ),
    )
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*closed 1 host at end of test_own_loop's loop: dut1*"])


def test_a_failed_close_warns_and_the_other_hosts_still_close(pytester, otto_plugins):
    """A close that raises is a warning naming the host, never a failed test."""
    pytester.makepyfile(fakehost=_FAKE)
    result = run_inner(
        pytester,
        otto_plugins,
        test_fail=(
            _CLASS_LOOPS + "from fakehost import FailingHost, RecordingHost\n"
            "class TestBoth:\n"
            "    async def test_x(self):\n"
            "        await FailingHost('bad1').run('true')\n"
            "        await RecordingHost('good1').run('true')\n"
        ),
    )
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "*closing host 'bad1' failed during the loop's sweep: RuntimeError('boom')*",
            "*closed 1 host at end of TestBoth's loop: good1*",
        ]
    )


def test_a_close_that_outlasts_the_teardown_deadline_is_abandoned(
    pytester, otto_plugins, monkeypatch
):
    """The policy's teardown deadline bounds the sweep; the stuck host is named and dropped."""
    from otto import invocation as inv
    from otto.invocation import RunPolicy, install_policy, reset_binding

    deadlines: dict[str, float | None] = {}
    real_shut_down = inv.shut_down

    async def recording_shut_down(loop, *, label, deadline):
        deadlines[label] = deadline
        return await real_shut_down(loop, label=label, deadline=deadline)

    # The runner imports shut_down when it sweeps, so the spy is what it calls.
    monkeypatch.setattr(inv, "shut_down", recording_shut_down)
    pytester.makepyfile(fakehost=_FAKE)
    binding = install_policy(RunPolicy(teardown_deadline=0.2))
    try:
        result = run_inner(
            pytester,
            otto_plugins,
            test_slow=(
                _CLASS_LOOPS + "from fakehost import SlowHost, RecordingHost\n"
                "class TestSlow:\n"
                "    async def test_x(self):\n"
                "        await SlowHost('slow1').run('true')\n"
                "        await RecordingHost('quick1').run('true')\n"
            ),
        )
    finally:
        reset_binding(binding)
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            (
                "*closing hosts at end of TestSlow's loop ran past its deadline;"
                " gave up after*dropped the connections of: slow1*"
            ),
            "*closed 1 host at end of TestSlow's loop: quick1*",
        ]
    )
    # The installed 0.2 s policy, not the 10 s default, bounds the runner's sweep.
    assert deadlines["TestSlow's loop"] == 0.2


def test_nothing_is_left_for_the_backstop(pytester, otto_plugins):
    pytester.makepyfile(fakehost=_FAKE)
    run_inner(
        pytester,
        otto_plugins,
        test_x=(
            "from fakehost import RecordingHost\n"
            "async def test_x():\n"
            "    await RecordingHost('d').run('true')\n"
        ),
    ).assert_outcomes(passed=1)
    from otto.invocation import abandon_closed_loops

    assert abandon_closed_loops() == []


def test_a_class_loop_outside_a_class_is_named_after_its_test(pytester, otto_plugins):
    """A plain function gets its own class-scoped runner from pytest, so the loop is the test's."""
    pytester.makepyfile(fakehost=_FAKE)
    result = run_inner(
        pytester,
        otto_plugins,
        test_plain=(
            _CLASS_LOOPS + "from fakehost import RecordingHost\n"
            "async def test_alone():\n"
            "    await RecordingHost('dut1').run('true')\n"
        ),
    )
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*closed 1 host at end of test_alone's loop: dut1*"])


def test_a_fixture_that_closes_its_host_leaves_the_sweep_nothing(pytester, otto_plugins):
    """The fixture's own teardown runs first, on the open loop; the sweep then has nothing to do."""
    pytester.makepyfile(fakehost=_FAKE)
    result = run_inner(
        pytester,
        otto_plugins,
        test_mod=(
            "import pytest, pytest_asyncio\n"
            "from fakehost import RecordingHost, CLOSED\n"
            "pytestmark = pytest.mark.asyncio(loop_scope='module')\n"
            "@pytest_asyncio.fixture(scope='module', loop_scope='module')\n"
            "async def dut():\n"
            "    h = RecordingHost('dut1')\n"
            "    await h.run('true')\n"
            "    yield h\n"
            "    CLOSED.append('teardown')\n"
            "    await h.run('true')\n"
            "    await h.close()\n"
            "async def test_one(dut):\n"
            "    await dut.run('true')\n"
        ),
        test_zz_after=(
            "from fakehost import CLOSED\n"
            "def test_after():\n"
            "    got = [c if isinstance(c, str) else c[0] for c in CLOSED]\n"
            "    assert got == ['teardown', 'dut1']\n"
        ),
    )
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*closed * at end of test_mod.py's loop*")
    from otto.invocation import abandon_closed_loops

    assert abandon_closed_loops() == []


def test_a_tests_open_context_leaves_its_hosts_open_until_the_runner_shuts_down(
    pytester, otto_plugins, monkeypatch
):
    from otto import bootstrap as bs

    bs._reset()
    monkeypatch.setattr(bs, "_result", bs.BootstrapResult(env=None, repos=[]))  # type: ignore[arg-type]
    pytester.makepyfile(fakehost=_FAKE)
    result = run_inner(
        pytester,
        otto_plugins,
        test_ctx=(
            "import otto\n"
            "from otto.config.lab import Lab\n"
            "from fakehost import RecordingHost, CLOSED\n"
            "HOST = RecordingHost('dut1')\n"
            "async def test_a():\n"
            "    async with otto.open_context(lab=Lab(name='rig')):\n"
            "        await HOST.run('true')\n"
            "    assert CLOSED == []\n"
            "async def test_b():\n"
            "    assert CLOSED == []\n"
        ),
    )
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*recorded close of dut1, loop closed: False*"])


def test_two_sessions_in_one_process_leave_no_registration_behind(pytester, otto_plugins):
    from otto import invocation as inv

    pytester.makepyfile(fakehost=_FAKE)
    body = (
        "from fakehost import RecordingHost\n"
        "async def test_x():\n"
        "    await RecordingHost('d').run('true')\n"
    )
    run_inner(pytester, otto_plugins, test_one=body).assert_outcomes(passed=1)
    assert inv.abandon_closed_loops() == []
    assert inv.open_registrations() == []
    run_inner(pytester, otto_plugins, test_two=body).assert_outcomes(passed=2)
    assert inv.abandon_closed_loops() == []
    assert inv.open_registrations() == []


def _node(name: str, path):
    from types import SimpleNamespace

    return SimpleNamespace(name=name, path=path)


@pytest.mark.parametrize(
    ("scope", "node", "in_class", "expected"),
    [
        ("session", ("session", ""), False, "the session's loop"),
        # A Package node is its directory: named after it, with no module file.
        ("package", ("net", "net"), False, "net's loop"),
        ("module", ("test_router.py", "net/test_router.py"), True, "test_router.py's loop"),
        ("class", ("TestRouter", "net/test_router.py"), True, "TestRouter's loop"),
        # Outside a class pytest hands a class-scoped fixture the test item itself.
        ("class", ("test_uplink", "net/test_router.py"), False, "test_uplink's loop"),
        ("function", ("test_uplink", "net/test_router.py"), True, "test_uplink's loop"),
    ],
)
def test_runner_label_names_the_scope_that_owns_the_loop(scope, node, in_class, expected, tmp_path):
    from types import SimpleNamespace

    from otto.suite.loops import runner_label

    class TestRouter:
        pass

    name, rel = node
    request = SimpleNamespace(
        node=_node(name, tmp_path / rel), cls=TestRouter if in_class else None
    )
    assert runner_label(scope, request) == expected  # type: ignore[arg-type]
