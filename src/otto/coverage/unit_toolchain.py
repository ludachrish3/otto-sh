"""The tools that capture a unit tier's directories: its own setting over the build's stamps.

A unit build is captured on the machine otto runs on, over build trees no lab
host owns, so its tools come from the tier's ``[coverage.tiers.<tier>.toolchain]``
table, and what the table leaves unset from the build itself: the notes' gcov
stamp chooses the counter reader by the rule e2e collection uses
(:func:`~otto.host.toolchain_discovery.gcov_for_stamp`), and ``lcov`` comes from
``PATH``. Everything resolves in the unit preflight, before the report touches
its destination; the harvest only uses what it resolved.
"""

import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..host import toolchain_discovery
from ..host.errors import CoverageToolMissingError
from ..host.toolchain import Toolchain, shell_unsafe
from ..host.toolchain_discovery import (
    StampSubject,
    gcov_for_stamp,
    gcov_stamp_major,
    is_llvm_cov,
    is_llvm_stamp,
    read_gcov_version,
)
from ..utils import anchor_path
from .errors import CoverageInputError

if TYPE_CHECKING:
    from .tiers import TierConfig

logger = logging.getLogger(__name__)

_GNU_GCOV_NAME = re.compile(r"^(?:.+-)?gcov(?:-\d+)?$")
"""A reader whose name proves it GNU gcov: ``gcov``, ``gcov-13``, ``arm-none-eabi-gcov``,
``x86_64-linux-gnu-gcov-13`` — never one containing ``llvm`` (see :func:`_proves_gnu`)."""


def _proves_gnu(name: str) -> bool:
    """Return whether the basename *name* proves a GNU gcov.

    ``llvm-gcov`` is the usual name of a script that runs ``llvm-cov gcov`` for
    clang data, so a name containing ``llvm`` proves nothing.
    """
    return "llvm" not in name and _GNU_GCOV_NAME.match(name) is not None


def setting_key(tier: str, field_name: str) -> str:
    """Return the full settings key of *tier*'s toolchain field *field_name*."""
    return f"coverage.tiers.{tier}.toolchain.{field_name}"


@dataclass(frozen=True)
class ConfiguredTool:
    """One configured tool: the value as written and the executable it resolved to."""

    written: str
    path: Path


@dataclass(frozen=True)
class ConfiguredTools:
    """A unit tier's configured tools, each resolved; ``None`` where the table is silent."""

    tier: str
    coverage_tool: ConfiguredTool | None = None
    lcov: ConfiguredTool | None = None
    lcov_args: list[str] = field(default_factory=list)


def resolve_configured(tier: "TierConfig", project_root: Path) -> ConfiguredTools:
    """Resolve *tier*'s configured ``coverage_tool`` and ``lcov`` to absolute executables.

    A value without a ``/`` is a command looked up on ``PATH``; one with a
    ``/`` is a path, ``~`` expanded and a relative one anchored at
    *project_root*.

    Raises:
        otto.coverage.errors.CoverageInputError: a configured tool is not on
            ``PATH``, is not an executable file, or resolves to a path lcov
            cannot run through its shell (``field=None``).
    """
    configured = tier.toolchain
    if configured is None:
        return ConfiguredTools(tier=tier.name)
    return ConfiguredTools(
        tier=tier.name,
        coverage_tool=_resolve_value(
            tier.name, "coverage_tool", configured.coverage_tool, project_root
        ),
        lcov=_resolve_value(tier.name, "lcov", configured.lcov, project_root),
        lcov_args=list(configured.lcov_args),
    )


def _resolve_value(
    tier: str, field_name: str, value: str | None, project_root: Path
) -> ConfiguredTool | None:
    if value is None:
        return None
    key = setting_key(tier, field_name)
    if "/" in value:
        try:
            path = anchor_path(Path(value), project_root)
        except ValueError as e:
            raise CoverageInputError(f"{key} = {value!r}: {e}", field=None) from e
        if not (path.is_file() and os.access(path, os.X_OK)):
            raise CoverageInputError(
                f"{key} = {value!r}: {path} is not an executable file. Install the tool there, "
                "or correct the setting (a path is anchored at the project root; a bare name "
                "is looked up on PATH).",
                field=None,
            )
    else:
        found = shutil.which(value)
        if found is None:
            raise CoverageInputError(
                f"{key} = {value!r}: no `{value}` command is on PATH. Install it, or correct "
                "the setting.",
                field=None,
            )
        path = Path(found).absolute()
    problem = shell_unsafe(str(path))
    if problem is not None:
        raise CoverageInputError(
            f"{key} = {value!r} resolves to {path}, which lcov cannot run: {problem}. Name a "
            "path without white space or shell metacharacters.",
            field=None,
        )
    return ConfiguredTool(written=value, path=path)


@dataclass(frozen=True)
class _Family:
    """The compiler a known gcov stamp proves, and the first notes file carrying it."""

    name: str
    """``clang`` or ``gcc <major>``."""
    stamp: str
    notes: Path

    @property
    def is_llvm(self) -> bool:
        return self.name == "clang"


def _family_of(stamp: str) -> str | None:
    if is_llvm_stamp(stamp):
        return "clang"
    major = gcov_stamp_major(stamp)
    return None if major is None else f"gcc {major}"


