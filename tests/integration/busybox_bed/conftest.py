"""Fixtures for the BusyBox bed guests (five QEMU guests behind test1).

Guests are built via the factory (`create_host_from_dict`) so the
committed lab entries are exercised exactly as `otto host` users
exercise them. Direct `UnixHost(...)` would default `term="ssh"` and
fail menu validation — don't.

Serialization: everything here joins xdist_group("busybox_bed") — the
family-wide group shared with the parametrized rows in the generic
suites (Tasks 5-6). Bed-down policy: FAIL naming the guest and the
recovery command. Never skip.
"""

import json
import shlex
from pathlib import Path

import pytest
import pytest_asyncio

from otto.config.lab import Lab
from otto.context import OttoContext, set_context
from otto.host.factory import create_host_from_dict
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from scripts.lab_health import _run_ssh, _ssh_user_pass
from tests._fixtures.labdata import host_data, lab_data_path
from tests.conftest import BUSYBOX_BED_GROUP

_BED_ROOT = Path(__file__).parent


def _guest_elements() -> list[str]:
    hosts = json.loads(lab_data_path("tech1").read_text())["hosts"]
    return [h["element"] for h in hosts if "busybox" in h.get("labs", []) and h.get("hop")]


GUESTS = _guest_elements()
# Loud, at import, because the alternative is silent. Every fixture below is
# parametrized over GUESTS, and pytest renders an empty parameter set as a
# SKIPPED row rather than a missing one -- so a lab.json that lost its
# `busybox` memberships (or its hops) would empty this suite and report the bed
# GREEN. "Never skip on bed-down" applies to a bed that is not there at all.
assert GUESTS, (
    "no BusyBox bed guests in tests/_fixtures/lab_data/tech1/lab.json: no host "
    "carries both a 'busybox' lab membership and a hop. This suite is "
    "parametrized over that roster and would empty-param-SKIP rather than fail, "
    "so it is asserted here instead."
)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Serialize the bed suite: the family-wide xdist group.

    ``tryfirst`` is load-bearing, not decoration, for the reason spelled
    out at length in ``tests/integration/host/conftest.py``: pluggy
    dispatches same-phase impls in LIFO registration order, and xdist's own
    ``pytest_collection_modifyitems`` is what turns the marker into the
    ``@group`` nodeid suffix the loadgroup scheduler actually reads. Naming
    this directory on the command line — which is exactly how the bed suite
    is run — makes this an *initial* conftest registered at startup, i.e.
    BEFORE xdist, so under LIFO it runs AFTER xdist and every stamp here is
    silently ignored.

    Measured on this tree with the real instrument, ``pytest -v
    tests/integration/busybox_bed/`` under the repo's default ``-n auto
    --dist loadgroup``: WITHOUT the decorator, zero of fifteen nodeids
    carried an ``@busybox_bed`` suffix and the guests were driven from four
    workers at once; WITH it, all fifteen carry the suffix on gw0. Note
    which instrument says so — ``--collect-only`` renders no ``@group``
    suffix at all, for a declared group or a stamped one, so it reports a
    working stamp and a broken stamp identically.
    """
    for item in items:
        if _BED_ROOT in item.path.parents:
            item.add_marker(pytest.mark.xdist_group(BUSYBOX_BED_GROUP))


@pytest.fixture(autouse=True, scope="module")
def _load_lab():
    """Install test1 so hop resolution finds test1 (snapshot/restore
    per tests/integration/host/conftest.py's discipline)."""
    from otto.context import _active

    snapshot = _active.get()
    lab = Lab(name="busybox_bed")
    data = host_data("test1")
    lab.add_host(
        UnixHost(
            ip=data["ip"],
            element=data["element"],
            creds=[Cred(**c) for c in data["creds"]],
            board=data.get("board"),
            is_virtual=True,
            log=LogMode.QUIET,
        )
    )
    set_context(OttoContext(lab=lab))
    yield
    _active.set(snapshot)


_bed_state: dict = {}


def _probe_guest(ne: str) -> "str | None":
    """Dial the guest FROM test1, which is the only place it is routable.

    The guest's ``ip`` is its own address on a /30 whose other end is a TAP
    device on test1, and the guests configure no default route — so this
    connect has to originate on the hop. The port comes off the entry the same
    way ``scripts/lab_health.py`` reads it (``telnet_options.port``, defaulting
    to 23): the bed entries declare no override, and hardcoding 23 here would
    stop tracking them if one ever did.
    """
    guest = host_data(ne)
    test1 = host_data("test1")
    user, password = _ssh_user_pass(test1["creds"])
    ip = guest["ip"]
    port = guest.get("telnet_options", {}).get("port", 23)
    probe = "python3 -c " + shlex.quote(
        f"import socket; socket.create_connection(({ip!r}, {port}), timeout=5).close()"
    )
    rc, _out, err = _run_ssh(test1["ip"], user, password, probe)
    if rc != 0:
        return (
            f"BusyBox bed guest {ne} ({ip}:{port}, from test1) is unreachable: "
            f"{err or f'rc={rc}'}. Is the bed provisioned and up? "
            "Recover with `make qemu-restart`; diagnose with "
            f"`journalctl -u busybox-qemu-{guest['sw_version']}` on test1."
        )
    return None


def _require_guest(ne: str) -> None:
    if ne not in _bed_state:
        _bed_state[ne] = _probe_guest(ne)
    if _bed_state[ne]:
        pytest.fail(_bed_state[ne])


def _build_guest(ne: str, **overrides):
    data = {**host_data(ne), **overrides}
    return create_host_from_dict(data, lab_name="busybox"), data["sw_version"]


@pytest_asyncio.fixture(params=GUESTS)
async def guest(request):
    """(host, version) on the resolved defaults: term=telnet, transfer=shell."""
    _require_guest(request.param)
    host, version = _build_guest(request.param)
    yield host, version
    await host.close()


@pytest_asyncio.fixture(params=GUESTS)
async def guest_nc(request):
    """(host, version) with the nc transfer pinned (menu-validated)."""
    _require_guest(request.param)
    host, version = _build_guest(request.param, transfer="nc")
    yield host, version
    await host.close()
