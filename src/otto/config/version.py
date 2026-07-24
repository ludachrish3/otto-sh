"""Semantic version parsing for product version strings declared in settings.toml."""

from dataclasses import dataclass
from re import (
    compile as compile_re,
)

from typing_extensions import override

version_re = compile_re(
    r"(?P<major>\d+)\."
    r"(?P<minor>\d+)\."
    r"(?P<patch>\d+)"
    r"(?P<extra>[-+.][0-9A-Za-z.+-]+)?"
    r"$"
)


@dataclass(
    init=False,
)
class Version:
    """Parsed semantic version from a product version string.

    Constructed from ``"major.minor.patch"`` plus an optional extra tag
    beginning with ``-``, ``+`` or ``.`` (e.g. ``1.2.3-rc1``); ``repr``
    round-trips the full string; ordering and constraint matching use
    :attr:`key` and deliberately ignore ``extra`` (a documented limitation:
    ``1.2.3-rc1`` compares equal to ``1.2.3`` for constraint purposes —
    SemVer prerelease precedence is intentionally not implemented).

    Note the asymmetry: the dataclass-generated ``__eq__`` is *structural* and
    includes ``extra`` (``Version("1.2.3-rc1") != Version("1.2.3")``), while
    dependency constraint matching goes through :attr:`key`, which ignores it.
    """

    major: int
    """Product major version."""

    minor: int
    """Product minor version."""

    patch: int
    """Product patch version."""

    extra: str | None
    """Optional extra tag including its leading separator (``"-rc1"``), or ``None``."""

    def __init__(
        self,
        version: str,
    ) -> None:

        match = version_re.match(version)
        if match is None:
            raise ValueError(
                f'Version string "{version}" does not match the expected format'
            ) from None

        version_dict = match.groupdict()

        self.major = int(version_dict["major"])
        self.minor = int(version_dict["minor"])
        self.patch = int(version_dict["patch"])
        self.extra = version_dict["extra"]

    @override
    def __repr__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}{self.extra or ''}"

    @property
    def key(self) -> tuple[int, int, int]:
        """The comparison triple — constraint matching deliberately ignores ``extra``."""
        return (self.major, self.minor, self.patch)
