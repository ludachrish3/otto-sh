"""Looking up the hosts an ``otto host`` invocation names.

The hop lookup lives here, and ``otto host <id> --hop <id>`` and its remote
path completion both call it, so a ``--hop`` resolves the same from either.
So do the two parse-time reads of the target: its class, which picks the verb
menu, and its help-time admission, which decides whether a help screen may
show that menu at all. Dispatch admits the target later, at its scoped lookup.

Otto internals are imported inside each function: ``otto host`` is a budgeted
import surface, and resolving its menu must not load the host stack.
"""

from typing import TYPE_CHECKING, NoReturn, cast

if TYPE_CHECKING:
    import typer

    from ..config.scope import HostOutOfScopeError
    from ..host.remote_host import RemoteHost
    from ..labs.loader import Lab


def hop_lab() -> "Lab":
    """Return the lab a ``--hop`` is looked up in: the run's whole lab, past any scope.

    A hop needs only lab membership, so this is
    :attr:`otto.context.OttoContext.unscoped`'s lab, never a scoped view.
    :func:`hop_host` searches it, and so does :func:`host_class`: a target's
    class is discovered whether or not the run may target it.

    Raises:
        RuntimeError: no context is installed.
    """
    from ..run_context import get_context

    return get_context().unscoped.lab


def hop_host(host_id: str) -> "RemoteHost":
    """Return the lab host *host_id* that a ``--hop`` names, in the run's scope or not.

    The lookup searches :func:`hop_lab`.

    Raises:
        KeyError: no host in the lab has the id; the message lists every
            host in the lab, and the error carries them.
        RuntimeError: no context is installed.
    """
    from ..host.targeting import unknown_host_error

    lab = hop_lab()
    host = lab.hosts.get(host_id)
    if host is None:
        raise unknown_host_error(host_id, lab_name=lab.name, available=list(lab.hosts))
    return cast("RemoteHost", host)


def host_class(host_id: str) -> "type | None":
    """Return the class of the lab host *host_id*, in the run's scope or not; ``None`` if none.

    Class discovery is not admission: the verb menu of a target the run may
    not reach is still its class's, so its help can name the refusal and an
    opted-out verb can still run. The lookup reads :func:`hop_lab` and builds
    nothing. No installed context, an unknown id, or any other failure is
    ``None``, except ``OptionsNotAvailableError``: a host constructor that
    read an option before the command read its flags is a defect, and is
    raised.
    """
    from ..params import OptionsNotAvailableError

    try:
        host = hop_lab().hosts.get(host_id)
    except OptionsNotAvailableError:
        raise
    except Exception:  # noqa: BLE001 — discovery is soft: no lab, no context → no class
        return None
    return None if host is None else type(host)


def help_refusal(
    ctx: "typer.Context", *, host_id: str, callback: object | None = None
) -> "HostOutOfScopeError | None":
    """Return the refusal a help screen for ``otto host <host_id>`` shows, or ``None``.

    Help is admitted at parse time, where the leaf never runs: the group's
    menu and each verb's ``--help`` ask here. *callback* is the verb's (or
    ``None`` for the group itself), and its verdict is read with the spec the
    root dispatch recorded (:func:`otto.cli.invoke.leaf_is_scoped`), so a
    verb that opts out of scoping is never refused. The refusal is the
    installed context's own ``get_host`` error.

    Returns the error only when a context is installed, :func:`host_class`
    finds the id, the verdict is scoped, and the lookup raises
    :class:`~otto.config.scope.HostOutOfScopeError`. Every other outcome is
    ``None`` (an unknown id, no lab, a project that failed to load, any other
    error), and the help screen is the one it always was.
    """
    from ..run_context import try_get_context
    from .invoke import leaf_is_scoped, recorded_spec

    context = try_get_context()
    if context is None or host_class(host_id) is None:
        return None
    if not leaf_is_scoped(recorded_spec(ctx), callback):
        return None
    from ..config.scope import HostOutOfScopeError
    from ..params import OptionsNotAvailableError

    try:
        context.get_host(host_id)
    except HostOutOfScopeError as refused:
        return refused
    except OptionsNotAvailableError:
        raise
    except Exception:  # noqa: BLE001 — help falls through: an unknown id or a load error keeps today's help
        return None
    return None


def refuse_target(refused: "HostOutOfScopeError") -> NoReturn:
    """Print *refused*, an out-of-scope target's refusal, and exit 1.

    The one render of the refusal, for the target's lookup, a verb's
    ``--help`` and the menu. Unwrapped (``soft_wrap``): the text names a
    settings path or an ``-E`` the reader copies.
    """
    from .invoke import fail

    fail(str(refused), soft_wrap=True)
