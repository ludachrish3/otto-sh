"""ctx.options(Cls) hands out the dispatched verb's bound options."""

import asyncio
import contextlib
from typing import Literal

import pytest

from otto import options
from otto.context import reset_context, set_context
from otto.params import (
    OptionsNotAvailableError,
    OptionsValidationError,
    RecordedOptions,
    command_options,
    get_options,
    register_options,
    use_options,
)
from otto.run_context import record_options


@options
class FirmwareOpts:
    firmware: str = "latest"


@options
class Strict:
    level: int = 1

    def __post_init__(self):
        if self.level < 1:
            raise ValueError("level must be >= 1")


@options
class RunOnly:
    debug: bool = False


@options
class Unregistered:
    x: int = 0


@pytest.fixture
def ctx(make_otto_context):
    register_options(FirmwareOpts, verbs=["test"])
    register_options(Strict, verbs=["test"])
    register_options(RunOnly, verbs=["run"])
    return make_otto_context()


def test_binding_builds_every_class_for_the_verb(ctx):
    ctx.bind_verb_options(verb="test", kwargs={"firmware": "2.1"})
    assert ctx.verb == "test"
    assert ctx.options(FirmwareOpts).firmware == "2.1"
    assert ctx.options(Strict).level == 1


def test_validation_fails_before_anything_runs(ctx):
    # The library's own error: bind_verb_options is a library entrypoint, and
    # the CLI translates it to a usage error at its boundary.
    with pytest.raises(OptionsValidationError, match="level"):
        ctx.bind_verb_options(verb="test", kwargs={"level": 0})


def test_a_class_for_another_verb_reads_its_defaults(ctx):
    ctx.bind_verb_options(verb="test", kwargs={})
    assert ctx.options(RunOnly).debug is False


def test_a_class_registered_after_binding_is_built_on_first_read(ctx):
    ctx.bind_verb_options(verb="test", kwargs={})

    @options
    class Late:
        x: int = 0

    register_options(Late, verbs=["test"])
    assert ctx.options(Late).x == 0


def test_an_unregistered_class_is_read_like_any_other(ctx):
    ctx.bind_verb_options(verb="test", kwargs={})
    assert ctx.options(Unregistered).x == 0


def test_nothing_bound_reads_defaults(ctx):
    assert ctx.options(FirmwareOpts).firmware == "latest"


def test_the_source_replays_the_parsed_flags(ctx):
    ctx.bind_verb_options(verb="test", kwargs={"firmware": "3.0"})
    assert ctx.verb_option_source().build(FirmwareOpts).firmware == "3.0"


def test_a_project_view_reaches_the_same_options(ctx):
    """A project-scoped ``ProjectContextView`` delegates ``options`` to the live context.

    ``for_project`` builds a facade over the same context (it needs no real,
    resolved project — see ``test_for_project_is_a_facade_over_the_same_context_not_a_copy``
    in ``tests/unit/test_context.py``), so this asserts unconditionally rather
    than skipping when no project is configured.
    """
    ctx.bind_verb_options(verb="test", kwargs={"firmware": "4"})
    view = ctx.for_project(name="acme")
    assert view.options(FirmwareOpts).firmware == "4"


@pytest.mark.parametrize("raises", [False, True], ids=["returns", "raises"])
def test_a_preserved_binding_comes_back_whole(ctx, raises):
    """Whatever is bound inside ``verb_binding_preserved`` is dropped on the way out."""
    ctx.bind_verb_options(verb="run", kwargs={"debug": True})
    before = ctx.options(RunOnly)
    outcome = pytest.raises(RuntimeError) if raises else contextlib.nullcontext()
    with outcome, ctx.verb_binding_preserved():
        ctx.bind_verb_options(verb="test", kwargs={"firmware": "9"})
        assert ctx.options(FirmwareOpts).firmware == "9"
        if raises:
            raise RuntimeError("inside")
    assert ctx.verb == "run"
    assert ctx.options(RunOnly) is before
    assert ctx.verb_option_source().build(RunOnly).debug is True


def test_preserving_an_unbound_context_leaves_it_unbound(ctx):
    with ctx.verb_binding_preserved():
        ctx.bind_verb_options(verb="test", kwargs={})
    assert ctx.verb is None
    assert ctx.options(FirmwareOpts).firmware == "latest"


@options
class Variant:
    variant: Literal["debug", "release"] = "release"


def test_binding_with_a_command_builds_the_classes_for_that_command(ctx):
    register_options(Variant, verbs=["run install"])
    ctx.bind_verb_options(verb="run", command="install", kwargs={"variant": "debug"})
    assert ctx.options(Variant).variant == "debug"
    assert Variant in ctx._recorded.offered
    before = ctx._recorded
    with pytest.raises(OptionsValidationError, match="variant"):
        ctx.bind_verb_options(verb="run", command="install", kwargs={"variant": "nope"})
    assert ctx._recorded is before
    assert ctx.options(Variant).variant == "debug"
    ctx.bind_verb_options(verb="run", kwargs={})
    assert Variant not in ctx._recorded.offered
    assert ctx.options(Variant).variant == "release"


@options
class Other:
    firmware: str = "x"


@contextlib.contextmanager
def _installed(ctx):
    binding = set_context(ctx)
    try:
        yield ctx
    finally:
        reset_context(binding)


