"""Which host a run may target, and how a miss is told.

:class:`HostScope` is the one judge of "may this run target this id": the
run context builds one per lookup, and ``get_host`` raises its refusal for an
id it does not admit. :func:`targetable_host` is the rule it applies to one
host: the built-in runner side, or a host the declared union admits.

The judge's refusal for an id no host has is :func:`unknown_host_error`, the
:class:`KeyError` that ``otto host --hop`` raises too, so the message reads the
same from both. It carries the ids the lookup could have found
(:class:`UnknownHostError`), so ``otto host`` lists the very ids its lookup
searched. A host resolving a peer (its hop, console server or power
controller) still raises its own message.

:func:`host_or_value_error` turns a lookup's miss into the :class:`ValueError`
a library function raises, the out-of-scope text kept.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from typing_extensions import final

from ..errors import _UnquotedKeyError
from ..invocation import LIBRARY_LAB_NAME

if TYPE_CHECKING:
    from ..config.scope import ProjectScope, ProjectScopeError
    from ..lab_view import LabView
    from ..labs.loader import Lab
    from .host import Host

_NO_LAB_BREADCRUMB = " — no lab is loaded; run inside 'async with otto.open_context(lab=...)'"
"""Appended for the library sentinel lab only (:data:`otto.invocation.LIBRARY_LAB_NAME`).

That lab is what ``run_tests()`` installs for a library caller with no active
context, and it never carries hosts, so every lookup in it misses. The hint
points such a caller at the real fix rather than leaving them staring at an
empty ``Available: []``. It is an addition: the message before it is unchanged.
"""


class UnknownHostError(_UnquotedKeyError):
    """A lookup named an id that no host it searched has.

    An :class:`~otto.errors.OttoError` and a :class:`KeyError`, built only by
    :func:`unknown_host_error`. ``str()`` is the message itself, without
    :class:`KeyError`'s quotes; the attributes are the facts it is built
    from, for a caller that renders the miss its own way.
    """

    def __init__(self, message: str, *, host_id: str, available: Sequence[str]) -> None:
        """Carry *message* as the error's text, and the id and candidates it names."""
        super().__init__(message)
        self.message = message
        """The text, as ``str()`` gives it."""
        self.host_id = host_id
        """The id that was looked up."""
        self.available = list(available)
        """The ids the lookup could have found, in the order it searched them."""


def unknown_host_error(
    host_id: str, *, lab_name: str, available: Sequence[str]
) -> UnknownHostError:
    """Return the :class:`KeyError` for a lookup of *host_id* that found no host.

    The message names the id, the lab and the ids that could have been named,
    sorted: ``No host 'x' in lab 'rig'. Available: ['a', 'b']``. For the
    library sentinel lab it also says that no lab is loaded and how to load one.

    Args:
        host_id: The id that was looked up.
        lab_name: The name of the lab it was looked up in.
        available: The ids the lookup could have found.
    """
    breadcrumb = _NO_LAB_BREADCRUMB if lab_name == LIBRARY_LAB_NAME else ""
    return UnknownHostError(
        f"No host {host_id!r} in lab {lab_name!r}. Available: {sorted(available)}{breadcrumb}",
        host_id=host_id,
        available=available,
    )


def targetable_host(
    host: object, scopes: "dict[str, ProjectScope]", *, exclude_projects: Sequence[str]
) -> bool:
    """Report whether a scoped run may target *host*.

    The built-in runner and the containers on it always may (they are judged
    by identity, :func:`otto.host.builtin_hosts.is_runner_side`); any other host
    may when the declared union admits it
    (:func:`otto.config.scope.union_admits`), which is every host when no
    project declared a ``[project]`` scope.

    Args:
        host: One lab host, of any family; a container is judged by its parent.
        scopes: The run's scope verdicts.
        exclude_projects: The run's ``--exclude-projects`` names, as typed.

    Returns:
        True when the run may target *host*.
    """
    from ..config.scope import union_admits
    from .builtin_hosts import is_runner_side

    return is_runner_side(host) or union_admits(host, scopes, exclude_projects=exclude_projects)


def host_or_value_error(lab: "LabView", host_id: str, *, unknown_message: str) -> "Host":
    """Return ``lab.hosts[host_id]``, turning a miss into a :class:`ValueError`.

    A host that is in the lab but outside the run's scope keeps its refusal's
    text, which names the fix; any other miss raises *unknown_message*.

    Args:
        lab: The lab, or a scoped view of one, to look the host up in.
        host_id: The id to look up.
        unknown_message: The message for an id the lookup does not hold.

    Raises:
        ValueError: The lookup missed.
    """
    from ..config.scope import HostOutOfScopeError

    try:
        return lab.hosts[host_id]
    except HostOutOfScopeError as refused:
        raise ValueError(str(refused)) from refused
    except KeyError as missing:
        raise ValueError(unknown_message) from missing


