"""``otto.params.options_params``: an options dataclass becomes Typer parameters.

Each field becomes one keyword parameter whose annotation keeps the field's
type and its ``typer.Option`` metadata (help text included), with the field's
default; an inherited field appears alongside the class's own.

``flag_spellings`` names every word Typer accepts for such a parameter, and
``merge_option_params`` refuses two fields that share one on a command.
"""

import dataclasses
import inspect
from dataclasses import dataclass
from typing import Annotated, Optional

import pytest
import typer

from otto.params import (
    OptionsCollisionError,
    OptionsOrigin,
    flag_spellings,
    merge_option_params,
    options_params,
)


@pytest.mark.parametrize(
    ("tp", "default"),
    [(str, "default"), (int, 5), (float, 0.9), (bool, True)],
    ids=["str", "int", "float", "bool"],
)
def test_a_field_keeps_its_type_and_default(tp, default):
    @dataclass
    class Opts:
        value: Annotated[tp, typer.Option()] = default

    (param,) = options_params(Opts)
    assert param.name == "value"
    assert param.default == default
    assert param.annotation.__args__[0] is tp


def test_an_optional_field_defaults_to_none():
    @dataclass
    class Opts:
        name: Annotated[str | None, typer.Option()] = None

    (param,) = options_params(Opts)
    assert param.default is None


def test_fields_from_several_bases_are_combined():
    @dataclass
    class NetOpts:
        vlan: Annotated[int, typer.Option()] = 100

    @dataclass
    class AuthOpts:
        username: Annotated[str, typer.Option()] = "admin"

    @dataclass
    class CombinedOpts(NetOpts, AuthOpts):
        extra: Annotated[str, typer.Option()] = "x"

    assert {p.name for p in options_params(CombinedOpts)} == {"vlan", "username", "extra"}


def test_help_text_is_preserved_and_absent_when_omitted():
    @dataclass
    class Opts:
        labeled: Annotated[str, typer.Option(help="Has help.")] = "x"
        unlabeled: Annotated[str, typer.Option()] = "y"

    by_name = {p.name: p.annotation.__metadata__[0] for p in options_params(Opts)}
    assert by_name["labeled"].help == "Has help."
    assert by_name["unlabeled"].help is None


def one_param(name: str, annotation: object, default: object) -> inspect.Parameter:
    """The one parameter ``options_params`` makes of a class whose one field is *name*."""
    spec = (
        dataclasses.field(default_factory=list)
        if isinstance(default, list)
        else dataclasses.field(default=default)
    )
    opts = dataclasses.make_dataclass("Opts", [(name, annotation, spec)])
    (param,) = options_params(opts)
    return param


def app_with(param: inspect.Parameter) -> typer.Typer:
    """A one-command app whose command takes *param*."""

    def command(**_: object) -> None: ...

    command.__signature__ = inspect.Signature([param])  # ty: ignore[unresolved-attribute] — Typer reads the signature a function carries
    app = typer.Typer()
    app.command()(command)
    return app


@pytest.mark.parametrize(
    ("name", "annotation", "default"),
    [
        ("level", int, 0),
        ("debug", bool, False),
        ("dry_run", bool, False),
        ("debug_level", Annotated[int, typer.Option("--debug", "-d")], 0),
        ("cov", Annotated[bool, typer.Option("--cov/--no-cov")], False),
        # One authored flag: Typer generates no off switch.
        ("loud", Annotated[bool, typer.Option("--loud")], False),
        # Typer strips None from the union: still a flag, with --no-keep.
        ("keep", Optional[bool], None),  # noqa: UP045 — the typing.Union spelling is the case under test
        ("keep_going", bool | None, None),
        # The second arm of Typer's is_flag: a list of bool with a "/" declaration.
        ("marks", Annotated[list[bool], typer.Option("--mark/--no-mark")], []),
    ],
    ids=[
        "int",
        "bool",
        "underscored-bool",
        "authored-with-short",
        "authored-pair",
        "one-authored-bool",
        "optional-bool",
        "underscored-optional-bool",
        "list-of-bool",
    ],
)
def test_the_spellings_are_the_ones_typer_builds(name, annotation, default):
    param = one_param(name, annotation, default)
    built = typer.main.get_command(app_with(param)).params[0]
    assert set(flag_spellings(param)) == {*built.opts, *built.secondary_opts}


