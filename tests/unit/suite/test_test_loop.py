"""``ensure`` and the per-test monitor events run on the loop the test body runs on (spec §6.4).

A converge opens host connections, and a connection belongs to the loop that
opened it. So whatever loop scope a test is pinned to, the converge runs on
that same loop, never on a fixture loop of otto's choosing.
"""

import pytest

from tests.unit.suite._inner import run_inner

pytest_plugins = ["pytester"]

_BODY = """
import asyncio, pytest
from otto import project
from otto.result import Result, Status

SEEN = {{}}

async def fake_ensure_installed(source):
    SEEN["ensure"] = id(asyncio.get_running_loop())
    return Result(Status.Success, msg="installed")

project.ensure_installed = fake_ensure_installed
{mark}
@pytest.mark.ensure("installed")
async def test_x():
    assert SEEN["ensure"] == id(asyncio.get_running_loop())
"""


pytestmark = pytest.mark.usefixtures("_restore_ensure_installed")


@pytest.mark.parametrize("scope", ["function", "class", "module", "session"])
def test_ensure_converges_on_the_tests_own_loop(pytester, otto_plugins, scope):
    mark = f"pytestmark = pytest.mark.asyncio(loop_scope={scope!r})"
    run_inner(pytester, otto_plugins, test_e=_BODY.format(mark=mark)).assert_outcomes(passed=1)


def test_ensure_in_a_class_pinned_to_its_own_loop_converges_there(pytester, otto_plugins):
    """The unpinned default (the session loop) is covered in test_session_loop_default.py."""
    body = _BODY.format(mark="").replace(
        '@pytest.mark.ensure("installed")\nasync def test_x():\n',
        "@pytest.mark.asyncio(loop_scope='class')\nclass TestPlain:\n"
        '    @pytest.mark.ensure("installed")\n    async def test_x(self):\n    ',
    )
    run_inner(pytester, otto_plugins, test_c=body).assert_outcomes(passed=1)


@pytest.mark.filterwarnings("ignore::pytest.PytestDeprecationWarning")
def test_the_deprecated_scope_keyword_still_picks_the_loop(pytester, otto_plugins):
    """pytest-asyncio still honours ``asyncio(scope=...)``, so otto reads it too."""
    mark = "pytestmark = pytest.mark.asyncio(scope='module')"
    run_inner(pytester, otto_plugins, test_e=_BODY.format(mark=mark)).assert_outcomes(passed=1)


def test_a_sync_test_converges_on_the_default_fixture_loop(pytester, otto_plugins):
    """A sync test has no loop; its converge runs on the session loop, like an unpinned fixture."""
    body = _BODY.replace(
        'async def test_x():\n    assert SEEN["ensure"] == id(asyncio.get_running_loop())',
        "def test_x(_session_scoped_runner):\n"
        '    assert SEEN["ensure"] == id(_session_scoped_runner.get_loop())',
    ).format(mark="")
    run_inner(pytester, otto_plugins, test_s=body).assert_outcomes(passed=1)


def test_plain_functions_get_a_banner_and_monitor_events(pytester, otto_plugins_with_monitor):
    plugins, events = otto_plugins_with_monitor
    result = run_inner(pytester, plugins, test_p="async def test_plain():\n    pass\n")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*=== test_plain ===*"])
    labels = [e.label for e in events()]
    assert labels == ["test_p.test_plain: start", "test_p.test_plain: pass"]


def test_class_tests_keep_the_class_name_and_record_a_failure(pytester, otto_plugins_with_monitor):
    plugins, events = otto_plugins_with_monitor
    result = run_inner(
        pytester,
        plugins,
        test_k=(
            "class TestRouter:\n"
            "    async def test_up(self):\n"
            "        pass\n"
            "    def test_down(self):\n"
            "        assert False\n"
        ),
    )
    result.assert_outcomes(passed=1, failed=1)
    assert [(e.label, e.color) for e in events()] == [
        ("TestRouter.test_up: start", "#888888"),
        ("TestRouter.test_up: pass", "#2ca02c"),
        ("TestRouter.test_down: start", "#888888"),
        ("TestRouter.test_down: fail", "#d62728"),
    ]


class _LoopRecordingCollector:
    """A monitor collector double that records the loop each event was added on."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []
        self.body_loop: int | None = None

    async def add_event(self, label: str, **_: object) -> None:
        import asyncio

        self.events.append((label, id(asyncio.get_running_loop())))


_EVENTS_BODY = """
import asyncio, pytest
from otto.suite.plugin import otto_plugin_key

{mark}

async def test_x(request):
    request.config.stash[otto_plugin_key].session_monitor_collector.body_loop = id(
        asyncio.get_running_loop()
    )
"""


@pytest.mark.parametrize("scope", [None, "function", "class", "module", "session"])
def test_monitor_events_are_added_on_the_tests_own_loop(pytester, otto_plugins, scope):
    """``None`` is an unpinned test, on otto test's default session loop."""
    collector = _LoopRecordingCollector()
    otto_plugins[0].session_monitor_collector = collector
    mark = "" if scope is None else f"pytestmark = pytest.mark.asyncio(loop_scope={scope!r})"
    run_inner(pytester, otto_plugins, test_m=_EVENTS_BODY.format(mark=mark)).assert_outcomes(
        passed=1
    )
    assert collector.body_loop is not None
    assert collector.events == [
        ("test_m.test_x: start", collector.body_loop),
        ("test_m.test_x: pass", collector.body_loop),
    ]
