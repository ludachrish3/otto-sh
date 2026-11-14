"""A session-setup hook registered for the life of one context, and only that context.

THE ONE register/teardown sequence for a temporary hook. A host carrying a
``session_setup`` resolves the registry entry when the hook EXECUTES
(``otto.host.session_setup``), not when the host is built, so a fixed hook
name shared by two overlapping contexts lets the first one to close remove
the entry the other's live host still runs. Each context therefore registers
under a name of its own and removes only that name.
"""

import contextlib
import uuid
from collections.abc import Iterator

from otto.host.session_setup import SESSION_SETUPS, register_session_setup


async def _noop(_session: object, _ctx: object) -> None:
    """Run nothing: the hook's presence, not its effect, is what a caller needs."""


@contextlib.contextmanager
def noop_session_setup(prefix: str) -> Iterator[str]:
    """Register a no-op session-setup hook under a fresh name; yield that name.

    The name is *prefix* plus a unique suffix, registered without
    ``overwrite`` (a clash would be a bug, so it raises), and unregistered on
    exit. Overlapping contexts never share an entry.
    """
    name = f"{prefix}-{uuid.uuid4().hex}"
    register_session_setup(name, _noop)
    try:
        yield name
    finally:
        SESSION_SETUPS.unregister(name=name)
