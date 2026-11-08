"""``run_sh_elevated``: the one core that wraps a script in ``sh -c`` and runs it elevated.

sudo takes an argv tail, so a script with a redirection, a pipe or a ``||`` must
reach it whole, inside ``sh -c``. Every elevated coverage script goes through
this one function, so the wrapping is decided once.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from otto.declared import DeclaredEntry
from otto.host import cov_sweep, kmod_kind, privilege
from otto.host import product as product_mod
from otto.host.product import gcda_check_cmd, gcda_delete_cmd
from otto.registry import resolved
from otto.result import CommandResult, Results
from otto.utils import Status
from tests.unit.host._gcda_frame import framed


class _RunHost:
    """Records ``run(cmd, **kw)`` and answers one successful command.

    ``exec`` (the unelevated probe) answers a permission error, so a
    root-owned product's probe goes on to its elevated re-probe.
    """

    def __init__(self):
        self.id = "h1"
        self.run_calls: list[tuple[str, dict]] = []
        self.exec_calls: list[str] = []

    async def run(self, cmd, **kwargs):
        self.run_calls.append((cmd, kwargs))
        return Results.collect(
            items=[CommandResult(status=Status.Success, value="", command=cmd, retcode=0)]
        )

    async def exec(self, cmd, **kwargs):
        self.exec_calls.append(cmd)
        return CommandResult(status=Status.Error, value="Permission denied", command=cmd, retcode=1)


@pytest.mark.asyncio
async def test_run_sh_elevated_wraps_once_and_runs_through_sudo():
    host = _RunHost()
    result = await privilege.run_sh_elevated(host, "echo 1 > /sys/x")
    assert result.is_ok
    assert host.run_calls == [("sh -c 'echo 1 > /sys/x'", {"sudo": True})]


@pytest.mark.asyncio
async def test_run_sh_elevated_passes_timeout_only_when_given():
    host = _RunHost()
    await privilege.run_sh_elevated(host, "true", timeout=60)
    assert host.run_calls == [("sh -c true", {"sudo": True, "timeout": 60})]


def _kmod_product() -> kmod_kind.KmodProduct:
    host = SimpleNamespace(
        id="h1",
        default_dest_dir=Path(),
        load=AsyncMock(),
        unload=AsyncMock(),
        lsmod=AsyncMock(),
        dev_tools=[],
        products=[],
    )
    entry = DeclaredEntry(
        name="demo",
        kind="kmod",
        seam="products",
        owner="r",
        base_dir=Path("/project"),
        match={},
        params={"artifact": "build/demo/otto_kmod_demo.ko"},
    )
    return resolved(product_mod.PRODUCT_KINDS.get("kmod").factory)(entry, host)


def _docker_product():
    entry = DeclaredEntry(
        name="app",
        kind="docker_image",
        seam="products",
        owner="r",
        base_dir=Path("/project"),
        match={},
        params={"image": "registry/app:1", "cov_dir": "/var/cov/app"},
    )
    host = SimpleNamespace(id="h1", default_dest_dir=Path(), dev_tools=[], products=[])
    return resolved(product_mod.PRODUCT_KINDS.get("docker_image").factory)(entry, host)


@pytest.mark.asyncio
async def test_every_elevated_coverage_caller_goes_through_the_core(monkeypatch):
    seen: list[tuple[object, str, dict]] = []

    async def _recorder(host, script, **kwargs):
        seen.append((host, script, kwargs))
        value = framed(script, "")
        return Results.collect(
            items=[
                CommandResult(
                    status=Status.Success, value=value, command=f"sh -c {script}", retcode=0
                )
            ]
        )

    monkeypatch.setattr(privilege, "run_sh_elevated", _recorder)
    host = _RunHost()

    docker = _docker_product()
    assert (await docker.reset_coverage(host=host)).is_ok
    assert (await _kmod_product()._run_sudo(host, "echo 1 > /x")).is_ok
    await cov_sweep.remove_coverage(host, [docker])

    scripts = [script for _, script, _ in seen]
    # The reset sends its removal and its check, both through the core; the
    # cleanup sweep sends its removal and, once the plain probe was refused,
    # its elevated re-probe.
    assert scripts == [
        gcda_delete_cmd("/var/cov/app"),
        gcda_check_cmd("/var/cov/app"),
        "echo 1 > /x",
        cov_sweep._remove_script(["/var/cov/app"]),
        cov_sweep._probe_script(["/var/cov/app"]),
    ]
    assert all(h is host for h, _, _ in seen)
    # Nothing bypassed the core: the host's own run() was never reached.
    assert host.run_calls == []
