"""Reference login-proxy registration + :class:`~otto.Cred` usage (sample).

Some accounts can't be reached by direct authentication — the classic case is
a service user with no login shell. A :class:`~otto.Cred` entry for one of
these names a registered *login proxy*: a small async callable that drives
whatever steps *become* that user (``su``, ``sudo -u``, entering a container,
...) after otto has already authenticated as a different, directly-loginable
account (the cred's ``via``).

A ``Cred`` is a plain frozen dataclass — ``login`` plus how to become it:

>>> from otto import Cred
>>> admin = Cred(login="admin", password="hunter2")
>>> admin.login
'admin'
>>> admin.proxy is None
True
>>> appuser = Cred(
...     login="appuser", proxy="examples-docker-shell", via="admin", params={"container": "app1"}
... )
>>> appuser.proxy, appuser.via
('examples-docker-shell', 'admin')

Proxies are registered once — typically from an ``init`` module listed in
``.otto/settings.toml`` — via :func:`otto.register_login_proxy`, the same
registration idiom as term/transfer backends. Copy :func:`enter_container`
below as a starting point, or register it directly (``overwrite=True`` makes
re-registration idempotent, which is also what keeps this doctest safe to run
more than once):

>>> from otto.examples.login_proxy import enter_container
>>> from otto import register_login_proxy
>>> register_login_proxy("examples-docker-shell", enter_container, overwrite=True)
>>> from otto.host.login_proxy import LOGIN_PROXIES
>>> "examples-docker-shell" in LOGIN_PROXIES
True

A proxied cred's ``via`` names the account to authenticate as first;
:func:`~otto.host.login_proxy.resolve_chain` walks that link back to the
directly-loginable cred otto authenticates the transport as, returning it
together with the hop(s) to apply afterwards (outermost/first-run first):

>>> from otto.host.login_proxy import resolve_chain
>>> creds = [admin, appuser]
>>> direct, hops = resolve_chain(creds, "appuser")
>>> direct.login
'admin'
>>> [hop.login for hop in hops]
['appuser']
>>> hops[0].params["container"]
'app1'
"""

import shlex

from otto.host.login_proxy import ProxyContext, ProxyIO


async def enter_container(io: ProxyIO, ctx: ProxyContext) -> None:
    """Enter a named Docker container as a login-proxy step.

    ``ctx.target.params["container"]`` names the container (set on the cred
    entry's ``params``, see :class:`~otto.Cred`). No password exchange is
    needed here since ``docker exec`` runs as whichever account is already
    authenticated (``ctx.via``) — a proxy step is free to send as many or as
    few ``send``/``expect`` exchanges as becoming the target user requires.
    """
    container = shlex.quote(ctx.target.params["container"])
    await io.send(f"docker exec -it {container} sh\n")
