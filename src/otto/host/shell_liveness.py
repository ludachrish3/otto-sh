"""Shared shell-liveness confirmation: prove a real shell is at its prompt.

One resend-until-deadline loop reused everywhere otto must confirm a shell is
responsive — the session readiness handshake, post-timeout recovery, and the
post-login-proxy-transition resync. Each caller supplies how to *render* a probe
and how to *recognize* its reply (delegated to the ``CommandFrame`` dialect),
plus a per-probe marker source; this module owns only the timing: settle, then
resend a probe on a short interval until confirmed or an overall deadline passes.

It lives below both ``session.py`` and ``login_proxy.py`` (``session.py`` imports
``login_proxy``, so this cannot live in ``session.py`` without a cycle) and
depends only on ``command_frame`` + asyncio.
"""

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable

from .command_frame import SessionMarkers


async def confirm_live(
    send: Callable[[str], Awaitable[None]],
    expect: Callable[[re.Pattern[str], float], Awaitable[str]],
    render: Callable[[SessionMarkers], str],
    pattern: Callable[[SessionMarkers], re.Pattern[str]],
    new_markers: Callable[[], SessionMarkers],
    *,
    settle: float,
    probe_timeout: float,
    deadline: float,
) -> bool:
    """Prove a real shell is at its prompt by probing until confirmed or timed out.

    Sleeps ``settle`` (absorbing any transition tty-flush), then repeatedly mints
    markers via ``new_markers``, sends ``render(markers)``, and waits up to
    ``min(probe_timeout, remaining)`` for ``pattern(markers)`` to match. Returns
    ``True`` on the first match, ``False`` if ``deadline`` elapses first. A
    per-probe timeout is swallowed and retried; other read errors propagate.
    """
    await asyncio.sleep(settle)
    loop = asyncio.get_running_loop()
    stop = loop.time() + deadline
    while loop.time() < stop:
        markers = new_markers()
        await send(render(markers))
        remaining = stop - loop.time()
        if remaining <= 0:
            break
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await expect(pattern(markers), min(probe_timeout, remaining))
            return True
    return False
