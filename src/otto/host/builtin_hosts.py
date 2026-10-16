"""Built-in hosts otto injects into every lab, regardless of lab-repository backend.

Kept import-light: the completion fast path consumes :func:`builtin_host_ids`
without paying the (heavy) ``LocalHost`` import, which happens lazily inside
:func:`make_builtin_local_host`.

It also holds the one fleet-membership rule, :func:`_flag_holding_out`, which
holds the built-in host and container hosts out of a fleet walk; its host-class
imports stay inside the function.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .local_host import LocalHost

BUILTIN_LOCAL_HOST_ID = "local"


# DEBT(no-tuple-return): similar ids iterated as a group; wants a list.
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


def _flag_holding_out(
    host: object, *, include_containers: bool, include_local: bool
) -> "str | None":
    """Name the membership flag that holds *host* out of a fleet, or ``None`` for a member.

    THE one membership-flag rule. Container hosts and the built-in ``local``
    host are not fleet members unless the caller's flag admits them: a sweep
    must never silently reach the runner itself, or a container nobody asked
    for. Three readers apply the rule and must agree host by host:
    :meth:`otto.context.OttoContext.all_hosts` yields only members, its
    empty-selection prediction (``otto.context._flags_hiding_every_match``)
    names the flags that hid every match, and
    :func:`otto.config.fleet.fleet_of_interest` answers what the walk would
    yield. A second copy of the rule in any of them would drift the first
    time one of them grew a case.

    The host classes are imported here, not at module level: neither is needed
    until a walk or a query runs, and ``LocalHost`` is heavy.

    Args:
        host: One lab host, of any family.
        include_containers: The caller's container flag.
        include_local: The caller's ``local`` flag.

    Returns:
        ``"include_containers"`` or ``"include_local"`` (the flag that would
        admit *host*), or ``None`` when *host* is a fleet member as asked.
    """
    from .docker_host import DockerContainerHost
    from .local_host import LocalHost

    if not include_containers and isinstance(host, DockerContainerHost):
        return "include_containers"
    if not include_local and isinstance(host, LocalHost):
        return "include_local"
    return None
