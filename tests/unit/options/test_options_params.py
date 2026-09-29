"""``otto.params.options_params``: an options dataclass becomes Typer parameters.

Each field becomes one keyword parameter whose annotation keeps the field's
type and its ``typer.Option`` metadata (help text included), with the field's
default; an inherited field appears alongside the class's own.
"""

from dataclasses import dataclass
from typing import Annotated

import pytest
import typer

from otto.params import options_params


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
