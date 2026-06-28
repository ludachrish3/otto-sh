"""Signature-driven CLI parameter synthesis."""

import inspect
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import pytest
import typer

from otto.cli.param_synth import (
    build_cli_binding,
    coerce_scalar,
    parse_comma_list,
    parse_kv_dict,
)
from otto.utils import Arg, Exclude, Opt


def test_coerce_scalar_types():
    assert coerce_scalar("true", bool) is True
    assert coerce_scalar("0", bool) is False
    assert coerce_scalar("7", int) == 7
    assert coerce_scalar("1.5", float) == 1.5
    assert coerce_scalar("/x", Path) == Path("/x")
    assert coerce_scalar("hi", str) == "hi"


def test_parse_comma_list():
    assert parse_comma_list("a,b,c", str) == ["a", "b", "c"]
    assert parse_comma_list("1,2", int) == [1, 2]
    assert parse_comma_list("", str) == []
    assert parse_comma_list(None, str) is None


def test_parse_kv_dict():
    assert parse_kv_dict("K=V,K2=V2", str) == {"K": "V", "K2": "V2"}
    assert parse_kv_dict(None, str) is None
    assert parse_kv_dict("", str) == {}


def _names(binding):
    return [p.name for p in binding.params]


def _by_name(binding, name):
    return next(p for p in binding.params if p.name == name)


def _has_typer(param, typer_type):
    # annotation is Annotated[T, typer.Argument()/Option()] -> check metadata
    meta = getattr(param.annotation, "__metadata__", ())
    return any(isinstance(m, typer_type) for m in meta)


def test_no_default_scalar_is_positional_argument():
    async def f(self, path: str): ...

    b = build_cli_binding(f)
    assert _names(b) == ["path"]
    p = _by_name(b, "path")
    assert p.default is inspect.Parameter.empty
    assert _has_typer(p, typer.models.ArgumentInfo)


def test_bool_default_wrapped_in_typer_option():
    async def f(self, hard: bool = False): ...

    b = build_cli_binding(f)
    p = _by_name(b, "hard")
    assert _has_typer(p, typer.models.OptionInfo) and p.default is False


def test_scalar_union_normalizes_to_str():
    async def f(self, path: "str | Path" = "."): ...

    b = build_cli_binding(f)
    p = _by_name(b, "path")
    # base type handed to Typer must be a non-union (str); union would assert in Typer
    base = getattr(p.annotation, "__origin__", p.annotation)
    assert base is str or p.annotation is str


def test_arg_marker_forces_positional_for_defaulted_scalar():
    async def f(self, path: Annotated["str | Path", Arg()] = "."): ...

    b = build_cli_binding(f)
    assert _has_typer(_by_name(b, "path"), typer.models.ArgumentInfo)


def test_variadic_arg_becomes_list_positional():
    async def f(self, cmds: Annotated[str | Sequence[str], Arg(variadic=True, elem_type=str)]): ...

    b = build_cli_binding(f)
    p = _by_name(b, "cmds")
    assert p.annotation.__metadata__  # Annotated
    assert (
        getattr(p.annotation, "__origin__", None) is list or p.annotation.__args__[0] == list[str]
    )


def test_exclude_marker_drops_param_and_records_default():
    async def f(self, log: Annotated[bool, Exclude] = True): ...

    b = build_cli_binding(f)
    assert "log" not in _names(b)
    assert b.excluded == {"log": True}


def test_opt_marker_forces_option():
    async def f(self, timeout: Annotated[float | None, Opt(help="t")] = None): ...

    b = build_cli_binding(f)
    assert _has_typer(_by_name(b, "timeout"), typer.models.OptionInfo)


def test_list_option_uses_str_with_converter():
    async def f(self, tags: list[str] = []):  # noqa: B006 — function never called; type must stay list[str] for synthesizer
        ...

    b = build_cli_binding(f)
    _by_name(b, "tags")
    assert "tags" in b.converters
    assert b.converters["tags"]("a,b") == ["a", "b"]


def test_two_variadics_is_error():
    async def f(
        self,
        a: Annotated[list, Arg(variadic=True, elem_type=str)],
        b: Annotated[list, Arg(variadic=True, elem_type=str)],
    ): ...

    with pytest.raises(ValueError, match="variadic"):
        build_cli_binding(f)
