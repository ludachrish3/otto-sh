"""Capture ``.gcda`` counters into lcov tracefiles.

Wraps ``lcov --capture``, executing it through
:class:`~otto.host.local_host.LocalHost` so it is fully async with proper
logging and timeout handling.
"""

import logging
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ...host.local_host import LocalHost
from ...host.toolchain_discovery import ensure_gcov_tool
from ...logger.mode import LogMode
from ...utils import Status
from .gcovfile import incompatibility

if TYPE_CHECKING:
    from ...host.toolchain import Toolchain
    from ..pairing import Pair

logger = logging.getLogger(__name__)


def _extra_args(toolchain: "Toolchain | None") -> str:
    """Render the host's ``toolchain.lcov_args`` as a leading-space fragment.

    The arguments belong to the lcov CAPTURE of that host's data: they say
    what reading this host's counters needs, which is why they are a property
    of its record. Empty — and the command byte-identical to what it was —
    for a host that declares none, which is every host until one needs lcov
    told something otto cannot know (a clang-built kernel module's unopenable
    header records, issue #385). Each entry is quoted: it is one argv token,
    and the command is a string run through a shell.
    """
    if toolchain is None or not toolchain.lcov_args:
        return ""
    return " " + " ".join(shlex.quote(arg) for arg in toolchain.lcov_args)


def short_failure(output: str, log_path: Path | None, tail: int = 20) -> str:
    """Return the first ``ERROR:`` line, the last *tail* lines, and where the full output is."""
    lines = (output or "").splitlines()
    first = next((ln for ln in lines if "ERROR:" in ln), None)
    body = ([first, "..."] if first else []) + lines[-tail:]
    where = str(log_path) if log_path is not None else "verbose.log of this run"
    return "\n".join([*body, f"full lcov output: {where}"])


class LcovMerger:
    """Capture coverage with one ``lcov --capture`` per product or directory.

    Works with any GCC version and with clang's gcov-compatible mode.

    The *lcov* and *gcov* constructor arguments serve as **defaults**.
    Individual :meth:`capture_pairs` / :meth:`capture_dir` calls can
    override them via the *toolchain* parameter to support per-host
    toolchains.
    """

    def __init__(
        self,
        localhost: LocalHost,
        lcov: str = "lcov",
        gcov: str = "gcov",
    ) -> None:
        self.localhost = localhost
        self.lcov = lcov
        self.gcov = gcov

    async def capture_pairs(
        self, pairs: "list[Pair]", staging: Path, output: Path, toolchain: "Toolchain | None" = None
    ) -> Path:
        """Stage each pair (``.gcda`` copied, ``.gcno`` symlinked beside it) and capture once."""
        if staging.exists():
            shutil.rmtree(staging)
        for pair in pairs:
            data = staging / pair.rel
            data.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pair.gcda, data)
            data.with_name(data.name[: -len(".gcda")] + ".gcno").symlink_to(pair.gcno.resolve())
        return await self._run_capture(staging, output, toolchain, includes=None, count=len(pairs))

    async def capture_dir(
        self,
        directory: Path,
        output: Path,
        toolchain: "Toolchain | None" = None,
        includes: list[str] | None = None,
    ) -> Path:
        """Capture a directory whose ``.gcda`` already sit beside their ``.gcno``."""
        from ..errors import CoverageDataMismatchError

        mismatches = []
        for gcda in sorted(directory.rglob("*.gcda")):
            gcno = gcda.with_suffix(".gcno")
            if not gcno.is_file():
                mismatches.append(f"{gcda}: no notes file {gcno}")
                continue
            reason = incompatibility(gcda, gcno)
            if reason is not None:
                mismatches.append(reason)
        if mismatches:
            raise CoverageDataMismatchError("\n".join(mismatches))
        return await self._run_capture(directory, output, toolchain, includes=includes, count=None)

    async def _run_capture(
        self,
        directory: Path,
        output: Path,
        toolchain: "Toolchain | None",
        *,
        includes: list[str] | None,
        count: int | None,
    ) -> Path:
        from ...logger.management import verbose_log_path
        from ..errors import CaptureFailedError, CoverageDataMismatchError, CoverageToolVersionError

        lcov = toolchain.lcov_bin if toolchain else self.lcov
        gcov = ensure_gcov_tool(toolchain.gcov_bin if toolchain else self.gcov, output.parent)
        include_args = ""
        if includes:
            include_args = " --ignore-errors unused" + "".join(
                f" --include {shlex.quote(p)}" for p in includes
            )
        cmd = (
            f"{lcov} --capture --directory {shlex.quote(str(directory))}"
            f" --gcov-tool {gcov} --rc branch_coverage=1 --ignore-errors source"
            f"{_extra_args(toolchain)}{include_args} --output-file {shlex.quote(str(output))}"
        )
        logger.info(
            "lcov capture%s -> %s", f": {count} file(s)" if count is not None else "", output
        )
        result = await self.localhost.exec(cmd, timeout=300, log=LogMode.QUIET)
        if result.status != Status.Success:
            raw = result.value or ""
            detail = short_failure(raw, verbose_log_path())
            if "stamp mismatch" in raw:
                raise CoverageDataMismatchError(detail)
            if "Incompatible GCC/GCOV version" in raw:
                raise CoverageToolVersionError(detail)
            raise CaptureFailedError(f"lcov --capture failed:\n{detail}")
        return output
