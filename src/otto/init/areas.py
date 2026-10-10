"""The areas of an otto repo, and how ``otto init`` tells which are present.

An *area* is one unit of repo setup the doctor checks and the scaffolder
writes: ``settings``, ``schemas``, ``lab``, ``tests``, ``instructions`` and
``kmodcov``, in that order (:data:`AREA_NAMES`). Detection only asks "is
this area here?" — whether it is any good is the doctor's question
(:func:`otto.init.doctor.check_repo`). Scaffolding writes one area's files
through :func:`~otto.init.write_policy.write_file` (:func:`scaffold_area`);
which areas a run writes is :mod:`otto.init.scaffolder`'s question.
"""

import dataclasses
import json
from collections.abc import Callable
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import InitConfig
from .settings_file import declared_init, settings_data, settings_path, settings_paths
from .templates import (
    CONFTEST_TEMPLATE,
    CREDS_JSON_TEMPLATE,
    INSTRUCTIONS_TEMPLATE,
    INVENTORY_JSON_TEMPLATE,
    KMODCOV_DEV_TOOL_TEMPLATE,
    KMODCOV_STARTER_KBUILD_TEMPLATE,
    KMODCOV_STARTER_README_TEMPLATE,
    LAB_JSON_TEMPLATE,
    LAB_README_TEMPLATE,
    SETTINGS_TEMPLATE,
    TEST_EXAMPLE_TEMPLATE,
    VSCODE_EXTENSIONS_TEMPLATE,
    VSCODE_SETTINGS_TEMPLATE,
)
from .write_policy import FileWrite, write_file

if TYPE_CHECKING:
    from ..labs.sources import LabSourceState


def _lab_states(root: Path) -> "list[LabSourceState] | None":
    """Return this repo's lab sources, each prepared against the backends registered now.

    ``None`` when there is no readable ``settings.toml``. Settings that parse
    but declare no ``[lab]`` table declare no source. ``otto init`` runs no
    init module, so a source whose backend an init module registers is
    unknown here (deferred); the built-in ``json`` backend is always known.

    Raises:
        ValueError: The ``[lab]`` envelope does not validate, two sources
            share a label, or a known source's options do not prepare
            (:class:`~otto.labs.errors.LabSourceConstructionError`).
    """
    from ..host.os_profile import ProfileContext
    from ..labs.sources import compile_lab_sources, prepare_lab_sources
    from ..models.settings import LabConfigSpec

    data = settings_data(root)
    if data is None:
        return None
    lab = data.get("lab")
    if lab is None:
        return []
    # pydantic's ValidationError IS a ValueError, so a caller's one arm covers
    # the envelope check, the label check and a source's preparation.
    pending = compile_lab_sources(
        LabConfigSpec.model_validate(lab),
        repo_name=str(data.get("name") or root.name),
        sut_dir=root,
    )
    # No data profiles: these states are asked which files they read, and no
    # source is built from them, so no host resolves an os_type here.
    return prepare_lab_sources(pending, profiles=ProfileContext.empty())


def lab_file_groups(root: Path) -> list[list[Path]]:
    """Every lab file this repo's file-backed ``[[lab.sources]]`` entries name, ONE LIST PER SOURCE.

    THE single reader of a repo's host-data declaration inside ``otto init``:
    detection and validation both go through it (via :func:`lab_files`), so
    the doctor can never disagree with the runtime — or with itself — about
    which files hold this repo's hosts. Checks the entries with the SAME
    :func:`otto.labs.sources.compile_lab_sources` ``Repo.parse_settings``
    uses, prepares each one whose backend is registered, then asks each
    file-backed source for its files (a directory entry contributes its
    ``lab.json``; a ``.json`` entry IS the file; a glob contributes every
    match). A source whose backend is not registered yet is left to
    :func:`deferred_lab_sources`.

    The grouping is load-bearing for the duplicate rules, which are per SOURCE:
    two files of ONE source declaring the same lab is a typo, the same
    declaration in two SOURCES is the documented override seam (spec §2.4).
    Callers that only need "which files exist" flatten it through
    :func:`lab_files`.

    Falls back to the conventional ``lab_data/lab.json`` only when there is no
    readable ``settings.toml`` at all — init must work on a repo it has not
    scaffolded yet. Settings that parse but declare no ``[lab]`` table declare
    no host data, so they yield no files. A ``[lab]`` table whose sources do
    not check or prepare raises that ``ValueError``: the doctor reports it
    once, under the lab area.
    """
    from ..labs.json_repository import LAB_FILENAME

    states = _lab_states(root)
    if states is None:
        return [[root / "lab_data" / LAB_FILENAME]]
    return [state.lab_files() for state in states if state.is_known() and state.is_file_backed()]