@final
@dataclass(frozen=True, kw_only=True)
class ScopeVerdicts:
    """What a scope judge reads to judge a host: the projects' verdicts and the run's ``-E``."""

    scopes: "dict[str, ProjectScope]"
    """The projects' scope verdicts for the lab, keyed by project name."""

    exclude_projects: Sequence[str]
    """The run's ``--exclude-projects`` names, as typed."""


@final
@dataclass(frozen=True, kw_only=True)
class HostScope:
    """The one judge of which lab hosts a run may target, and of how a refusal reads.

    Built per call from a lab and its scope verdicts. Judging one id
    (:meth:`admits`) reads that one host; only :meth:`targetable_ids` and
    :meth:`refusal` read the whole lab, and a refusal is built only to be
    raised. An unscoped judge admits every lab host and never calls
    :attr:`verdicts`, so it never resolves a scope or discovers a project.
    """

    lab: "Lab"
    """The canonical lab the hosts are looked up in."""

    scoped: bool
    """Whether the run's command is scoped; ``False`` admits every lab host."""

    verdicts: "Callable[[], ScopeVerdicts]"
    """Returns the run's verdicts; called only by a scoped judgment or a diagnostic."""

    empty_refusal: "Callable[[], ProjectScopeError | None] | None" = None
    """Returns the run's empty-scope refusal, or ``None`` when hosts are in play.

    ``None`` here skips that check, for a judge with no run behind it."""

    def admits(self, host_id: str) -> bool:
        """Report whether the run may target *host_id*: a lab host, in scope when scoped."""
        host = self.lab.hosts.get(host_id)
        if host is None:
            return False
        if not self.scoped:
            return True
        verdicts = self.verdicts()
        return targetable_host(host, verdicts.scopes, exclude_projects=verdicts.exclude_projects)

    def targetable_ids(self) -> list[str]:
        """Return the ids the run may target, in the lab's order."""
        hosts = self.lab.hosts
        if not self.scoped:
            return list(hosts)
        verdicts = self.verdicts()
        return [
            host_id
            for host_id, host in hosts.items()
            if targetable_host(host, verdicts.scopes, exclude_projects=verdicts.exclude_projects)
        ]

    def refusal(self, host_id: str) -> KeyError:
        """Return the error a lookup of *host_id* raises, for an id :meth:`admits` refused.

        An id the lab does not hold gets :func:`unknown_host_error`, listing
        the targetable ids. A lab host outside the scope gets
        :class:`~otto.config.scope.HostOutOfScopeError`, naming why and the
        fix; when :attr:`empty_refusal` is given and returns a refusal (no
        host is in play), its text is used instead, which names every
        declaration.

        Raises:
            ValueError: *host_id* is a host the run may target, so there is
                nothing to refuse.
        """
        host = self.lab.hosts.get(host_id)
        if host is None:
            return unknown_host_error(
                host_id, lab_name=self.lab.name, available=self.targetable_ids()
            )
        from ..config.scope import HostOutOfScopeError, host_outside, outside_message

        nothing_to_refuse = f"host {host_id!r} is one this run may target; nothing to refuse"
        if not self.scoped:
            raise ValueError(nothing_to_refuse)
        verdicts = self.verdicts()
        exclude = verdicts.exclude_projects
        outside = (
            None
            if targetable_host(host, verdicts.scopes, exclude_projects=exclude)
            else host_outside(
                host, verdicts.scopes, lab_name=self.lab.name, exclude_projects=exclude
            )
        )
        if outside is None:
            raise ValueError(nothing_to_refuse)
        empty = self.empty_refusal() if self.empty_refusal is not None else None
        if empty is not None:
            return HostOutOfScopeError(str(empty), outside=outside)
        return HostOutOfScopeError(outside_message(outside), outside=outside)

    def settings_files(self) -> list[str]:
        """Return the ``settings.toml`` files whose patterns would widen the run's scope.

        An unscoped judge has no scope to widen: it returns an empty list and
        resolves nothing.
        """
        if not self.scoped:
            return []
        from ..config.scope import settings_files as files_to_widen

        verdicts = self.verdicts()
        return files_to_widen(verdicts.scopes, exclude_projects=verdicts.exclude_projects)
