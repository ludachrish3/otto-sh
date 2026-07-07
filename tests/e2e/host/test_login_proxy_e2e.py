"""End-to-end tests for login proxies against the live mysql-provisioned Unix bed.

The three Unix test VMs (carrot/tomato/pepper) each carry a Unix ``mysql``
account with a restricted shell (``/bin/false``) and ``DenyUsers mysql`` in
``sshd_config`` — direct SSH as ``mysql`` must fail. The only way to *become*
``mysql`` is a root-mediated ``sudo su -s /bin/bash mysql``: ``vagrant`` has
passwordless sudo, and util-linux's ``su -s`` is silently ignored for a
non-root caller against a restricted-shell target, so the switch must go
through sudo.

What this module proves, end to end against the real bed, using a CUSTOM
``sudo-su-shell`` login proxy (registered here — see below):

- direct auth as ``mysql`` is denied (only the proxy path works);
- proxied session *establishment* (``user='mysql'`` → default session runs
  as mysql);
- ``switch_user`` / ``as_user`` roundtrip (become mysql, then restore);
- ``oneshot`` routing through the proxied pool (Task 8);
- ``nc`` transfer ownership under the proxied user;
- ``interact --as-user`` over the real PTY bridge (Task 9);
- the BUILT-IN ``"su"`` proxy's ``switch_user``/``as_user`` path with the
  ``test`` account, with no custom proxy code at all
  (``test_builtin_su_proxy_switch_user_does_not_hang``);

plus cred-chain resolution (``via``/``proxy`` validated at ingest by
``CredSpec`` when the proxy is registered).

NOTE — the post-transition *resync* this module used to bake into its own
``sudo-su-shell`` proxy (``_wait_for_shell_ready``, see git history) has been
REMOVED. A su/sudo/exit transition is a foreground-process handoff on the
pty: su/login/sudo traditionally flush pending terminal input across a
privilege boundary (a typeahead-attack defense), so the very next
sentinel-wrapped command otto writes can land in that flush window and be
silently discarded. This was confirmed on this same live bed to race 100%
reliably — including with the BUILT-IN ``"su"`` proxy and the default
``"exit"`` undo, i.e. the gap was in the shared engine, not this module's
proxy. The fix now lives in the chokepoint every hop passes through
regardless of which proxy is registered: ``otto.host.login_proxy._resync_shell``,
called from the end of ``run_proxy``/``run_undo``. Every switch/undo in this
module (still exercised via the custom ``sudo-su-shell`` proxy, since
``mysql``'s restricted shell needs root-mediated ``sudo su``) is therefore
exercising the ENGINE's resync end to end, with no per-proxy workaround left
to mask a regression. ``test_builtin_su_proxy_switch_user_does_not_hang``
additionally drives the built-in ``"su"`` proxy directly — the path that was
previously untested here and confirmed 100% reliably hanging pre-fix.

Containment
-----------
``tests/_fixtures/lab_data/tech1/lab.json`` (the "veggies" lab) is loaded
directly by several *unit* tests that do not register any login proxies —
``CredSpec`` validates a cred's ``proxy`` name against the ``LOGIN_PROXIES``
registry at ingest, so adding a proxy-referencing cred to that shared file
would break every context that loads it without the proxy registered. This
module therefore never touches shared lab data:

- The ``sudo-su-shell`` proxy is registered at MODULE scope below
  (``overwrite=True`` so re-import under xdist is idempotent).
- Tests 1-5 build hosts from INLINE dicts via
  :func:`otto.storage.factory.create_host_from_dict`, reading only the
  leased VM's IP read-only from ``tech1/lab.json`` (via
  ``tests._fixtures.labdata.host_data``) — never its ``creds``.
- Test 6 (the ``interact --as-user`` bridge) drives a real ``otto``
  subprocess, which needs its OWN registration (it never imports this test
  module) — it scaffolds a throwaway, fully self-contained SUT directory
  under ``tmp_path`` with its own init module and its own single-host
  ``lab.json``, again built from the leased IP.

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
# No per-proxy resync here anymore — the post-transition marker-echo resync
# lives in the shared engine (``otto.host.login_proxy._resync_shell``, called
# from the end of ``run_proxy``/``run_undo``) and applies to every hop
# automatically. See the module docstring NOTE for the history/rationale.
# ---------------------------------------------------------------------------


async def _sudo_su_shell(io, ctx):
    # Root-mediated: non-root `su -s` is silently ignored for restricted-shell
    # targets (util-linux). vagrant is passwordless sudo on the test VMs.
    await io.send(f"sudo su -s /bin/bash {ctx.target.login}\n")


async def _sudo_su_shell_undo(io, ctx):
    await io.send("exit\n")


register_login_proxy("sudo-su-shell", _sudo_su_shell, undo=_sudo_su_shell_undo, overwrite=True)


# ---------------------------------------------------------------------------
# Inline host-dict builder (never touches tech1/lab.json's creds)
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

    ``ip`` is read read-only from ``tech1/lab.json`` (the veggies lab's
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


async def _assert_sshd_reachable(element: str, ip: str) -> None:
    """Fail LOUD (host-named) if sshd isn't reachable on :22 — never skip.

    ``lease_unix_host`` is a pure local flock with no liveness check, so a
    DOWN VM leases fine. Without this probe, a connection-level ``OSError`` /
    ``TimeoutError`` from an unreachable host would be indistinguishable from
    a genuine auth denial and the deny-assertion would false-positive-PASS.
    A bounded raw TCP connect isolates "bed down" from "sshd up and denied".
    """
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, 22), timeout=10)
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"{element}_seed ({ip}) unreachable on :22 — bed down? "
            f"(login-proxy e2e must fail loud on host-down, never skip): {exc!r}"
        ) from exc
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_direct_ssh_as_mysql_is_denied(leased_host: tuple[str, str]) -> None:
    """A raw asyncssh connection as ``mysql``/``Password1`` must be AUTH-denied.

    ``sshd_config`` on every bed VM carries ``DenyUsers mysql`` and the
    account's shell is ``/bin/false`` — direct authentication as ``mysql``
    must never succeed, proving the only path in is the root-mediated proxy.

    Fails loud on host-down first (``_assert_sshd_reachable``), then asserts
    the auth is specifically rejected with :class:`asyncssh.PermissionDenied`
    — NOT any connection-level ``OSError`` / ``TimeoutError``, which would
    mean "bed unreachable" masquerading as "denied".
    """
    element, ip = leased_host
    await _assert_sshd_reachable(element, ip)
    with pytest.raises(asyncssh.PermissionDenied):
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
from otto import register_login_proxy


async def _sudo_su_shell(io, ctx):
    # Root-mediated: non-root `su -s` is silently ignored for restricted-shell
    # targets (util-linux). vagrant is passwordless sudo on the test VMs.
    # No per-proxy resync needed — the engine (otto.host.login_proxy.run_proxy
    # /run_undo) resyncs after every hop now. See test_login_proxy_e2e.py's
    # module docstring NOTE.
    await io.send(f"sudo su -s /bin/bash {ctx.target.login}\\n")


async def _sudo_su_shell_undo(io, ctx):
    await io.send("exit\\n")


register_login_proxy("sudo-su-shell", _sudo_su_shell, undo=_sudo_su_shell_undo, overwrite=True)
"""


