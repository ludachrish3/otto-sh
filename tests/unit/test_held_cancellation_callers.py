"""Every cleanup that runs after its body was cancelled keeps that cancellation (D3, M10).

One case per migrated ``compensate(held=)`` caller: the body is cancelled,
the compensating action is patched to raise ``OSError``, and the
cancellation, not the ``OSError``, leaves the caller. A second test drives a
non-cancellation body error with the same failing cleanup and pins today's
precedence: the cleanup's error replaces the body's. The two nc listener
reaps run only from an ``except asyncio.CancelledError`` handler, so a
non-cancellation body error never reaches them and they have no second case.

Each case reuses the setup of the module that already tests that site.
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.asyncio]

Driver = Callable[[pytest.MonkeyPatch, Path], Awaitable[BaseException]]


async def _outcome(task: "asyncio.Future[Any]") -> BaseException:
    """The exception *task* ends with, a cancellation included."""
    try:
        await task
    except BaseException as exc:  # noqa: BLE001 — the outcome is what the test reads
        return exc
    raise AssertionError(f"the caller returned {task.result()!r} instead of raising")


async def _cancel_once(task: "asyncio.Future[Any]", reached: asyncio.Event) -> BaseException:
    await reached.wait()
    await asyncio.sleep(0)
    task.cancel()
    return await _outcome(task)


# --- otto.host.transfer.nc: the two listener reaps ---------------------------


def _failing_nc_reap() -> Any:
    async def cancel_and_reap(_self: Any, listen_task: "asyncio.Task[Any]", _port: int) -> None:
        # The listener task is cancelled, then the remote reap fails.
        listen_task.cancel()
        await asyncio.gather(listen_task, return_exceptions=True)
        raise OSError("reap failed")

    return cancel_and_reap


async def _nc_get_cancelled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BaseException:
    from otto.host.transfer import NcFileTransfer
    from tests.unit.host.test_transfer_nc_get import _make_ft, _ok

    dst = tmp_path / "dst"
    dst.mkdir()
    listening = asyncio.Event()

    async def exec_side_effect(cmd: str, timeout: "float | None" = None, **kw: object):
        if " -l -p " in cmd:
            listening.set()
            await asyncio.Event().wait()
        if cmd.startswith("stat"):
            return _ok("9000 regular file\n")
        return _ok("9000\n")

    async def block_forever(*_a: object, **_k: object) -> None:
        await asyncio.Event().wait()

    ft = _make_ft(AsyncMock(side_effect=exec_side_effect), has_tunnel=True)
    ft._connections.forward_port = AsyncMock(return_value=15000)
    monkeypatch.setattr(NcFileTransfer, "_cancel_and_reap", _failing_nc_reap())
    monkeypatch.setattr(
        NcFileTransfer, "_wait_for_remote_listener", AsyncMock(side_effect=block_forever)
    )
    # Sequential: a concurrent dispatch's gather reports the caller's own
    # cancellation whatever the per-file coroutine raised, which would hide
    # the site; sequentially, a per-file Exception becomes that file's entry.
    task = asyncio.ensure_future(
        ft._get_files_nc_tunneled([Path("/remote/data.bin")], dst, concurrent=False)
    )
    return await _cancel_once(task, listening)


async def _nc_put_cancelled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BaseException:
    import otto.host.transfer.nc as transfer_mod
    from otto.host.transfer import NcFileTransfer
    from tests.unit.host.test_transfer_nc_put import _make_ft, _ok

    src = tmp_path / "small.bin"
    src.write_bytes(b"hello world")
    connecting = asyncio.Event()

    async def scripted_exec(cmd: str, timeout: "float | None" = None, **kw: object):
        if " -l " in cmd:
            await asyncio.Event().wait()
        return _ok("9000\n")

    async def parked_connect(host: str, port: int, timeout: float = 2.0):
        connecting.set()
        await asyncio.Event().wait()

    ft = _make_ft(AsyncMock(side_effect=scripted_exec))
    monkeypatch.setattr(transfer_mod, "_connect_with_retry", parked_connect)
    monkeypatch.setattr(NcFileTransfer, "_wait_for_remote_listener", AsyncMock(return_value=None))
    monkeypatch.setattr(NcFileTransfer, "_cancel_and_reap", _failing_nc_reap())
    task = asyncio.ensure_future(ft._put_files_nc([src], tmp_path / "dst", concurrent=False))
    return await _cancel_once(task, connecting)


# --- otto.link.manage: the impair rollback -----------------------------------


async def _failing_rollback(*_a: object, **_k: object) -> None:
    raise OSError("rollback failed")


async def _link_cancelled(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> BaseException:
    from otto.link import manage
    from otto.link.params import ImpairmentParams
    from tests.unit.link.test_manage_impair import _bed

    lab, test1, test2, _ = _bed()
    test1.qdisc_texts = [
        "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n",
        "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n",
    ]
    reached = asyncio.Event()

    async def parked(cmd: str, timeout: "float | None" = None, **kw: object):
        if cmd == "ip -o addr show":
            return test2._result(cmd)
        reached.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # parked until the test cancels

    test2.exec = parked
    test2.run = parked
    monkeypatch.setattr(manage, "_rollback", _failing_rollback)
    task = asyncio.ensure_future(
        manage.impair_link(lab, ident="edge", params=ImpairmentParams(delay_ms=50.0))
    )
    return await _cancel_once(task, reached)


async def _link_failed(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> BaseException:
    from otto.link import manage
    from otto.link.params import ImpairmentParams
    from tests.unit.link.test_manage_impair import _bed

    lab, test1, _, _ = _bed()
    test1.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n", ""]
    monkeypatch.setattr(manage, "_rollback", _failing_rollback)
    return await _outcome(
        asyncio.ensure_future(
            manage.impair_link(
                lab, ident="edge", params=ImpairmentParams(delay_ms=50.0), from_host="test1"
            )
        )
    )


# --- otto.tunnel.manage: the add rollback ------------------------------------


def _tunnel_pair(monkeypatch: pytest.MonkeyPatch) -> Any:
    from otto.tunnel import manage
    from tests.unit.tunnel.test_manage_add import _pair, _PickRNG

    monkeypatch.setattr(manage, "_carrier_rng", _PickRNG())
    monkeypatch.setattr(manage, "_kill_tunnel_on", _failing_rollback)
    return _pair()


async def _tunnel_add_cancelled(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> BaseException:
    from otto.tunnel import manage
    from otto.tunnel.discovery import DISCOVERY_PS_COMMAND
    from otto.tunnel.socat import FREE_PORT_PROBE_COMMAND

    lab, _calls, _tunnel = _tunnel_pair(monkeypatch)
    launching = asyncio.Event()

    def gate(host: Any) -> None:
        orig = host.exec

        async def gated(cmd: str, timeout: "float | None" = None, **kw: object):
            if not (
                "command -v socat" in cmd
                or cmd in (FREE_PORT_PROBE_COMMAND, DISCOVERY_PS_COMMAND)
                or cmd.startswith("kill ")
            ):
                launching.set()  # a launch: park until the test cancels the add
                await asyncio.Event().wait()
            return await orig(cmd, timeout=timeout, **kw)

        host.exec = gated

    gate(lab.hosts["a"])
    gate(lab.hosts["b"])
    task = asyncio.ensure_future(
        manage.add_tunnel(lab, hosts=[("a", None), ("b", None)], port=8080)
    )
    return await _cancel_once(task, launching)


async def _tunnel_add_failed(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> BaseException:
    from otto.tunnel import manage

    lab, _calls, _tunnel = _tunnel_pair(monkeypatch)
    lab.hosts["a"].launch_fail_at = 0  # a launch fails after b's succeeded
    return await _outcome(
        asyncio.ensure_future(manage.add_tunnel(lab, hosts=[("a", None), ("b", None)], port=8080))
    )


# --- otto.host.session.HostSession.as_user: the undo -------------------------


async def _failing_run_undo(*_a: object, **_k: object) -> None:
    raise OSError("undo failed")


def _host_session(monkeypatch: pytest.MonkeyPatch) -> Any:
    from unittest.mock import AsyncMock as _AsyncMock

    from otto.host import session
    from otto.host.login_proxy import Cred
    from tests.unit.host.test_session import _realistic_shell_expect

    shell = _AsyncMock(spec=session.ShellSession)
    shell.current_user = "alice"
    _realistic_shell_expect(shell)
    monkeypatch.setattr(session, "run_undo", _failing_run_undo)
    return session.HostSession(
        "n",
        shell,
        lambda *_: None,
        lambda *_: None,
        lambda _: None,
        creds=[Cred(login="root", password="rootpw")],
        host_id="n",
    )


async def _as_user_body(cm: Any, inside: asyncio.Event, error: "BaseException | None") -> None:
    async with cm:
        inside.set()
        if error is not None:
            raise error
        await asyncio.Event().wait()


async def _session_as_user_cancelled(
    monkeypatch: pytest.MonkeyPatch, _tmp_path: Path
) -> BaseException:
    hs = _host_session(monkeypatch)
    inside = asyncio.Event()
    task = asyncio.ensure_future(_as_user_body(hs.as_user(user="root"), inside, None))
    return await _cancel_once(task, inside)


async def _session_as_user_failed(
    monkeypatch: pytest.MonkeyPatch, _tmp_path: Path
) -> BaseException:
    hs = _host_session(monkeypatch)
    body = _as_user_body(hs.as_user(user="root"), asyncio.Event(), RuntimeError("body"))
    return await _outcome(asyncio.ensure_future(body))


# --- otto.host.privilege.PosixPrivilege.as_user: the undo --------------------


def _privileged_host(monkeypatch: pytest.MonkeyPatch) -> Any:
    from otto.host import UnixHost
    from otto.host.element import Element
    from otto.host.privilege import PosixPrivilege
    from otto.logger.mode import LogMode
    from tests.unit.host.test_privilege import _MULTI_HOP_CREDS, _mock_session_mgr

    host = UnixHost(
        ip="10.0.0.1", element=Element(name="box"), creds=_MULTI_HOP_CREDS, log=LogMode.QUIET
    )
    host._session_mgr = _mock_session_mgr(user="root")

    async def failing_undo(_self: Any, _applied: Any, _prev: str) -> None:
        raise OSError("undo failed")

    monkeypatch.setattr(PosixPrivilege, "_undo_switch", failing_undo)
    return host


async def _privilege_as_user_cancelled(
    monkeypatch: pytest.MonkeyPatch, _tmp_path: Path
) -> BaseException:
    host = _privileged_host(monkeypatch)
    inside = asyncio.Event()
    task = asyncio.ensure_future(_as_user_body(host.as_user(user="mysql"), inside, None))
    return await _cancel_once(task, inside)


async def _privilege_as_user_failed(
    monkeypatch: pytest.MonkeyPatch, _tmp_path: Path
) -> BaseException:
    host = _privileged_host(monkeypatch)
    body = _as_user_body(host.as_user(user="mysql"), asyncio.Event(), RuntimeError("body"))
    return await _outcome(asyncio.ensure_future(body))


# --- otto.docker.compose.composed: the teardown ------------------------------


def _composed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    from otto.docker import compose
    from tests.unit.docker.test_compose import _make_lab, _make_project

    async def failing_down(**_kw: object) -> None:
        raise OSError("down failed")

    monkeypatch.setattr(compose, "compose_up", AsyncMock(return_value={}))
    monkeypatch.setattr(compose, "compose_down", failing_down)
    return compose.composed(project=_make_project(tmp_path), lab=_make_lab(), own=True)


async def _compose_cancelled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BaseException:
    inside = asyncio.Event()
    task = asyncio.ensure_future(_as_user_body(_composed(monkeypatch, tmp_path), inside, None))
    return await _cancel_once(task, inside)


async def _compose_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BaseException:
    body = _as_user_body(_composed(monkeypatch, tmp_path), asyncio.Event(), RuntimeError("body"))
    return await _outcome(asyncio.ensure_future(body))


# --- otto.docker.deployment.deployed: the teardown ---------------------------


def _deployed(monkeypatch: pytest.MonkeyPatch, teardown: Any) -> Any:
    from types import SimpleNamespace

    from otto.docker import deployment

    monkeypatch.setattr(
        deployment, "resolve_use_case", lambda *_a, **_k: SimpleNamespace(selection=None)
    )
    monkeypatch.setattr(deployment, "validated_services", lambda *_a: None)
    monkeypatch.setattr(deployment, "is_dry_run", lambda: False)
    monkeypatch.setattr(deployment, "deploy", AsyncMock(return_value=None))
    monkeypatch.setattr(deployment, "teardown", teardown)
    return deployment.deployed("integration", own=True)


async def _failing_teardown(*_a: object, **_k: object) -> None:
    raise OSError("teardown failed")


async def _deployed_cancelled(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> BaseException:
    inside = asyncio.Event()
    cm = _deployed(monkeypatch, _failing_teardown)
    task = asyncio.ensure_future(_as_user_body(cm, inside, None))
    return await _cancel_once(task, inside)


# --- otto.tunnel.check._column: the teardown ---------------------------------


def _check_bed(monkeypatch: pytest.MonkeyPatch) -> Any:
    from otto.tunnel import check
    from tests.unit.tunnel._check_fakes import bed

    async def failing_teardown(*_a: object, **_k: object) -> None:
        raise OSError("teardown failed")

    the_bed = bed().install(monkeypatch)
    monkeypatch.setattr(check, "_teardown", failing_teardown)
    return the_bed


async def _check_cancelled(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> BaseException:
    from otto.tunnel.check import check_tunnel
    from tests.unit.tunnel._check_helpers import ABC, PORT

    the_bed = _check_bed(monkeypatch)
    a = the_bed.hosts["a"]
    a.block_on = "head -c 1400"
    task = asyncio.ensure_future(check_tunnel(lab=the_bed, hosts=ABC, port=PORT, protocol="tcp"))
    return await _cancel_once(task, a.entered)


async def _check_failed(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> BaseException:
    from otto.tunnel import check
    from tests.unit.tunnel._check_helpers import ABC, PORT

    the_bed = _check_bed(monkeypatch)

    async def failing_probe(*_a: object, **_k: object) -> None:
        raise RuntimeError("probe")

    monkeypatch.setattr(check, "_probe", failing_probe)
    return await _outcome(
        asyncio.ensure_future(check.check_tunnel(lab=the_bed, hosts=ABC, port=PORT, protocol="tcp"))
    )


# --- otto.coverage.objects: the scan cleanup, from both scan paths -----------


def _scan_host(monkeypatch: pytest.MonkeyPatch, key: str, answer: Any) -> Any:
    from otto.coverage.objects import ObjectListCache
    from tests.unit.cov import test_object_list as tol

    async def failing_cleanup(*_a: object, **_k: object) -> bool:
        raise OSError("cleanup failed")

    monkeypatch.setattr("otto.coverage.objects._scan_token", lambda: tol._TOKEN)
    monkeypatch.setattr(ObjectListCache, "_scan_cleanup", failing_cleanup)
    return tol._DockerHost(tol._TWO, answers={key: answer})


def _parked(reached: asyncio.Event) -> Any:
    async def answer(_cmd: str, _timeout: object) -> None:
        reached.set()
        await asyncio.Event().wait()

    return answer


async def _scan_container_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> BaseException:
    from otto.coverage.objects import ObjectListCache
    from tests.unit.cov import test_object_list as tol

    reached = asyncio.Event()
    host = _scan_host(monkeypatch, tol._EXEC, _parked(reached))
    task = asyncio.ensure_future(
        ObjectListCache(tmp_path)._scan_container(tol._RefImg(), host, "img-c")
    )
    return await _cancel_once(task, reached)


async def _scan_container_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BaseException:
    from otto.coverage.objects import ObjectListCache
    from tests.unit.cov import test_object_list as tol

    host = _scan_host(monkeypatch, tol._EXEC, RuntimeError("scan"))
    return await _outcome(
        asyncio.ensure_future(
            ObjectListCache(tmp_path)._scan_container(tol._RefImg(), host, "img-c")
        )
    )


async def _scan_snapshot_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> BaseException:
    from tests.unit.cov import test_object_list as tol

    reached = asyncio.Event()
    host = _scan_host(monkeypatch, tol._START, _parked(reached))
    return await _cancel_once(asyncio.ensure_future(tol._snapshot(host, tmp_path)), reached)


async def _scan_snapshot_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BaseException:
    from tests.unit.cov import test_object_list as tol

    host = _scan_host(monkeypatch, tol._START, RuntimeError("scan"))
    return await _outcome(asyncio.ensure_future(tol._snapshot(host, tmp_path)))


CANCELLED: "dict[str, Driver]" = {
    "nc get listener reap": _nc_get_cancelled,
    "nc put listener reap": _nc_put_cancelled,
    "link impair rollback": _link_cancelled,
    "tunnel add rollback": _tunnel_add_cancelled,
    "HostSession.as_user undo": _session_as_user_cancelled,
    "PosixPrivilege.as_user undo": _privilege_as_user_cancelled,
    "composed teardown": _compose_cancelled,
    "deployed teardown": _deployed_cancelled,
    "tunnel check teardown": _check_cancelled,
    "scan cleanup (container)": _scan_container_cancelled,
    "scan cleanup (snapshot)": _scan_snapshot_cancelled,
}

FAILED: "dict[str, Driver]" = {
    "link impair rollback": _link_failed,
    "tunnel add rollback": _tunnel_add_failed,
    "HostSession.as_user undo": _session_as_user_failed,
    "PosixPrivilege.as_user undo": _privilege_as_user_failed,
    "composed teardown": _compose_failed,
    "tunnel check teardown": _check_failed,
    "scan cleanup (container)": _scan_container_failed,
    "scan cleanup (snapshot)": _scan_snapshot_failed,
}


@pytest.mark.parametrize("site", list(CANCELLED))
async def test_a_failing_cleanup_never_replaces_the_bodys_cancellation(site, monkeypatch, tmp_path):
    left = await CANCELLED[site](monkeypatch, tmp_path)
    assert isinstance(left, asyncio.CancelledError), f"{site}: {left!r} left, not the cancellation"


@pytest.mark.parametrize("site", list(FAILED))
async def test_a_failing_cleanup_still_replaces_a_non_cancellation_body_error(
    site, monkeypatch, tmp_path
):
    left = await FAILED[site](monkeypatch, tmp_path)
    assert isinstance(left, OSError), f"{site}: {left!r} left, not the cleanup's error"


async def _held_passed_by(cm: Any, monkeypatch: pytest.MonkeyPatch) -> "list[Any]":
    """The ``held`` each ``compensate`` call got while *cm*'s body was cancelled."""
    import otto.lifecycle

    helds: "list[Any]" = []

    async def spy(coro: Any, **kw: Any) -> Any:
        helds.append(kw.get("held"))
        coro.close()

    monkeypatch.setattr(otto.lifecycle, "compensate", spy)
    inside = asyncio.Event()
    left = await _cancel_once(asyncio.ensure_future(_as_user_body(cm, inside, None)), inside)
    assert isinstance(left, asyncio.CancelledError)
    return helds


@pytest.mark.parametrize("entry", ["composed", "deployed"])
async def test_both_docker_entry_points_bound_an_interrupted_teardown_alike(
    entry, monkeypatch, tmp_path
):
    # held= is what arms the teardown deadline at entry, so composed() and
    # deployed() bound an interrupted teardown the same way (before:
    # deployed() passed no held, its teardown unbounded until a second cancel).
    cm = (
        _composed(monkeypatch, tmp_path)
        if entry == "composed"
        else _deployed(monkeypatch, _failing_teardown)
    )
    helds = await _held_passed_by(cm, monkeypatch)
    assert len(helds) == 1
    assert isinstance(helds[0], asyncio.CancelledError)
