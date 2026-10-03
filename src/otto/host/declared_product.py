"""
:class:`DeclaredProduct` — the public base for a declared product or dev tool.

A project subclasses it to give a declared product (or dev tool) its own
behaviour. It is the ``shell`` kind's runtime form: one artifact, staged via
:meth:`~otto.host.host.Host.put`, with optional ``install``/``uninstall``/
``check`` command strings run on the host. One class serves BOTH seams —
:class:`~otto.host.product.Product` and :class:`~otto.host.dev_tool.DevTool`
share one abstract surface — and which seam an instance lives in is decided by
the registry that built it, never by the type. It has its own module because
:mod:`otto.host.dev_tool` imports :mod:`otto.host.product`, so a class that
subclasses both can live in neither.

The defaults are the honest floor of today's Host surface: without an
``install`` command, install is a no-op success (staging placed the artifact);
without ``check``, ``is_installed`` answers False — otto assumes not installed
and re-stages, which is safe for the simple cases this class serves.
``host.exists()`` does not exist yet (see
:class:`~otto.host.product.ShellProduct`); when the remote file-ops phase
lands, the ``check`` default upgrades to an artifact-existence test. A
``[[products]]`` entry with ``kind = "shell"`` builds this class as is; an
entry with ``class = "pkg.mod:Sub"`` builds a subclass, which overrides any of
``stage``/``install``/``uninstall``/``is_installed``/``get_logs``/``plan``.

**Plan honesty.** :meth:`DeclaredProduct.plan` describes the declared command strings. A
subclass that replaces ``install`` (or ``stage``/``uninstall``) without
replacing ``plan`` is NOT previewed with the string that will never run, nor
with nothing — that step is reported unchecked, naming the method, so
``otto -n run install`` says what it cannot see. A subclass that wants a real
preview overrides ``plan``. A subclass that replaces ``stage`` also skips the
stage-directory lookup: its plan names no staging directory, refusal or
login-home gap, because its ``stage`` need not read them and the plan must not
claim a lookup the run may never make.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from typing_extensions import override

from ..result import Result
from ..utils import Status
from .dev_tool import DevTool
from .product import ProductPlan, ShellProduct, planned_stage_dir, put_line

if TYPE_CHECKING:
    from .host import Host

_PLANNED_HOOKS = ("stage", "install", "uninstall")
"""The hooks a plan describes, in phase order; an override of any is reported unchecked."""


@dataclass
class DeclaredProduct(ShellProduct, DevTool):
    """A ``kind = "shell"`` entry's runtime form, and the base for ``class =`` entries.

    ``stage`` is :class:`~otto.host.product.ShellProduct`'s (``host.put``);
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

    def _overridden(self, hook: str) -> str | None:
        """``"Sub.hook"`` when the subclass replaced *hook*, else ``None``."""
        if getattr(type(self), hook) is getattr(DeclaredProduct, hook):
            return None
        # Name the class that DEFINES the hook, not the leaf that inherited it.
        owner = next(c for c in type(self).__mro__ if hook in vars(c))
        return f"{owner.__name__}.{hook}"

    @override
    def plan(self, host: "Host") -> ProductPlan:
        # The seam the entry was declared in, as the registry stamped it; an
        # object built by hand carries no entry and reads as a product.
        seam = self.source_entry.seam if self.source_entry is not None else "products"
        noun = "dev tool" if seam == "dev_tools" else "product"
        unchecked: list[str] = []
        stage_lines: list[str] = []
        if self._overridden("stage") is None:
            where = planned_stage_dir(self.stage_dir, host, who=f"{noun} {self.name!r}")
            if where.refusal is not None:
                return ProductPlan(unchecked=[where.refusal])
            if where.unchecked is not None:
                unchecked.append(where.unchecked)
            stage_lines = [put_line(self.artifact, where.directory)]
        declared = {
            "stage": stage_lines,
            "install": [] if self.install_cmd is None else [self.install_cmd],
            "uninstall": [] if self.uninstall_cmd is None else [self.uninstall_cmd],
        }
        for hook in _PLANNED_HOOKS:
            replaced = self._overridden(hook)
            if replaced is not None:
                declared[hook] = []
                unchecked.append(f"{hook}: {replaced} (code; no plan)")
        return ProductPlan(
            stage=declared["stage"],
            install=declared["install"],
            uninstall=declared["uninstall"],
            unchecked=unchecked,
        )
