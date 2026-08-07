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
    assert _has_typer(p, typer.models.OptionInfo)
    assert p.default is False


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


def test_opt_name_renames_the_flag():
    async def verb(self, dest_dir: Annotated[str, Opt(name="--dest", help="Target.")] = "/tmp"):
        """Verb."""

    binding = build_cli_binding(verb)
    (param,) = binding.params
    typer_meta = param.annotation.__metadata__[0]
    assert "--dest" in typer_meta.param_decls


def test_opt_name_renames_the_flag_for_list_option():
    async def verb(self, tags: Annotated[list[str], Opt(name="--tag", help="Tags.")] = []):  # noqa: B006 — function never called; type must stay list[str] for the comma-list synthesizer branch
        """Verb."""

    binding = build_cli_binding(verb)
    (param,) = binding.params
    typer_meta = param.annotation.__metadata__[0]
    assert "--tag" in typer_meta.param_decls


def test_arg_name_sets_the_metavar():
    async def verb(self, src: Annotated[str, Arg(name="SOURCE")]):
        """Verb."""

    binding = build_cli_binding(verb)
    (param,) = binding.params
    typer_meta = param.annotation.__metadata__[0]
    assert typer_meta.metavar == "SOURCE"


def _autocompletion_of(param):
    """Read the ``autocompletion`` callback off a synthesized param's typer metadata."""
    (meta,) = [
        m
        for m in param.annotation.__metadata__
        if isinstance(m, typer.models.ArgumentInfo | typer.models.OptionInfo)
    ]
    return meta.autocompletion


def test_remote_path_marker_attaches_completer_to_variadic_arg():
    async def verb(
        self,
        src_files: Annotated[list[Path], Arg(variadic=True, elem_type=Path, remote_path="any")],
        dest_dir: Path,
    ):
        """Verb."""

    binding = build_cli_binding(verb)
    assert callable(_autocompletion_of(_by_name(binding, "src_files")))
    assert _autocompletion_of(_by_name(binding, "dest_dir")) is None


def test_remote_path_marker_attaches_completer_to_scalar_arg():
    async def verb(self, dest_dir: Annotated[Path, Arg(remote_path="dir")]):
        """Verb."""

    binding = build_cli_binding(verb)
    param = _by_name(binding, "dest_dir")
    assert _has_typer(param, typer.models.ArgumentInfo)
    assert callable(_autocompletion_of(param))


def test_remote_path_marker_attaches_completer_to_opt():
    async def verb(self, target: Annotated[str, Opt(remote_path="any")] = "/tmp"):
        """Verb."""

    binding = build_cli_binding(verb)
    param = _by_name(binding, "target")
    assert _has_typer(param, typer.models.OptionInfo)
    assert callable(_autocompletion_of(param))


@pytest.mark.parametrize("kind", ["any", "dir"])
def test_remote_path_kind_reaches_the_completer(monkeypatch, kind):
    import otto.cli.remote_completion as rc

    seen = {}

    def fake(ctx, incomplete, kind="any"):
        seen["kind"] = kind
        seen["incomplete"] = incomplete
        return ["x"]

    monkeypatch.setattr(rc, "remote_path_completer", fake)

    async def verb(self, dest_dir: Annotated[Path, Arg(remote_path=kind)]):
        """Verb."""

    completer = _autocompletion_of(_by_name(build_cli_binding(verb), "dest_dir"))
    assert completer(object(), "/et") == ["x"]
    assert seen == {"kind": kind, "incomplete": "/et"}


def test_remote_path_on_a_comma_list_option_is_rejected():
    async def verb(self, paths: Annotated[list[str], Opt(remote_path="any")] = []):  # noqa: B006 — function never called; type must stay list[str] for the comma-list synthesizer branch
        """Verb."""

    with pytest.raises(ValueError, match="remote_path is not supported") as excinfo:
        build_cli_binding(verb)
    # The message must carry the remedy, not just the refusal.
    assert "Arg(variadic=True, remote_path=...)" in str(excinfo.value)


def test_unmarked_params_get_no_completer():
    async def verb(self, dest_dir: Path, flag: bool = False):
        """Verb."""

    binding = build_cli_binding(verb)
    assert _autocompletion_of(_by_name(binding, "dest_dir")) is None
    assert _autocompletion_of(_by_name(binding, "flag")) is None


def test_markers_without_remote_path_get_no_completer():
    """A marker present but *not* carrying remote_path must stay completion-free.

    Pins all three branches the remote_path wiring touches, so a helper that
    returned a truthy callback unconditionally could not pass.
    """

    async def verb(
        self,
        src_files: Annotated[list[Path], Arg(variadic=True, elem_type=Path, help="Files.")],
        src: Annotated[str, Arg(name="SOURCE")],
        dest_dir: Annotated[str, Opt(name="--dest", help="Target.")] = "/tmp",
    ):
        """Verb."""

    binding = build_cli_binding(verb)
    assert _autocompletion_of(_by_name(binding, "src_files")) is None  # variadic Arg
    assert _autocompletion_of(_by_name(binding, "src")) is None  # scalar Arg
    assert _autocompletion_of(_by_name(binding, "dest_dir")) is None  # scalar Opt
