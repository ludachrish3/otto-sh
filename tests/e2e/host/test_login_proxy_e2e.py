"""End-to-end tests for login proxies against the live mysql-provisioned Unix bed.

The three Unix test VMs (carrot/tomato/pepper) each carry a Unix ``mysql``
account with a restricted shell (``/bin/false``) and ``DenyUsers mysql`` in
``sshd_config`` — direct SSH as ``mysql`` must fail. The only way to *become*
``mysql`` is a root-mediated ``sudo su -s /bin/bash mysql``: ``vagrant`` has
passwordless sudo, and util-linux's ``su -s`` is silently ignored for a
non-root caller against a restricted-shell target, so the switch must go
through sudo. This module proves the login-proxy machinery (Tasks 1-9) does
exactly that, end to end, against the real bed.

Containment
-----------
``tests/_fixtures/lab_data/tech1/hosts.json`` (the "veggies" lab) is loaded
directly by several *unit* tests that do not register any login proxies —
``CredSpec`` validates a cred's ``proxy`` name against the ``LOGIN_PROXIES``
registry at ingest, so adding a proxy-referencing cred to that shared file
would break every context that loads it without the proxy registered. This
module therefore never touches shared lab data:

- The ``sudo-su-shell`` proxy is registered at MODULE scope below
  (``overwrite=True`` so re-import under xdist is idempotent).
- Tests 1-5 build hosts from INLINE dicts via
  :func:`otto.storage.factory.create_host_from_dict`, reading only the
  leased VM's IP read-only from ``tech1/hosts.json`` (via
  ``tests._fixtures.labdata.host_data``) — never its ``creds``.
- Test 6 (the ``interact --as-user`` bridge) drives a real ``otto``
  subprocess, which needs its OWN registration (it never imports this test
  module) — it scaffolds a throwaway, fully self-contained SUT directory
  under ``tmp_path`` with its own init module and its own single-host
  ``hosts.json``, again built from the leased IP.

Zero shared-file mutation; zero blast radius on any other test.

xdist group
-----------
All tests are pinned to ``login_proxy_e2e`` to serialize subprocess-coverage
finalisation from a single worker (matching the convention in
``test_host_transfer_e2e.py`` / ``test_host_priv_modules_e2e.py``), and per
the dev-VM load rule this module is intended to be run as a single pass
(``-p no:xdist`` or the default), not under an xdist storm.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import asyncssh
import pytest

from otto import register_login_proxy
from otto.storage.factory import create_host_from_dict
from otto.utils import Status
from tests._fixtures._host_pool import UNIX_POOL as _UNIX_POOL
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import host_data
from tests.e2e.host._pty_driver import InteractiveOttoSession

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("login_proxy_e2e")]


# ---------------------------------------------------------------------------
# Module-scope login-proxy registration (containment: see module docstring)
#
# ``_wait_for_shell_ready`` post-transition sync: a ``su``/``sudo`` switch is
# a foreground-process handoff on the pty, and real hardware exhibits a
# narrow but very real race there — bytes written the instant control
# changes hands can be silently dropped (su/login/sudo traditionally flush
# pending terminal input across a privilege boundary as a defense against
# typeahead attacks). A naive fire-and-forget ``await io.send(...)`` proxy
# (matching the task brief's illustrative snippet) reproduced a 100%
# reliable hang (0/8 trials) on the very next sentinel-wrapped
# ``host.run()`` after the switch/undo — confirmed against the live bed with
# both this custom proxy AND the built-in ``"su"`` proxy, so the gap is in
# the shared no-sync-after-transition contract, not this proxy specifically.
# Resyncing with a fresh, unique marker (retried a few times with a bounded
# per-attempt wait — mirroring the connection-level READY handshake in
# ``otto.host.session``) made it 8/8 reliable. Scoped to this test module's
# own proxy (via the public ``undo=`` extension point) rather than patching
# the shared built-in ``"su"`` proxy / default ``"exit"`` undo in
# ``otto.host.login_proxy`` — that shared code has wide blast radius (used by
# ``switch_user``/``as_user`` generally) and deserves its own properly
# reviewed fix + regression coverage; flagged in the task report as a
# follow-up finding.
# ---------------------------------------------------------------------------

_READY_MARKER_ATTEMPTS = 5
_READY_MARKER_TIMEOUT = 2.0


async def _wait_for_shell_ready(io) -> None:
    """Resync with the shell after a su/sudo transition before returning control.

    See the module-scope comment above for why this is needed. Raises
    ``TimeoutError`` (wrapped by ``otto.host.login_proxy`` into a
    ``LoginProxyError`` with hop context) if the shell never resyncs.
    """
    for _ in range(_READY_MARKER_ATTEMPTS):
        marker = f"__OTTO_LP_READY_{uuid.uuid4().hex}__"
        await io.send(f"echo {marker}\n")
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await io.expect(marker, timeout=_READY_MARKER_TIMEOUT)
            return
    raise TimeoutError("shell did not resync after su/sudo transition")


async def _sudo_su_shell(io, ctx):
    # Root-mediated: non-root `su -s` is silently ignored for restricted-shell
    # targets (util-linux). vagrant is passwordless sudo on the test VMs.
    await io.send(f"sudo su -s /bin/bash {ctx.target.login}\n")
    await _wait_for_shell_ready(io)


async def _sudo_su_shell_undo(io, ctx):
    await io.send("exit\n")
    await _wait_for_shell_ready(io)


register_login_proxy("sudo-su-shell", _sudo_su_shell, undo=_sudo_su_shell_undo, overwrite=True)


# ---------------------------------------------------------------------------
# Inline host-dict builder (never touches tech1/hosts.json's creds)
# ---------------------------------------------------------------------------

_MYSQL_CREDS: list[dict[str, str]] = [
    {"login": "vagrant", "password": "vagrant"},
    {"login": "mysql", "password": "Password1", "proxy": "sudo-su-shell", "via": "vagrant"},
]


def _mysql_host_dict(ip: str, element: str, **overrides: object) -> dict[str, object]:
    """Build an inline host dict carrying the mysql proxied cred.

    Fresh per call (no shared mutable state) and never written to any file —
    validated in-process by :func:`create_host_from_dict`, where the
    ``sudo-su-shell`` proxy IS registered (see module scope, above).
    """
    data: dict[str, object] = {
        "ip": ip,
        "element": element,
        "board": "seed",
        "creds": [dict(c) for c in _MYSQL_CREDS],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Fixture: lease one Unix host from the pool
# ---------------------------------------------------------------------------


@pytest.fixture
def leased_host(tmp_path_factory) -> Iterator[tuple[str, str]]:
    """Lease one Unix host from the pool; yield ``(element, ip)``.

    ``ip`` is read read-only from ``tech1/hosts.json`` (the veggies lab's
    real IP-to-element map) via :func:`tests._fixtures.labdata.host_data` —
    the shared file's ``creds`` are never consulted, so this never depends on
    (or risks) anything mutated there.
    """
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _UNIX_POOL) as element:
        yield element, host_data(element)["ip"]


# ---------------------------------------------------------------------------
# Test 1: direct SSH as mysql is denied (pure asyncssh, no otto)
# ---------------------------------------------------------------------------


async def _attempt_direct_mysql_login(ip: str) -> None:
    """Attempt a raw asyncssh connection as mysql/Password1; should never succeed."""
    conn = await asyncio.wait_for(
        asyncssh.connect(ip, username="mysql", password="Password1", known_hosts=None),
        timeout=20,
    )
    async with conn:
        pass


@pytest.mark.asyncio
async def test_direct_ssh_as_mysql_is_denied(leased_host: tuple[str, str]) -> None:
    """A raw asyncssh connection as ``mysql``/``Password1`` must fail.

    ``sshd_config`` on every bed VM carries ``DenyUsers mysql`` and the
    account's shell is ``/bin/false`` — direct authentication as ``mysql``
    must never succeed, proving the only path in is the root-mediated proxy.
    """
    ip = leased_host[1]
    with pytest.raises((asyncssh.Error, OSError, asyncio.TimeoutError)):
        await _attempt_direct_mysql_login(ip)


# ---------------------------------------------------------------------------
# Test 2: proxied default session (host.user='mysql')
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxied_default_session(leased_host: tuple[str, str]) -> None:
    """A host configured with ``user='mysql'`` must land its default session on mysql."""
    element, ip = leased_host
    host = create_host_from_dict(_mysql_host_dict(ip, element, user="mysql"))
    try:
        result = (await host.run("whoami")).only
        assert result.status == Status.Success, f"whoami failed: {result.value!r}"
        assert result.value.strip() == "mysql"
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# Test 3: switch_user / as_user roundtrip (from vagrant)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_user_roundtrip(leased_host: tuple[str, str]) -> None:
    """``as_user('mysql')`` switches in, runs as mysql, and restores vagrant on exit."""
    element, ip = leased_host
    host = create_host_from_dict(_mysql_host_dict(ip, element))  # default user: vagrant
    try:
        before = (await host.run("whoami")).only.value.strip()
        assert before == "vagrant"

        async with host.as_user("mysql"):
            during = (await host.run("whoami")).only.value.strip()
            assert during == "mysql"

        after = (await host.run("whoami")).only.value.strip()
        assert after == "vagrant"
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# Test 4: oneshot runs as the proxied user (Task 8 proxied-pool routing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oneshot_runs_as_proxied_user(leased_host: tuple[str, str]) -> None:
    """``oneshot`` on a proxied-user host must route through the proxied pool session."""
    element, ip = leased_host
    host = create_host_from_dict(_mysql_host_dict(ip, element, user="mysql"))
    try:
        result = await host.oneshot("whoami")
        assert result.status == Status.Success, f"oneshot whoami failed: {result.value!r}"
        assert result.value.strip() == "mysql"
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# Test 5: nc-transferred file lands owned by the proxied user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nc_put_owned_by_proxied_user(leased_host: tuple[str, str], tmp_path: Path) -> None:
    """A file ``put`` via ``nc`` to a proxied-user host must land owned by that user."""
    element, ip = leased_host
    host = create_host_from_dict(_mysql_host_dict(ip, element, user="mysql", transfer="nc"))

    remote_dir = f"/tmp/otto_lp_{uuid.uuid4().hex}"
    filename = "owned.txt"
    src = tmp_path / filename
    src.write_text("login-proxy nc ownership check\n")

    try:
        mkdir = (await host.run(f"mkdir -p {remote_dir}")).only
        assert mkdir.status == Status.Success, f"remote mkdir failed: {mkdir.value!r}"

        put_result = await host.put([src], Path(remote_dir))
        assert put_result.status == Status.Success, f"nc put failed: {put_result.msg}"

        stat = (await host.run(f"stat -c %U {remote_dir}/{filename}")).only
        assert stat.status == Status.Success, f"stat failed: {stat.value!r}"
        assert stat.value.strip() == "mysql"
    finally:
        await host.run(f"rm -rf {remote_dir}")
        await host.close()


# ---------------------------------------------------------------------------
# Test 6: interact --as-user over the PTY bridge
# ---------------------------------------------------------------------------

_LP_E2E_LAB = "lp_e2e_lab"

_INIT_MODULE_SOURCE = """\
import asyncio
import contextlib
import uuid

