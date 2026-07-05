"""Stability/soak tests for proxied-user (mysql) command execution + transfer.

Companion to ``tests/e2e/host/test_login_proxy_e2e.py`` (which proves the
proxied paths work at all) — this module re-runs the same shapes of
assertion under ``pytest-repeat`` (``--count``) via ``make stability-unix``
to flush intermittent flakiness in:

- proxied session *establishment* (``user="mysql"``);
- ``as_user`` switch/undo roundtrip (soaks
  ``otto.host.login_proxy._resync_shell``, the post-transition tty-flush
  resync fix — see the e2e module's docstring NOTE for the history);
- proxied-user file transfer across ALL Unix backends (scp/sftp/ftp/nc) —
  content AND ownership together, a real coverage gap the design doc found
  (only ``nc`` had ownership coverage before this file);
- ``oneshot`` fan-out as the proxied user (pooled-session cross-contamination
  guard).

FINDING (confirmed on the live bed, not a flake): ``scp``/``sftp``/``ftp``
puts can NEVER land ``mysql``-owned for a proxied host, by construction —
``ConnectionManager.credentials`` (``otto/host/connections.py``) resolves a
proxied ``login_target`` to its via-chain's directly-loginable cred (here
``vagrant``) for the RAW transport auth on every one of ``ssh()``/``sftp()``/
``ftp()``; ``proxy_hops`` (the ``sudo su`` replay) is only ever applied by
``otto.host.session`` (the shell) and ``otto.host.interact`` (the PTY
bridge) — never by any ``otto/host/transfer/*.py`` backend. So a
transport-level put/get always runs (and its output file always lands
owned) as the resolved DIRECT cred, never the proxied one. Only ``nc``
achieves ``mysql`` ownership, because it pipes bytes through the
ALREADY-proxied interactive shell session (``cat`` runs as whatever user
that shell is currently proxied to) rather than opening its own transport
connection. ``test_proxied_transfer_content_and_ownership`` below asserts
this real, per-backend-different, 100%-reproducible behavior rather than a
single ``mysql``-for-all expectation — see its docstring. This is a design
finding for the spec (`docs/superpowers/specs/2026-07-05-proxy-appshell-stability-tests-design.md`
§3.3), not a product bug fixed here (test-only change, per this task's
non-goals).

Containment (mirrors the e2e module exactly, see its docstring for the full
rationale): the ``sudo-su-shell`` login proxy is registered at MODULE scope
below (``overwrite=True``), and ``_MYSQL_CREDS`` / ``_mysql_host_dict`` are
redefined locally rather than imported from the e2e module, so this file
never depends on (or risks) anything the e2e module does at collection
time. Hosts are built from inline dicts; only the leased VM's IP is read
from ``tech1/hosts.json`` (via :func:`tests._fixtures.labdata.host_data`) —
never its ``creds``.

Runs via ``make stability-unix`` (``-m "stability and integration"``,
``--count=10``; nightly ``--count=100``); excluded from ``make coverage``
(``-m "not stability"``).
"""

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from otto import register_login_proxy
from otto.host.unix_host import UnixHost
from otto.result import CommandResult
from otto.storage.factory import create_host_from_dict
from otto.utils import Status
from tests._fixtures._host_pool import UNIX_POOL as _UNIX_POOL
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import host_data
from tests.integration.host._transfer_retry import transfer_with_retry

pytestmark = [pytest.mark.timeout(120), pytest.mark.stability]


# ---------------------------------------------------------------------------
# Module-scope login-proxy registration (containment: see module docstring).
#
# No per-proxy resync here — the post-transition resync lives in the shared
# engine (``otto.host.login_proxy._resync_shell``, called from the end of
# ``run_proxy``/``run_undo``) and applies to every hop automatically. See
# ``tests/e2e/host/test_login_proxy_e2e.py``'s module docstring NOTE.
# ---------------------------------------------------------------------------


async def _sudo_su_shell(io, ctx):
    # Root-mediated: non-root `su -s` is silently ignored for restricted-shell
    # targets (util-linux). vagrant is passwordless sudo on the test VMs.
    await io.send(f"sudo su -s /bin/bash {ctx.target.login}\n")


