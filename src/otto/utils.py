"""Shared utilities: status enums, CLI overlay sentinels, and async helpers."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
    Literal,
    NamedTuple,
    ParamSpec,
    TypeVar,
    Union,
    get_args,
    get_origin,
)


# TODO: add more complicated tag parsing later
def split_on_commas(values: list[str] | str) -> list[str]:
    """Split a string or list of strings on commas into a flat list.

    Args:
        values: A single comma-separated string, or a list of such strings.

    Returns:
        A flat list of the individual values.

    >>> split_on_commas("a,b,c")
    ['a', 'b', 'c']
    >>> split_on_commas(["a,b", "c,d"])
    ['a', 'b', 'c', 'd']
    >>> split_on_commas("single")
    ['single']
    """
    all_values: list[str] = []

    match values:
        case str():
            return values.split(",")

        case list():
            for value in values:
                new_values = split_on_commas(value)
                all_values += new_values

            return all_values


def _get_literal_values(
    type_: Any,
) -> list[TypeVar]:

    origin = get_origin(type_)
    if origin is Literal:
        return list(get_args(type_))
    if origin is Union:
        values: list[TypeVar] = []
        for arg in get_args(type_):
            values += _get_literal_values(arg)
        return values
    raise ValueError(f"{type_} is {origin}, not a Literal or Union of Literals")


P = ParamSpec("P")
R = TypeVar("R")


def async_typer_command(f: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, R]:
    """Wrap an async Typer command so it runs under the active ``OttoContext`` scope.

    Calls ``asyncio.run`` on the coroutine. If an ``OttoContext`` is already open
    (i.e. ``try_get_context()`` returns one), the coroutine runs inside its async
    context manager scope; otherwise it runs bare.
    """

    @functools.wraps(f)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        from .context import try_get_context

        async def _run() -> R:
            ctx = try_get_context()
            if ctx is None:
                return await f(*args, **kwargs)
            async with ctx.scope:
                return await f(*args, **kwargs)

        return asyncio.run(_run())

    return wrapper


@dataclass(frozen=True)
class Arg:
    """CLI overlay: force a parameter to a positional argument.

    ``variadic=True`` makes it a space-separated list of ``elem_type`` (used for
    Python-union list params Typer can't read, e.g. ``str | Sequence[...]``).
    ``elem_type`` also overrides the CLI type for a scalar union. Imports no typer.
    """

    variadic: bool = False
    elem_type: type | None = None
    name: str | None = None
    help: str | None = None


@dataclass(frozen=True)
class Opt:
    """CLI overlay: force a parameter to a ``--option``. Imports no typer."""

    elem_type: type | None = None
    name: str | None = None
    help: str | None = None


class _Exclude:
    """Sentinel: drop a parameter from the CLI (filled with its default)."""

    __slots__ = ()


Exclude = _Exclude()


def cli_exposed(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    help_: str | None = None,
    success: str | None = None,
    output_dir: bool = True,
) -> Callable[..., Any]:
    """Mark a host coroutine method for auto-exposure as an ``otto host`` subcommand.

    ``name`` defaults to the method name with underscores dashed.
    ``success`` is an optional message printed on a successful ``(Status, "")``
    result (e.g. "Transfer complete.").
    ``output_dir=False`` marks a read-only verb that creates no per-invocation
    output directory (e.g. ``exists``/``lsmod``); the default ``True`` keeps one.

    Usable bare (``@cli_exposed``) or called (``@cli_exposed(name=..., ...)``).
    """

    def deco(f: Callable[..., Any]) -> Callable[..., Any]:
        f.__cli_exposed__ = True  # ty: ignore[unresolved-attribute]
        f.__cli_name__ = name or f.__name__.replace("_", "-")  # ty: ignore[unresolved-attribute]
        f.__cli_help__ = help_  # ty: ignore[unresolved-attribute]
        f.__cli_success__ = success  # ty: ignore[unresolved-attribute]
        f.__cli_output_dir__ = output_dir  # ty: ignore[unresolved-attribute]
        return f

    return deco(fn) if fn is not None else deco


T = TypeVar("T")


def is_literal(value: Any, literal_type: type[T]) -> T:
    """Raise a TypeError if value is not a valid member of the Literal type."""
    valid = _get_literal_values(literal_type)
    if value not in valid:
        raise TypeError(f"{value!r} is not a valid value. Expected one of: {valid}")
    return value


# TODO: Restructure this file into a directory names utils and then a file per group of functionality:  # noqa: E501 — TODO comment
# status for the below status enums
# types for the above str split on commas
class Status(Enum):
    """General status enum for commands and tests.

    >>> Status.Success
    <Status.Success: 0>
    >>> Status.Failed
    <Status.Failed: 1>
    >>> Status(0) is Status.Success
    True
    """

    Success = 0
    Failed = 1
    Error = 2
    Unstable = 3
    Skipped = 4

    @property
    def is_ok(self) -> bool:
        """True for statuses that should be treated as passing (Success, Skipped)."""
        return self in (Status.Success, Status.Skipped)


class CommandStatus(NamedTuple):
    """Result of a command execution on a host.

    >>> result = CommandStatus("echo hi", "hi", Status.Success, 0)
    >>> result.status
    <Status.Success: 0>
    >>> result.retcode
    0
    """

    command: str
    """Command that was issued"""

    output: str
    "Command output"

    status: Status
    """Command status"""

    retcode: int
    """Command shell retcode"""
