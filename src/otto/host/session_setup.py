"""Session setup hooks: registered code between the landing shell and hand-over.

A host entry may name a **session setup**: an async callable that runs once
on every shell session otto opens, after the landing handshake and after
every login-proxy hop, with a real :class:`~otto.host.session.HostSession` in
hand. It can ``run()`` framed commands in the landing dialect, ``send`` /
``expect`` raw bytes to manoeuvre into an application, attach an
:class:`~otto.host.app_shell.AppShell`, and finally enter the host's
``command_frame`` — or leave that to otto, which enters it unconditionally
after the hook returns and so confirms the shell before anyone gets it.

Registration mirrors the login-proxy and command-frame registries: call
:func:`register_session_setup` from an ``init`` module, then name the hook
in lab data as ``"session_setup": "<name>"`` or as a table
``{"type": "<name>", ...params}``, exactly like ``power_control``.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ..registry import Registry, caller_module
from .command_frame import CommandFrame

# The error MOVED to `errors` -- `session.py` names it in an `except` arm at
# module level and must not import THIS module there (it is off the CLI startup
# import graph; see `SessionManager._apply_session_setup`). Re-exported so
# `from otto.host.session_setup import SessionSetupError` keeps working.
from .errors import SessionSetupError as SessionSetupError  # noqa: PLC0414 — explicit re-export

if TYPE_CHECKING:
    from .session import HostSession, ShellSession

SetupKind = Literal["default", "named", "exec_pool", "bridge"]
"""Which session is being set up. A hook that provisions something once (a
database) does it on ``"default"`` and only *enters* on the others."""


@dataclass(frozen=True)
class SetupContext:
    """Everything a hook may need besides the session itself.

    Deliberately NOT the host object: ``host.run()`` re-enters the session
    lock that is held while the hook runs and would deadlock. Host-specific
    data rides in :attr:`params`.
    """

    host_id: str
    host_name: str
    user: str
    """Identity the session is in when the hook starts (``""`` when the family
    tracks none). The hook must not change it; becoming someone is a login
    proxy's job."""
    params: dict[str, Any]
    kind: SetupKind


SessionSetupFn = Callable[["HostSession", SetupContext], Awaitable[None]]
"""``async def setup(session, ctx) -> None``."""


@dataclass(frozen=True)
class SessionSetup:
    """The runtime value a host carries: which hook, with which params."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)


SESSION_SETUPS: Registry[SessionSetupFn] = Registry(
    "session setup", register_hint="otto.register_session_setup()"
)


def register_session_setup(name: str, fn: SessionSetupFn, *, overwrite: bool = False) -> None:
    """Register *fn* under *name*; lab data selects it by that name."""
    SESSION_SETUPS.register(name, fn, overwrite=overwrite, origin=caller_module())


def session_setup_from_spec(value: Any) -> SessionSetup | None:
    """Coerce a lab-data value into a :class:`SessionSetup`.

    Accepts ``None`` / an existing instance (pass-through, unchecked — an
    already-built :class:`SessionSetup` is trusted as-is), a ``str`` (a
    hook name with no params), or a ``dict`` with a ``type`` key whose
    remaining keys become the params — the ``power_control`` idiom. A name
    arriving as a string or inside a table is checked against the registry
    here as well as at lab load, so a directly-constructed host fails at
    construction, not at first command.
    """
    if value is None or isinstance(value, SessionSetup):
        return value
    if isinstance(value, str):
        SESSION_SETUPS.get(value)
        return SessionSetup(name=value)
    if isinstance(value, dict):
        cfg = dict(value)
        name = cfg.pop("type", None)
        if not isinstance(name, str):
            raise ValueError(  # noqa: TRY004 — existing API contract; test suite expects ValueError
                f"session_setup table must carry a string 'type' naming a registered "
                f"session setup; got {value!r}"
            )
        SESSION_SETUPS.get(name)
        return SessionSetup(name=name, params=cfg)
    raise ValueError(f"cannot build a SessionSetup from {value!r}")


async def apply_session_setup(
    session: "ShellSession",
    handle: "HostSession",
    ctx: SetupContext,
    setup: SessionSetup,
    target_frame: CommandFrame,
) -> None:
    """Run *setup*'s hook over *handle*, then enter *target_frame* on *session*.

    The ONE place the two failure shapes are wrapped, shared by the
    SessionManager and the login bridge: the hook raising (any exception →
    ``SessionSetupError(... failed: ...)``), and frame entry failing (the
    ``ConnectionError`` from a handshake that never confirmed → ``... left
    no shell that answers frame ...``). Wrapping here, inside the step,
    matters: ``SessionManager._ensure_session`` retries a bare
    ``ConnectionError`` as a transport race, and a hook failure is not one.
    :class:`~otto.host.errors.RawLandingError` propagates as itself — it is
    the hook author's bug, named for what it is.
    """
    from .errors import RawLandingError

    fn = SESSION_SETUPS.get(setup.name)
    try:
        await fn(handle, ctx)
    except (RawLandingError, SessionSetupError):
        raise
    except Exception as exc:
        raise SessionSetupError(
            f"{ctx.host_id}: session setup {setup.name!r} failed: {exc}"
        ) from exc
    try:
        await session.enter_frame(target_frame)
    except SessionSetupError:
        raise
    except ConnectionError as exc:
        raise SessionSetupError(
            f"{ctx.host_id}: session setup {setup.name!r} left no shell that answers "
            f"frame {target_frame.type_name!r}"
        ) from exc
