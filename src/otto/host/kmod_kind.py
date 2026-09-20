"""
The built-in ``kmod`` kind — a Linux kernel module as a product.

The kernel's module loader drives the verbs: ``install`` is
:meth:`~otto.host.unix_host.UnixHost.load` (stage + ``insmod``), ``is_installed``
reads :meth:`~otto.host.unix_host.UnixHost.lsmod`, ``uninstall`` is
:meth:`~otto.host.unix_host.UnixHost.unload`. Coverage comes one of three ways,
chosen per entry by ``coverage``:

``none``
    no counters of its own; the hooks are the defaults.
``module``
    the module links against ``otto_kgcov`` (``src/otto/kgcov/``), declared on
    its host as a ``kgcov`` dev tool (:mod:`otto.host.kmod_tool_kind`) that
    ``install`` loads on demand when it is not already resident: otto then
    passes ``gcov_dir=<cov_dir>`` to ``insmod``, ``prepare_coverage`` asks the
    library to dump through debugfs while the module is loaded (an unloaded
    module already dumped at exit), and ``reset_coverage`` zeroes it there.
``kernel``
    the kernel itself has ``CONFIG_GCOV_KERNEL``: counters live under
    ``/sys/kernel/debug/gcov/`` and ``prepare_coverage`` copies the module's
    subtree (``gcov_path``) into ``cov_dir``; ``reset_coverage`` writes each
    entry, never the global ``reset``.

Every method lands files in the ``GCOV_PREFIX=<cov_dir>`` layout with no
strip, so the fetcher, the run tree and the report see a user-space product.
The kernel writes as root, so the deletes run under sudo. Products only.
"""

import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import override

from ..declared import DeclaredEntry
from ..result import Result
from ..utils import Status, anchor_path
from .kmod_tool_kind import kgcov_tool_for
from .product import PRODUCT_KINDS, ShellProduct, cov_dir_of, cov_dir_of_name, sudo_gcda_delete
from .shell_kind import bool_param, str_list_param, str_param, substitute_placeholders

if TYPE_CHECKING:
    from .host import Host

logger = logging.getLogger(__name__)

KGCOV_DEBUGFS = "/sys/kernel/debug/otto_kgcov"
"""Where ``otto_kgcov`` exposes ``<module>/dump`` and ``<module>/reset``."""

KERNEL_GCOV_DEBUGFS = "/sys/kernel/debug/gcov"
"""The in-kernel gcov tree (``CONFIG_GCOV_KERNEL``); ``gcov_path`` lives under it."""

COVERAGE_METHODS = ("none", "module", "kernel")
"""The three ``coverage`` values a ``kmod`` product accepts."""

_VALID = (
    "artifact, module_name, params, coverage, gcov_path, cov_dir, instrumented, debug_log_globs"
)
_MODULE_VERBS = ("load", "unload", "lsmod")


def _sudo_sh(script: str) -> str:
    """Wrap *script* as one sudo-able command (redirections included)."""
    return f"sh -c {shlex.quote(script)}"


