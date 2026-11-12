"""How a test names the served monitor source on a write: the token ``/api/mode`` reports.

The ONE rule, for every transport (an in-loop ASGI call, a real HTTP request,
the dashboard harness): a write (any method but GET) carries
``SOURCE_TOKEN_HEADER``, by default the token ``/api/mode`` reports for the
served source, as a tab sends it; ``None`` omits the header and a string
sends that token.
"""

from collections.abc import Awaitable, Callable

MODE_PATH = "/api/mode"
"""The route that reports the served source's state, token included."""


class FromMode:
    """A write's default token: the one ``/api/mode`` reports for the served source."""


FROM_MODE = FromMode()


def mode_token(mode: object) -> str:
    """Return the served source's token from an ``/api/mode`` body, as a tab reads it."""
    assert isinstance(mode, dict), mode
    return str(mode["token"])


async def resolve_token(
    token: "str | FromMode | None",
    method: str,
    get_mode: Callable[[], Awaitable[object]],
) -> str | None:
    """Return the token a request sends: the ONE default-token rule.

    ``get_mode`` is the transport's own GET of :data:`MODE_PATH`, returning
    its JSON body; it is called only for a write that takes the default.
    """
    if not isinstance(token, FromMode):
        return token
    if method == "GET":
        return None
    return mode_token(await get_mode())
