"""Option values outside any context: blocks, a command's record, the one lookup, the snapshot."""

import asyncio
import contextvars
import dataclasses

import pytest

from otto.params import (
    OptionsNotAvailableError,
    OptionsRegistrationError,
    OptionsValidationError,
    RecordedOptions,
    command_options,
    get_options,
    lookup_options,
    options_snapshot,
    own_options,
    use_options,
)

from ._samples import Acme, Install, Mine, Needs, Strict, Texty, ToolLike, Variant


def test_with_no_context_a_read_is_the_defaults():
    assert get_options(Acme) == Acme()


def test_a_block_hands_back_the_instance_and_ends():
    inst = Acme(debug_level=3)
    with use_options(inst):
        assert get_options(Acme) is inst
    assert get_options(Acme).debug_level == 0


def test_a_block_gives_every_field_by_name():
    with use_options(Acme(debug_level=3)):
        assert get_options(Install).debug_level == 3
        assert get_options(Install).variant == "field"
        assert get_options(Install) is get_options(Install)


def test_an_inner_block_overrides_and_keeps_what_it_did_not_set():
    with use_options(Install(variant="debug", debug_level=3)), use_options(Acme(debug_level=5)):
        assert (get_options(Install).variant, get_options(Install).debug_level) == ("debug", 5)


def test_a_block_that_touches_none_of_a_class_keeps_the_outer_instance():
    inst = Acme(debug_level=3)
    with use_options(inst), use_options(Variant(variant="debug")):
        assert get_options(Acme) is inst


def test_instances_handed_together_must_agree():
    with (
        pytest.raises(ValueError, match="debug_level"),
        use_options(Acme(debug_level=1), Install(debug_level=2)),
    ):
        pass


def test_the_outer_values_return_after_an_exception_and_a_cancellation():
    with use_options(Acme(debug_level=1)):
        with pytest.raises(RuntimeError), use_options(Acme(debug_level=2)):
            raise RuntimeError
        assert get_options(Acme).debug_level == 1

        async def body() -> None:
            with use_options(Acme(debug_level=3)):
                await asyncio.sleep(10)

        async def main() -> None:
            task = asyncio.ensure_future(body())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert get_options(Acme).debug_level == 1

        asyncio.run(main())
        assert get_options(Acme).debug_level == 1
    assert get_options(Acme).debug_level == 0


@pytest.mark.asyncio
async def test_two_tasks_each_see_their_own_block():
    async def read(level: int) -> int:
        with use_options(Acme(debug_level=level)):
            await asyncio.sleep(0)
            return get_options(Acme).debug_level

    assert await asyncio.gather(read(1), read(2)) == [1, 2]


@pytest.mark.asyncio
async def test_a_task_started_inside_a_block_keeps_it():
    gate = asyncio.Event()

    async def later() -> int:
        await gate.wait()
        return get_options(Acme).debug_level

    with use_options(Acme(debug_level=3)):
        task = asyncio.ensure_future(later())
    gate.set()
    assert await task == 3
    assert get_options(Acme).debug_level == 0


def test_a_block_made_first_lays_itself_over_the_block_it_is_entered_in():
    saved = use_options(Acme(debug_level=1))
    with use_options(Variant(variant="debug")), saved:
        assert get_options(Acme).debug_level == 1
        assert get_options(Variant).variant == "debug"


@pytest.mark.asyncio
async def test_a_block_made_in_one_task_keeps_the_values_of_the_task_that_enters_it():
    saved = use_options(Acme(debug_level=1))

    async def enter() -> tuple[int, str]:
        with use_options(Variant(variant="debug")), saved:
            await asyncio.sleep(0)
            return get_options(Acme).debug_level, get_options(Variant).variant

    with use_options(Variant(variant="maker")):
        assert await asyncio.ensure_future(enter()) == (1, "debug")


def test_an_own_instance_gives_only_the_named_fields():
    own = Install(variant="debug", debug_level=9)
    with own_options(own, gives=("variant",)):
        assert get_options(Install) is own
        assert get_options(Variant).variant == "debug"
        assert get_options(Acme).debug_level == 0


def test_an_offered_class_takes_everything_parsed_and_another_only_what_was_given():
    r = RecordedOptions(parsed={"dev": True}, offered=(ToolLike,))
    assert lookup_options(ToolLike, r).dev is True
    assert lookup_options(Mine, r).dev is False
    r = RecordedOptions(parsed={"dev": True}, given=frozenset({"dev"}), offered=(ToolLike,))
    assert lookup_options(Mine, r).dev is True


def test_a_pending_command_refuses_a_read():
    with pytest.raises(OptionsNotAvailableError, match="has not read its flags yet"):
        lookup_options(Acme, RecordedOptions(pending=True))


def test_a_contextless_command_holds_its_values_for_its_run():
    recorded = RecordedOptions(parsed={"debug_level": 3}, given=frozenset({"debug_level"}))
    with command_options(recorded):
        assert get_options(Acme).debug_level == 3
    assert get_options(Acme).debug_level == 0


