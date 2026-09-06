"""The documented session-setup hooks, on the documented lab entries, against the bed.

Loads the Getting Started example project itself — its lab data, its init
module, its hooks and its frame — so the example the page shows is the
example that is tested. Leases test1 explicitly: both hooked entries live on
that VM.

Fails LOUD on host-down: never a skip.
"""

import asyncio
import contextlib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from otto.utils import Status
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.gs_example import EXAMPLE, load_example_lab
from tests.e2e.host._pty_driver import InteractiveOttoSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(180),
    # One worker for the whole module, like every other bed module here:
    # the lane runs `-n auto --dist loadgroup`, and these tests share a
    # leased VM and finalize subprocess coverage.
    pytest.mark.xdist_group("session_setup_e2e"),
]

_MARKER = "/tmp/otto-gs-provisioned"  # the path the documented hook writes


@pytest.fixture
def test1_lease(tmp_path_factory) -> Iterator[str]:
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, ("test1",)) as element:
        yield element


async def _assert_reachable(host) -> None:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host.ip, 22), timeout=10)
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"{host.name} ({host.ip}) unreachable on :22 — bed down?: {exc!r}"
        ) from exc
    writer.close()


@pytest.mark.asyncio
async def test_single_dialect_hook_on_test1(test1_lease):
    host = load_example_lab("unix").hosts["test1"]
    await _assert_reachable(host)
    try:
        await host.run(f"rm -f {_MARKER}")
        await host.close()  # a fresh open, so the default session's hook provisions
        r = (await host.run("echo $APP_ENV")).only
        assert r.status == Status.Success
        assert r.value == "lab"
        assert (await host.run(f"cat {_MARKER}")).only.value == "ok"
        async with await host.open_session("aux") as aux:
            assert (await aux.run("echo $APP_ENV")).only.value == "lab"
        ex = await host.exec("echo $APP_ENV")
        assert ex.value == "lab"
    finally:
        # The hook provisions on every default-session open, so the bed keeps
        # the marker unless this test takes it away again. Suppressed, close
        # included: neither a cleanup failure nor a close on an already-broken
        # host may displace the assertion that failed.
        with contextlib.suppress(Exception):
            await host.run(f"rm -f {_MARKER}")
        with contextlib.suppress(Exception):
            await host.close()


@pytest.mark.asyncio
async def test_two_dialect_hook_on_test1_py(test1_lease):
    host = load_example_lab("pyrepl").hosts["test1-py"]
    await _assert_reachable(host)
    try:
        r = (await host.run("1 + 1")).only
        assert r.status == Status.Success
        assert r.value == "2"
        assert r.retcode == 0
        bad = (await host.run("1 / 0")).only
        assert bad.status == Status.Failed
        assert bad.retcode == 1
        async with await host.open_session("aux") as aux:
            assert (await aux.run("2 * 3")).only.value == "6"
        ex = await host.exec("sum(range(4))")
        assert ex.value == "6"
    finally:
        # Suppressed, like the sibling above: a close on an already-broken
        # host must not displace the assertion that failed.
        with contextlib.suppress(Exception):
            await host.close()


def test_login_lands_in_the_set_up_shell(test1_lease, tmp_path: Path) -> None:
    xdir = tmp_path / "xdir"
    with InteractiveOttoSession(
        ["-R", "-l", "unix", "host", "test1", "login"], xdir=xdir, sut_dirs=EXAMPLE
    ) as sess:
        sess.expect(b"Press Ctrl+] to disconnect", timeout=60)
        sess.drain(1.0)
        sess.sendline("echo APP=$APP_ENV")
        sess.expect(b"echo APP=$APP_ENV", timeout=15)  # echo is on: the typed line comes back first
        sess.expect(b"APP=lab", timeout=15)
        # The shell a human is handed keeps its own history: `otto login` never
        # suppresses it, and a hook on the host does not change that. Both
        # halves matter — `set +o history` would kill up-arrow recall, and a
        # neutralized HISTFILE would silently drop the session from the file.
        # Two short commands rather than one long one: a line wider than the
        # 80-column pty is REDRAWN by readline, so its echo comes back in
        # pieces and cannot be expected as typed.
        sess.sendline("echo hist=$([[ -o history ]]&&echo on)")
        sess.expect(b"[[ -o history ]]", timeout=15)  # the typed line's echo, consumed first
        sess.expect(b"hist=on", timeout=15)  # `set +o history` would print a bare `hist=`
        sess.sendline("echo hf=${HISTFILE:-unset}")
        sess.expect(b"${HISTFILE:-unset}", timeout=15)  # likewise: the echo, then the value
        histfile = re.search(rb"hf=(\S+)", sess.expect(re.compile(rb"hf=\S+"), timeout=15))[1]
        assert histfile != b"/dev/null", f"login suppressed the human's history: {histfile!r}"
        sess.disconnect()
        sess.expect(b"disconnected from", timeout=10)
        assert sess.wait(timeout=10) == 0


def test_login_lands_in_the_python_repl(test1_lease, tmp_path: Path) -> None:
    xdir = tmp_path / "xdir"
    with InteractiveOttoSession(
        ["-R", "-l", "pyrepl", "host", "test1-py", "login"], xdir=xdir, sut_dirs=EXAMPLE
    ) as sess:
        sess.expect(b"Press Ctrl+] to disconnect", timeout=60)
        sess.drain(1.0)
        sess.sendline("6 * 7")
        sess.expect(b"42", timeout=15)
        sess.expect(b">>> ", timeout=15)
        sess.sendline("exit()")
        # The shell has to EVALUATE this for the expect to match: echo is on,
        # so a literal marker would be satisfied by the tty echo of the typed
        # line whether `exit()` returned the human to bash or left them at the
        # REPL. `$((40+2))` is arithmetic no echo can perform.
        sess.sendline("echo back-in-$((40+2))")
        sess.expect(b"back-in-42", timeout=15)
        sess.disconnect()
        sess.expect(b"disconnected from", timeout=10)
        assert sess.wait(timeout=10) == 0