def _scaffold_sut_dir(sut_dir: Path, ip: str, element: str) -> str:
    """Build a throwaway, fully self-contained SUT dir for the CLI subprocess.

    The child ``otto`` process never imports this test module, so it needs
    its OWN ``sudo-su-shell`` registration — wired via an ``init`` module
    (mirrors how real SUT repos register custom login proxies). Returns the
    host id (``"<element>_seed"``) the single host in the scaffolded
    ``lab.json`` will resolve to.
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
    (sut_dir / "lab_data" / "lab.json").write_text(json.dumps({"hosts": hosts}))

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


# ---------------------------------------------------------------------------
# Test 7: the BUILT-IN "su" proxy — the previously-broken, un-resynced path
# ---------------------------------------------------------------------------

_BUILTIN_SU_CREDS: list[dict[str, str]] = [
    {"login": "vagrant", "password": "vagrant"},
    {"login": "test", "password": "Password1"},
]


def _builtin_su_host_dict(ip: str, element: str, **overrides: object) -> dict[str, object]:
    """Build an inline host dict with NO custom proxy — pure built-in ``su``.

    Neither cred here names a ``proxy``, so ``switch_user``/``as_user`` fall
    through to ``otto.host.login_proxy``'s built-in ``"su"`` registration
    (:func:`otto.host.login_proxy._su_proxy`) with no custom code involved at
    all — the exact path the module docstring's NOTE describes as previously
    untested and confirmed 100% reliably hanging pre-fix. ``test`` is a plain
    bash account with a real password on every bed VM (``provision_test_vm``),
    so a non-root ``su test`` prompts for and accepts it directly.
    """
    data: dict[str, object] = {
        "ip": ip,
        "element": element,
        "board": "seed",
        "creds": [dict(c) for c in _BUILTIN_SU_CREDS],
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_builtin_su_proxy_switch_user_does_not_hang(leased_host: tuple[str, str]) -> None:
    """Regression: the engine resync, not any proxy's own workaround, fixes the race.

    Exercises the BUILT-IN ``"su"`` proxy directly (no custom proxy code
    anywhere in this test) via ``as_user("test")``: enters as ``test``, runs
    TWO commands back to back (the second one is the load-bearing part — it
    proves the switch didn't just get lucky once), then exits the block
    (default ``"exit"`` undo) and runs a further command to prove the
    restore-to-``vagrant`` transition didn't drop anything either.

    Pre-fix, this hung 100% of the time waiting for the begin-marker of the
    first command after the switch (the su tty-flush ate it); see the module
    docstring NOTE and the resync-fix report for the RED (reverted-fix, timed
    out) vs GREEN (this test, passing) evidence.
    """
    element, ip = leased_host
    host = create_host_from_dict(_builtin_su_host_dict(ip, element))  # default user: vagrant
    try:
        before = (await host.run("whoami")).only.value.strip()
        assert before == "vagrant"

        async with host.as_user("test"):
            during = (await host.run("whoami")).only.value.strip()
            assert during == "test"
            # Nothing was dropped by the switch: a second command right after it.
            still_alive = (await host.run("echo still-alive")).only.value.strip()
            assert still_alive == "still-alive"

        # The default "exit" undo's own resync must not drop the next command.
        after = (await host.run("whoami")).only.value.strip()
        assert after == "vagrant"
    finally:
        await host.close()