def deferred_lab_sources(root: Path) -> list[str]:
    """Describe every source whose backend is not registered before init, in order.

    ``otto init`` runs no init module, so such a source cannot be checked
    here; otto checks it when it prepares the source, after init. Raises
    what :func:`lab_file_groups` raises.
    """
    states = _lab_states(root) or []
    return [
        f"{state.pending.label} (backend {state.pending.backend!r})"
        for state in states
        if not state.is_known()
    ]


def lab_files(root: Path) -> list[Path]:
    """Every lab file this repo's json sources name, flattened in source order.

    The "does this repo have host data, and where" view, for detection and for
    any caller that does not care which source a file came from. See
    :func:`lab_file_groups` for the per-source view the duplicate rules need,
    and for the ``ValueError`` both raise when the lab sources do not prepare.
    """
    return [lab_file for group in lab_file_groups(root) for lab_file in group]


def instruction_libs(root: Path) -> list[Path]:
    """Return the directories init modules are looked up in, anchored to *root*.

    The settings' ``libs``, else the conventional ``pylib`` when there is no
    readable settings file.
    """
    paths = settings_paths(root)
    return paths["libs"] if paths is not None else [root / "pylib"]


def _is_module_name(name: str) -> bool:
    """Return True for a dotted name of identifiers.

    Anything else could not be imported, and joined onto a path piece by
    piece (``"../x"`` splits into ``["", "", "/x"]``) it could land outside
    the repo.
    """
    return all(part.isidentifier() for part in name.split("."))


def _first_declared_module(root: Path) -> str | None:
    """Return the first declared init module, when it is a module name at all.

    A first entry that is not one is the settings area's problem to report;
    the scaffold then behaves as if none were declared.
    """
    declared = declared_init(root) or []
    return declared[0] if declared and _is_module_name(declared[0]) else None


def tests_init_module(root: Path, config: InitConfig) -> str:
    """The init module the example tests import ``RepoOptions`` from.

    The first declared init module — it is imported at startup and is the one
    a found instructions area holds — else the module the scaffold writes.
    """
    return _first_declared_module(root) or config.init_module


def kmodcov_entries(root: Path) -> list[dict[str, Any]]:
    """Every ``[[dev_tools]]`` entry of kind ``kmodcov`` the settings declare.

    ``[]`` when there is no readable settings file — mirrors
    :func:`~otto.init.settings_file.settings_data`.
    """
    data = settings_data(root)
    if data is None:
        return []
    entries = data.get("dev_tools", [])
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("kind") == "kmodcov"]


def _detect_settings(root: Path) -> bool:
    return settings_path(root).is_file()


def _detect_schemas(root: Path) -> bool:
    return next((root / ".otto" / "schemas").glob("*.schema.json"), None) is not None


def _detect_lab(root: Path) -> bool:
    """Report a lab file present — or lab sources that do not compile.

    A broken declaration counts as present so the scaffolder never writes
    lab files beside it; the doctor reads the lab area as blocked.
    """
    try:
        return any(lab_file.is_file() for lab_file in lab_files(root))
    except ValueError:
        return True


def _detect_tests(root: Path) -> bool:
    paths = settings_paths(root)
    tests_dirs = paths["tests"] if paths is not None else [root / "tests"]
    return any(next(tests_dir.glob("test_*.py"), None) is not None for tests_dir in tests_dirs)


def _detect_instructions(root: Path) -> bool:
    """Report the area present when ``init`` is declared and at least one entry resolves."""
    from ..config.repo import find_init_module

    modules = declared_init(root) or []
    libs = instruction_libs(root)
    return any(find_init_module(module, libs) is not None for module in modules)


def _detect_kmodcov(root: Path) -> bool:
    return (
        bool(kmodcov_entries(root))
        or (root / "third_party" / "otto_kmodcov" / "kmodcov.h").is_file()
    )


_DETECT: dict[str, Callable[[Path], bool]] = {
    "settings": _detect_settings,
    "schemas": _detect_schemas,
    "lab": _detect_lab,
    "tests": _detect_tests,
    "instructions": _detect_instructions,
    "kmodcov": _detect_kmodcov,
}

AREA_NAMES: list[str] = list(_DETECT)
"""Every area, in the order the doctor reports and the scaffolder writes them."""


def detect_areas(root: Path) -> list[str]:
    """Return the areas present in *root*, in :data:`AREA_NAMES` order."""
    return [name for name, detect in _DETECT.items() if detect(root)]


