"""Which primitive one command ran on: the values of ``CommandResult.route``.

:class:`ExecRoute` names each route a host's ``exec`` or ``run`` can take.
Each exec primitive records the route it is on the result it returns
(:attr:`otto.result.CommandResult.route`), and the dispatch checks that
record against the route it planned. A container's ``exec`` runs on its
parent's route, so its value is ``container/`` followed by the parent's
(:func:`container_route`), and :func:`ran_route` reads either shape back.

This module is a leaf: it imports nothing from otto, so ``otto.host.host``
and ``otto.host.session`` both import it at module level.
"""

from enum import Enum

__all__ = ["ExecRoute", "container_route", "ran_route"]


class ExecRoute(Enum):
    """Which primitive one command runs on.

    Named rather than recomputed at each reader because more than one reader
    needs the answer: the dispatch that runs the command, and
    :meth:`~otto.host.session.SessionManager.line_budget`, which has to say
    whether a command typed through ``exec`` meets a line discipline. Both
    read a host's :class:`~otto.host.session.ExecPlan`, so they cannot drift
    apart. The value a result carries is recorded by the primitive that ran.

    ``POOLED_SHELL`` and ``DEFAULT_SESSION`` type into a shell, so a command
    on them is line-edited where that shell has a terminal; the routes'
    records (:data:`~otto.host.session.EXEC_ROUTES`) declare that
    ``line_budget`` limitation. The others type into no shell of otto's:
    ``SSH_CHANNEL`` opens a bare pty-less channel, ``LOCAL_SUBPROCESS``
    spawns a process, and ``CONTAINER`` hands the command to the parent host
    as ``docker exec``.

    ``UNSUPPORTED`` and bare ``CONTAINER`` never appear as a
    :attr:`CommandResult.route <otto.result.CommandResult.route>` value: an
    unsupported term raises before anything runs, and a container reports
    ``container/<parent route>`` (:func:`container_route`).
    """

    SSH_CHANNEL = "ssh-channel"
    """A bare ssh exec channel, no pty allocated."""

    POOLED_SHELL = "pooled-shell"
    """A pooled pty-backed shell session, for telnet or ANY host whose login is proxied."""

    DEFAULT_SESSION = "default-session"
    """The host's one default session, the one ``run`` uses: a ``console``
    term, which serves a single client, or an embedded target, whose console
    has no stateless primitive."""

    LOCAL_SUBPROCESS = "local-subprocess"
    """A fresh local subprocess (``LocalHost``)."""

    CONTAINER = "container"
    """``docker exec`` dispatched through the parent host (``DockerContainerHost``)."""

    UNSUPPORTED = "unsupported"
    """A term this manager cannot exec on at all; dispatching it raises."""


def container_route(parent_route: str) -> str:
    """Return the route a container's ``exec`` reports: ``container/`` and its parent's.

    The one place that value is spelled. A container's command runs on the
    parent host's own route, so *parent_route* is what the parent's primitive
    recorded; an empty one means the parent's result came from no primitive,
    which is a bug in otto, not a condition of the lab.

    Raises:
        AssertionError: *parent_route* is empty.
    """
    if not parent_route:
        raise AssertionError(
            "a container's exec reached its parent, but the parent's result names no "
            "route: every exec primitive records the route it is, so this is a bug in otto"
        )
    return f"{ExecRoute.CONTAINER.value}/{parent_route}"


def ran_route(route: str) -> "ExecRoute | None":
    """Return the :class:`ExecRoute` a result's ``route`` value names, or ``None``.

    ``container/<parent route>`` names :attr:`ExecRoute.CONTAINER`; ``""`` and
    any value that is not a route name ``None``.
    """
    head = route.partition("/")[0]
    return next((r for r in ExecRoute if r.value == head), None)