def test_a_given_value_reaches_another_class_by_name(ctx):
    ctx.bind_verb_options(verb="test", kwargs={"firmware": "2.1"})
    assert ctx.options(Other).firmware == "2.1"


def test_two_reads_return_one_object(ctx):
    ctx.bind_verb_options(verb="test", kwargs={"firmware": "2.1"})
    assert ctx.options(FirmwareOpts) is ctx.options(FirmwareOpts)


def test_two_reads_on_a_context_nothing_bound_return_one_object(ctx):
    assert ctx.options(FirmwareOpts) is ctx.options(FirmwareOpts)
    assert ctx.verb is None


def test_the_public_source_is_the_recorded_flags_without_a_block(ctx):
    ctx.bind_verb_options(verb="test", kwargs={"firmware": "2.1"})
    with use_options(FirmwareOpts(firmware="9")):
        assert ctx.verb_option_source().build(FirmwareOpts).firmware == "2.1"
        assert ctx.options(FirmwareOpts).firmware == "9"


def test_a_context_built_inside_another_reads_its_flags_until_bound(ctx, make_otto_context):
    ctx.bind_verb_options(verb="test", kwargs={"firmware": "2.1"})
    with _installed(ctx):
        inner = make_otto_context()
    assert inner.options(FirmwareOpts).firmware == "2.1"
    inner.bind_verb_options(verb="test", kwargs={})
    assert inner.options(FirmwareOpts).firmware == "latest"
    assert ctx.options(FirmwareOpts).firmware == "2.1"


def test_two_independent_contexts_keep_their_own_flags(make_otto_context):
    register_options(FirmwareOpts, verbs=["test"])
    one, two = make_otto_context(), make_otto_context()
    one.bind_verb_options(verb="test", kwargs={"firmware": "1"})
    two.bind_verb_options(verb="test", kwargs={"firmware": "2"})
    assert one.options(FirmwareOpts).firmware == "1"
    assert two.options(FirmwareOpts).firmware == "2"


@pytest.mark.asyncio
async def test_flags_bound_in_a_helper_task_are_visible_after_it(ctx):
    async def helper():
        ctx.bind_verb_options(verb="test", kwargs={"firmware": "5"})

    await asyncio.create_task(helper())
    assert ctx.options(FirmwareOpts).firmware == "5"


def test_a_block_applies_over_whichever_context_is_read(ctx, make_otto_context):
    other = make_otto_context()
    ctx.bind_verb_options(verb="test", kwargs={"firmware": "1"})
    other.bind_verb_options(verb="test", kwargs={"firmware": "2"})
    with use_options(Other(firmware="9")):
        assert ctx.options(FirmwareOpts).firmware == "9"
        assert other.options(FirmwareOpts).firmware == "9"


def test_a_pending_context_refuses_a_read(ctx):
    record_options(ctx, RecordedOptions(group="run", pending=True))
    with pytest.raises(OptionsNotAvailableError, match="has not read its flags yet"):
        ctx.options(FirmwareOpts)


def test_a_pending_context_refuses_its_source(ctx):
    record_options(ctx, RecordedOptions(group="run", pending=True))
    with pytest.raises(OptionsNotAvailableError, match="has not read its flags yet"):
        ctx.verb_option_source()


def test_get_options_reads_the_active_context(ctx):
    ctx.bind_verb_options(verb="test", kwargs={"firmware": "2.1"})
    with _installed(ctx):
        assert get_options(FirmwareOpts) is ctx.options(FirmwareOpts)


def test_a_context_opened_inside_a_contextless_command_reads_its_record(make_otto_context):
    with command_options(
        RecordedOptions(parsed={"firmware": "2.1"}, given=frozenset({"firmware"}))
    ):
        assert make_otto_context().options(FirmwareOpts).firmware == "2.1"


def test_a_bad_binding_inside_a_block_is_refused_and_keeps_the_earlier_record(ctx):
    """A block's instance of a class does not stand in for validating the binding's value."""
    ctx.bind_verb_options(verb="test", kwargs={"firmware": "2.1"})
    before = ctx._recorded
    with use_options(Strict(level=7)), pytest.raises(OptionsValidationError, match="level"):
        ctx.bind_verb_options(verb="test", kwargs={"level": "bad"})
    assert ctx._recorded is before
    assert ctx.options(FirmwareOpts).firmware == "2.1"


@pytest.mark.parametrize("entry", ["command leaf", "bind_verb_options"])
def test_a_bad_value_names_the_field_and_not_the_class_on_both_paths(entry, ctx):
    """The same bad value reads as on main: ``level: ...``, with no ``Strict:`` prefix.

    The command leaf's text is ``usage_error_from`` over the same
    ``OptionsValidationError``; both start from ``build_options``' message.
    """
    import typer

    from otto.cli.invoke import LeafOptionSpec, prepare_leaf_options

    def leaf(**kw):
        del kw

    if entry == "command leaf":
        with pytest.raises(typer.BadParameter) as caught:
            prepare_leaf_options(
                {"level": "bad"}, LeafOptionSpec(group="test", build=(Strict,)), leaf=leaf
            )
        text = caught.value.format_message().removeprefix("Invalid value: ")
    else:
        with pytest.raises(OptionsValidationError, match="level") as caught:
            ctx.bind_verb_options(verb="test", kwargs={"level": "bad"})
        text = str(caught.value)
    assert text.startswith("level: "), text
    assert "Strict" not in text, text
