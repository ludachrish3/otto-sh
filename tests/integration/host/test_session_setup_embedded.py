"""The raw landing on the documented Zephyr entry, over the real hop-then-telnet transport.

Loads the Getting Started example project itself — its lab data, its init
module and its ``land-on-zephyr`` hook — so the entry the docs show is the
entry that runs. Its ``test4`` hop lives in the same lab, so the transport
resolves the hop from the host's own lab back-reference.

A single-client console: only the default session is used (a named session
would open a second telnet the console refuses). What this proves: the raw
engine on real bytes — no readiness handshake before the hook, the hook's
newline and prompt wait, then frame entry in the Zephyr dialect, whose own
handshake is the only confirmation otto gets. What it does not prove, and
the spec says so: a landing state that echoes nothing; the guest is already
at its prompt.

Parametrized on the backend id rather than reading the element name inline:
every single-console protection in this directory's conftest (per-device
``xdist_group``, the cross-worker console lock, the wedged-backend fast-fail
and the wedge attribution) keys on an item's *parametrization* naming a known
backend. An unparametrized module would silently opt out of all four.
"""

import pytest

from otto.utils import Status
from tests._fixtures.gs_example import load_example_lab
from tests.conftest import _ZEPHYR_BACKEND_NE

pytestmark = [
    pytest.mark.integration,
    pytest.mark.embedded,
    pytest.mark.timeout(120),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["zephyr_lfs"])
async def test_raw_landing_reaches_the_zephyr_shell(backend):
    # The example's raw-landing entry and this directory's console protections
    # have to be talking about the same device; the backend id is what the
    # conftest sees, the element name is what the example lab publishes.
    assert _ZEPHYR_BACKEND_NE[backend] == "zephyr37_lfs"
    # Host ids are `slug(element.name)`, so `zephyr37_lfs` keys as `zephyr37-lfs`.
    host = load_example_lab("embedded").hosts["zephyr37-lfs"]
    # The path this guard exists for: otto writes nothing on landing, so the
    # hook's send/expect is the first traffic and frame entry the only confirmation.
    assert host.landing_frame is not None
    assert host.landing_frame.type_name == "raw"
    try:
        r = (await host.run("kernel version")).only
        assert r.status == Status.Success, r
        assert "Zephyr version" in r.value
        # The command landed in the Zephyr dialect, entered after the hook.
        session = host._session_mgr._session
        assert session is not None
        assert type(session._frame).type_name == "zephyr"
        # exec on an embedded host shares the persistent session; same shell,
        # so the session object must be the very one the run above used.
        ex = await host.exec("kernel uptime")
        assert ex.status == Status.Success
        assert "Uptime" in ex.value, ex  # the board answers `Uptime: <n> ms`
        assert host._session_mgr._session is session
    finally:
        await host.close()
