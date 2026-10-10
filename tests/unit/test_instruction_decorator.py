"""@instruction registers data and hands the function back unchanged."""

import dataclasses

import pytest

from otto.instructions import INSTRUCTIONS, STANDALONE_INSTRUCTIONS, instruction
from otto.registry import registering_repo


@pytest.fixture(autouse=True)
def _clean_registry():
    names = set(STANDALONE_INSTRUCTIONS.names())
    yield
    for name in set(STANDALONE_INSTRUCTIONS.names()) - names:
        STANDALONE_INSTRUCTIONS.unregister(name)


@dataclasses.dataclass
class _Opts:
    debug: bool = False


def test_decorated_function_is_the_function_itself():
    async def deploy_widget(opts: _Opts) -> _Opts:
        return opts

    decorated = instruction(options=_Opts)(deploy_widget)
    assert decorated is deploy_widget


@pytest.mark.asyncio
async def test_decorated_function_takes_the_instance_it_is_handed():
    @instruction(options=_Opts)
    async def deploy_widget(opts: _Opts) -> _Opts:
        return opts

    given = _Opts(debug=True)
    assert await deploy_widget(given) is given


def test_registers_a_data_entry():
    async def deploy_widget(opts: _Opts) -> None: ...

    instruction(options=_Opts, help="Push it.")(deploy_widget)
    entry = INSTRUCTIONS.get("deploy-widget")
    assert entry.handler is deploy_widget
    assert entry.options_cls is _Opts
    assert entry.help == "Push it."
    assert entry.project is None


def test_explicit_name_wins():
    async def f() -> None: ...

    instruction("my-name")(f)
    assert INSTRUCTIONS.get("my-name").handler is f


def test_refuses_a_sync_function():
    def f() -> None: ...

    with pytest.raises(TypeError, match="must be `async def`"):
        instruction()(f)


def test_refuses_options_with_no_annotated_parameter():
    async def f(other: int = 0) -> None: ...

    with pytest.raises(TypeError, match="has no parameter annotated as _Opts"):
        instruction(options=_Opts)(f)


def test_refuses_a_repo_claiming_a_first_party_name():
    async def install() -> None: ...

    with registering_repo("widget"), pytest.raises(ValueError, match="project instruction"):
        instruction()(install)


def test_refuses_an_unknown_typer_keyword():
    async def f() -> None: ...

    with pytest.raises(TypeError, match="hidden"):
        instruction(hidden=True)(f)


def test_refuses_the_bare_decorator():
    """Bare ``@instruction`` would bind the function's name to the inner decorator."""

    async def deploy_widget() -> None: ...

    before = set(INSTRUCTIONS.names())
    with pytest.raises(TypeError, match=r"write @instruction\(\), with parentheses"):
        instruction(deploy_widget)
    assert set(INSTRUCTIONS.names()) == before


def test_refuses_a_positional_name_and_name_keyword_together():
    with pytest.raises(TypeError, match=r"'one'.*'two'"):
        instruction("one", name="two")
