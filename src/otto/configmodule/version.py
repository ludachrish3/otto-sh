from dataclasses import dataclass
from re import (
    compile,
)

versionRe = compile(
    r'(?P<major>\d+)\.'
    r'(?P<minor>\d+)\.'
    r'(?P<patch>\d+)'
)

@dataclass(
    init=False,
)
class Version():

    major: int
    """Product major version."""

    minor: int
    """Product minor version."""

    patch: int
    """Product patch version."""

    def __init__(self,
        version: str,
    ):

        match = versionRe.match(version)
        if match is None:
            raise ValueError(
                f'Version string "{version}" does not match the expected format'
            ) from None

        versionDict = match.groupdict()

        self.major = int(versionDict['major'])
        self.minor = int(versionDict['minor'])
        self.patch = int(versionDict['patch'])

    def __repr__(self):
        return f'{self.major}.{self.minor}.{self.patch}'
