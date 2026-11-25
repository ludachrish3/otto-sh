"""A ``use_options`` block opened in a test body or a fixture is read by the test (spec §5.2).

Each case runs an inner session the way ``otto test`` does
(:func:`tests.unit.suite._inner.run_inner`). A block a fixture opens around
its ``yield`` is in force for the test it serves, sync or async. The
``ensure`` marker's converge runs in a function fixture otto sets up first,
so a block in the test's own function fixture does not steer it, and one in
a module fixture, set up before it, does.
"""

from otto import options
from otto.project import orchestrator
from otto.result import Result
from otto.utils import Status
from tests.unit.suite._inner import run_inner

pytest_plugins = ["pytester"]


@options
class Acme:
    """The class every case reads."""

    debug_level: int = 0


_ACME = f"import sys\n\nAcme = sys.modules[{__name__!r}].Acme\n"


def test_a_block_in_a_test_body_is_read_by_the_test(pytester, otto_plugins) -> None:
    body = (
        _ACME
        + "from otto import get_options, use_options\n\n"
        + "def test_x():\n"
        + "    with use_options(Acme(debug_level=3)):\n"
        + "        assert get_options(Acme).debug_level == 3\n"
        + "    assert get_options(Acme).debug_level == 0\n"
    )
    run_inner(pytester, otto_plugins, test_e=body).assert_outcomes(passed=1)


def test_a_block_in_a_sync_fixture_is_read_by_the_test(pytester, otto_plugins) -> None:
    body = (
        _ACME
        + "import pytest\nfrom otto import get_options, use_options\n\n"
        + "@pytest.fixture\n"
        + "def level():\n"
        + "    with use_options(Acme(debug_level=3)):\n"
        + "        yield\n\n"
        + "def test_x(level):\n"
        + "    assert get_options(Acme).debug_level == 3\n"
    )
    run_inner(pytester, otto_plugins, test_e=body).assert_outcomes(passed=1)


def test_a_block_in_an_async_fixture_is_read_by_the_test(pytester, otto_plugins) -> None:
    body = (
        _ACME
        + "import pytest\nimport pytest_asyncio\nfrom otto import get_options, use_options\n\n"
        + "@pytest_asyncio.fixture\n"
        + "async def level():\n"
        + "    with use_options(Acme(debug_level=3)):\n"
        + "        yield\n\n"
        + "@pytest.mark.asyncio\n"
        + "async def test_x(level):\n"
        + "    assert get_options(Acme).debug_level == 3\n"
    )
    run_inner(pytester, otto_plugins, test_e=body).assert_outcomes(passed=1)


def test_a_function_fixture_does_not_steer_the_ensure_converge_and_a_module_fixture_does(
    pytester, otto_plugins, monkeypatch
) -> None:
    import otto

    seen: list[int] = []

    async def fake_ensure_installed(*, opts):
        del opts
        seen.append(otto.get_options(Acme).debug_level)
        return Result(status=Status.Success)

    monkeypatch.setattr(orchestrator, "ensure_installed", fake_ensure_installed)
    body = (
        _ACME
        + "import pytest\nfrom otto import use_options\n\n"
        + "@pytest.fixture\n"
        + "def per_test():\n"
        + "    with use_options(Acme(debug_level=3)):\n"
        + "        yield\n\n"
        + "@pytest.fixture(scope='module')\n"
        + "def per_module():\n"
        + "    with use_options(Acme(debug_level=3)):\n"
        + "        yield\n\n"
        + "@pytest.mark.ensure('installed')\n"
        + "def test_a(per_test):\n"
        + "    pass\n\n"
        + "@pytest.mark.ensure('installed')\n"
        + "def test_b(per_module):\n"
        + "    pass\n"
    )
    run_inner(pytester, otto_plugins, test_e=body).assert_outcomes(passed=2)
    assert seen == [0, 3]
