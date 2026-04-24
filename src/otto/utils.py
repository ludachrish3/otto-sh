import asyncio
import functools
from enum import Enum
from typing import (
    Any,
    Callable,
    Coroutine,
    Literal,
    NamedTuple,
    ParamSpec,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
)


# TODO: add more complicated tag parsing later
def splitOnCommas(
    values: list[str] | str
) -> list[str]:
    """Split a string or list of strings on commas into a flat list.

    Args:
        values: A single comma-separated string, or a list of such strings.

    Returns:
        A flat list of the individual values.

    >>> splitOnCommas("a,b,c")
    ['a', 'b', 'c']
    >>> splitOnCommas(["a,b", "c,d"])
    ['a', 'b', 'c', 'd']
    >>> splitOnCommas("single")
    ['single']
    """

    allValues: list[str] = []

    match values:
        case str():
            return values.split(",")

        case list():
            for value in values:
                newValues = splitOnCommas(value)
                allValues += newValues

            return allValues

def _get_literal_values(
    type: Any,
) -> list[TypeVar]:

    origin = get_origin(type)
    if origin is Literal:
        return list(get_args(type))
    elif origin is Union:
        values: list[TypeVar] = []
        for arg in get_args(type):
            values += _get_literal_values(arg)
        return values
    else:
        raise ValueError(f"{type} is {origin}, not a Literal or Union of Literals")

P = ParamSpec("P")
R = TypeVar("R")

def async_typer_command(f: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, R]:
    @functools.wraps(f)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return asyncio.run(f(*args, **kwargs))
    return wrapper

T = TypeVar("T")
def is_literal(value: Any, literal_type: Type[T]) -> T:
    """Raise a TypeError if value is not a valid member of the Literal type."""

    valid = _get_literal_values(literal_type)
    if value not in valid:
        raise TypeError(f"{value!r} is not a valid value. Expected one of: {valid}")
    return value

# TODO: Restructure this file into a directory names utils and then a file per group of functionality:
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

    Success  = 0
    Failed   = 1
    Error    = 2
    Unstable = 3
    Skipped  = 4

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