from otto import register_login_proxy

_READY_MARKER_ATTEMPTS = 5
_READY_MARKER_TIMEOUT = 2.0


async def _wait_for_shell_ready(io):
    # Resync after a su/sudo transition — see test_login_proxy_e2e.py's
    # module-scope comment for why this matters (a naive fire-and-forget
    # send reproduced a 100%-reliable hang against the live bed).
    for _ in range(_READY_MARKER_ATTEMPTS):
        marker = f"__OTTO_LP_READY_{uuid.uuid4().hex}__"
        await io.send(f"echo {marker}\\n")
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await io.expect(marker, timeout=_READY_MARKER_TIMEOUT)
            return
    raise TimeoutError("shell did not resync after su/sudo transition")


async def _sudo_su_shell(io, ctx):
    # Root-mediated: non-root `su -s` is silently ignored for restricted-shell
    # targets (util-linux). vagrant is passwordless sudo on the test VMs.
    await io.send(f"sudo su -s /bin/bash {ctx.target.login}\\n")
    await _wait_for_shell_ready(io)


async def _sudo_su_shell_undo(io, ctx):
    await io.send("exit\\n")
    await _wait_for_shell_ready(io)


register_login_proxy("sudo-su-shell", _sudo_su_shell, undo=_sudo_su_shell_undo, overwrite=True)
"""


def _scaffold_sut_dir(sut_dir: Path, ip: str, element: str) -> str:
    """Build a throwaway, fully self-contained SUT dir for the CLI subprocess.

    The child ``otto`` process never imports this test module, so it needs
    its OWN ``sudo-su-shell`` registration — wired via an ``init`` module
    (mirrors how real SUT repos register custom login proxies). Returns the
    host id (``"<element>_seed"``) the single host in the scaffolded
    ``hosts.json`` will resolve to.
    """
    (sut_dir / ".otto").mkdir(parents=True)
    (sut_dir / "initlib").mkdir(parents=True)
    (sut_dir / "lab_data").mkdir(parents=True)

    (sut_dir / ".otto" / "settings.toml").write_text(
        'name = "lp_e2e"\n'
        'version = "1.0.0"\n'
        'labs = ["${sut_dir}/lab_data"]\n'
        'libs = ["${sut_dir}/initlib"]\n'
        'init = ["lp_e2e_init"]\n'
    )
    (sut_dir / "initlib" / "lp_e2e_init.py").write_text(_INIT_MODULE_SOURCE)

    hosts = [
        {
            "ip": ip,
            "element": element,
            "board": "seed",
            "creds": [dict(c) for c in _MYSQL_CREDS],
            "labs": [_LP_E2E_LAB],
        }
    ]
    (sut_dir / "lab_data" / "hosts.json").write_text(json.dumps(hosts))

    return f"{element}_seed"


def test_interact_as_user_over_bridge(leased_host: tuple[str, str], tmp_path: Path) -> None:
    """``otto host <id> login --as-user mysql`` must bridge the human directly onto mysql.

    Drives the real interactive PTY bridge (Task 9) through a throwaway,
    self-contained SUT directory — see :func:`_scaffold_sut_dir` — so the
    subprocess has its own login-proxy registration and its own single-host
    lab, with zero shared lab-data mutation.
    """
    element, ip = leased_host
    sut_dir = tmp_path / "sut"
    host_id = _scaffold_sut_dir(sut_dir, ip, element)
    xdir = tmp_path / "xdir"

    with InteractiveOttoSession(
        ["-R", "-l", _LP_E2E_LAB, "host", host_id, "login", "--as-user", "mysql"],
        xdir=xdir,
        sut_dirs=sut_dir,
    ) as sess:
        sess.expect(b"Press Ctrl+] to disconnect", timeout=30)
        # Let the proxy exchange (sudo su -s /bin/bash mysql) and the new
        # bash shell's startup settle before typing into the bridge.
        sess.drain(1.0)
        sess.sendline("whoami")
        sess.expect(b"mysql", timeout=15)
        sess.disconnect()
        sess.expect(b"disconnected from", timeout=10)
        assert sess.wait(timeout=10) == 0
