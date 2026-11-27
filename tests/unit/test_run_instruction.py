"""run_instruction dispatches a registered instruction from Python with the CLI's binding rules."""

import dataclasses

import pytest

import otto
from otto.instruction_catalog import STANDALONE_INSTRUCTIONS, InstructionEntry
from otto.instructions import bind_handler_kwargs, instruction, run_instruction
from otto.labs.loader import Lab
from otto.params import (
    OPTIONS,
    OptionsValidationError,
    register_options,
)
from otto.run_context import (
    LIBRARY_LAB_NAME,
    OttoContext,
    reset_context,
    set_context,
    try_get_context,
)


@pytest.fixture
def ctx():
    # Registered per test, not at import: a module-scope registration would
    # add a `run` flag to every instruction any other test module builds.
    register_options(_Verb, verbs=["run"])
    return OttoContext(lab=Lab(name=LIBRARY_LAB_NAME))


@pytest.fixture(autouse=True)
def _clean_registries():
    inst = set(STANDALONE_INSTRUCTIONS.names())
    opts = set(OPTIONS.names())
    yield
    for name in set(STANDALONE_INSTRUCTIONS.names()) - inst:
        STANDALONE_INSTRUCTIONS.unregister(name=name)
    for name in set(OPTIONS.names()) - opts:
        OPTIONS.unregister(name=name)


@otto.options
class _Verb:
    tag: str = "none"


@dataclasses.dataclass
class _Own:
    debug: bool = False


@pytest.mark.asyncio
async def test_run_instruction_injects_context_and_registered_class(ctx):
    seen = {}

    @instruction(options=_Own)
    async def deploy(opts: _Own, c: OttoContext, verb: _Verb) -> str:
        seen.update(opts=opts, c=c, verb=verb)
        return "ok"

    assert (
        await run_instruction(ctx, name="deploy", opts=[_Own(debug=True), _Verb(tag="x")]) == "ok"
    )
    assert seen["opts"].debug is True
    assert seen["c"] is ctx
    assert seen["verb"].tag == "x"


@pytest.mark.asyncio
async def test_run_instruction_builds_a_registered_class_from_defaults(ctx):
    @instruction()
    async def tagged(verb: _Verb) -> str:
        return verb.tag

    assert await run_instruction(ctx, name="tagged") == "none"


@pytest.mark.asyncio
async def test_run_instruction_installs_the_context_for_the_call_when_none_is_active(ctx):
    @instruction()
    async def which(c: OttoContext) -> bool:
        return try_get_context() is c

    assert try_get_context() is None
    assert await run_instruction(ctx, name="which") is True
    assert try_get_context() is None


@pytest.mark.asyncio
async def test_run_instruction_installs_the_context_over_a_different_active_one(ctx):
    other = OttoContext(lab=Lab(name=LIBRARY_LAB_NAME))

    @instruction()
    async def which() -> bool:
        return try_get_context() is ctx

    token = set_context(other)
    try:
        assert await run_instruction(ctx, name="which") is True
        assert try_get_context() is other
    finally:
        reset_context(token)


@pytest.mark.asyncio
async def test_run_instruction_leaves_an_already_active_context_active(ctx):
    @instruction()
    async def which() -> bool:
        return try_get_context() is ctx

    token = set_context(ctx)
    try:
        assert await run_instruction(ctx, name="which") is True
        assert try_get_context() is ctx
    finally:
        reset_context(token)


def test_bind_handler_kwargs_injects_without_binding(ctx):
    async def handler(verb: _Verb, extra: int) -> None: ...

    entry = InstructionEntry(name="h", module=__name__, handler=handler)
    bound = bind_handler_kwargs(ctx, entry, {"tag": "x", "extra": 1})
    assert bound["extra"] == 1
    assert bound["verb"].tag == "none"
    assert ctx.verb is None


@pytest.mark.asyncio
async def test_run_instruction_keeps_the_invocations_values(ctx):
    @instruction()
    async def tagged(verb: _Verb) -> str:
        return verb.tag

    ctx.bind_verb_options(verb="run", kwargs={"tag": "before"})
    assert await run_instruction(ctx, name="tagged") == "before"


@pytest.mark.asyncio
async def test_run_instruction_leaves_ctx_verb_alone(ctx):
    seen = []

    @instruction()
    async def which(c: OttoContext) -> None:
        seen.append(c.verb)

    ctx.bind_verb_options(verb="test", kwargs={})
    await run_instruction(ctx, name="which", opts=[_Verb(tag="x")])
    assert seen == ["test"]
    assert ctx.verb == "test"


