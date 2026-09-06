"""Reference session-setup registrations (sample).

A **session setup** is a registered async callable otto runs once on every
shell session it opens — after the readiness handshake and after every
login-proxy hop — with a real :class:`~otto.host.session.HostSession` in hand.
It is where environment setup lives: exporting a variable, provisioning
through an :class:`~otto.AppShell`, or manoeuvring from the shell otto lands
in into the application the host's ``command_frame`` describes.

Register from an ``init`` module via :func:`otto.register_session_setup`
(``overwrite=True`` keeps re-registration idempotent, which is also what
keeps this doctest safe to run more than once):

>>> from otto import register_session_setup, SetupContext
>>> from otto.examples.session_setup import export_app_env, enter_python
>>> register_session_setup("examples-export-app-env", export_app_env, overwrite=True)
>>> register_session_setup("examples-enter-python", enter_python, overwrite=True)
>>> from otto.host.session_setup import SESSION_SETUPS
>>> "examples-export-app-env" in SESSION_SETUPS
True

Lab data names the hook as a string, or as a table whose ``type`` is the
name and whose other keys become ``ctx.params`` — the same idiom as
``power_control``:

>>> from otto.host.session_setup import session_setup_from_spec
>>> session_setup_from_spec({"type": "examples-export-app-env", "env": "lab"}).params
{'env': 'lab'}

A name nothing registered fails at coercion (and at lab load) with the
registered names listed, so a typo is diagnosable from the message alone:

>>> session_setup_from_spec("examples-export-app-emv")
Traceback (most recent call last):
    ...
ValueError: ...examples-export-app-emv...

``SetupContext.kind`` says which session is being set up, so work that must
happen once (provisioning) is done on the default session only:

>>> SetupContext(host_id="h", host_name="h", user="u", params={}, kind="default").kind
'default'
"""

import shlex

from otto.host.session import HostSession
from otto.host.session_setup import SetupContext


async def export_app_env(session: HostSession, ctx: SetupContext) -> None:
    """Export ``APP_ENV`` in the landed shell; single-dialect, so ``run()`` works throughout."""
    env = ctx.params.get("env", "lab")
    await session.run(f"export APP_ENV={shlex.quote(env)}")


async def enter_python(session: HostSession, ctx: SetupContext) -> None:
    """Manoeuvre from a bash landing into a ``python3`` REPL, then let frame entry confirm it.

    The host declares ``landing_frame: "bash"`` and a ``command_frame``
    registered for the REPL's dialect. ``run()`` here is bash-framed; the
    ``send``/``expect`` pair navigates; returning without ``enter_frame()``
    is fine — otto enters the target frame unconditionally afterwards.
    """
    present = (await session.run("command -v python3")).only
    if present.retcode > 0:
        raise RuntimeError(f"{ctx.host_name}: python3 is not on PATH")
    await session.send("python3 -u -i\n")
    await session.expect(r">>> \Z", timeout=ctx.params.get("start_timeout", 10.0))
