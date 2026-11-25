"""Plain ``@options`` classes the option-value tests share; none is registered for a verb."""

from pydantic import Field

from otto import options


@options
class Acme:
    debug_level: int = 0
    other: int = 0


@options
class Install:
    variant: str = "field"
    debug_level: int = 0


@options
class Variant:
    variant: str = "field"


@options
class Needs:
    target: str


@options
class Strict:
    level: int = Field(default=1, ge=1)


@options
class Texty:
    level: str = "x"


@options
class ToolLike:
    dev: bool = True


@options
class Mine:
    dev: bool = False
