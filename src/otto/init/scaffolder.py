"""Which areas to scaffold, and scaffolding them: the library behind ``otto init``'s writes.

:func:`scaffold_candidates` picks the areas a run writes, :func:`scaffold_prerequisites`
names the missing areas they cannot be written without, and :func:`scaffold`
writes them all — every file through one write policy
(:func:`~otto.init.write_policy.write_file`), so a user's file is never
edited. Nothing here prints: what the user must do or should know comes
back as :attr:`ScaffoldReport.notices`.
"""

import dataclasses
from pathlib import Path

from .areas import AREA_NAMES, detect_areas, scaffold_area, tests_init_module
from .config import InitConfig
from .errors import InitInputError
from .settings_file import declared_init, settings_path
from .write_policy import FileWrite

_OPT_IN = {"kmodcov"}
"""Areas only their own request scaffolds.

A kernel-module coverage library is not for every repo."""

_REFRESHABLE = {"schemas", "kmodcov"}
"""Otto-owned areas an explicit request refreshes even when present."""

_NEEDS = {"tests": ["instructions"]}
"""The example tests import ``RepoOptions`` from the init module the instructions area writes."""


@dataclasses.dataclass(frozen=True)
class ScaffoldReport:
    """What :func:`scaffold` did. ``areas`` includes ``prerequisites``, in write order."""

    areas: list[str]
    prerequisites: list[str]
    writes: list[FileWrite]
    notices: list[str]


def _check_names(areas: list[str]) -> None:
    unknown = [name for name in areas if name not in AREA_NAMES]
    if unknown:
        raise InitInputError(
            f"unknown area(s) {', '.join(unknown)}; known areas: {', '.join(AREA_NAMES)}",
            field="areas",
        )


def _instructions_offered(root: Path) -> bool:
    """Offer instructions unless the settings declare no ``init``: a repo may have no init modules.

    A repo with no settings file gets them from the template, which declares
    the init module.
    """
    return not settings_path(root).is_file() or bool(declared_init(root))


def scaffold_candidates(
    root: Path, *, requested: list[str] | None = None, all_areas: bool = False
) -> list[str]:
    """Return the areas a run would scaffold, in :data:`AREA_NAMES` order, without prerequisites.

    A requested area is a candidate when missing; ``schemas`` and ``kmodcov``
    also when present (they refresh). ``all_areas`` adds every other missing
    area except the opt-in ``kmodcov``, and except instructions when the
    settings declare no ``init`` (a repo with no init modules is legitimate).

    Raises:
        InitInputError: a requested name is not an area (``field="areas"``).
    """
    requested = list(requested or [])
    _check_names(requested)
    present = set(detect_areas(root))
    chosen: list[str] = []
    for name in AREA_NAMES:
        if name in requested:
            wanted = name not in present or name in _REFRESHABLE
        else:
            wanted = (
                all_areas
                and name not in present
                and name not in _OPT_IN
                and (name != "instructions" or _instructions_offered(root))
            )
        if wanted:
            chosen.append(name)
    return chosen


def _requested_needs(areas: list[str]) -> set[str]:
    return {pre for name in areas for pre in _NEEDS.get(name, [])}


def _prerequisites(areas: list[str], present: set[str]) -> list[str]:
    if not areas:
        return []
    needed = {"settings"} | _requested_needs(areas)
    if "settings" not in present:
        needed.add("instructions")
    return [name for name in AREA_NAMES if name in needed - set(areas) - present]


def scaffold_prerequisites(root: Path, areas: list[str]) -> list[str]:
    """Return the missing areas *areas* cannot be written without, not already in *areas*.

    Settings for any area (it is the repo marker); instructions for tests
    (they import ``RepoOptions`` from it) and for settings written from the
    template, which declares the init module — without it the repo cannot
    load, since the loader imports every declared init module.

    Raises:
        InitInputError: a name in *areas* is not an area (``field="areas"``).
    """
    _check_names(areas)
    return _prerequisites(areas, set(detect_areas(root)) if areas else set())


def _prerequisite_notice(name: str, areas: list[str]) -> str:
    """Say why prerequisite *name* is being scaffolded."""
    if name == "settings":
        return "settings.toml is the repo marker — scaffolding it first."
    if name == "instructions" and name not in _requested_needs(areas):
        return (
            "the instructions area holds the init module the new settings.toml declares "
            "— scaffolding it."
        )
    return f"the {name} area is a prerequisite of what you asked for — scaffolding it."


def scaffold(config: InitConfig, areas: list[str]) -> ScaffoldReport:
    """Scaffold *areas* plus their prerequisites, in :data:`AREA_NAMES` order; never prints.

    Every file goes through :func:`~otto.init.write_policy.write_file`:
    otto-owned files are refreshed, user-owned files are created only when
    absent. Notices say what the user must do or should know.

    Raises:
        InitInputError: a name in *areas* is not an area (``field="areas"``).
    """
    _check_names(areas)
    root = config.root
    present_before = set(detect_areas(root))
    prerequisites = _prerequisites(areas, present_before)
    ordered = [name for name in AREA_NAMES if name in {*areas, *prerequisites}]
    notices = [_prerequisite_notice(name, areas) for name in prerequisites]
    writes: list[FileWrite] = []
    for name in ordered:
        written = scaffold_area(name, config, ordered)
        writes.extend(written.writes)
        notices.extend(written.notices)
    if "tests" in ordered and "instructions" not in ordered and "instructions" in present_before:
        # otto reads no init module to see what it declares, so it names the
        # module the tests import from and exactly what that module must hold.
        module = tests_init_module(root, config)
        notices.append(
            f"the example tests import RepoOptions from {module}: make sure {module} "
            'declares @otto.options(verbs=["run", "test"]) class RepoOptions with a '
            "`message: str` field (the instructions area otto scaffolds does)."
        )
    return ScaffoldReport(ordered, prerequisites, writes, notices)
