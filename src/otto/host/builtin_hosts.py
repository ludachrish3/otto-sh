"""Built-in hosts otto injects into every lab, regardless of lab-repository backend.

Kept import-light: the completion fast path consumes :func:`builtin_host_ids`
without paying the (heavy) ``LocalHost`` import, which happens lazily inside
:func:`make_builtin_local_host`.

It also holds the one fleet-membership rule, :func:`_flag_holding_out`, which
holds the runner side and container hosts out of a fleet walk; its host-class
imports stay inside the function. And it holds the one runner-by-identity
decision, :func:`is_runner_side`, with the readers built on it:
:func:`builtin_ids`; :func:`lab_scopes`, the one way a lab's scope verdicts
are resolved; :func:`admitted_ids`, the one way a walk's admitted set is
computed; :func:`without_runner_side`, which turns an admitted set into
the hosts in play; and :func:`require_nonempty_in_play`, which refuses a walk
that leaves none.
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.project import Project
    from ..config.scope import ProjectScope
    from .host import Host
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


def is_runner_side(host: object) -> bool:
    """Report whether *host* is the built-in runner, or a container on it.

    The one runner-by-identity decision. A container is judged by its parent
    (:func:`otto.config.scope.judged_host`), so the runner and every
    container it runs answer alike, through :func:`is_builtin_host`'s
    identity test rather than the id ``local``: a lab entry that reuses the id
    is an ordinary host, and so are the containers on it.

    Args:
        host: One lab host, of any family.

    Returns:
        True for the built-in runner and the containers whose parent it is.
    """
    from ..config.scope import judged_host

    return is_builtin_host(judged_host(host))


def builtin_ids(hosts: Mapping[str, object]) -> frozenset[str]:
    """Return the ids in *hosts* that are on the runner side (:func:`is_runner_side`).

    Read live, so a container started on the runner after the lab loaded is
    in the answer of the next call.

    Args:
        hosts: A lab's ``{id: host}`` mapping, as it stands at the call.

    Returns:
        The ids of the built-in runner and its containers; empty when the lab
        holds neither.
    """
    return frozenset(host_id for host_id, host in hosts.items() if is_runner_side(host))


def lab_scopes(
    projects: "Sequence[Project]",
    *,
    component_names: Sequence[str],
    hosts: "Mapping[str, Host]",
) -> "dict[str, ProjectScope]":
    """Resolve each project's scope verdict over one lab; the runner side is in no universe.

    The one way this lab's verdicts are resolved: a run context and the pure
    fleet query read identical data because both call it. The runner and its
    containers (:func:`builtin_ids`) are kept out of every universe by
    identity, never by the id ``local`` nor by the runner's stamped
    ``source_lab``, which is whichever ``-l`` component came first. So the
    component order never changes a universe, and a lab entry that reuses the
    id ``local`` is scoped like any other host.

    Args:
        projects: Every parsed project (order is kept in the result).
        component_names: The loaded lab's component labs, never the composite name.
        hosts: The loaded lab's ``{id: host}`` mapping.

    Returns:
        ``{project name: ProjectScope}``.
    """
    from ..config.scope import resolve_scopes

    return resolve_scopes(
        list(projects), list(component_names), hosts, exclude_ids=builtin_ids(hosts)
    )


def admitted_ids(
    hosts: "Mapping[str, Host]",
    scopes: "dict[str, ProjectScope]",
    owner: "str | None",
    *,
    exclude_projects: Sequence[str],
) -> set[str]:
    """Return the ids a fleet walk may reach: the scope's admission, the runner side by identity.

    The one way a walk's admitted set is computed, so the run context's base
    set (``OttoContext.admissible_ids``) and the pure query
    (:func:`otto.fleet.fleet_of_interest`) cannot disagree. The built-in
    runner and its containers (:func:`builtin_ids`, read live) are admitted
    whatever any project's ``host_patterns`` say and whatever the ``-l``
    component order stamped on them; whether a walk wants them stays its own
    ``include_local`` choice (:func:`_flag_holding_out`).

    Args:
        hosts: The lab's live ``{id: host}`` mapping.
        scopes: The run's scope verdicts (:func:`lab_scopes`).
        owner: ``Project.name`` bounding the walk, or ``None`` for the union.
        exclude_projects: The run's ``--exclude-projects`` names, as typed.

    Returns:
        The admitted ids, the runner side included.

    Raises:
        otto.bootstrap.ProjectScopeError: *owner* names no resolved project.
    """
    from ..config.scope import scoped_ids

    return scoped_ids(
        hosts,
        scopes,
        owner,
        exclude_projects=tuple(exclude_projects),
        runner_ids=builtin_ids(hosts),
    )


def without_runner_side(ids: set[str], hosts: Mapping[str, object]) -> set[str]:
    """Remove the runner side (:func:`is_runner_side`) from an admitted set already computed.

    What turns a walk's admitted set into the hosts in play: otto can always
    run on the machine it runs on, so neither the runner nor a container on
    it is a host a reservation covers or a declared scope counts. An id
    *hosts* does not hold is kept, judged as no runner.

    Args:
        ids: An admitted set, e.g. :func:`admitted_ids`' answer.
        hosts: The lab's ``{id: host}`` mapping the set was computed over.

    Returns:
        A new set: *ids* less the runner and its containers.
    """
    return {host_id for host_id in ids if not is_runner_side(hosts.get(host_id))}


def require_nonempty_in_play(
    admitted: set[str],
    *,
    hosts: Mapping[str, object],
    scopes: "dict[str, ProjectScope]",
    owner: "str | None",
    exclude_projects: Sequence[str],
) -> None:
    """Refuse a walk whose admitted set puts no host in play while some project declared a scope.

    The one in-play-then-refuse sequence: *admitted* less the runner side
    (:func:`without_runner_side`), judged by
    :func:`otto.config.scope.require_nonempty_fleet`. The run context's
    refusing walks (``OttoContext.admissible_ids``) and the pure fleet query
    (:func:`otto.fleet.fleet_of_interest`) both call it, so the query
    answers ``[]`` exactly where the walk refuses.

    Args:
        admitted: The walk's admitted set (:func:`admitted_ids`' answer),
            computed by the caller once.
        hosts: The lab's ``{id: host}`` mapping the set was computed over.
        scopes: The run's scope verdicts (:func:`lab_scopes`).
        owner: ``Project.name`` bounding the walk, or ``None`` for the union.
        exclude_projects: The run's ``--exclude-projects`` names, as typed.

    Raises:
        otto.bootstrap.ProjectScopeError: Nothing is in play and some project
            declared a ``[project]`` scope.
    """
    from ..config.scope import require_nonempty_fleet

    require_nonempty_fleet(
        scopes,
        without_runner_side(admitted, hosts),
        owner,
        exclude_projects=tuple(exclude_projects),
    )


def _owns_on(host: object, owner: "str | None") -> bool:
    """Whether *owner* owns a product or dev tool on *host* (``False`` for no owner)."""
    if owner is None:
        return False
    attachments = [*getattr(host, "products", []), *getattr(host, "dev_tools", [])]
    return any(getattr(item, "owner", None) == owner for item in attachments)


def _flag_holding_out(
    host: object, *, include_containers: bool, include_local: bool, owner: "str | None" = None
) -> "str | None":
    """Name the membership flag that holds *host* out of a fleet, or ``None`` for a member.

    THE one membership-flag rule. Container hosts and the runner side (the
    built-in runner and the containers on it, :func:`is_runner_side`) are not
    fleet members unless the caller's flag admits them: a sweep must never
    silently reach the runner, or a container nobody asked for. The container
    flag is checked first, so a container on the runner needs both flags. One
    exception, also part of the rule: a walk scoped to a project (*owner*)
    admits the containers that project owns a product or dev tool on -- its
    own containers are its fleet (#676) -- though one on the runner still
    needs *include_local*. Three readers apply the rule
    and must agree host by host:
    :meth:`otto.context.OttoContext.all_hosts` yields only members, its
    empty-selection prediction (``otto.run_context._flags_hiding_every_match``)
    names the flags that hid every match, and
    :func:`otto.fleet.fleet_of_interest` answers what the walk would
    yield. A second copy of the rule in any of them would drift the first
    time one of them grew a case.

    The container class is imported here, not at module level: it is not
    needed until a walk or a query runs.

    Args:
        host: One lab host, of any family.
        include_containers: The caller's container flag.
        include_local: The caller's runner flag.
        owner: The project the walk is scoped to, or ``None`` for an unscoped walk.

    Returns:
        ``"include_containers"`` or ``"include_local"`` (the flag that would
        admit *host*), or ``None`` when *host* is a fleet member as asked.
    """
    from .docker_host import DockerContainerHost

    if (
        not include_containers
        and isinstance(host, DockerContainerHost)
        and not _owns_on(host, owner)
    ):
        return "include_containers"
    if not include_local and is_runner_side(host):
        return "include_local"
    return None
