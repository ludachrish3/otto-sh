"""InstructionEntry is data; command_name and options_parameter are the decorator's pure rules."""

import dataclasses

import pytest
import typer.main

from otto.instructions import InstructionEntry, command_name, options_parameter


async def _handler(debug: bool = False) -> None: ...


def test_entry_holds_a_handler_and_its_options_class():
    entry = InstructionEntry(name="deploy", module="m", handler=_handler, options_cls=None)
    assert entry.handler is _handler
    assert entry.project is None
    assert entry.help is None
    assert entry.registered_by is None


def test_entry_requires_exactly_one_of_handler_and_project():
    with pytest.raises(ValueError, match="exactly one of handler and project"):
        InstructionEntry(name="x", module="m")
    with pytest.raises(ValueError, match="exactly one of handler and project"):
        InstructionEntry(name="x", module="m", handler=_handler, project=object())  # type: ignore[arg-type]


def test_entry_accepts_a_project_without_a_handler():
    project = object()
    entry = InstructionEntry(name="install", module="m", project=project)  # type: ignore[arg-type]
    assert entry.project is project
    assert entry.handler is None


@pytest.mark.parametrize("name", ["deploy", "get_logs", "Install_Tools", "run_on_container", "x"])
def test_command_name_matches_typer(name):
    assert command_name(name) == typer.main.get_command_name(name)


@dataclasses.dataclass
class _Opts:
    debug: bool = False


async def _with_opts(opts: _Opts) -> None: ...


async def _without(other: int) -> None: ...


def test_options_parameter_names_the_annotated_parameter():
    assert options_parameter(_with_opts, _Opts) == "opts"
    assert options_parameter(_with_opts, None) is None


def test_options_parameter_refuses_a_missing_parameter():
    with pytest.raises(
        TypeError, match="declares options=_Opts but has no parameter annotated as _Opts"
    ):
        options_parameter(_without, _Opts)


async def _returns_opts(other: int) -> _Opts: ...


def test_options_parameter_ignores_the_return_annotation():
    with pytest.raises(TypeError, match="has no parameter annotated as _Opts"):
        options_parameter(_returns_opts, _Opts)