def test_no_record_is_a_no_op_block():
    with command_options(None):
        assert get_options(Acme) == Acme()


def test_a_missing_required_field_is_a_validation_error():
    with pytest.raises(OptionsValidationError, match="target"):
        get_options(Needs)


def test_a_snapshot_holds_the_values_in_force():
    own = Install(variant="lab")
    with use_options(Acme(debug_level=3)):
        src = options_snapshot(None, own=own, handed={"other": 7})
    assert src.kwargs == {"debug_level": 3, "other": 7}
    assert src.build(Install) is own
    assert src.build(Acme).other == 7


def test_a_snapshot_gives_only_what_was_given():
    r = RecordedOptions(
        parsed={"dev": True, "debug_level": 3},
        given=frozenset({"debug_level"}),
        offered=(ToolLike,),
    )
    src = options_snapshot(r)
    assert src.kwargs == {"dev": True, "debug_level": 3}
    assert src.given == frozenset({"debug_level"})
    assert src.build(Mine).dev is False
    assert src.build(ToolLike).dev is True


def test_a_snapshot_hands_back_the_instances_in_force():
    inst = Acme(debug_level=3)
    with use_options(inst):
        assert options_snapshot(None).build(Acme) is inst
    with use_options(inst), use_options(Install(debug_level=5)):  # touched inside: rebuilt
        assert options_snapshot(None).build(Acme).debug_level == 5


def test_a_snapshot_and_the_lookup_agree_on_a_class_an_inner_block_changed():
    with own_options(Install(variant="lab")), use_options(Acme(debug_level=5)):
        built = options_snapshot(None).build(Install)
        assert (built.variant, built.debug_level) == ("lab", 5)
        assert built is get_options(Install)


def test_a_handed_value_equal_to_the_one_in_force_keeps_the_instance():
    inst = Acme(debug_level=3)
    with use_options(inst):
        assert options_snapshot(None, handed=dataclasses.asdict(inst)).build(Acme) is inst


def test_a_snapshot_without_layers_is_the_recorded_flags_only():
    r = RecordedOptions(parsed={"debug_level": 3}, given=frozenset({"debug_level"}))
    with use_options(Acme(debug_level=9)):
        assert options_snapshot(r, layers=False).build(Acme).debug_level == 3


def test_one_block_read_against_two_records_answers_for_each():
    a = RecordedOptions(parsed={"other": 1}, given=frozenset({"other"}))
    b = RecordedOptions(parsed={"other": 2}, given=frozenset({"other"}))
    with use_options(Install(debug_level=5)):
        assert (lookup_options(Acme, a).other, lookup_options(Acme, b).other) == (1, 2)
        assert lookup_options(Acme, a) is lookup_options(Acme, a)


def test_a_block_inside_an_own_instance_keeps_the_fields_it_did_not_set():
    own = Install(variant="lab")
    with own_options(own), use_options(Acme(debug_level=5)):
        built = get_options(Install)
        assert (built.variant, built.debug_level) == ("lab", 5)
    with own_options(own):
        assert get_options(Install) is own


def test_a_first_read_failure_names_the_class_and_the_field():
    with pytest.raises(OptionsValidationError, match=r"Needs.*target"):
        get_options(Needs)


def test_an_instance_or_a_plain_class_is_not_an_options_class():
    with pytest.raises(OptionsNotAvailableError):
        get_options(Acme())  # ty: ignore[invalid-argument-type]
    with pytest.raises(OptionsNotAvailableError):
        get_options(int)


def test_a_block_refuses_a_class_or_a_non_dataclass():
    with pytest.raises(OptionsRegistrationError):
        use_options(Acme)
    with pytest.raises(OptionsRegistrationError):
        use_options(5)


def test_a_failed_build_is_not_cached_and_names_both_classes():
    with use_options(Texty(level="high")):
        for _ in range(2):
            with pytest.raises(OptionsValidationError, match=r"Strict.*level.*Texty"):
                get_options(Strict)


def test_a_first_read_error_names_the_class_once():
    with pytest.raises(OptionsValidationError, match=r"^Needs: target: Field required$"):
        get_options(Needs)
    with (
        use_options(Texty(level="high")),
        pytest.raises(
            OptionsValidationError,
            match=r"^Strict\.level \(from Texty\): Input should be a valid integer",
        ) as foreign,
    ):
        get_options(Strict)
    assert str(foreign.value).count("Strict") == 1


def test_a_block_left_in_another_context_still_restores():
    outer = Acme(debug_level=1)
    with use_options(outer):
        inner = use_options(Acme(debug_level=3))
        contextvars.copy_context().run(inner.__enter__)
        inner.__exit__(None, None, None)  # left from this context: reset() cannot apply
        assert get_options(Acme) is outer


def test_a_snapshot_of_a_pending_record_is_refused():
    with pytest.raises(OptionsNotAvailableError, match="has not read its flags yet"):
        options_snapshot(RecordedOptions(pending=True))
