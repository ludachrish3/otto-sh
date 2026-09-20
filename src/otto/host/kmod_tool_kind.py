"""
The built-in ``kmod`` DEV-TOOL kind, and its ``kgcov`` subtype.

A kernel module a repo places on a host as tooling rather than as software
under test: a tracer, a test driver, or — the subtype — the otto_kgcov
coverage library. The kernel's module loader drives the verbs, exactly as
for the ``kmod`` PRODUCT kind (:mod:`otto.host.kmod_kind`): ``install`` is
:meth:`~otto.host.unix_host.UnixHost.load`, ``uninstall`` is
:meth:`~otto.host.unix_host.UnixHost.unload`, ``is_installed`` reads
:meth:`~otto.host.unix_host.UnixHost.lsmod`. What differs is the lifecycle a
dev tool has: installed by ``install-tools`` before any product, removed by
``cleanup`` after every product, never part of the product ``is_installed``
answer — and no coverage of its own, so none of the product kind's coverage
params exist here.

``kgcov`` fixes the module name to ``otto_kgcov`` and adds the library's
interface check: a built ``.ko`` reports ``<otto version>+kgcov<n>`` through
``MODULE_VERSION`` (:mod:`otto.kgcov`), and one whose ``n`` is not this
otto's :data:`otto.kgcov.INTERFACE` is refused — at lab load when the file
exists, and again at ``install``, before it is ever loaded. Lab load also
refuses both ways the binding can fail: a host matching two kgcov entries (a
host has one kernel and holds one otto_kgcov), and a host whose
``coverage = "module"`` product matches none. Which product needs the
library, and how it is loaded on demand, is the product kind's side
(:mod:`otto.host.kmod_kind`).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import override

from ..declared import DeclaredEntry
from ..result import Result
from ..utils import Status, anchor_path
from .dev_tool import DEV_TOOL_KINDS, DevTool
from .shell_kind import str_param

if TYPE_CHECKING:
    from .host import Host

KGCOV_MODULE_NAME = "otto_kgcov"
"""What ``/proc/modules`` shows for the library; the ``kgcov`` kind fixes it."""

_VALID = "artifact, module_name, params"
_VALID_KGCOV = "artifact, params, source"
_MODULE_VERBS = ("load", "unload", "lsmod")


@dataclass
class KmodTool(DevTool):
    """A ``kind = "kmod"`` dev tool's runtime form."""

    name: str
    artifact: Path
    module_name: str = ""
    """What ``/proc/modules`` shows: the artifact stem with ``-`` → ``_`` unless declared."""

    params: str = ""
    """``insmod`` parameters, verbatim (no placeholders: a dev tool has no cov_dir)."""

    owner: str | None = None

    def __post_init__(self) -> None:
        if not self.module_name:
            self.module_name = self.artifact.stem.replace("-", "_")

    @override
    async def stage(self, host: "Host") -> Result:
        """No-op success: ``load`` stages the ``.ko`` itself and removes it after ``insmod``."""
        return Result(Status.Success)

    @override
    async def install(self, host: "Host") -> Result:
        """Load the module; a module of this name already resident is left in place.

        Idempotent, mirroring :meth:`~otto.host.unix_host.UnixHost.unload`:
        ``install-tools`` asked for twice, or a run whose ``cleanup`` never
        got to unload it, would otherwise reach an ``insmod`` that fails with
        ``File exists``. Under a dry run ``lsmod`` declines, so residency
        reads as false and the load's own decline is what gets announced.
        """
        if await self.is_installed(host):
            return Result(Status.Success)
        return await host.load(self.artifact, self.module_name, params=self.params)  # ty: ignore[unresolved-attribute]

    @override
    async def uninstall(self, host: "Host") -> Result:
        return await host.unload(self.module_name)  # ty: ignore[unresolved-attribute]

    @override
    async def is_installed(self, host: "Host") -> bool:
        """Resident when ``lsmod`` SUCCEEDS and lists the name; a failed read is not-installed."""
        listing = await host.lsmod()  # ty: ignore[unresolved-attribute]
        return listing.is_ok and self.module_name in listing.value