@pytest.mark.asyncio
async def test_two_concurrent_calls_each_see_their_own_options(ctx):
    import asyncio

    @instruction()
    async def tagged(verb: _Verb) -> str:
        await asyncio.sleep(0)
        return verb.tag

    results = await asyncio.gather(
        run_instruction(ctx, name="tagged", opts=[_Verb(tag="a")]),
        run_instruction(ctx, name="tagged", opts=[_Verb(tag="b")]),
    )
    assert results == ["a", "b"]


@pytest.mark.asyncio
async def test_an_instance_handed_in_is_the_object_read_back(ctx):
    handed = _Own(debug=True)

    @instruction(options=_Own)
    async def same(opts: _Own) -> bool:
        return opts is handed and otto.get_options(_Own) is handed

    assert await run_instruction(ctx, name="same", opts=[handed]) is True


@pytest.mark.asyncio
async def test_on_a_context_nothing_bound_the_parameter_is_the_object_the_lookup_returns(ctx):
    @instruction(options=_Own)
    async def same(opts: _Own, c: OttoContext) -> bool:
        return opts is c.options(type(opts))

    assert await run_instruction(ctx, name="same") is True


@pytest.mark.asyncio
async def test_a_non_dataclass_in_opts_is_still_a_registration_error(ctx):
    from otto.params import OptionsRegistrationError

    @instruction()
    async def f() -> None: ...

    with pytest.raises(OptionsRegistrationError):
        await run_instruction(ctx, name="f", opts=[object()])


@pytest.mark.asyncio
async def test_run_instruction_restores_the_verb_binding(ctx):
    @instruction()
    async def noop(verb: _Verb) -> None: ...

    ctx.bind_verb_options(verb="run", kwargs={"tag": "before"})
    await run_instruction(ctx, name="noop", opts=[_Verb(tag="during")])
    assert ctx.options(_Verb).tag == "before"


@pytest.mark.asyncio
async def test_run_instruction_restores_state_when_the_handler_raises(ctx):
    @instruction()
    async def boom(verb: _Verb) -> None:
        raise RuntimeError("x")

    ctx.bind_verb_options(verb="run", kwargs={"tag": "before"})
    with pytest.raises(RuntimeError):
        await run_instruction(ctx, name="boom", opts=[_Verb(tag="during")])
    assert ctx.options(_Verb).tag == "before"
    assert try_get_context() is None


@pytest.mark.asyncio
async def test_run_instruction_binds_own_class_once_when_it_is_also_registered(ctx):
    @otto.options(verbs=["run"])
    class Both:
        n: int = 0

    @instruction(options=Both)
    async def f(opts: Both, c: OttoContext) -> bool:
        return c.options(Both) is opts

    assert await run_instruction(ctx, name="f", opts=[Both(n=3)]) is True


@pytest.mark.asyncio
async def test_run_instruction_accepts_an_instance_registered_for_another_verb(ctx):
    @otto.options(verbs=["test"])
    class TestOnly:
        n: int = 0

    @instruction()
    async def f() -> None: ...

    await run_instruction(ctx, name="f", opts=[TestOnly()])


@pytest.mark.asyncio
async def test_run_instruction_reports_a_missing_required_field(ctx):
    @dataclasses.dataclass
    class Needs:
        target: str

    @instruction(options=Needs)
    async def f(opts: Needs) -> None: ...

    with pytest.raises(OptionsValidationError, match="target"):
        await run_instruction(ctx, name="f")


@pytest.mark.asyncio
async def test_run_instruction_unknown_name_is_the_registry_error(ctx):
    with pytest.raises(ValueError, match="Unknown instruction 'nope'"):
        await run_instruction(ctx, name="nope")


def _orchestrator():
    """Return the orchestrator; the project instructions (``status``, ...) derive on their own."""
    import otto.project.orchestrator

    return otto.project.orchestrator


@pytest.mark.asyncio
async def test_run_instruction_routes_a_project_name_through_the_orchestrator(ctx, monkeypatch):
    orchestrator = _orchestrator()
    calls = []

    def validate(name, c, kwargs, *, announce, source=None):
        calls.append(("validate", name, c, kwargs, announce))
        return []

    async def fake(name, kwargs, *, source=None):
        calls.append(("run", name, kwargs))
        return "walked"

    monkeypatch.setattr(orchestrator, "project_instruction_body_options", validate)
    monkeypatch.setattr(orchestrator, "run_project_instruction", fake)
    assert await run_instruction(ctx, name="status", opts=[_Verb(tag="x")]) == "walked"
    # The validation walk is quiet: the run's own walk announces the skips.
    assert calls == [
        ("validate", "status", ctx, {"tag": "x"}, False),
        ("run", "status", {"tag": "x"}),
    ]


