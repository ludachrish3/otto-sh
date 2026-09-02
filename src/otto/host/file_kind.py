"""
The built-in ``file`` kind — the zero-code declared product/dev tool.

One artifact, staged via :meth:`~otto.host.host.Host.put`, with optional
``install``/``uninstall``/``check`` command strings run on the host. One
class serves BOTH seams: :class:`~otto.host.product.Product` and
:class:`~otto.host.dev_tool.DevTool` deliberately share the same abstract
surface, so a single concrete type satisfies the two contracts — which seam
an instance lives in is decided by the registry that built it, never by the
type.

The defaults are the honest floor of today's Host surface: without an
``install`` command, install is a no-op success (staging placed the
artifact); without ``check``, ``is_installed`` answers False — otto assumes
not installed and re-stages, which is safe for the simple cases this kind
serves. ``host.exists()`` does not exist yet (see
:class:`~otto.host.product.FileProduct`); when the remote file-ops phase
lands, the ``check`` default upgrades to an artifact-existence test.
Anything richer than this is a repo-registered kind — the mechanism working
as intended, not a limitation.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import override

from ..declared import DeclaredEntry
from ..result import Result
from ..utils import Status, anchor_path
from .dev_tool import DEV_TOOL_KINDS, DevTool
from .product import PRODUCT_KINDS, FileProduct

if TYPE_CHECKING:
    from .host import Host


@dataclass
class DeclaredFile(FileProduct, DevTool):
    """A ``kind = "file"`` entry's runtime form (both seams).

    ``stage`` is :class:`~otto.host.product.FileProduct`'s (``host.put``);
    the three remaining hooks run the declared command strings, or take the
    module docstring's honest defaults when a string is absent.
    """

    install_cmd: str | None = None
    uninstall_cmd: str | None = None
    check_cmd: str | None = None

    @override
    async def install(self, host: "Host") -> Result:
        if self.install_cmd is None:
            return Result(Status.Success)
        return await host.run(self.install_cmd)

    @override
    async def uninstall(self, host: "Host") -> Result:
        if self.uninstall_cmd is None:
            return Result(Status.Success)
        return await host.run(self.uninstall_cmd)

    @override
    async def is_installed(self, host: "Host") -> bool:
        if self.check_cmd is None:
            return False
        return (await host.run(self.check_cmd)).status is Status.Success


def _str_param(
    entry: DeclaredEntry, params: dict[str, Any], key: str, *, required: bool = False
) -> str | None:
    value = params.pop(key, None)
    if value is None:
        if required:
            raise ValueError(
                f"[[{entry.seam}]] {entry.name!r}: kind 'file' requires an {key!r} param"
            )
        return None
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004 — existing API contract; test suite expects ValueError
            f"[[{entry.seam}]] {entry.name!r}: {key!r} must be a string, got {value!r}"
        )
    return value


def _file_kind(entry: DeclaredEntry, host: "Host") -> DeclaredFile:  # noqa: ARG001 — required by the KindRegistry factory signature Callable[[DeclaredEntry, Host], T]; this simple kind ignores host
    """Build a :class:`DeclaredFile` from a validated entry's params."""
    params = dict(entry.params)
    artifact = _str_param(entry, params, "artifact", required=True)
    assert artifact is not None  # noqa: S101 — internal invariant: required=True makes _str_param raise above when missing
    dest_dir = _str_param(entry, params, "dest_dir")
    install = _str_param(entry, params, "install")
    uninstall = _str_param(entry, params, "uninstall")
    check = _str_param(entry, params, "check")
    if params:
        raise ValueError(
            f"[[{entry.seam}]] {entry.name!r}: kind 'file' got unknown param(s): "
            f"{sorted(params)}; valid: artifact, dest_dir, install, uninstall, check"
        )
    return DeclaredFile(
        # Local path: forward slashes in TOML, anchored to the declaring repo
        # (never the CWD); dest_dir stays in the HOST's path domain — host.put
        # resolves it against the host's default_dest_dir (spec §4).
        artifact=anchor_path(Path(artifact), entry.base_dir),
        name=entry.name,
        dest_dir=Path(dest_dir) if dest_dir else Path(),
        install_cmd=install,
        uninstall_cmd=uninstall,
        check_cmd=check,
    )


PRODUCT_KINDS.register("file", _file_kind, origin=__name__)
DEV_TOOL_KINDS.register("file", _file_kind, origin=__name__)
