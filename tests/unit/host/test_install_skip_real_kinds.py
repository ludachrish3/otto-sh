"""#367's probe policy, driven through REAL product kinds on a real host.

``Host.install`` reads every product's ``is_installed`` before staging
anything: True passes the product over, False (including a probe that could
not read the host) installs it, and an exception aborts before any stage. The
fakes in ``test_install_outcome_lines.py`` pin the loop; these pin that the
built-in kinds' own probes give the answers the policy assumes. Only the
host's command seam is scripted, so the kinds' hooks run as shipped.
"""

from pathlib import Path

import pytest

from otto.host.declared_product import DeclaredProduct
from otto.host.errors import HostUnreachableError
from otto.host.kmod_kind import KmodProduct
from otto.host.local_host import LocalHost
from otto.result import CommandResult, Result
from otto.utils import Status

_CHECK = "test -d /opt/x"


class _Seam:
    """The host's command seam, scripted per command and recording what it was asked."""

    def __init__(self, host, monkeypatch, *, check_passes=False, lsmod=None):
        self.ran: list[str] = []
        self.puts: list[Path] = []
        self.loads: list[str] = []
        self.probes: list[str] = []
        self._check_passes = check_passes
        self._lsmod = lsmod if lsmod is not None else Result(Status.Success, value=[])
        monkeypatch.setattr(host, "run", self._run)
        monkeypatch.setattr(host, "put", self._put)
        monkeypatch.setattr(host, "mkdir", self._mkdir)
        monkeypatch.setattr(host, "lsmod", self._lsmod_read, raising=False)
        monkeypatch.setattr(host, "load", self._load, raising=False)

    async def _run(self, cmd, *args, **kwargs):
        self.ran.append(cmd)
        ok = cmd != _CHECK or self._check_passes
        return CommandResult(
            Status.Success if ok else Status.Failed, command=cmd, retcode=0 if ok else 1
        )

    async def _put(self, src, dest=None, *args, **kwargs):
        self.puts.append(Path(src))
        return Result(Status.Success)

    async def _mkdir(self, *args, **kwargs):
        return Result(Status.Success)

    async def _lsmod_read(self):
        self.probes.append("lsmod")
        if isinstance(self._lsmod, BaseException):
            raise self._lsmod
        return self._lsmod

    async def _load(self, artifact, module_name, *args, **kwargs):
        self.loads.append(module_name)
        self.probes.append("load")
        return Result(Status.Success)


def _declared(tmp_path) -> DeclaredProduct:
    artifact = tmp_path / "x.tar"
    artifact.write_text("payload")
    return DeclaredProduct(
        artifact=artifact,
        name="x",
        stage_dir=tmp_path / "stage",
        install_cmd="make-it",
        check_cmd=_CHECK,
    )


def _kmod(tmp_path) -> KmodProduct:
    artifact = tmp_path / "demo.ko"
    artifact.write_bytes(b"\x7fELF")
    return KmodProduct(artifact=artifact, stage_dir=tmp_path / "stage")


def _host(*products) -> LocalHost:
    host = LocalHost()
    host.products = list(products)
    return host


@pytest.mark.asyncio
async def test_a_declared_check_that_fails_lets_the_install_run(tmp_path, monkeypatch):
    product = _declared(tmp_path)
    host = _host(product)
    seam = _Seam(host, monkeypatch, check_passes=False)
    result = await host.install()
    assert result.is_ok, result.msg
    assert seam.ran == [_CHECK, "make-it"]
    assert seam.puts == [product.artifact]  # the stage ran


@pytest.mark.asyncio
async def test_a_declared_check_that_passes_skips_stage_and_install(tmp_path, monkeypatch):
    host = _host(_declared(tmp_path))
    seam = _Seam(host, monkeypatch, check_passes=True)
    result = await host.install()
    assert result.status is Status.Success
    assert seam.ran == [_CHECK]
    assert seam.puts == []


@pytest.mark.asyncio
async def test_a_kmod_probe_that_cannot_read_lsmod_installs(tmp_path, monkeypatch):
    product = _kmod(tmp_path)
    host = _host(product)
    seam = _Seam(host, monkeypatch, lsmod=Result(Status.Failed, msg="lsmod: not found"))
    result = await host.install()
    assert result.is_ok, result.msg
    assert seam.loads == [product.module_name]  # host.load was reached
    assert seam.probes == ["lsmod", "load"]  # the failed probe came first


@pytest.mark.asyncio
async def test_a_resident_kmod_is_passed_over(tmp_path, monkeypatch):
    product = _kmod(tmp_path)
    host = _host(product)
    seam = _Seam(host, monkeypatch, lsmod=Result(Status.Success, value=[product.module_name]))
    result = await host.install()
    assert result.status is Status.Success
    assert seam.loads == []  # a second insmod would fail with "File exists"


@pytest.mark.asyncio
async def test_a_probe_that_raises_aborts_before_staging(tmp_path, monkeypatch):
    host = _host(_declared(tmp_path), _kmod(tmp_path))
    seam = _Seam(
        host,
        monkeypatch,
        check_passes=False,  # a is not installed: it would stage, were it reached
        lsmod=HostUnreachableError("lsmod: the transport dropped"),
    )
    with pytest.raises(HostUnreachableError):
        await host.install()
    # The probe pass comes first: a's stage never ran.
    assert seam.puts == []
    assert seam.loads == []