@pytest.mark.asyncio
async def test_run_instruction_passes_a_project_bodys_own_options_instance(ctx, monkeypatch):
    """A body's own class is admitted, as ``otto run install --ensure`` admits its flag."""
    from otto.project.options import InstallOptions

    orchestrator = _orchestrator()
    received = []

    async def fake(name, kwargs, *, source=None):
        received.append(kwargs)

    monkeypatch.setattr(orchestrator, "run_project_instruction", fake)
    await run_instruction(ctx, name="install", opts=[InstallOptions(ensure=True)])
    assert [kwargs["ensure"] for kwargs in received] == [True]


@pytest.mark.asyncio
async def test_run_instruction_accepts_a_class_registered_for_test_only_for_a_project_name(ctx):
    @otto.options(verbs=["test"])
    class TestOnly:
        n: int = 0

    _orchestrator()
    await run_instruction(ctx, name="install", opts=[TestOnly()])


@pytest.mark.asyncio
async def test_run_instruction_validates_project_bodies_before_any_runs(ctx, monkeypatch):
    orchestrator = _orchestrator()
    ran = []

    def invalid(name, c, kwargs, *, announce, source=None):
        raise OptionsValidationError("target: Field required")

    async def fake(name, kwargs, *, source=None):
        ran.append(name)

    monkeypatch.setattr(orchestrator, "project_instruction_body_options", invalid)
    monkeypatch.setattr(orchestrator, "run_project_instruction", fake)
    with pytest.raises(OptionsValidationError, match="target"):
        await run_instruction(ctx, name="status")
    assert ran == []


# --- which parameter is injected: a class that covers the instruction (§13 item 27) ---


_LAZY_MODULE = "tests_unit_lazily_covering"
_LAZY_SOURCE = '''\
"""A class registered for ``run`` by its import path."""

import otto


@otto.options
class Lazy:
    level: int = 4
'''


def test_a_parameter_annotated_with_a_lazily_registered_covering_class_is_recognised_without_importing_it(  # noqa: E501 — a name that says the rule
    ctx, tmp_path, monkeypatch
):
    """Coverage is read from the entries: the registration is never resolved to decide it."""
    import importlib
    import inspect
    import sys

    from otto.cli.invoke import prepare_command_target

    (tmp_path / f"{_LAZY_MODULE}.py").write_text(_LAZY_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, _LAZY_MODULE, raising=False)
    register_options(f"{_LAZY_MODULE}:Lazy", verbs=["run"])
    lazy = importlib.import_module(_LAZY_MODULE).Lazy

    async def handler(knobs: lazy, extra: int = 0) -> None: ...  # ty: ignore[invalid-type-form]

    resolved = []
    monkeypatch.setattr("otto.params.resolve_registration", resolved.append)
    monkeypatch.setattr("otto.cli.invoke.resolve_registration", resolved.append)
    entry = InstructionEntry(name="h", module=__name__, handler=handler)
    bound = bind_handler_kwargs(ctx, entry, {"knobs": "typed", "extra": 1})
    assert isinstance(bound["knobs"], lazy)
    assert bound["extra"] == 1
    wrapped = prepare_command_target(handler, path=["run", "h"])
    assert list(inspect.signature(wrapped).parameters) == ["extra"]
    assert resolved == []


@otto.options
class _TestOnly:
    level: int = 0


def test_a_class_registered_for_another_command_only_is_an_ordinary_parameter(ctx):
    import inspect

    from otto.cli.invoke import prepare_command_target

    register_options(_TestOnly, verbs=["test", "run other"])

    async def handler(knobs: _TestOnly) -> None: ...

    entry = InstructionEntry(name="h", module=__name__, handler=handler)
    handed = _TestOnly(level=9)
    assert bind_handler_kwargs(ctx, entry, {"knobs": handed})["knobs"] is handed
    wrapped = prepare_command_target(handler, path=["run", "h"])
    assert list(inspect.signature(wrapped).parameters) == ["knobs"]


def test_a_class_registered_for_the_instruction_by_name_is_injected(ctx):
    """The new input: an entry that names the instruction covers it."""
    register_options(_TestOnly, verbs=["run h"])

    async def handler(knobs: _TestOnly) -> None: ...

    entry = InstructionEntry(name="h", module=__name__, handler=handler)
    assert isinstance(bind_handler_kwargs(ctx, entry, {})["knobs"], _TestOnly)
