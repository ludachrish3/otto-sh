"""Built-in hosts otto injects into every lab, regardless of lab-repository backend.

Kept import-light: the completion fast path consumes :func:`builtin_host_ids`
without paying the (heavy) ``LocalHost`` import, which happens lazily inside
:func:`make_builtin_local_host`.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .local_host import LocalHost

BUILTIN_LOCAL_HOST_ID = "local"


# PERMANENT(no-tuple-return): immutable homogeneous sequence, deliberately not a list.
# ast-grep-ignore: no-tuple-return
def builtin_host_ids() -> tuple[str, ...]:
    """Return the host IDs otto injects into every lab (for completion / enumeration)."""
    return (BUILTIN_LOCAL_HOST_ID,)


def make_builtin_local_host() -> "LocalHost":
    """Construct the built-in ``LocalHost`` (imports ``LocalHost`` lazily — it is heavy)."""
    from .local_host import LocalHost

    return LocalHost()


def is_builtin_host(host: object) -> bool:
    """Report whether *host* is one otto injected itself, not a lab entry reusing the id.

    ``load_lab`` injects the built-in ``local`` host only when the lab does not
    already define one, so a lab that declares its own ``local`` entry gets an
    ORDINARY host under that id — one the user wrote down, whose
    ``resources`` a reservation reader must keep honouring. Identity, not the
    id string, is therefore the test.

    The id check comes first so the heavy ``LocalHost`` import is unreachable
    for every other host: callers run this over a whole fleet, and
    :mod:`otto.host.builtin_hosts` is on the completion fast path precisely
    because it stays import-light.
    """
    if getattr(host, "id", None) not in builtin_host_ids():
        return False
    from .local_host import LocalHost

    return isinstance(host, LocalHost)
