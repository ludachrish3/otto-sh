"""A login proxy: become another account through passwordless sudo.

The built-in ``su`` proxy asks for the target's password; on a VM whose
``vagrant`` account has passwordless sudo, ``sudo -i -u <login>`` is the
honest route. The cred names the target, so the one registration serves any
login the ``via`` account may sudo to — ``root`` is simply the one this
project declares.
"""

# doc: begin sudo-proxy
import shlex

from otto.host.login_proxy import ProxyContext, ProxyIO, register_login_proxy


async def become_root(io: ProxyIO, ctx: ProxyContext) -> None:
    """Enter a login shell as the cred being become -- no password exchanged."""
    await io.send(f"sudo -i -u {shlex.quote(ctx.target.login)}\n")


register_login_proxy("sudo-root", become_root)
# doc: end sudo-proxy
