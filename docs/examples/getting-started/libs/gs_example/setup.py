"""The single-dialect session setup the customizations page walks through."""

# doc: begin provision-app
import shlex

from otto import SetupContext
from otto.examples.app_shell import PyRepl  # the REPL example the app-shell page shows
from otto.host.session import HostSession


async def provision_app(session: HostSession, ctx: SetupContext) -> None:
    """Export APP_ENV, and on the default session provision through a REPL.

    ``ctx.kind`` says which session this is: named and pooled sessions
    only inherit the environment, the default session also does the
    one-time work — here a marker file written from inside ``python3``,
    which the bed test reads back.

    The ``env`` param is optional and defaults to ``lab``, so a host that
    names the hook as a bare string still works. The value is quoted: an
    ``env`` with a space in it would otherwise export the first word and
    run the rest.
    """
    await session.run(f"export APP_ENV={shlex.quote(ctx.params.get('env', 'lab'))}")
    if ctx.kind == "default":
        async with PyRepl.attach(session) as py:
            await py.cmd("open('/tmp/otto-gs-provisioned', 'w').write('ok')")


# doc: end provision-app
