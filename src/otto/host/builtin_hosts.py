"""Built-in hosts otto injects into every lab, regardless of storage backend.

Kept import-light: the completion fast path consumes :func:`builtin_host_ids`
without paying the (heavy) ``LocalHost`` import, which happens lazily inside
:func:`make_builtin_local_host`.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .local_host import LocalHost

BUILTIN_LOCAL_HOST_ID = "local"


def builtin_host_ids() -> tuple[str, ...]:
    """Return the host IDs otto injects into every lab (for completion / enumeration)."""
    return (BUILTIN_LOCAL_HOST_ID,)


def make_builtin_local_host() -> "LocalHost":
    """Construct the built-in ``LocalHost`` (imports ``LocalHost`` lazily — it is heavy)."""
    from .local_host import LocalHost

    return LocalHost()
