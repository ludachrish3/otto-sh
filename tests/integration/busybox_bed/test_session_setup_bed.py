"""A hook that runs `exec ash` from the landing shell needs no landing_frame.

The bash and ash frames render the same bytes, so the post-hook frame entry
(a second ash handshake) confirms the exec'd shell exactly as it confirmed
the landing one -- the same-bytes claim in the spec, on real bytes.

What this leg discriminates is that the `exec` actually HAPPENED, which the
shell's name cannot show: `$0` on a BusyBox guest reads `ash` (or `-ash`)
both before and after `exec ash`. So the hook sets an unexported MARK beside
the exported FROM_HOOK, and the session otto hands back is asked for both:
an exported variable survives an exec'd shell, an unexported one cannot.
FROM_HOOK=1 with MARK unset is a shell that replaced the landing one and
inherited its environment -- and no other outcome produces that pair.
"""

import pytest

from otto.host.session_setup import SESSION_SETUPS, register_session_setup
from otto.utils import Status
from tests.integration.busybox_bed.conftest import _build_guest, _require_guest

pytestmark = [pytest.mark.asyncio]


async def _exec_ash(session, ctx) -> None:
    await session.run("export FROM_HOOK=1")
    # NOT exported: a fresh shell started by `exec` keeps the environment and
    # drops everything else, so this is what tells the two shells apart.
    await session.run("MARK=1")
    await session.send("exec ash\n")


@pytest.fixture
def hook():
    register_session_setup("bed-exec-ash", _exec_ash, overwrite=True)
    yield "bed-exec-ash"
    SESSION_SETUPS.unregister("bed-exec-ash")


async def test_exec_ash_from_the_landing_shell(hook):
    # The bed conftest's own builder: it is what the rework's factory boundary
    # is written against, so this test never restates the factory call.
    _require_guest("bb1350")
    host, _version = _build_guest("bb1350", session_setup=hook)
    try:
        r = (await host.run("echo ${FROM_HOOK:-unset} ${MARK:-unset} $0")).only
        assert r.status == Status.Success
        fields = r.value.split()
        assert len(fields) == 3, f"expected three fields, got {r.value!r}"
        from_hook, mark, shell = fields
        assert from_hook == "1", f"the hook's exported variable did not survive: {r.value!r}"
        assert mark == "unset", f"this is the landing shell, not an exec'd one: {r.value!r}"
        assert shell.endswith("ash"), f"the frame is answering some other shell: {r.value!r}"
    finally:
        await host.close()
