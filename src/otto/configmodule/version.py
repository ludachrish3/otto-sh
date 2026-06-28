from dataclasses import dataclass
from re import (
    compile as compile_re,
)

from typing_extensions import override

version_re = compile_re(
    r"(?P<major>\d+)\."
    r"(?P<minor>\d+)\."
    r"(?P<patch>\d+)"
)


@dataclass(
    init=False,
)
class Version:
    major: int
    """Product major version."""

    minor: int
    """Product minor version."""

    patch: int
    """Product patch version."""

    def __init__(
        self,
        version: str,
    ):

        match = version_re.match(version)
        if match is None:
            raise ValueError(
                f'Version string "{version}" does not match the expected format'
            ) from None

        version_dict = match.groupdict()

        self.major = int(version_dict["major"])
        self.minor = int(version_dict["minor"])
        self.patch = int(version_dict["patch"])

    @override
    def __repr__(self):
        return f"{self.major}.{self.minor}.{self.patch}"
