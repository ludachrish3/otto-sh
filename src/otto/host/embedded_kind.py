"""
The built-in ``embedded`` kind — a binary loaded into an embedded target's runtime.

An extension has no filesystem home: the load IS the transfer, so ``stage``
is a no-op and ``install`` pushes the object through the host's pluggable
:class:`~otto.host.binary_loader.BinaryLoader` — Zephyr's ``llext-hex`` is the
first one otto ships, not the only one a project can register. Modelling it
as a product is what lets the coverage pipeline treat a board like any other
host: the product name is the ``<product>`` segment of the run tree, and the
instrumentation scan sees the ``.gcda`` filename strings in the object's
``.rodata`` exactly as it does in a Unix binary.

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
from .product import ShellProduct
from .shell_kind import bool_param, reject_retired_params, str_list_param, str_param

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

_DEFAULT_RESET_FN = "cov_reset"
"""The exported reset function an entry gets when it names none."""

_RESET_CONFIRMATION = "gcov_clear"
"""The line embedded-gcov's ``__gcov_clear()`` prints when it has zeroed the
counters (under ``GCOV_OPT_PRINT_STATUS``, on by default in its
``gcov_public.h``). A reset counts only when the board prints it: the Zephyr
shell answers a call to a function the extension does not export with a
silent success, so a bare success proves nothing ran. It must be a whole line
(``_reset_confirmed``): a ``reset_fn`` named ``gcov_clear`` puts the word in
the echoed call too."""


def _reset_confirmed(output: str) -> bool:
    """Return True when a line of *output*, stripped, is exactly ``gcov_clear``."""
    return any(line.strip() == _RESET_CONFIRMATION for line in output.splitlines())


_RESET_FIX = (
    "An extension resets its counters by exporting `void cov_reset(void) { __gcov_clear(); }` "
    "(built with GCOV_OPT_PROVIDE_CLEAR_COUNTERS); set reset_fn to use another name."
)

_VALID = "artifact, call_after_load, dump_fn, reset_fn, instrumented, debug_log_globs"


@dataclass
class EmbeddedProduct(ShellProduct):
    """A ``kind = "embedded"`` entry's runtime form."""

    call_after_load: list[str] = field(default_factory=list)
    """Exported functions called, in order, right after a successful load
    (``["cov_init"]`` runs the embedded-gcov constructor)."""

    dump_fn: str = _DEFAULT_DUMP_FN
    """The exported function the embedded coverage collector calls to dump counters."""

    reset_fn: str = _DEFAULT_RESET_FN
    """The exported function that zeroes this extension's counters in place
    (``void cov_reset(void) { __gcov_clear(); }``)."""

    @staticmethod
    def _loader(host: Any) -> "BinaryLoader":
        """Return *host*'s binary loader, or fail loud naming the host."""
        loader = getattr(host, "loader", None)
        if loader is None:
            who = getattr(host, "id", None) or type(host).__name__
            raise ValueError(f"{who} has no binary loader")
        return loader

    @property
    @override
    def stages_artifact(self) -> bool:
        """Answer False — the load IS the transfer, so no file is placed under a directory.

        Which is also why an ``embedded`` entry takes no ``stage_dir`` param —
        there is no destination to name — and why two extensions sharing an
        object basename are not a staging collision.
        """
        return False

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

    @override
    async def reset_coverage(self, host: "Host") -> Result:
        """Zero this extension's counters in place by calling :attr:`reset_fn`.

        An embedded target has no ``.gcda`` files to delete: its counters
        live in the loaded extension's memory, so the reset is a call into
        the extension, the same way the dump is. Three answers are not plain
        failures:

        - a declined call (dry run) is returned as declined;
        - a board where the extension is not loaded has no counters to clear,
          which the loader recognises from its device's own answer
          (:meth:`~otto.host.binary_loader.BinaryLoader.reports_not_loaded`)
          and which is a success;
        - a success counts only when the board printed the ``gcov_clear``
          line; without it the reset failed, naming both ways that happens.

        Messages never name the product: every consumer (``otto cov
        clean``'s report, :class:`~otto.coverage.errors.CoverageCleanError`)
        prefixes the host and product itself.
        """
        loader = self._loader(host)
        call = await host.exec(loader.call_command(self.name, self.reset_fn), timeout=_CALL_TIMEOUT)
        if call.status is Status.NotRun:
            return Result(Status.NotRun)
        output = call.value or ""
        if loader.reports_not_loaded(output, self.name):
            return Result(Status.Success, msg="not loaded, so there are no counters to clear")
        said = output.strip()
        if call.status is not Status.Success:
            detail = f": {said}" if said else " and the board printed nothing"
            return Result(
                Status.Error, msg=f"reset_fn {self.reset_fn!r} failed{detail}. {_RESET_FIX}"
            )
        if not _reset_confirmed(output):
            printed = (
                f"no {_RESET_CONFIRMATION!r} line in {said!r}" if said else "it printed nothing"
            )
            return Result(
                Status.Error,
                msg=(
                    f"reset_fn {self.reset_fn!r} failed: the board did not confirm the reset "
                    f"({printed}). Either the extension does not export {self.reset_fn!r} "
                    "(the Zephyr shell reports an unknown function as a silent success), or "
                    "embedded-gcov was built without GCOV_OPT_PRINT_STATUS. " + _RESET_FIX
                ),
            )
        return Result(Status.Success)


def _embedded_kind(entry: DeclaredEntry, host: "Host") -> EmbeddedProduct:
    """Build an :class:`EmbeddedProduct`; refuse a host with no binary loader."""
    if getattr(host, "loader", None) is None:
        raise ValueError(
            f"[[products]] {entry.name!r}: kind 'embedded' matched host "
            f"{getattr(host, 'id', '?')}, which has no binary loader — only embedded hosts "
            "with a `loader` can carry it"
        )
    params = dict(entry.params)
    # No `stage_dir` of its own (the load IS the transfer), but the retired
    # spelling still has to be answered the way every other kind answers it —
    # a generic "unknown param" would leave the reader guessing at the rename.
    reject_retired_params(entry, params)
    artifact = str_param(entry, params, "artifact", required=True)
    assert artifact is not None  # noqa: S101 — internal invariant: required=True makes str_param raise above when missing
    call_after_load = str_list_param(entry, params, "call_after_load")
    dump_fn = str_param(entry, params, "dump_fn")
    if dump_fn == "":
        raise ValueError(f"[[products]] {entry.name!r}: 'dump_fn' must not be empty")
    reset_fn = str_param(entry, params, "reset_fn")
    if reset_fn == "":
        raise ValueError(f"[[products]] {entry.name!r}: 'reset_fn' must not be empty")
    instrumented = bool_param(entry, params, "instrumented")
    debug_log_globs = str_list_param(entry, params, "debug_log_globs")
    if params:
        raise ValueError(
            f"[[products]] {entry.name!r}: kind 'embedded' got unknown param(s): "
            f"{sorted(params)}; valid: {_VALID}"
        )
    return EmbeddedProduct(
        # Local path: forward slashes in TOML, anchored to the declaring repo
        # (never the CWD). There is no dest_dir — the load has no destination.
        artifact=anchor_path(Path(artifact), entry.base_dir),
        name=entry.name,
        debug_log_globs=debug_log_globs,
        instrumented_override=instrumented,
        call_after_load=call_after_load,
        dump_fn=_DEFAULT_DUMP_FN if dump_fn is None else dump_fn,
        reset_fn=_DEFAULT_RESET_FN if reset_fn is None else reset_fn,
    )
