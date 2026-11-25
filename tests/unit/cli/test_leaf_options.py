"""One preparation records what an instruction or command leaf was given.

``prepare_leaf_options`` splits a leaf's own parameters from its option fields,
works out which fields someone gave, builds and validates the classes the leaf
offers, and records the result: on the context with one installed, held for
the command's run with none.
"""

import contextlib
import dataclasses
import io

import pytest

import otto
from otto.cli.invoke import LeafOptionSpec, prepare_command_target, prepare_leaf_options
from otto.cli.registry import CLI_COMMANDS, cli_command, resolve_spec_command
from otto.cli.run import instruction
from otto.context import OttoContext, reset_context, set_context
from otto.labs.loader import Lab
from otto.params import register_options
from tests._fixtures.bootstrapstub import bootstrap_stub


@pytest.fixture(autouse=True)
def _clean_cli_commands():
    before = set(CLI_COMMANDS.names())
    yield
    for name in set(CLI_COMMANDS.names()) - before:
        CLI_COMMANDS.unregister(name=name)


@otto.options
class Level:
    level: int = 7


@dataclasses.dataclass
class Other:
    level: int = 0


@otto.options
class Tagged:
    tag: str = "none"


def _target_of(name: str):
    """Return the prepared ``otto run`` target of standalone instruction *name*."""
    from otto.instruction_catalog import INSTRUCTIONS

    return prepare_command_target(INSTRUCTIONS.get(name).handler, verb="run")


@contextlib.contextmanager
def _no_context():
    """Run the block with no context installed (the CLI tests' autouse stub set aside)."""
    from otto import invocation

    token = invocation._ACTIVE.set(None)
    try:
        yield
    finally:
        invocation._ACTIVE.reset(token)


def test_only_option_fields_are_recorded(run_cli):
    register_options(Level, verbs=["run"])
    seen = {}

    @instruction()
    async def counted(ctx: OttoContext, count: int = 1) -> None:
        seen["kwargs"] = ctx.verb_option_source().kwargs
        seen["count"] = count

    result = run_cli(["run", "counted", "--count", "3"])
    assert result.exit_code == 0, result.output
    assert seen == {"kwargs": {"level": 7}, "count": 3}


def test_an_unset_flag_is_given_to_nobody_and_a_typed_default_is(run_cli):
    register_options(Level, verbs=["run"])
    seen = []

    @instruction()
    async def x() -> None:
        seen.append(otto.get_options(Other).level)

    assert run_cli(["run", "x"]).exit_code == 0
    assert run_cli(["run", "x", "--level", "7"]).exit_code == 0
    assert seen == [0, 7]


def test_an_own_options_class_is_readable_below_the_instruction(run_cli):
    seen = {}

    @dataclasses.dataclass
    class Own:
        debug: bool = False

    def helper() -> Own:
        return otto.get_options(Own)

    @instruction(options=Own)
    async def owned(opts: Own) -> None:
        seen["same"] = helper() is opts
        seen["debug"] = opts.debug

    result = run_cli(["run", "owned", "--debug"])
    assert result.exit_code == 0, result.output
    assert seen == {"same": True, "debug": True}


def test_a_validator_runs_once_per_command(run_cli):
    calls = []

    @dataclasses.dataclass
    class Counted:
        n: int = 0

        def __post_init__(self) -> None:
            calls.append(self.n)

    @instruction(options=Counted)
    async def once(opts: Counted, ctx: OttoContext) -> None:
        assert ctx.options(Counted) is opts
        assert otto.get_options(Counted) is opts

    result = run_cli(["run", "once", "--n", "2"])
    assert result.exit_code == 0, result.output
    assert calls == [2]