class UnitToolchains:
    """Resolve each unit directory's capture toolchain; one per report.

    It probes the system gcov's major at most once, on the first directory
    whose stamps need it, as the e2e resolver does.
    """

    def __init__(self) -> None:
        self._probed = False
        self._system_major: int | None = None

    def _major(self) -> int | None:
        if not self._probed:
            self._system_major = toolchain_discovery.system_gcov_major()
            self._probed = True
        return self._system_major

    def resolve(
        self, configured: ConfiguredTools, label: str, directory: Path, notes: list[Path]
    ) -> Toolchain:
        """Return the toolchain that captures *directory*, a directory of the view *label*.

        *notes* are the directory's kept notes files (those naming a product
        source); their stamps prove the compiler. A configured field wins over
        what the stamps choose, field by field.

        Raises:
            otto.coverage.errors.CoverageInputError: the kept notes carry two
                known compilers, or the configured ``coverage_tool``'s name
                proves a family the stamps contradict (``field=None``).
            otto.host.errors.CoverageToolMissingError: a default tool the data
                needs is not on ``PATH``, or is at a path lcov cannot run.
        """
        family = _one_family(label, directory, notes)
        tool = configured.coverage_tool
        if tool is not None:
            _check_family(configured.tier, tool, directory, family)
            gcov = tool.path
        else:
            gcov = self._discover(configured.tier, label, directory, family)
        if configured.lcov is not None:
            lcov = configured.lcov.path
        else:
            lcov = _default_tool("lcov", "lcov", configured.tier, label, directory)
        return Toolchain(
            sysroot=Path("/"), gcov=gcov, lcov=lcov, lcov_args=list(configured.lcov_args)
        )

    def _discover(self, tier: str, label: str, directory: Path, family: _Family | None) -> Path:
        key = setting_key(tier, "coverage_tool")
        found: Path | None = None
        if family is not None:
            found = gcov_for_stamp(
                family.stamp,
                family.notes,
                system_major=None if family.is_llvm else self._major(),
                subject=StampSubject(
                    name=f"unit tier {label} (directory {directory})",
                    log=f"unit {label}",
                    remedy=f"set `{key}`",
                    where=directory,
                ),
            )
        if found is None:
            return _default_tool("gcov", "coverage_tool", tier, label, directory)
        problem = shell_unsafe(str(found))
        if problem is not None:
            raise _unsafe_found(found, problem, label, key)
        return found


def _one_family(label: str, directory: Path, notes: list[Path]) -> _Family | None:
    """Return the one compiler the known stamps of *notes* prove; ``None`` when none is known."""
    families: dict[str, _Family] = {}
    for path in notes:
        stamp = read_gcov_version(path)
        name = _family_of(stamp) if stamp is not None else None
        if stamp is None or name is None:
            continue  # an unreadable or unrecognised stamp proves nothing
        families.setdefault(name, _Family(name=name, stamp=stamp, notes=path))
    if len(families) > 1:
        first, second = list(families.values())[:2]
        raise CoverageInputError(
            f"unit tier {label}: {directory} holds the notes of more than one compiler — "
            f"{first.notes} is {first.name}'s (gcov stamp {first.stamp!r}) and {second.notes} "
            f"is {second.name}'s (gcov stamp {second.stamp!r}). No single reader decodes both; "
            "build them into separate directories.",
            field=None,
        )
    return next(iter(families.values()), None)


def _check_family(tier: str, tool: ConfiguredTool, directory: Path, family: _Family | None) -> None:
    """Refuse a configured reader whose name proves a family the stamps contradict."""
    if family is None:
        return
    name = tool.path.name
    if is_llvm_cov(name) and not family.is_llvm:
        reads = "an llvm-cov reads only clang's coverage data"
    elif _proves_gnu(name) and family.is_llvm:
        reads = "a GNU gcov cannot read clang's coverage data"
    else:
        return
    key = setting_key(tier, "coverage_tool")
    raise CoverageInputError(
        f"{key} = {tool.written!r} cannot read {directory}: {reads}, and {family.notes} is "
        f"{family.name}'s (gcov stamp {family.stamp!r}). Name a reader for {family.name}'s "
        "data, or leave the setting unset to let the stamp choose.",
        field=None,
    )


def _default_tool(tool: str, field_name: str, tier: str, label: str, directory: Path) -> Path:
    """Return *tool* from ``PATH``, the default when *field_name* is not configured."""
    key = setting_key(tier, field_name)
    found = shutil.which(tool)
    if found is None:
        raise CoverageToolMissingError(
            f"unit tier {label}: capturing {directory} needs `{tool}`, but no `{tool}` is on "
            f"PATH. Install it, or set `{key}`."
        )
    path = Path(found).absolute()
    problem = shell_unsafe(str(path))
    if problem is not None:
        raise _unsafe_found(path, problem, label, key)
    return path


def _unsafe_found(path: Path, problem: str, label: str, key: str) -> CoverageToolMissingError:
    return CoverageToolMissingError(
        f"unit tier {label}: the tool found on PATH at {path} cannot be run by lcov: "
        f"{problem}. Install it at a plain path, or set `{key}`."
    )