async def _sudo_su_shell_undo(io, ctx):
    await io.send("exit\n")


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
# Fixtures: lease one Unix host from the pool
# ---------------------------------------------------------------------------

# nc-telnet-style port allocation races the same host's `ss` scan in the
# TOCTOU window before `nc -l` binds (see test_session_stability_integration.py's
# module docstring); pin the nc transfer param to one xdist group so
# `--dist loadgroup` runs it serially.
_NC_SERIAL_GROUP = pytest.mark.xdist_group("nc-serial")


@pytest.fixture
def leased_host(tmp_path_factory) -> Iterator[tuple[str, str]]:
    """Lease one Unix host from the pool; yield ``(element, ip)``.

    ``ip`` is read read-only from ``tech1/hosts.json`` (the veggies lab's
    real IP-to-element map) via :func:`tests._fixtures.labdata.host_data` —
    the shared file's ``creds`` are never consulted.
    """
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _UNIX_POOL) as element:
        yield element, host_data(element)["ip"]


@pytest_asyncio.fixture
async def proxied_host(request, tmp_path_factory):
    """Proxied-user (mysql) host leased from the Unix pool, by transfer backend.

    Parametrized ``indirect`` across ``["scp", "sftp", "ftp", "nc"]`` (see
    :data:`_TRANSFERS` below) — leases whichever pool host is free rather
    than pinning to one VM, spreading load the same way
    ``tests.conftest.transfer_host`` does for the non-proxied case.
    """
    transfer = request.param
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _UNIX_POOL) as element:
        ip = host_data(element)["ip"]
        host = create_host_from_dict(_mysql_host_dict(ip, element, user="mysql", transfer=transfer))
        try:
            yield host
        finally:
            await host.close()


_TRANSFERS = pytest.mark.parametrize(
    "proxied_host",
    ["scp", "sftp", "ftp", pytest.param("nc", id="nc", marks=_NC_SERIAL_GROUP)],
    indirect=True,
)


# ---------------------------------------------------------------------------
# 1. Proxied session establishment (soaks `user="mysql"` -> default session)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxied_command_roundtrip(leased_host: tuple[str, str]) -> None:
    """A ``user="mysql"`` host must land its default session on mysql, repeatedly.

    Proves the proxied session establishes cleanly under repetition (the
    ``--count`` soak), not just once: ``whoami`` must resolve to ``mysql``
    and a real command's output must survive intact.
    """
    element, ip = leased_host
    host = create_host_from_dict(_mysql_host_dict(ip, element, user="mysql"))
    try:
        whoami = (await host.run("whoami")).only
        assert whoami.status == Status.Success, f"whoami failed: {whoami.value!r}"
        assert whoami.value.strip() == "mysql"

        marker = f"proxied_{uuid.uuid4().hex}"
        echo = (await host.run(f"echo {marker}")).only
        assert echo.status == Status.Success, f"echo failed: {echo.value!r}"
        assert echo.value.strip() == marker
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# 2. as_user / switch_user roundtrip (soaks the su/exit resync)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxied_as_user_roundtrip(leased_host: tuple[str, str]) -> None:
    """``as_user("mysql")`` switches in, runs as mysql, and restores vagrant on exit.

    Soaks ``otto.host.login_proxy._resync_shell`` on both the su-in and the
    exit-out transitions — the tty-flush race the e2e module's docstring
    NOTE documents as previously 100% reproducible before the engine fix.
    """
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
# 3. Transfer content AND ownership together, across all Unix backends
# ---------------------------------------------------------------------------


# The ultimate directly-loginable cred in `_MYSQL_CREDS`' via-chain (mysql's
# `via="vagrant"`). `ConnectionManager.credentials` (see the module docstring
# FINDING) resolves every transport-level connection — scp/sftp/ftp alike —
# to this login, never to the proxied "mysql", so that is who actually
# authenticates (and therefore owns any file it writes) for those three
# backends. Named here rather than hardcoding "vagrant" inline so the
# rationale reads at the assertion site.
_DIRECT_LOGIN = "vagrant"


