"""
The built-in ``llext`` kind — a Zephyr LLEXT extension as a product.

An extension has no filesystem home: the load IS the transfer, so ``stage``
is a no-op and ``install`` pushes the object through the host's binary
loader. Modelling it as a product is what lets the coverage pipeline treat
a board like any other host: the product name is the ``<product>`` segment
of the run tree, and the instrumentation scan sees the ``.gcda`` filename
strings in the object's ``.rodata`` exactly as it does in a Unix binary.

Products only — there is no dev-tool analog, because a dev tool is a helper
the host runs, not code loaded into the device's own runtime. Unlike the
``shell`` kind, an entry here takes no ``{cov_dir}``/``{name}`` substitution:
it declares no command strings to substitute into.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import override

from ..declared import DeclaredEntry
from ..result import Result
from ..utils import Status, anchor_path
from .product import PRODUCT_KINDS, ShellProduct
from .shell_kind import bool_param, str_list_param, str_param

if TYPE_CHECKING:
    from .binary_loader import BinaryLoader
    from .host import Host

_CALL_TIMEOUT = 60.0
"""Seconds allowed one ``call_after_load`` function — a coverage constructor
walks every instrumented translation unit, so it is not instant."""

_LIST_TIMEOUT = 20.0
"""Seconds allowed the loader's list command; it only prints what is resident."""

_DEFAULT_DUMP_FN = "cov_dump"
"""The exported dump function an entry gets when it names none."""

_VALID = "artifact, call_after_load, dump_fn, instrumented, debug_log_globs"


@dataclass
class LlextProduct(ShellProduct):
    """A ``kind = "llext"`` entry's runtime form."""

    call_after_load: list[str] = field(default_factory=list)
    """Exported functions called, in order, right after a successful load
    (``["cov_init"]`` runs the embedded-gcov constructor)."""

    dump_fn: str = _DEFAULT_DUMP_FN
    """The exported function the embedded coverage collector calls to dump counters."""

    @staticmethod
    def _loader(host: Any) -> "BinaryLoader":
        """Return *host*'s binary loader, or fail loud naming the host."""
        loader = getattr(host, "loader", None)
        if loader is None:
            who = getattr(host, "id", None) or type(host).__name__
            raise ValueError(f"{who} has no binary loader")
        return loader

    @override
    async def stage(self, host: "Host") -> Result:
        """No-op success — an extension has no staging step; the load is the transfer."""
        return Result(Status.Success)

    @override
    async def install(self, host: "Host") -> Result:
        """Load the object, then call each :attr:`call_after_load` function in order.

        Returns the load's own result on a load failure, and stops at the
        first function that fails, naming it — a half-initialized extension
        is not an install. The loader is resolved BEFORE the load, so a host
        that cannot carry this product is refused without touching it.
        """
        loader = self._loader(host)
        result = await host.load(self.artifact, self.name)  # ty: ignore[unresolved-attribute]
        if not result.is_ok:
            return result
        for fn in self.call_after_load:
            call = await host.exec(loader.call_command(self.name, fn), timeout=_CALL_TIMEOUT)
            if call.status is not Status.Success:
                return Result(
                    Status.Error, msg=f"{self.name}: {fn} failed after load: {call.value}"
                )
        return Result(Status.Success)

    @override
    async def uninstall(self, host: "Host") -> Result:
        """Unload the extension, returning ``host.unload``'s result unchanged."""
        return await host.unload(self.name)  # ty: ignore[unresolved-attribute]

    @override
    async def is_installed(self, host: "Host") -> bool:
        """Ask the loader's list command whether this extension is resident.

        A list command that did not SUCCEED answers False, never a match
        against its output. On a wedged or timed-out console the capture can
        hold the echo of the earlier ``llext load_hex <name> ...`` or a stale
        listing, which the name match would read as resident — and a True here
        skips the install, so ``call_after_load`` never runs and the run ends
        with silently empty coverage. Not-installed is the safe answer: the
        caller re-installs, which is idempotent.
        """
        loader = self._loader(host)
        listing = await host.exec(loader.list_command(), timeout=_LIST_TIMEOUT)
        if listing.status is not Status.Success:
            return False
        return loader.is_loaded(self.name, listing.value)


def _llext_kind(entry: DeclaredEntry, host: "Host") -> LlextProduct:
    """Build an :class:`LlextProduct`; refuse a host with no binary loader."""
    if getattr(host, "loader", None) is None:
        raise ValueError(
            f"[[products]] {entry.name!r}: kind 'llext' matched host {getattr(host, 'id', '?')}, "
            "which has no binary loader — only embedded hosts with a `loader` can carry it"
        )
    params = dict(entry.params)
    artifact = str_param(entry, params, "artifact", required=True)
    assert artifact is not None  # noqa: S101 — internal invariant: required=True makes str_param raise above when missing
    call_after_load = str_list_param(entry, params, "call_after_load")
    dump_fn = str_param(entry, params, "dump_fn")
    if dump_fn == "":
        raise ValueError(f"[[products]] {entry.name!r}: 'dump_fn' must not be empty")
    instrumented = bool_param(entry, params, "instrumented")
    debug_log_globs = str_list_param(entry, params, "debug_log_globs")
    if params:
        raise ValueError(
            f"[[products]] {entry.name!r}: kind 'llext' got unknown param(s): "
            f"{sorted(params)}; valid: {_VALID}"
        )
    return LlextProduct(
        # Local path: forward slashes in TOML, anchored to the declaring repo
        # (never the CWD). There is no dest_dir — the load has no destination.
        artifact=anchor_path(Path(artifact), entry.base_dir),
        name=entry.name,
        debug_log_globs=debug_log_globs,
        instrumented_override=instrumented,
        call_after_load=call_after_load,
        dump_fn=_DEFAULT_DUMP_FN if dump_fn is None else dump_fn,
    )


PRODUCT_KINDS.register("llext", _llext_kind, origin=__name__)