@dataclasses.dataclass(frozen=True)
class AreaWrite:
    """What scaffolding one area wrote, and what it needs to tell the user."""

    writes: list[FileWrite]
    notices: list[str] = dataclasses.field(default_factory=list)


_VSCODE_SETTINGS_KEPT = (
    "existing .vscode/settings.json left untouched — see "
    "docs/cli/schema/editors.md for the schema associations"
)


def _scaffold_settings(config: InitConfig, areas: list[str]) -> AreaWrite:
    """Write the settings template; with ``kmodcov`` in the same run, its commented entry too."""
    text = SETTINGS_TEMPLATE.format(
        name=config.name, version=config.version, init_module=config.init_module
    )
    if "kmodcov" in areas:
        text += KMODCOV_DEV_TOOL_TEMPLATE.format(kmodcov_dir=config.kmodcov_dir)
    write = write_file(settings_path(config.root), text, "user")
    # Pre-wired paths must exist so later areas (and bootstrap) never trip
    # over a missing conventional directory.
    for directory in ("lab_data", "tests", "pylib"):
        (config.root / directory).mkdir(exist_ok=True)
    return AreaWrite([write])


def _scaffold_schemas(config: InitConfig, areas: list[str]) -> AreaWrite:  # noqa: ARG001 — uniform signature
    """Write the editor schemas (otto-owned, orphans pruned), then the ``.vscode`` wiring.

    VS Code settings are JSONC (comments, trailing commas): merging risks
    corrupting a user file, so an existing ``settings.json`` is kept and the
    notice points at the docs that carry the associations. The snippets file
    is generated from the live models and auto-loaded by VS Code, so it is
    otto's to refresh.
    """
    from ..models.jsonschema import write_schemas
    from ..models.snippets import build_snippets

    out = config.root / ".otto" / "schemas"
    before = set(out.glob("*.schema.json")) if out.is_dir() else set()
    result = write_schemas(out)
    writes = [FileWrite(p, "refreshed" if p in before else "created") for p in result.written]
    writes += [FileWrite(p, "pruned") for p in result.pruned]
    vscode = config.root / ".vscode"
    wiring = write_file(vscode / "settings.json", VSCODE_SETTINGS_TEMPLATE, "user")
    writes.append(wiring)
    writes.append(write_file(vscode / "extensions.json", VSCODE_EXTENSIONS_TEMPLATE, "user"))
    snippets = json.dumps(build_snippets(), indent=2) + "\n"
    writes.append(write_file(vscode / "otto.code-snippets", snippets, "otto"))
    notices = [_VSCODE_SETTINGS_KEPT] if wiring.outcome == "kept" else []
    return AreaWrite(writes, notices)


def _scaffold_lab(config: InitConfig, areas: list[str]) -> AreaWrite:  # noqa: ARG001 — uniform signature
    """Write the three-file lab area (spec 2026-09-06 creds-store §8.1); ``creds.json`` is 0o600."""
    lab_dir = config.root / "lab_data"
    return AreaWrite(
        [
            write_file(
                lab_dir / "lab.json", json.dumps(LAB_JSON_TEMPLATE, indent=4) + "\n", "user"
            ),
            write_file(
                lab_dir / "inventory.json",
                json.dumps(INVENTORY_JSON_TEMPLATE, indent=4) + "\n",
                "user",
            ),
            write_file(
                lab_dir / "creds.json",
                json.dumps(CREDS_JSON_TEMPLATE, indent=4) + "\n",
                "user",
                mode=0o600,
            ),
            write_file(lab_dir / "README.md", LAB_README_TEMPLATE, "user"),
        ]
    )


def _scaffold_tests(config: InitConfig, areas: list[str]) -> AreaWrite:  # noqa: ARG001 — uniform signature
    """Write the example tests; they import ``RepoOptions`` from :func:`tests_init_module`."""
    tests_dir = config.root / "tests"
    example = TEST_EXAMPLE_TEMPLATE.format(init_module=tests_init_module(config.root, config))
    return AreaWrite(
        [
            write_file(tests_dir / "test_example.py", example, "user"),
            write_file(tests_dir / "conftest.py", CONFTEST_TEMPLATE, "user"),
        ]
    )


