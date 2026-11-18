"""The one spelling of a test's session manager and session handles.

A test that builds a :class:`~otto.host.session.SessionManager`, a
:class:`~otto.host.session.HostSession` or a ``_SessionProxyIO`` directly
builds it through these, so a new required construction argument is added
here once. Each supplies the identity fold
(:func:`~otto.host.session.identity_fold`: the handle adds no standing mode)
unless the test passes a ``fold_log`` of its own.
"""

from typing import Any

from otto.host.session import HostSession, SessionManager, _SessionProxyIO, identity_fold


def session_manager(*args: Any, **kwargs: Any) -> SessionManager:
    """Return ``SessionManager(*args, **kwargs)``, the identity fold unless one is given."""
    kwargs.setdefault("fold_log", identity_fold)
    return SessionManager(*args, **kwargs)


def host_session(*args: Any, **kwargs: Any) -> HostSession:
    """Return ``HostSession(*args, **kwargs)``, the identity fold unless one is given."""
    kwargs.setdefault("fold_log", identity_fold)
    return HostSession(*args, **kwargs)


def proxy_io(*args: Any, **kwargs: Any) -> _SessionProxyIO:
    """Return ``_SessionProxyIO(*args, **kwargs)``, the identity fold unless one is given."""
    kwargs.setdefault("fold_log", identity_fold)
    return _SessionProxyIO(*args, **kwargs)