@dataclass
class KmodProduct(ShellProduct):
    """A ``kind = "kmod"`` entry's runtime form."""

    module_name: str = ""
    """What ``/proc/modules`` shows: the artifact stem with ``-`` → ``_`` unless declared."""

    params: str = ""
    """``insmod`` parameters, placeholders already expanded."""

    coverage: str = "none"
    """One of :data:`COVERAGE_METHODS`."""

    gcov_path: str | None = None
    """``coverage = "kernel"`` only: this module's subtree under :data:`KERNEL_GCOV_DEBUGFS`."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.module_name:
            self.module_name = self.artifact.stem.replace("-", "_")

    @override
    async def stage(self, host: "Host") -> Result:
        """No-op success: ``load`` stages the ``.ko`` itself and removes it after ``insmod``."""
        return Result(Status.Success)

    @override
    async def install(self, host: "Host") -> Result:
        params = self.params
        if self.coverage == "module":
            # A real error stops here; a NotRun does NOT. The session merely
            # declined the library's insmod, and the consumer's own line
            # below must still be announced (and declined in turn) — a dry
            # run exists to show the whole plan, and that second decline is
            # what carries the NotRun back as the overall result.
            library = await self._ensure_library(host)
            if not library.is_ok and library.status is not Status.NotRun:
                return library
            # UnixHost.load appends `params` to the insmod line UNQUOTED, so
            # the gcov_dir=<cov_dir> token is quoted as ONE argument here — a
            # cov_dir containing whitespace would otherwise split in two.
            gcov_dir_arg = shlex.quote(f"gcov_dir={cov_dir_of(self)}")
            params = f"{params} {gcov_dir_arg}".strip()
        result = await host.load(self.artifact, self.module_name, params=params)  # ty: ignore[unresolved-attribute]
        if self.coverage == "module" and not result.is_ok and result.status is not Status.NotRun:
            # The library is in (or was already): this is the CONSUMER's own
            # insmod failing, so the message says so and names the library's
            # state — otherwise a `vermagic`/`Unknown symbol` line reads like
            # otto_kgcov never loaded.
            return Result(
                Status.Error, msg=f"{self.name}: {result.msg} ({await self._kgcov_context(host)})"
            )
        return result

    async def _ensure_library(self, host: Any) -> Result:
        """Load the host's otto_kgcov dev tool when it is not resident; the consumer needs it.

        The tool's own ``install`` runs the interface check first. A decline
        (``NotRun``, a dry run) comes back as-is, like every other hook here,
        and the caller lets the consumer's own ``insmod`` be announced after
        it rather than treating the decline as a stop. ``install-tools``
        before this costs nothing: a resident library is left alone.
        ``cleanup`` removes it after the products.

        The load ANNOUNCES itself at INFO once whenever the library is not
        already resident — under a dry run that announcement is all that
        happens, as everywhere else in this file. The run's verbose.log is
        where a lane reads the line back.
        """
        tool = kgcov_tool_for(host)
        if tool is None:
            return Result(
                Status.Error,
                msg=f'{self.name}: coverage = "module" needs otto_kgcov on host '
                f"{getattr(host, 'id', '?')}, and no [[dev_tools]] entry of kind 'kgcov' "
                "matches that host",
            )
        # The tool's own install short-circuits on a resident library too,
        # so this pre-check is redundant for correctness — it is kept because
        # it keeps the interface read (a modinfo parse of the .ko) off the
        # hot path, and because it is what decides whether the INFO line
        # below is said at all.
        if await tool.is_installed(host):
            return Result(Status.Success)
        logger.info(f"{getattr(host, 'id', '?')}: {self.name}: loading otto_kgcov ({tool.name})")
        result = await tool.install(host)
        if result.status is Status.NotRun or result.is_ok:
            return result
        return Result(
            Status.Error, msg=f"{self.name}: loading otto_kgcov ({tool.name}) failed: {result.msg}"
        )

    async def _kgcov_context(self, host: Any) -> str:
        """Name the host's kgcov tool and whether the library is resident, for an error.

        ERROR PATHS ONLY: it costs an extra ``lsmod``, which a hook that
        succeeded must never pay. Residency is stated rather than asked —
        the answer is one read away, and the reader has neither the host nor
        the tool name in hand.
        """
        tool = kgcov_tool_for(host)
        if tool is None:
            return "otto_kgcov: no kgcov dev tool declared for this host"
        resident = "resident" if await tool.is_installed(host) else "not resident"
        return f"otto_kgcov: {tool.name!r} dev tool, {resident}"

    @override
    async def uninstall(self, host: "Host") -> Result:
        return await host.unload(self.module_name)  # ty: ignore[unresolved-attribute]

    @override
    async def is_installed(self, host: "Host") -> bool:
        """Resident when ``lsmod`` SUCCEEDS and lists the name; a failed read is not-installed."""
        listing = await host.lsmod()  # ty: ignore[unresolved-attribute]
        return listing.is_ok and self.module_name in listing.value

    # ── hooks ────────────────────────────────────────────────────────────────

    @override
    async def prepare_coverage(self, host: "Host") -> Result:
        if self.coverage == "module":
            return await self._prepare_module(host)
        if self.coverage == "kernel":
            return await self._prepare_kernel(host)
        return await super().prepare_coverage(host)

    @override
    async def reset_coverage(self, host: "Host") -> Result:
        if self.coverage == "none":
            return await super().reset_coverage(host)
        if self.coverage == "module":
            loaded = await self._module_loaded(host)
            if loaded.status is Status.NotRun:
                # lsmod itself declined: residency is unknowable, so announce
                # the write a real run might issue too (declined in turn by
                # the same session) — the shared delete below closes it out.
                reset_file = shlex.quote(self._kgcov_file("reset"))
                await self._run_sudo(host, f"echo 1 > {reset_file}")
            else:
                if not loaded.is_ok:
                    return loaded
                if loaded.value:
                    reset_file = shlex.quote(self._kgcov_file("reset"))
                    zero = await self._run_sudo(host, f"echo 1 > {reset_file}")
                    if not zero.is_ok:
                        if zero.status is Status.NotRun:
                            return zero  # the session declined; nothing failed
                        return Result(
                            Status.Error,
                            msg=f"{zero.msg} ({await self._kgcov_context(host)})",
                        )
        else:
            # Guarded by `test -d` first, for the same reason _prepare_kernel's
            # script is: an empty `find` (a missing gcov_path) would otherwise
            # exit 0 via the trailing loop's own status. See that method's
            # comment for the full rationale; paths are quoted the same way.
            gcov_path_q = shlex.quote(self.gcov_path or "")
            script = (
                f'test -d {gcov_path_q} && find {gcov_path_q} -name "*.gcda" -type f | '
                'while read -r f; do echo 1 > "$f" || exit 1; done'
            )
            zero = await self._run_sudo(host, script)
            if not zero.is_ok and zero.status is not Status.NotRun:
                return self._kernel_gcov_error(zero)
            # A NotRun here (the session declining under --dry-run) falls
            # through to the shared delete below, which announces its own
            # command and carries the same NotRun back as the overall result.
        # The kernel wrote the files as root: the default delete, elevated.
        return await sudo_gcda_delete(self, host)

    def _kgcov_file(self, name: str) -> str:
        return f"{KGCOV_DEBUGFS}/{self.module_name}/{name}"

    def _kernel_gcov_error(self, result: Result) -> Result:
        """Build the friendly ``Error`` naming :attr:`gcov_path` for a failed kernel-gcov script.

        Called from both the read side (:meth:`_prepare_kernel`) and the write
        side (:meth:`reset_coverage`'s kernel arm), so the wording covers both.
        """
        return Result(
            Status.Error,
            msg=f"{self.name}: cannot read or write {self.gcov_path} — does this kernel have "
            f"CONFIG_GCOV_KERNEL, and is the module loaded or its data kept "
            f"(gcov_persist=1)? ({result.msg})",
        )

    async def _module_loaded(self, host: Any) -> Result:
        """Read ``lsmod`` once; ``value`` carries whether this module is resident.

        Unlike :meth:`is_installed` (whose ``bool`` return cannot carry a
        decline), this is what the coverage hooks call: ``Status.NotRun`` on a
        declined read propagates as-is — the session already announced the
        decline — and a failed (non-declined) read becomes ``Status.Error``
        naming ``lsmod``'s own message, rather than being silently read as
        "not installed".
        """
        listing = await host.lsmod()
        if listing.status is Status.NotRun:
            return Result(Status.NotRun)
        if not listing.is_ok:
            return Result(Status.Error, msg=f"{self.name}: lsmod failed: {listing.msg}")
        return Result(Status.Success, value=self.module_name in listing.value)

    async def _run_sudo(self, host: Any, script: str) -> Result:
        """Run *script* under sudo through the host's shell session; a plain Result.

        A result whose status is ``Status.NotRun`` is the session declining
        the command under ``otto --dry-run`` — it is returned as-is
        (``Result(Status.NotRun)``), never mapped to ``Status.Error``, so both
        hooks propagate the decline and the remote fetcher
        (:mod:`otto.coverage.fetcher.remote`) treats it as a dry run rather
        than a failure.
        """
        needs_sh = any(ch in script for ch in "><|;")
        result = await host.run(_sudo_sh(script) if needs_sh else script, sudo=True)
        if result.status is Status.NotRun:
            return Result(Status.NotRun)
        if result.is_ok:
            return Result(Status.Success)
        output = result.only.value.strip() if hasattr(result, "only") else ""
        return Result(Status.Error, msg=f"{self.name}: `{script}` failed: {output}")

    async def _prepare_module(self, host: Any) -> Result:
        loaded = await self._module_loaded(host)
        if loaded.status is not Status.NotRun:
            if not loaded.is_ok:
                return loaded
            if not loaded.value:
                # Unloaded: otto_kgcov dumped at KGCOV_EXIT(); the files are already there.
                return Result(Status.Success)
        # lsmod declined (dry run) or the module is loaded: announce the
        # debugfs write either way — a decline here becomes the session's own
        # [DRY RUN] line, and the overall result mirrors it (Status.NotRun).
        dump = self._kgcov_file("dump")
        result = await self._run_sudo(host, f"echo 1 > {shlex.quote(dump)}")
        if result.status is Status.NotRun or result.is_ok:
            return result
        return Result(
            Status.Error,
            msg=f"{self.name}: cannot write {dump} — {await self._kgcov_context(host)}; was the "
            f"module built with its consumer snippet? ({result.msg})",
        )

    async def _prepare_kernel(self, host: Any) -> Result:
        assert self.gcov_path is not None  # noqa: S101 — the builder requires it for this method
        rel = self.gcov_path[len(KERNEL_GCOV_DEBUGFS) + 1 :]
        cov_dir = cov_dir_of(self)
        # debugfs entries report size 0, so `cp` and `scp` fetch nothing; `cat`
        # reads them whole. The tree is kept relative to the gcov root, which
        # is the GCOV_PREFIX layout every other method produces. Guarded by
        # `test -d` first: an empty `find` (a missing gcov_path) would
        # otherwise exit 0 via the trailing `while` loop's own status —
        # nothing to iterate is not the same as "reached the path". Paths are
        # individually shlex.quoted; the whole script is wrapped once more by
        # _run_sudo when it needs `sh -c`.
        gcov_root = shlex.quote(KERNEL_GCOV_DEBUGFS)
        rel_q = shlex.quote(rel)
        dest = shlex.quote(cov_dir)
        script = (
            f'cd {gcov_root} && test -d {rel_q} && find {rel_q} -name "*.gcda" -type f | '
            f'while read -r f; do mkdir -p {dest}/"$(dirname "$f")" && '
            f'cat "$f" > {dest}/"$f" || exit 1; done'
        )
        result = await self._run_sudo(host, script)
        if result.status is Status.NotRun or result.is_ok:
            return result
        return self._kernel_gcov_error(result)


def _kmod_kind(entry: DeclaredEntry, host: "Host") -> KmodProduct:
    """Build a :class:`KmodProduct`; refuse a host without the module verbs."""
    missing = [v for v in _MODULE_VERBS if not callable(getattr(host, v, None))]
    if missing:
        raise ValueError(
            f"[[products]] {entry.name!r}: kind 'kmod' matched host {getattr(host, 'id', '?')}, "
            "which has no load/unload/lsmod — only Unix hosts can carry a kernel module"
        )
    params = dict(entry.params)
    artifact = str_param(entry, params, "artifact", required=True)
    assert artifact is not None  # noqa: S101 — required=True raises above when missing
    if not artifact.endswith(".ko"):
        raise ValueError(f"[[products]] {entry.name!r}: 'artifact' must be a .ko, got {artifact!r}")
    module_name = str_param(entry, params, "module_name")
    insmod_params = str_param(entry, params, "params") or ""
    coverage = str_param(entry, params, "coverage") or "none"
    if coverage not in COVERAGE_METHODS:
        valid = ", ".join(COVERAGE_METHODS)
        raise ValueError(
            f"[[products]] {entry.name!r}: 'coverage' must be one of {valid}, got {coverage!r}"
        )
    gcov_path = str_param(entry, params, "gcov_path")
    if coverage == "kernel":
        if not gcov_path:
            raise ValueError(
                f"[[products]] {entry.name!r}: 'gcov_path' is required with coverage = \"kernel\""
            )
        if not gcov_path.startswith(KERNEL_GCOV_DEBUGFS + "/"):
            raise ValueError(
                f"[[products]] {entry.name!r}: 'gcov_path' must be under {KERNEL_GCOV_DEBUGFS}/, "
                f"got {gcov_path!r}"
            )
    elif gcov_path is not None:
        raise ValueError(
            f"[[products]] {entry.name!r}: 'gcov_path' only applies with coverage = \"kernel\""
        )
    if coverage == "module" and "gcov_dir=" in insmod_params:
        raise ValueError(
            f"[[products]] {entry.name!r}: 'params' must not set gcov_dir — otto passes the "
            "entry's cov_dir"
        )
    cov_dir = str_param(entry, params, "cov_dir")
    if cov_dir == "":
        raise ValueError(f"[[products]] {entry.name!r}: 'cov_dir' must not be empty")
    instrumented = bool_param(entry, params, "instrumented")
    debug_log_globs = str_list_param(entry, params, "debug_log_globs")
    if params:
        raise ValueError(
            f"[[products]] {entry.name!r}: kind 'kmod' got unknown param(s): "
            f"{sorted(params)}; valid: {_VALID}"
        )
    values = {"cov_dir": cov_dir or cov_dir_of_name(entry.name), "name": entry.name}
    expanded = substitute_placeholders(entry, "params", insmod_params, values) or ""
    return KmodProduct(
        artifact=anchor_path(Path(artifact), entry.base_dir),
        name=entry.name,
        cov_dir=cov_dir,
        debug_log_globs=debug_log_globs,
        instrumented_override=instrumented,
        module_name=module_name or "",
        params=expanded,
        coverage=coverage,
        gcov_path=gcov_path,
    )


PRODUCT_KINDS.register("kmod", _kmod_kind, origin=__name__)