def _scaffold_instructions(config: InitConfig, areas: list[str]) -> AreaWrite:  # noqa: ARG001 — uniform signature
    """Write the init module the settings declare, else ``config.init_module``.

    Either goes under the first ``libs`` directory (``pylib`` when the
    settings name none), a dotted name as nested packages. A declared module
    that already resolves is the user's: nothing is written, and its file is
    reported ``kept`` — a package written beside ``foo.py`` would shadow it.
    A dotted declared module whose parent is a namespace package spread over
    several ``libs`` becomes a regular package in the first one.

    A module the settings do not declare would never be imported, so the
    notice says which lines to set — ``libs`` too, when the settings name
    none, or the doctor would not find the module under ``pylib``.
    """
    from ..config.repo import find_init_module

    root = config.root
    declared = _first_declared_module(root)
    libs = instruction_libs(root)
    if declared is not None:
        spec = find_init_module(declared, libs)
        if spec is not None:
            return AreaWrite([FileWrite(_module_path(spec), "kept")])
    module = declared or config.init_module
    base = libs[0] if libs else root / "pylib"
    parts = module.split(".")
    # A dotted name's parents are written as plain packages so it imports.
    writes = [
        write_file(base.joinpath(*parts[:depth], "__init__.py"), "", "user")
        for depth in range(1, len(parts))
    ]
    writes.append(
        write_file(
            base.joinpath(*parts, "__init__.py"),
            INSTRUCTIONS_TEMPLATE.format(name=config.name),
            "user",
        )
    )
    notices = []
    if declared is None and settings_path(root).is_file():
        lines = f'init = ["{module}"]' + ("" if libs else ' and libs = ["pylib"]')
        notices.append(
            f"{module} is not in the settings' init list, so otto will not import it: "
            f"set {lines} in .otto/settings.toml"
        )
    return AreaWrite(writes, notices)


def _module_path(spec: ModuleSpec) -> Path:
    """Return the file (or a namespace package's first directory) *spec* imports from."""
    if spec.origin is not None and spec.has_location:
        return Path(spec.origin)
    return Path(next(iter(spec.submodule_search_locations or []), spec.name))


def _kmodcov_wired(root: Path) -> bool:
    """Return True when the settings declare a kmodcov entry or already carry the commented one.

    Decoded leniently: a settings file that is not UTF-8 is the settings
    area's problem to report, never a scaffold traceback.
    """
    if kmodcov_entries(root):
        return True
    return '#kind = "kmodcov"' in settings_path(root).read_bytes().decode(errors="replace")


def _scaffold_kmodcov(config: InitConfig, areas: list[str]) -> AreaWrite:  # noqa: ARG001 — uniform signature
    """Export the library (otto-owned, refreshed whole), then the consumer starter (user-owned).

    ``settings.toml`` is never edited: when it exists and declares no kmodcov
    entry, the notice carries the commented ``[[dev_tools]]`` entry to paste.
    """
    from ..kmodcov import SHIPPED_FILES, VERSION_HEADER, export_tree

    vendored = config.kmodcov_path
    library = [vendored / name for name in (*SHIPPED_FILES, VERSION_HEADER)]
    existed = {path: path.exists() for path in library}
    export_tree(vendored)
    writes = [FileWrite(path, "refreshed" if existed[path] else "created") for path in library]
    starter = vendored.parent / f"{vendored.name}-consumer"
    for name, text in (
        ("kmodcov_begin.c", '#include "kmodcov.h"\nKMODCOV_SENTINEL_BEGIN;\n'),
        ("kmodcov_end.c", '#include "kmodcov.h"\nKMODCOV_SENTINEL_END;\n'),
        ("Kbuild.example", KMODCOV_STARTER_KBUILD_TEMPLATE.format(kmodcov_dir_name=vendored.name)),
        ("README.md", KMODCOV_STARTER_README_TEMPLATE.format(kmodcov_dir=config.kmodcov_dir)),
    ):
        writes.append(write_file(starter / name, text, "user"))
    notices = []
    if settings_path(config.root).is_file() and not _kmodcov_wired(config.root):
        snippet = KMODCOV_DEV_TOOL_TEMPLATE.format(kmodcov_dir=config.kmodcov_dir).strip("\n")
        notices.append(
            "add this commented [[dev_tools]] entry to .otto/settings.toml and fill it in:\n"
            f"{snippet}"
        )
    return AreaWrite(writes, notices)


_SCAFFOLD: dict[str, Callable[[InitConfig, list[str]], AreaWrite]] = {
    "settings": _scaffold_settings,
    "schemas": _scaffold_schemas,
    "lab": _scaffold_lab,
    "tests": _scaffold_tests,
    "instructions": _scaffold_instructions,
    "kmodcov": _scaffold_kmodcov,
}


def scaffold_area(name: str, config: InitConfig, areas: list[str]) -> AreaWrite:
    """Scaffold area *name*; *areas* is everything this call writes (settings reads it)."""
    return _SCAFFOLD[name](config, areas)