def test_a_lab_free_command_holds_its_values_for_its_run_only(monkeypatch):
    from otto import bootstrap as bootstrap_mod
    from otto.cli.invoke import wrap_leaf_callbacks

    @dataclasses.dataclass
    class Cls:
        n: int = 0

    seen = []

    @cli_command(name="holds", options=Cls, lab_free=True)
    async def holds(opts: Cls) -> None:
        seen.append(otto.get_options(Cls).n)

    monkeypatch.setattr(bootstrap_mod, "bootstrap", bootstrap_stub)
    spec = CLI_COMMANDS.get("holds")
    with _no_context():
        for args in (["--n", "3"], []):
            cmd = wrap_leaf_callbacks(resolve_spec_command(spec), spec)
            with contextlib.redirect_stdout(io.StringIO()), pytest.raises(SystemExit) as ei:
                cmd.main(args=args, prog_name="holds", standalone_mode=True)
            assert ei.value.code == 0
        assert seen == [3, 0]
        assert otto.get_options(Cls).n == 0


@pytest.mark.asyncio
async def test_a_direct_call_counts_everything_as_given():
    register_options(Level, verbs=["run"])
    seen = []

    @instruction()
    async def direct() -> None:
        seen.append(otto.get_options(Other).level)

    ctx = OttoContext(lab=Lab(name="direct"))
    token = set_context(ctx)
    try:
        await _target_of("direct")(level=7)
    finally:
        reset_context(token)
    assert seen == [7]


def test_a_direct_call_inside_another_command_does_not_use_that_commands_parser(run_cli):
    register_options(Level, verbs=["run"])
    seen = []

    @instruction()
    async def inner() -> None:
        seen.append(otto.get_options(Other).level)

    @instruction()
    async def outer() -> None:
        await _target_of("inner")(level=7)

    result = run_cli(["run", "outer"])
    assert result.exit_code == 0, result.output
    assert seen == [7]


def test_a_leaf_that_awaits_its_own_target_gives_what_it_passes(run_cli):
    register_options(Level, verbs=["run"])
    seen = []

    @instruction()
    async def again(depth: int = 0) -> None:
        if depth == 0:
            await _target_of("again")(depth=1, level=7)
        else:
            seen.append(otto.get_options(Other).level)

    result = run_cli(["run", "again"])
    assert result.exit_code == 0, result.output
    assert seen == [7]


def test_a_direct_call_of_a_project_command_leaves_the_callers_flags(run_cli):
    register_options(Tagged, verbs=["run"])

    @dataclasses.dataclass
    class Cls:
        n: int = 0

    async def target(opts: Cls) -> None: ...

    prepared = prepare_command_target(target, Cls)
    seen = []

    @instruction()
    async def outer() -> None:
        await prepared(n=1)
        seen.append(otto.get_options(Tagged).tag)

    result = run_cli(["run", "outer", "--tag", "x"])
    assert result.exit_code == 0, result.output
    assert seen == ["x"]


def test_a_bad_value_is_a_usage_error_before_the_body(run_cli):
    ran = []

    @otto.options
    class Positive:
        n: int = 1

        def __post_init__(self) -> None:
            if self.n < 1:
                raise ValueError("n must be positive")

    @instruction(options=Positive)
    async def bad(opts: Positive) -> None:
        ran.append(opts)

    result = run_cli(["run", "bad", "--n", "0"])
    assert result.exit_code == 2, result.output
    assert ran == []


def test_a_class_the_command_does_not_offer_fails_on_first_read_as_a_runtime_error(run_cli):
    started = []

    @dataclasses.dataclass
    class Needs:
        target: str

    @instruction()
    async def needy() -> None:
        started.append(True)
        otto.get_options(Needs)

    result = run_cli(["run", "needy"])
    assert started == [True]
    assert result.exit_code not in (0, 2)
    text = result.output + repr(result.exception)
    assert "Needs" in text
    assert "target" in text


def test_the_spec_is_what_the_preparation_records():
    spec = LeafOptionSpec(group=None, build=(Other,))

    async def leaf(**kw: object) -> None: ...

    with _no_context():
        prepared = prepare_leaf_options({"level": 4, "plain": 1}, spec, leaf=leaf)
        assert prepared.rest == {"plain": 1}
        assert prepared.recorded.given == frozenset({"level"})
        assert list(prepared.instances) == [Other]
        with prepared.scope():
            assert otto.get_options(Other) is prepared.instances[Other]
        assert otto.get_options(Other).level == 0
