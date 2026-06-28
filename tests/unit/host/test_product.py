"""Unit tests for the Product lifecycle strategy and orchestration."""

from pathlib import Path

import pytest

from otto.host.product import FileProduct, Product
from otto.utils import Status


class _DummyFileProduct(FileProduct):
    """FileProduct with the abstract halves stubbed so it can instantiate."""

    async def install(self, host):
        return Status.Success, ""

    async def uninstall(self, host):
        return Status.Success, ""

    async def is_installed(self, host):
        return True


def test_fileproduct_name_defaults_to_artifact_basename():
    p = _DummyFileProduct(artifact=Path("/builds/app-1.2.tar.gz"))
    assert p.name == "app-1.2.tar.gz"


def test_fileproduct_explicit_name_wins():
    p = _DummyFileProduct(artifact=Path("/builds/app.tar.gz"), name="myapp")
    assert p.name == "myapp"


def test_product_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        Product()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_fileproduct_stage_delegates_to_host_put():
    from unittest.mock import AsyncMock

    p = _DummyFileProduct(artifact=Path("/builds/app.bin"), dest_dir=Path("/opt"))
    host = AsyncMock()
    host.put.return_value = (Status.Success, "")
    status, _msg = await p.stage(host)
    assert status is Status.Success
    host.put.assert_awaited_once_with(Path("/builds/app.bin"), Path("/opt"))


def test_every_host_has_empty_products_by_default():
    from otto.host.embedded_host import ZephyrHost
    from otto.host.local_host import LocalHost
    from otto.host.unix_host import UnixHost

    assert LocalHost().products == []
    assert UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"}, log=False).products == []
    assert ZephyrHost(ip="192.0.2.1", element="sprout", log=False).products == []


def test_products_can_be_injected_at_construction():
    from otto.host.local_host import LocalHost

    p = _DummyFileProduct(artifact=Path("/b/app.bin"))
    host = LocalHost()
    host.products = [p]
    assert host.products == [p]


class _FakeProduct(Product):
    def __init__(self, name, *, installed=False, fail_on=None):
        self.name = name
        self._installed = installed
        self.fail_on = fail_on
        self.calls: list[str] = []

    async def stage(self, host):
        self.calls.append("stage")
        return (Status.Error, "boom") if self.fail_on == "stage" else (Status.Success, "")

    async def install(self, host):
        self.calls.append("install")
        return (Status.Error, "boom") if self.fail_on == "install" else (Status.Success, "")

    async def uninstall(self, host):
        self.calls.append("uninstall")
        return (Status.Error, "boom") if self.fail_on == "uninstall" else (Status.Success, "")

    async def is_installed(self, host):
        return self._installed


def _host_with(products):
    from otto.host.local_host import LocalHost

    h = LocalHost()
    h.products = list(products)
    return h


@pytest.mark.asyncio
async def test_stage_runs_every_product_stage():
    a, b = _FakeProduct("a"), _FakeProduct("b")
    status, _ = await _host_with([a, b]).stage()
    assert status is Status.Success
    assert a.calls == ["stage"]
    assert b.calls == ["stage"]


@pytest.mark.asyncio
async def test_stage_empty_is_success_noop():
    status, msg = await _host_with([]).stage()
    assert status is Status.Success
    assert msg == ""


@pytest.mark.asyncio
async def test_install_stages_then_installs():
    a = _FakeProduct("a")
    status, _ = await _host_with([a]).install()
    assert status is Status.Success
    assert a.calls == ["stage", "install"]


@pytest.mark.asyncio
async def test_install_stage_only_skips_install():
    a = _FakeProduct("a")
    status, _ = await _host_with([a]).install(stage_only=True)
    assert status is Status.Success
    assert a.calls == ["stage"]


@pytest.mark.asyncio
async def test_install_short_circuits_on_stage_failure():
    a = _FakeProduct("a", fail_on="stage")
    b = _FakeProduct("b")
    status, msg = await _host_with([a, b]).install()
    assert status is Status.Error
    assert msg == "boom"
    assert b.calls == []  # never reached


@pytest.mark.asyncio
async def test_uninstall_is_best_effort_across_products():
    a = _FakeProduct("a", fail_on="uninstall")
    b = _FakeProduct("b")
    status, msg = await _host_with([a, b]).uninstall()
    assert status is Status.Error
    assert msg == "boom"
    assert a.calls == ["uninstall"]
    assert b.calls == ["uninstall"]  # both attempted


@pytest.mark.asyncio
async def test_is_installed_true_only_when_all_installed():
    assert (
        await _host_with(
            [_FakeProduct("a", installed=True), _FakeProduct("b", installed=True)]
        ).is_installed()
        is True
    )
    assert (
        await _host_with(
            [_FakeProduct("a", installed=True), _FakeProduct("b", installed=False)]
        ).is_installed()
        is False
    )


@pytest.mark.asyncio
async def test_is_installed_empty_is_false():
    assert await _host_with([]).is_installed() is False


@pytest.mark.asyncio
async def test_is_uninstalled_is_inverse():
    h = _host_with([_FakeProduct("a", installed=False)])
    assert await h.is_uninstalled() is True
    assert await h.is_installed() is False


@pytest.mark.asyncio
async def test_install_under_dry_run_does_not_transfer(tmp_path):
    from tests.conftest import active_context

    class _StageOnlyProduct(FileProduct):
        async def install(self, host):
            return Status.Success, ""

        async def uninstall(self, host):
            return Status.Success, ""

        async def is_installed(self, host):
            return False

    artifact = tmp_path / "app.bin"
    artifact.write_bytes(b"x")
    dest = tmp_path / "dest"
    host = _host_with([_StageOnlyProduct(artifact=artifact, dest_dir=dest)])
    with active_context(dry_run=True):
        status, _ = await host.install(stage_only=True)
    assert status.is_ok
    assert not dest.exists()  # LocalHost.put was a dry-run no-op
