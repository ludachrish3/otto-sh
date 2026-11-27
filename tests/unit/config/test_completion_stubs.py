"""Unit tests for :mod:`otto.config.completion_stubs`.

Drive the stub rebuilder without going through a subprocess: verify that a
round-tripped option schema produces a Typer command whose callback signature
carries the expected flags, kinds, defaults, and help text.

Note: this module intentionally does NOT use ``from __future__ import
annotations`` — the tests rely on runtime-evaluated ``Annotated[...]`` forms
so ``serialize_options`` can introspect them. PEP 563 would stringify the
annotations and every test would silently fail by returning ``None``.
"""

import inspect
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin

import pytest
import typer

from otto.config.completion_cache import serialize_options
from otto.config.completion_stubs import build_stub_command


def _unwrap_typer_option(annotation: Any) -> tuple[Any, Any]:
    """Return ``(base_type, typer.Option)`` from an ``Annotated[...]`` annotation."""
    assert get_origin(annotation) is not None  # Annotated or typing equivalent
    args = get_args(annotation)
    base = args[0]
    meta = next(a for a in args[1:] if hasattr(a, "param_decls"))
    return base, meta


def test_roundtrip_preserves_flags_and_kind() -> None:
    """A callback's options serialize and rebuild to an equivalent signature."""

    def source(
        name: Annotated[str, typer.Option("--name", "-n", help="device name")] = "dev",
        count: Annotated[int, typer.Option("--count", help="how many")] = 3,
        rich: Annotated[bool, typer.Option("--rich/--plain", help="style")] = False,
        where: Annotated[Path, typer.Option("--where", help="dest dir")] = Path("/tmp"),
    ) -> None: ...

    schema = serialize_options(source, command_name="source")
    assert schema is not None
    stub_app = build_stub_command("source", schema)

    cmd = stub_app.registered_commands[0]
    sig = inspect.signature(cmd.callback)
    params = sig.parameters
    assert list(params) == ["name", "count", "rich", "where"]

    name_base, name_opt = _unwrap_typer_option(params["name"].annotation)
    assert name_base is str
    assert "--name" in (name_opt.default, *getattr(name_opt, "param_decls", ()))
    assert params["name"].default == "dev"

    count_base, _ = _unwrap_typer_option(params["count"].annotation)
    assert count_base is int
    assert params["count"].default == 3

    rich_base, _ = _unwrap_typer_option(params["rich"].annotation)
    assert rich_base is bool
    assert params["rich"].default is False

    where_base, _ = _unwrap_typer_option(params["where"].annotation)
    assert where_base is Path
    assert params["where"].default == Path("/tmp")


def test_roundtrip_str_list() -> None:
    """``list[str]`` round-trips via the ``str_list`` kind."""

    def source(
        tags: Annotated[list[str] | None, typer.Option("--tag", help="repeat me")] = None,
    ) -> None: ...

    schema = serialize_options(source, command_name="source")
    assert schema is not None
    assert schema[0]["kind"] == "str_list"

    stub_app = build_stub_command("source", schema)
    param = inspect.signature(stub_app.registered_commands[0].callback).parameters["tags"]
    base, _ = _unwrap_typer_option(param.annotation)
    assert base == list[str]


def test_an_unsupported_annotation_keeps_its_option_opaque() -> None:
    """An annotation outside the supported kinds keeps its record, opaque: no flag is dropped."""
    from decimal import Decimal

    from otto.config.completion_cache import OPAQUE_KIND

    def source(
        amount: Annotated[Decimal, typer.Option("--amount")] = Decimal(0),
    ) -> None: ...

    [amount] = serialize_options(source, command_name="source")
    assert (amount["name"], amount["flags"], amount["kind"]) == (
        "amount",
        ["--amount"],
        OPAQUE_KIND,
    )


def test_stub_callback_raises_if_invoked() -> None:
    """The rebuilt callback is never meant to run — it must fail loudly if it does."""
    stub_app = build_stub_command("noop", options=[])
    callback = stub_app.registered_commands[0].callback
    with pytest.raises(RuntimeError, match="completion-stub callback was invoked"):
        callback()


def test_stub_command_name_sanitized_for_dunder() -> None:
    """Names with dashes are valid CLI names but not Python identifiers."""
    stub_app = build_stub_command("test-instruction", options=[])
    cmd = stub_app.registered_commands[0]
    assert cmd.name == "test-instruction"
    assert cmd.callback.__name__ == "test_instruction"


@pytest.mark.parametrize(
    "declared", [("--feature/--no-feature",), ("-f", "--feature/--no-feature")]
)
def test_a_repeated_switch_rebuilds_as_a_switch_that_repeats(declared: "tuple[str, ...]") -> None:
    """A ``list[bool]`` switch's row rebuilds as a flag that repeats, with every spelling.

    Click offers an option already given only when it repeats, so a stub that
    did not repeat would hide the switch after its first use.
    """
    from otto.config._typer import built_spellings, carrier
    from otto.config.completion_stubs import stub_param_from_row
    from otto.config.completion_tree import param_rows

    live_param = inspect.Parameter(
        "feature",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=Annotated[list[bool], typer.Option(*declared)],
    )
    [live] = carrier([live_param]).params
    row = param_rows([live])["feature"]
    [stub] = carrier([stub_param_from_row(row)]).params
    assert (stub.is_flag, stub.multiple) == (live.is_flag, live.multiple) == (True, True)
    assert sorted(built_spellings(stub)) == sorted(built_spellings(live))