def _require_module_verbs(entry: DeclaredEntry, host: Any) -> None:
    missing = [v for v in _MODULE_VERBS if not callable(getattr(host, v, None))]
    if missing:
        raise ValueError(
            f"[[dev_tools]] {entry.name!r}: kind {entry.kind!r} matched host "
            f"{getattr(host, 'id', '?')}, which has no load/unload/lsmod — only Unix hosts can "
            "carry a kernel module"
        )


def _artifact_param(entry: DeclaredEntry, params: dict[str, Any]) -> Path:
    artifact = str_param(entry, params, "artifact", required=True)
    assert artifact is not None  # noqa: S101 — required=True raises above when missing
    if not artifact.endswith(".ko"):
        raise ValueError(
            f"[[dev_tools]] {entry.name!r}: 'artifact' must be a .ko, got {artifact!r}"
        )
    return anchor_path(Path(artifact), entry.base_dir)


def _params_param(entry: DeclaredEntry, params: dict[str, Any]) -> str:
    raw = str_param(entry, params, "params") or ""
    if "{" in raw or "}" in raw:
        raise ValueError(
            f"[[dev_tools]] {entry.name!r}: 'params' takes no placeholders (a dev tool has no "
            f"cov_dir), got {raw!r}"
        )
    return raw


def _kmod_tool_kind(entry: DeclaredEntry, host: "Host") -> KmodTool:
    """Build a :class:`KmodTool`; refuse a host without the module verbs."""
    _require_module_verbs(entry, host)
    params = dict(entry.params)
    artifact = _artifact_param(entry, params)
    module_name = str_param(entry, params, "module_name") or ""
    insmod_params = _params_param(entry, params)
    if params:
        raise ValueError(
            f"[[dev_tools]] {entry.name!r}: kind 'kmod' got unknown param(s): "
            f"{sorted(params)}; valid: {_VALID}"
        )
    return KmodTool(
        name=entry.name, artifact=artifact, module_name=module_name, params=insmod_params
    )


DEV_TOOL_KINDS.register("kmod", _kmod_tool_kind, origin=__name__)


@dataclass
class KgcovTool(KmodTool):
    """A ``kind = "kgcov"`` dev tool: the otto_kgcov library built for one kernel."""

    source: Path | None = None
    """The vendored sources this build came from (for the drift advisory); optional."""

    def __post_init__(self) -> None:
        self.module_name = KGCOV_MODULE_NAME

    @override
    async def install(self, host: "Host") -> Result:
        """Refuse a wrong-interface artifact, then load it unless it is already resident.

        The interface check runs FIRST, before the base class's residency
        short-circuit: a ``.ko`` this otto cannot drive is refused whether or
        not a module of that name happens to be loaded.
        """
        problem = interface_problem(self)
        if problem is not None:
            return Result(Status.Error, msg=problem)
        return await super().install(host)


def interface_problem(tool: KgcovTool) -> str | None:
    """Why *tool*'s ``.ko`` cannot be loaded by this otto, or ``None`` when it can.

    Absent file: "not built"; no ``MODULE_VERSION``: not an otto_kgcov built
    from an export; a ``MODULE_VERSION`` carrying no ``+kgcov<n>`` suffix: said
    in words, never as "kgcovNone"; another interface number: the number. Every
    mismatch ends in the re-export remedy, which names the vendored directory
    only when the entry declares one — the artifact's parent is the BUILD
    directory, and advising an export into it would put sources where the
    ``.ko`` lands.
    """
    from ..kgcov import INTERFACE, interface_of, modinfo_version

    if not tool.artifact.is_file():
        return f"{tool.name}: {tool.artifact} is not built (no such file)"
    version = modinfo_version(tool.artifact)
    if version is None:
        return (
            f"{tool.name}: {tool.artifact} carries no MODULE_VERSION — not an otto_kgcov built "
            "from exported sources?"
        )
    found = interface_of(version)
    if found == INTERFACE:
        return None
    reports = (
        f"reports {version}, which carries no kgcov interface number"
        if found is None
        else f"reports {version} (interface kgcov{found})"
    )
    remedy = (
        f"re-export the library with `otto cov kgcov export {tool.source}` and rebuild"
        if tool.source is not None
        else "re-export the vendored library (declare `source` on the entry to have it named "
        "here) and rebuild"
    )
    return (
        f"{tool.name}: {tool.artifact} {reports}, but this otto drives interface "
        f"kgcov{INTERFACE} — {remedy}"
    )


