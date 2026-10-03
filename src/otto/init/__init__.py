"""
otto.init — the library behind ``otto init``: a repo-setup doctor and scaffolder.

Not to be confused with the ``init`` key of ``.otto/settings.toml``, which
names the repo's *init modules* (imported at startup to register
instructions, host classes and options). This package checks and scaffolds a
whole repo; one of the areas it handles is those init modules.

The doctor, :func:`check_repo`, runs the loader's own settings compile, so a
repo it passes is a repo otto loads. The scaffolder, :func:`scaffold`, writes
what is missing through one write policy: otto-owned files are refreshed,
user-owned files are created only when absent and never edited. Neither
prints; ``otto init`` renders their reports.

    from pathlib import Path
    from otto.init import InitConfig, check_repo, scaffold, scaffold_candidates

    config = InitConfig.for_repo(Path("."), name="acme")
    report = scaffold(config, scaffold_candidates(config.root, all_areas=True))
    doctor = check_repo(config.root)
    assert doctor.ok, [v.problems for v in doctor.verdicts]

Every name is exported lazily (PEP 562); see ``otto.config``'s ``__dir__``.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .areas import AREA_NAMES as AREA_NAMES
    from .areas import detect_areas as detect_areas
    from .config import InitConfig as InitConfig
    from .doctor import AreaVerdict as AreaVerdict
    from .doctor import DoctorReport as DoctorReport
    from .doctor import check_repo as check_repo
    from .errors import InitInputError as InitInputError
    from .scaffolder import ScaffoldReport as ScaffoldReport
    from .scaffolder import scaffold as scaffold
    from .scaffolder import scaffold_candidates as scaffold_candidates
    from .scaffolder import scaffold_prerequisites as scaffold_prerequisites
    from .write_policy import FileWrite as FileWrite

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "AREA_NAMES": "otto.init.areas",
    "AreaVerdict": "otto.init.doctor",
    "DoctorReport": "otto.init.doctor",
    "FileWrite": "otto.init.write_policy",
    "InitConfig": "otto.init.config",
    "InitInputError": "otto.init.errors",
    "ScaffoldReport": "otto.init.scaffolder",
    "check_repo": "otto.init.doctor",
    "detect_areas": "otto.init.areas",
    "scaffold": "otto.init.scaffolder",
    "scaffold_candidates": "otto.init.scaffolder",
    "scaffold_prerequisites": "otto.init.scaffolder",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.init's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "AREA_NAMES",
    "AreaVerdict",
    "DoctorReport",
    "FileWrite",
    "InitConfig",
    "InitInputError",
    "ScaffoldReport",
    "check_repo",
    "detect_areas",
    "scaffold",
    "scaffold_candidates",
    "scaffold_prerequisites",
]
