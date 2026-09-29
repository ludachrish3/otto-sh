"""ctx.options(Cls) hands out the dispatched verb's bound options."""

import contextlib

import pytest

from otto import options
from otto.params import OptionsNotAvailableError, OptionsValidationError, register_options


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
    ctx.bind_verb_options("test", {"firmware": "2.1"})
    assert ctx.verb == "test"
    assert ctx.options(FirmwareOpts).firmware == "2.1"
    assert ctx.options(Strict).level == 1


def test_validation_fails_before_anything_runs(ctx):
    # The library's own error: bind_verb_options is a library entrypoint, and
    # the CLI translates it to a usage error at its boundary.
    with pytest.raises(OptionsValidationError, match="level"):
        ctx.bind_verb_options("test", {"level": 0})


def test_other_verbs_classes_are_not_built_and_say_so(ctx):
    ctx.bind_verb_options("test", {})
    with pytest.raises(OptionsNotAvailableError, match="RunOnly is registered for run, not test"):
        ctx.options(RunOnly)


def test_a_class_registered_after_binding_says_so(ctx):
    ctx.bind_verb_options("test", {})

    @options
    class Late:
        x: int = 0

    register_options(Late, verbs=["test"])
    with pytest.raises(
        OptionsNotAvailableError, match="Late was registered after otto test bound its options"
    ):
        ctx.options(Late)


def test_an_unregistered_class_names_itself(ctx):
    ctx.bind_verb_options("test", {})
    with pytest.raises(OptionsNotAvailableError, match="Unregistered is not registered") as err:
        ctx.options(Unregistered)
    # The remedy is the form the docs lead with, in the place it must live.
    assert "@otto.options(verbs=[...]) in an init module" in str(err.value)


def test_nothing_bound_says_no_verb_is_bound(ctx):
    with pytest.raises(OptionsNotAvailableError, match="no verb's options are bound"):
        ctx.options(FirmwareOpts)


def test_the_source_replays_the_parsed_flags(ctx):
    ctx.bind_verb_options("test", {"firmware": "3.0"})
    assert ctx.verb_option_source().build(FirmwareOpts).firmware == "3.0"


def test_a_project_view_reaches_the_same_options(ctx):
    """A repo-scoped ``ProjectContextView`` delegates ``options`` to the live context.

    ``for_repo`` builds a facade over the same context (it needs no real,
    resolved repo — see ``test_for_repo_is_a_facade_over_the_same_context_not_a_copy``
    in ``tests/unit/test_context.py``), so this asserts unconditionally rather
    than skipping when no repo is configured.
    """
    ctx.bind_verb_options("test", {"firmware": "4"})
    view = ctx.for_repo("acme")
    assert view.options(FirmwareOpts).firmware == "4"


@pytest.mark.parametrize("raises", [False, True], ids=["returns", "raises"])
def test_a_preserved_binding_comes_back_whole(ctx, raises):
    """Whatever is bound inside ``verb_binding_preserved`` is dropped on the way out."""
    ctx.bind_verb_options("run", {"debug": True})
    before = ctx.options(RunOnly)
    outcome = pytest.raises(RuntimeError) if raises else contextlib.nullcontext()
    with outcome, ctx.verb_binding_preserved():
        ctx.bind_verb_options("test", {"firmware": "9"})
        assert ctx.options(FirmwareOpts).firmware == "9"
        if raises:
            raise RuntimeError("inside")
    assert ctx.verb == "run"
    assert ctx.options(RunOnly) is before
    assert ctx.verb_option_source().build(RunOnly).debug is True


def test_preserving_an_unbound_context_leaves_it_unbound(ctx):
    with ctx.verb_binding_preserved():
        ctx.bind_verb_options("test", {})
    with pytest.raises(OptionsNotAvailableError, match="no verb's options are bound"):
        ctx.options(FirmwareOpts)
