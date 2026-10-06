"""A fixture ``otto`` surface for the dump's version-invariance test (dump spec §6).

Kept as strings, not files: ``--doctest-modules`` would import a second package
named ``otto`` during collection. Each shape here is one that has differed
between minors or under hash randomisation somewhere: composite flags, a
frozenset default, Unicode defaults, both TypedDict flavours, pydantic aliases,
dataclass storage, nested classes, aliases across namespaces, ``auto()`` values
every minor computes alike.

``DRIFT_FILES`` is the counter-example: an ``auto()`` whose value differs between
minors, so no one committed dump can match them all (dump spec §6).
"""

ROOT = """\
import abc
import dataclasses
import enum
import functools
from typing import NamedTuple, Optional, Protocol, TypedDict

import pydantic
import typing_extensions

__all__ = [
    "Color", "Perm", "Level", "Point", "Pair", "Opts", "Req", "Model", "Outer",
    "PointAlias", "compute", "MAX", "Boom",
    "IntList", "MaybeInt", "IntOrNone", "Sub", "Abs", "Proto", "bound", "Text",
    "Step", "Bits",
]


class Color(enum.Enum):
    RED = 1
    GREEN = 2
    CRIMSON = 1


class Perm(enum.Flag):
    NONE = 0
    R = 1
    W = 2
    X = 4
    RW = 3


class Level(enum.IntFlag):
    LOW = 1
    HIGH = 2
    BOTH = 3
    X = 4


# auto() after ascending values: "the last plus one" (3.10) and "the highest plus
# one" (3.11+) agree, so these records are the same on every minor.
class Step(enum.Enum):
    FIRST = enum.auto()
    SECOND = enum.auto()
    TENTH = 10
    ELEVENTH = enum.auto()


class Bits(enum.Flag):
    A = enum.auto()
    B = enum.auto()
    E = 16
    F = enum.auto()


@dataclasses.dataclass
class Point:
    x: int
    y: int = 0
    label: str = "na\\u00efve \\u2603 tab\\there"
    tags: list = dataclasses.field(default_factory=list)
    _cache: dict = dataclasses.field(default_factory=dict, init=False, repr=False)

    def moved(self, dx: int, /, dy: int = 0, *, scale: float = 1.0) -> "Point":
        return self


class Pair(NamedTuple):
    left: int
    right: int = 2


class Opts(TypedDict, total=False):
    name: str
    count: int


class Req(typing_extensions.TypedDict):
    path: str
    mode: typing_extensions.NotRequired[int]


class Model(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(populate_by_name=True)

    name: str = pydantic.Field(default="x", alias="Name")
    perms: Perm = Perm.R | Perm.W | Perm.X
    level: Level = Level.BOTH
    mixed: Level = Level.LOW | Level.X
    sizes: frozenset = frozenset({3, 1, 2, -7})
    names: frozenset = frozenset({"b", "a c", "\\u00e9"})
    ratio: float = float("inf")
    tiny: float = 0.1


class Outer:
    class Inner:
        def go(self, *args, **kwargs):
            return None

    async def fetch(self, url: str, timeout: float = 1.5) -> bytes:
        return b""

    async def stream(self):
        yield b""

    @property
    def size(self) -> int:
        return 0

    @staticmethod
    def make():
        return None

    @classmethod
    def build(cls, *, colour: Color = Color.CRIMSON, perm: Perm = Perm.NONE):
        return cls()


class Boom(ValueError):
    pass


PointAlias = Point


class Sub(Point):
    pass


class Abs(abc.ABC):
    @abc.abstractmethod
    def zeta(self): ...

    @abc.abstractmethod
    def alpha(self): ...

    @abc.abstractmethod
    def mid(self): ...


class Proto(Protocol):
    def first(self) -> int: ...

    def second(self, x: int) -> str: ...


IntList = list[int]
MaybeInt = Optional[int]
IntOrNone = int | None
Text = str


def compute(a, b=(1, "two", None, 2.5), *rest, flag=Perm.R | Perm.X, **extra):
    return None


MAX = 10
bound = functools.partial(compute, 1)
"""

SUB = """\
from otto import Point as P
from otto import Perm, compute

__all__ = ["P", "Perm", "compute", "helper"]


def helper(p: P, /, *, mode: str = "fast", perm: Perm = Perm.R | Perm.X) -> None:
    return None
"""

FILES = {"otto/__init__.py": ROOT, "otto/sub.py": SUB}
NAMESPACES = ["otto", "otto.sub"]

DRIFT = """\
import enum

__all__ = ["Drift"]


class Drift(enum.Enum):
    HIGH = 5
    LOW = 1
    NEXT = enum.auto()
"""

DRIFT_FILES = {"otto/__init__.py": DRIFT}


def drift_expected(minor: int) -> str:
    """Return the dump of ``DRIFT_FILES`` on Python 3.<minor>.

    ``NEXT`` is the last value plus one on 3.10 and the highest value plus one
    from 3.11 on: the record differs, so the matrix lane cannot pass it.
    """
    following = "I:2" if minor == 10 else "I:6"
    return (
        "# api-snapshot v2\n# producer-schema 1\n"
        "name\totto:Drift\tenum\nmro\totto:Drift\t-\n"
        "enum\totto:Drift\tHIGH\tI:5\nenum\totto:Drift\tLOW\tI:1\n"
        f"enum\totto:Drift\tNEXT\t{following}\n"
    )