@pytest.mark.parametrize(
    ("name", "annotation", "default"),
    [
        ("level", int, typer.Option(0)),
        ("debug_level", int, typer.Option(0, "--debug", "-d")),
        ("debug", bool, typer.Option(False)),
        ("cov", bool, typer.Option(False, "--cov/--no-cov")),
        ("loud", bool, typer.Option(False, "--loud")),
        # A default value that looks like a flag is a value in this style.
        ("prefix", str, typer.Option("--x")),
        ("target", str, typer.Argument("here")),
    ],
    ids=[
        "int",
        "authored-with-short",
        "bool",
        "authored-pair",
        "one-authored-bool",
        "flag-like-default",
        "argument",
    ],
)
def test_the_default_value_style_spellings_are_the_ones_typer_builds(name, annotation, default):
    param = inspect.Parameter(
        name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation
    )
    built = typer.main.get_command(app_with(param)).params[0]
    expected = {*built.opts, *built.secondary_opts} if built.param_type_name == "option" else set()
    assert set(flag_spellings(param)) == expected


def test_a_bare_bool_field_has_a_generated_off_switch():
    assert flag_spellings(one_param("debug", bool, False)) == ["--debug", "--no-debug"]


@dataclass
class _DebugLevel:
    debug_level: Annotated[int, typer.Option("--debug")] = 0


@dataclass
class _Debug:
    debug: bool = False


def test_two_fields_with_one_spelling_collide():
    with pytest.raises(OptionsCollisionError, match=r"--debug.*project 'a'.*project 'b'") as err:
        merge_option_params(
            [OptionsOrigin(_DebugLevel, "a"), OptionsOrigin(_Debug, "b")], what="otto run"
        )
    text = str(err.value)
    assert text.startswith("otto run: ")
    assert "'debug_level'" in text
    assert "'debug'" in text


def test_a_generated_off_switch_collides_with_an_authored_flag():
    @dataclass
    class Quiet:
        quiet: Annotated[bool, typer.Option("--no-debug")] = False

    with pytest.raises(OptionsCollisionError, match=r"--no-debug.*project 'a'.*project 'b'"):
        merge_option_params(
            [OptionsOrigin(_Debug, "a"), OptionsOrigin(Quiet, "b")], what="otto run"
        )


def test_a_short_spelling_collides_too():
    @dataclass
    class Ex:
        ex: Annotated[int, typer.Option("--ex", "-d")] = 0

    @dataclass
    class Why:
        why: Annotated[int, typer.Option("--why", "-d")] = 0

    with pytest.raises(OptionsCollisionError, match=r"flag -d .*project 'a'.*project 'b'"):
        merge_option_params([OptionsOrigin(Ex, "a"), OptionsOrigin(Why, "b")], what="otto test")


def test_a_reserved_spelling_names_its_owner():
    @dataclass
    class Loud:
        loud: Annotated[bool, typer.Option("--verbose")] = False

    with pytest.raises(OptionsCollisionError, match=r"--verbose.*otto flash erase's --verbose"):
        merge_option_params(
            [OptionsOrigin(Loud, "a")],
            what="otto flash erase",
            reserved_flags={"--verbose": "otto flash erase's --verbose"},
        )


def test_one_inherited_field_is_still_one_flag():
    @dataclass
    class Shared:
        debug: bool = False

    @dataclass
    class First(Shared):
        first: int = 0

    @dataclass
    class Second(Shared):
        second: int = 0

    params = merge_option_params(
        [OptionsOrigin(First, "a"), OptionsOrigin(Second, "b")], what="otto run"
    )
    assert [p.name for p in params] == ["debug", "first", "second"]