@_TRANSFERS
@pytest.mark.asyncio
async def test_proxied_transfer_content_and_ownership(
    proxied_host: UnixHost, tmp_path: Path
) -> None:
    """A proxied-user put must round-trip byte-identical, with backend-real ownership.

    Closes a real coverage gap: pre-existing e2e coverage only asserted
    ownership for ``nc``
    (``test_login_proxy_e2e.py::test_nc_put_owned_by_proxied_user``); no test
    asserted content and ownership *together*, and none covered
    scp/sftp/ftp under the proxy at all.

    Per the module docstring FINDING, ownership is NOT uniformly ``mysql``
    across backends — confirmed on the live bed, not a flake: ``nc`` writes
    through the already-proxied shell (owned by ``mysql``), while
    scp/sftp/ftp open their own transport connection authenticated as the
    via-chain's direct cred (:data:`_DIRECT_LOGIN`, i.e. ``vagrant``) and so
    always land owned by *that* login. The remote dir is created
    world-writable so the direct-login transfer backends can write into a
    dir the proxied ``mkdir`` made ``mysql``-owned.
    """
    remote_dir = f"/tmp/otto_lp_stab_{uuid.uuid4().hex}"
    filename = "owned.bin"
    payload = uuid.uuid4().bytes * 64  # random binary payload

    src = tmp_path / "src" / filename
    src.parent.mkdir()
    src.write_bytes(payload)

    remote_file = f"{remote_dir}/{filename}"
    expected_owner = "mysql" if proxied_host.transfer == "nc" else _DIRECT_LOGIN
    try:
        mkdir = (await proxied_host.run(f"mkdir -p {remote_dir} && chmod 0777 {remote_dir}")).only
        assert mkdir.status == Status.Success, f"remote mkdir failed: {mkdir.value!r}"

        put_result = await transfer_with_retry(lambda: proxied_host.put([src], Path(remote_dir)))
        assert put_result.is_ok, f"put failed: {put_result.msg}"

        stat = (await proxied_host.run(f"stat -c %U {remote_file}")).only
        assert stat.status == Status.Success, f"stat failed: {stat.value!r}"
        assert stat.value.strip() == expected_owner, (
            f"file not owned by {expected_owner!r}: {stat.value!r}"
        )

        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        get_result = await transfer_with_retry(
            lambda: proxied_host.get([Path(remote_file)], dest_dir)
        )
        assert get_result.is_ok, f"get failed: {get_result.msg}"

        got = dest_dir / filename
        assert got.exists(), f"local file {got} missing after get"
        assert got.read_bytes() == payload, "round-tripped content is not byte-identical"
    finally:
        await proxied_host.run(f"rm -rf {remote_dir}")


# ---------------------------------------------------------------------------
# 4. oneshot fan-out as the proxied user (pooled-session double-checkout guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxied_oneshot_fanout(leased_host: tuple[str, str]) -> None:
    """8 concurrent proxied-user oneshots must complete with intact, uncontaminated output.

    Mirrors ``test_real_oneshot_pool_high_fanout``
    (``test_session_stability_integration.py``); here the pooled sessions are
    all proxied through ``sudo-su-shell``, so a double-checkout would surface
    as one call's marker leaking into another's result.
    """
    element, ip = leased_host
    host = create_host_from_dict(_mysql_host_dict(ip, element, user="mysql"))
    try:
        N = 8  # noqa: N806 — single-letter math dimension
        results = await asyncio.gather(
            *(host.oneshot(f"echo mysql_{i}") for i in range(N)),
            return_exceptions=True,
        )

        exceptions = [r for r in results if isinstance(r, BaseException)]
        assert not exceptions, f"{len(exceptions)} oneshots raised; first: {exceptions[0]!r}"

        statuses = cast("list[CommandResult]", results)
        failed = [(i, r) for i, r in enumerate(statuses) if not r.status.is_ok]
        assert not failed, f"{len(failed)} non-ok statuses; first: {failed[0]}"

        for i, r in enumerate(statuses):
            assert f"mysql_{i}" in r.value, f"oneshot {i} got mangled output: {r.value!r}"
    finally:
        await host.close()
