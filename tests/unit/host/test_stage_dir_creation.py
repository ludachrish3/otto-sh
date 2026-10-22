"""A product's ``stage_dir`` is created on the host when it is missing (#673).

Copying an artifact to a directory that does not exist lands it ON that path
as a file, so the next step (``chmod``, ``insmod``, ``docker load``) fails far
from the cause. Staging therefore runs ``mkdir -p`` on the resolved directory
first; only a failed creation is an error, and its reason is in the result.

Every staging kind is driven against a :class:`~otto.host.local_host.LocalHost`
over ``tmp_path`` — no live host — so the directory is really made (or really
refused) by the shell. Its ``put`` is swapped for scp's semantics: the local
copy creates a missing destination itself, which a remote transfer does not.
"""

import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.declared import DeclaredEntry
from otto.host import docker_image_kind, kmod_kind, kmod_tool_kind  # noqa: F401 — registers kinds
from otto.host import product as product_mod
from otto.host.dev_tool import DEV_TOOL_KINDS
from otto.host.local_host import LocalHost
from otto.host.product import ShellProduct
from otto.registry import resolved
from otto.result import Result
from otto.utils import Status

pytestmark = pytest.mark.asyncio


class _Shell(ShellProduct):
    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)

    async def is_installed(self, host):
        return False


class _ScpLikeHost(LocalHost):
    """A LocalHost whose ``put`` behaves like ``scp`` to a remote directory.

    ``scp art host:/missing`` writes the artifact AS ``/missing``; only an
    existing directory receives it inside. The kernel-module kinds also need
    ``load``/``unload``/``lsmod``, recorded here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.load = AsyncMock(return_value=Result(Status.Success))
        self.unload = AsyncMock(return_value=Result(Status.Success))
        self.lsmod = AsyncMock(return_value=Result(Status.Success, value=[]))

    async def put(self, src_files, dest_dir, *args, **kwargs):
        src = src_files[0] if isinstance(src_files, list) else src_files
        dest = dest_dir / src.name if dest_dir.is_dir() else dest_dir
        try:
            shutil.copy(src, dest)
        except OSError as e:
            return Result(Status.Error, msg=str(e))
        return Result(Status.Success, value={src: Result(Status.Success, value=dest)})


def _entry(kind: str, seam: str, **params) -> DeclaredEntry:
    return DeclaredEntry(
        name="demo",
        kind=kind,
        seam=seam,
        owner="r",
        base_dir=Path("/project"),
        match={},
        params=params,
    )


@pytest.fixture
def artifact(tmp_path: Path) -> Path:
    path = tmp_path / "build" / "demo.ko"
    path.parent.mkdir()
    path.write_bytes(b"payload")
    return path


async def test_a_missing_stage_dir_is_created_and_the_artifact_lands_inside_it(tmp_path, artifact):
    # A space in the path: the mkdir must quote the directory as one argument.
    stage = tmp_path / "not yet" / "stage"
    product = _Shell(artifact=artifact, stage_dir=stage)

    result = await product.stage(_ScpLikeHost())

    assert result.is_ok, result
    assert stage.is_dir()
    assert (stage / artifact.name).read_bytes() == b"payload"


async def test_an_existing_stage_dir_is_used_as_is(tmp_path, artifact):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "keep.txt").write_text("x")

    result = await _Shell(artifact=artifact, stage_dir=stage).stage(_ScpLikeHost())

    assert result.is_ok, result
    assert (stage / "keep.txt").read_text() == "x"
    assert (stage / artifact.name).is_file()


async def test_a_stage_dir_that_cannot_be_created_is_an_error_naming_why(tmp_path, artifact):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    stage = blocker / "stage"
    product = _Shell(artifact=artifact, name="app", stage_dir=stage)

    result = await product.stage(_ScpLikeHost())

    assert not result.is_ok, result
    assert str(stage) in result.msg
    assert "app" in result.msg
    # The shell's own reason travels with it, not just "failed".
    assert "Not a directory" in result.msg or "File exists" in result.msg, result.msg
    assert not (blocker / artifact.name).exists()


async def test_a_docker_image_tarball_stage_creates_its_stage_dir(tmp_path):
    tarball = tmp_path / "app.tar"
    tarball.write_bytes(b"tar")
    stage = tmp_path / "images"
    product = resolved(product_mod.PRODUCT_KINDS.get("docker_image").factory)(
        _entry("docker_image", "products", image=str(tarball), stage_dir=str(stage)),
        _ScpLikeHost(),
    )

    result = await product.stage(_ScpLikeHost())

    assert result.is_ok, result
    assert (stage / "app.tar").is_file()


async def test_a_kmod_product_install_creates_its_stage_dir_before_loading(tmp_path, artifact):
    stage = tmp_path / "mods"
    host = _ScpLikeHost()
    product = resolved(product_mod.PRODUCT_KINDS.get("kmod").factory)(
        _entry("kmod", "products", artifact=str(artifact), stage_dir=str(stage)), host
    )

    result = await product.install(host)

    assert result.is_ok, result
    assert stage.is_dir()
    assert host.load.await_args.kwargs["dest_dir"] == stage


async def test_a_kmod_dev_tool_install_creates_its_stage_dir_before_loading(tmp_path, artifact):
    stage = tmp_path / "tools"
    host = _ScpLikeHost()
    tool = resolved(DEV_TOOL_KINDS.get("kmod").factory)(
        _entry("kmod", "dev_tools", artifact=str(artifact), stage_dir=str(stage)), host
    )

    result = await tool.install(host)

    assert result.is_ok, result
    assert stage.is_dir()
    assert host.load.await_args.kwargs["dest_dir"] == stage


async def test_a_kmod_install_whose_stage_dir_cannot_be_made_never_loads(tmp_path, artifact):
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    host = _ScpLikeHost()
    product = resolved(product_mod.PRODUCT_KINDS.get("kmod").factory)(
        _entry("kmod", "products", artifact=str(artifact), stage_dir=str(blocker / "mods")), host
    )

    result = await product.install(host)

    assert not result.is_ok, result
    assert str(blocker / "mods") in result.msg
    host.load.assert_not_awaited()


async def test_only_a_host_with_a_shell_mkdir_makes_its_stage_dir() -> None:
    """The one predicate the hook and the plan share is private, not public API."""
    import otto.host.product as product_mod

    assert not hasattr(product_mod, "creates_stage_dir")
    assert "creates_stage_dir" not in product_mod.__all__
    assert product_mod._creates_stage_dir(LocalHost()) is True
    assert product_mod._creates_stage_dir(object()) is False