def kgcov_tool_for(host: Any) -> KgcovTool | None:
    """Find the kgcov dev tool attached to *host*, or ``None`` (two are refused at lab load)."""
    for tool in getattr(host, "dev_tools", []):
        if isinstance(tool, KgcovTool):
            return tool
    return None


def check_kgcov_bindings(host: Any) -> None:
    """Refuse a host whose kgcov binding cannot hold, on either side.

    Two rules: a host matching TWO kgcov entries is refused — one kernel, one
    otto_kgcov — and a host carrying a ``coverage = "module"`` kernel-module
    product but NO kgcov entry is refused too, since that product's ``install``
    has no library to load.

    Called at the ingest chokepoint (:func:`otto.host.factory.apply_providers`)
    after the dev tools are attached.
    """
    from .kmod_kind import KmodProduct  # function-local: kmod_kind imports this module

    tools = [t for t in getattr(host, "dev_tools", []) if isinstance(t, KgcovTool)]
    if len(tools) > 1:
        names = ", ".join(repr(t.name) for t in tools)
        raise ValueError(
            f"host {getattr(host, 'id', '?')}: {len(tools)} [[dev_tools]] entries of kind "
            f"'kgcov' match it ({names}); a host runs one kernel and holds one otto_kgcov — "
            "narrow their `match` tables so exactly one applies"
        )
    needy = [
        p
        for p in getattr(host, "products", [])
        if isinstance(p, KmodProduct) and p.coverage == "module"
    ]
    if needy and not tools:
        names = ", ".join(repr(p.name) for p in needy)
        raise ValueError(
            f'[[products]] {names}: coverage = "module" on host {getattr(host, "id", "?")} '
            "needs otto_kgcov, and no [[dev_tools]] entry of kind 'kgcov' matches that host — "
            "declare one naming the .ko built for its kernel (see the kernel-modules docs page)"
        )


def _kgcov_tool_kind(entry: DeclaredEntry, host: "Host") -> KgcovTool:
    """Build a :class:`KgcovTool`; the interface check runs now when the ``.ko`` exists."""
    _require_module_verbs(entry, host)
    params = dict(entry.params)
    artifact = _artifact_param(entry, params)
    insmod_params = _params_param(entry, params)
    if "gcov_dir=" in insmod_params:
        raise ValueError(
            f"[[dev_tools]] {entry.name!r}: 'params' must not set gcov_dir — that is a "
            "consumer's parameter, which otto passes to the product's own insmod"
        )
    source = str_param(entry, params, "source")
    if params:
        raise ValueError(
            f"[[dev_tools]] {entry.name!r}: kind 'kgcov' got unknown param(s): "
            f"{sorted(params)}; valid: {_VALID_KGCOV}"
        )
    tool = KgcovTool(
        name=entry.name,
        artifact=artifact,
        params=insmod_params,
        source=anchor_path(Path(source), entry.base_dir) if source else None,
    )
    if tool.artifact.is_file():
        problem = interface_problem(tool)
        if problem is not None:
            raise ValueError(f"[[dev_tools]] {problem}")
    return tool


DEV_TOOL_KINDS.register("kgcov", _kgcov_tool_kind, origin=__name__)
