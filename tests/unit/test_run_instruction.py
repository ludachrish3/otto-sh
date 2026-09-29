"""run_instruction dispatches a registered instruction from Python with the CLI's binding rules."""

import dataclasses

import pytest

import otto
from otto.config.lab import Lab
from otto.context import (
    LIBRARY_LAB_NAME,
    OttoContext,
    reset_context,
    set_context,
    try_get_context,
)
from otto.instructions import (
    INSTRUCTIONS,
    InstructionEntry,
    bind_handler_kwargs,
    instruction,
    run_instruction,
)
from otto.params import (
    OPTIONS,
    OptionsRegistrationError,
    OptionsValidationError,
    register_options,
)


@pytest.fixture
def ctx():
    # Registered per test, not at import: a module-scope registration would
    # add a `run` flag to every instruction any other test module builds.
    register_options(_Verb, verbs=["run"])
    return OttoContext(lab=Lab(name=LIBRARY_LAB_NAME))


@pytest.fixture(autouse=True)
def _clean_registries():
    inst = set(INSTRUCTIONS.names())
    opts = set(OPTIONS.names())
    yield
    for name in set(INSTRUCTIONS.names()) - inst:
        INSTRUCTIONS.unregister(name)
    for name in set(OPTIONS.names()) - opts:
        OPTIONS.unregister(name)


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

    assert await run_instruction(ctx, "deploy", [_Own(debug=True), _Verb(tag="x")]) == "ok"
    assert seen["opts"].debug is True
    assert seen["c"] is ctx
    assert seen["verb"].tag == "x"


@pytest.mark.asyncio
async def test_run_instruction_builds_a_registered_class_from_defaults(ctx):
    @instruction()
    async def tagged(verb: _Verb) -> str:
        return verb.tag

    assert await run_instruction(ctx, "tagged") == "none"


@pytest.mark.asyncio
async def test_run_instruction_installs_the_context_for_the_call_when_none_is_active(ctx):
    @instruction()
    async def which(c: OttoContext) -> bool:
        return try_get_context() is c

    assert try_get_context() is None
    assert await run_instruction(ctx, "which") is True
    assert try_get_context() is None


@pytest.mark.asyncio
async def test_run_instruction_installs_the_context_over_a_different_active_one(ctx):
    other = OttoContext(lab=Lab(name=LIBRARY_LAB_NAME))

    @instruction()
    async def which() -> bool:
        return try_get_context() is ctx

    token = set_context(other)
    try:
        assert await run_instruction(ctx, "which") is True
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
        assert await run_instruction(ctx, "which") is True
        assert try_get_context() is ctx
    finally:
        reset_context(token)


def test_bind_handler_kwargs_binds_only_option_fields_on_the_verb(ctx):
    async def handler(verb: _Verb, extra: int) -> None: ...

    entry = InstructionEntry(name="h", module=__name__, handler=handler)
    bound = bind_handler_kwargs(ctx, entry, {"tag": "x", "extra": 1})
    assert bound["extra"] == 1
    assert bound["verb"].tag == "x"
    assert ctx.verb_option_source().kwargs == {"tag": "x"}


@pytest.mark.asyncio
async def test_run_instruction_restores_the_verb_binding(ctx):
    @instruction()
    async def noop(verb: _Verb) -> None: ...

    ctx.bind_verb_options("run", {"tag": "before"})
    await run_instruction(ctx, "noop", [_Verb(tag="during")])
    assert ctx.options(_Verb).tag == "before"


@pytest.mark.asyncio
async def test_run_instruction_restores_state_when_the_handler_raises(ctx):
    @instruction()
    async def boom(verb: _Verb) -> None:
        raise RuntimeError("x")

    ctx.bind_verb_options("run", {"tag": "before"})
    with pytest.raises(RuntimeError):
        await run_instruction(ctx, "boom", [_Verb(tag="during")])
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

    assert await run_instruction(ctx, "f", [Both(n=3)]) is True


@pytest.mark.asyncio
async def test_run_instruction_refuses_an_instance_registered_for_another_verb(ctx):
    @otto.options(verbs=["test"])
    class TestOnly:
        n: int = 0

    @instruction()
    async def f() -> None: ...

    with pytest.raises(OptionsRegistrationError):
        await run_instruction(ctx, "f", [TestOnly()])


@pytest.mark.asyncio
async def test_run_instruction_reports_a_missing_required_field(ctx):
    @dataclasses.dataclass
    class Needs:
        target: str

    @instruction(options=Needs)
    async def f(opts: Needs) -> None: ...

    with pytest.raises(OptionsValidationError, match="target"):
        await run_instruction(ctx, "f")


@pytest.mark.asyncio
async def test_run_instruction_unknown_name_is_the_registry_error(ctx):
    with pytest.raises(ValueError, match="Unknown instruction 'nope'"):
        await run_instruction(ctx, "nope")


def _publish_project_instructions():
    """Publish the project instructions (``status``, ``install``, ...); return the orchestrator."""
    import otto.project.actions
    import otto.project.commands
    import otto.project.orchestrator

    otto.project.commands.publish_project_instructions()
    return otto.project.orchestrator


@pytest.mark.asyncio
async def test_run_instruction_routes_a_project_name_through_the_orchestrator(ctx, monkeypatch):
    orchestrator = _publish_project_instructions()
    calls = []

    def validate(name, c, kwargs, *, announce):
        calls.append(("validate", name, c, kwargs, announce))
        return []

    async def fake(name, kwargs):
        calls.append(("run", name, kwargs))
        return "walked"

    monkeypatch.setattr(orchestrator, "project_instruction_body_options", validate)
    monkeypatch.setattr(orchestrator, "run_project_instruction", fake)
    assert await run_instruction(ctx, "status", [_Verb(tag="x")]) == "walked"
    # The validation walk is quiet: the run's own walk announces the skips.
    assert calls == [
        ("validate", "status", ctx, {"tag": "x"}, False),
        ("run", "status", {"tag": "x"}),
    ]


@pytest.mark.asyncio
async def test_run_instruction_passes_a_project_bodys_own_options_instance(ctx, monkeypatch):
    """A body's own class is admitted, as ``otto run install --ensure`` admits its flag."""
    from otto.project.options import InstallOptions

    orchestrator = _publish_project_instructions()
    received = []

    async def fake(name, kwargs):
        received.append(kwargs)

    monkeypatch.setattr(orchestrator, "run_project_instruction", fake)
    await run_instruction(ctx, "install", [InstallOptions(ensure=True)])
    assert [kwargs["ensure"] for kwargs in received] == [True]


@pytest.mark.asyncio
async def test_run_instruction_refuses_a_class_registered_for_test_only_for_a_project_name(ctx):
    @otto.options(verbs=["test"])
    class TestOnly:
        n: int = 0

    _publish_project_instructions()
    with pytest.raises(OptionsRegistrationError, match="TestOnly"):
        await run_instruction(ctx, "install", [TestOnly()])


@pytest.mark.asyncio
async def test_run_instruction_validates_project_bodies_before_any_runs(ctx, monkeypatch):
    orchestrator = _publish_project_instructions()
    ran = []

    def invalid(name, c, kwargs, *, announce):
        raise OptionsValidationError("target: Field required")

    async def fake(name, kwargs):
        ran.append(name)

    monkeypatch.setattr(orchestrator, "project_instruction_body_options", invalid)
    monkeypatch.setattr(orchestrator, "run_project_instruction", fake)
    with pytest.raises(OptionsValidationError, match="target"):
        await run_instruction(ctx, "status")
    assert ran == []
