"""Effective-user resolution for the reservation check.

Precedence: ``--holder USERNAME`` > ``getpass.getuser()``.

There is deliberately no persistent config or environment-variable source.
A CLI flag is always visible on the command line that uses it; a sticky
env var in shell rc would be invisible and could cause a user to operate
under someone else's identity for weeks without realizing.  When
``--holder`` IS used, the top-level Typer callback prints a bold-magenta
banner so the override is impossible to miss.
"""

import getpass
from dataclasses import dataclass
from typing import Literal

IdentitySource = Literal["--holder", "$USER"]


@dataclass(frozen=True)
class ResolvedIdentity:
    """Effective reservation identity for this otto invocation.

    Attributes
    ----------
    username : str
        The username passed to the backend.
    source : Literal["--holder", "$USER"]
        Where the username came from, used for diagnostic output.
    """

    username: str
    source: IdentitySource


def resolve_username(holder: str | None) -> ResolvedIdentity:
    """Resolve the effective reservation identity.

    Parameters
    ----------
    holder : str | None
        The value of the top-level ``--holder`` Typer option.  ``None`` or
        an empty string means the option was not supplied.

    Returns
    -------
    ResolvedIdentity
        ``(username, source)`` — ``source="--holder"`` when the flag was
        used, otherwise ``source="$USER"``.
    """
    if holder:
        return ResolvedIdentity(username=holder, source="--holder")
    return ResolvedIdentity(username=getpass.getuser(), source="$USER")
