"""The ``otto init`` doctor: :func:`check_repo` and the :class:`DoctorReport` it returns.

Every check runs the code otto itself loads a repo with — the settings
compile, the lab loader's section, entry and duplicate rules, the host and
link specs, the inventory build, importlib's finder — so a repo the doctor
passes is a repo otto loads. The doctor never prints and never raises for
what a repo holds: ``otto init`` renders the report.
"""

import dataclasses
import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .areas import (
    AREA_NAMES,
    detect_areas,
    instruction_libs,
    kmodcov_entries,
    lab_file_groups,
)
from .errors import InitInputError
from .settings_file import declared_init, settings_data, settings_path, settings_paths

if TYPE_CHECKING:
    # Annotation-only: importing ``otto.inventory`` for real pulls ~77 otto
    # modules (see ``otto/inventory/config.py``'s module docstring). Every
    # real use below is a function-local import.
    from ..host.element import Element
    from ..inventory import Inventory

VerdictState = Literal["ok", "failed", "absent", "blocked"]

_BLOCKED = "settings did not compile"
_ABSENT = {
    "settings": "no .otto/settings.toml",
    "schemas": "no .otto/schemas/*.schema.json",
    "lab": "no lab file found",
    "tests": "no test_*.py found",
    "instructions": "no init modules declared",
    "kmodcov": "no kmodcov dev tool declared and no vendored library",
}


@dataclasses.dataclass(frozen=True)
class AreaVerdict:
    """The doctor's verdict on one area.

    ``problems`` is non-empty only when ``state`` is ``failed``; ``detail`` is
    the one-line explanation an ``absent`` or ``blocked`` verdict carries.
    """

    name: str
    state: VerdictState
    problems: list[str] = dataclasses.field(default_factory=list)
    detail: str = ""


@dataclasses.dataclass(frozen=True)
class DoctorReport:
    """Everything :func:`check_repo` found. Warnings and labels never fail it."""

    verdicts: list[AreaVerdict]
    warnings: list[str]
    inventory_label: str | None
    creds_label: str | None

    @property
    def ok(self) -> bool:
        """True when no area failed (``absent`` and ``blocked`` do not fail)."""
        return all(verdict.state != "failed" for verdict in self.verdicts)

    def verdict(self, name: str) -> AreaVerdict:
        """Return the verdict for area *name*."""
        return next(verdict for verdict in self.verdicts if verdict.name == name)


def check_repo(root: Path) -> DoctorReport:
    """Check every area of the repo at *root*; never prints, never raises for its content.

    settings: the loader's own compile (:func:`otto.config.repo.validate_settings`).
    schemas: ``schema_drift``. lab: the runtime
    loader's section, entry and duplicate rules, host entries resolved
    against the inventory, links via ``LinkSpec`` — or ``blocked`` when the
    settings do not compile, so their error is shown once. tests: a light
    check (dirs, ``test_*.py``, ``ast.parse``). instructions: ``absent``
    when ``init`` is omitted or empty (a repo with no init modules is
    legitimate), ``blocked`` when the settings are not valid TOML, else
    every entry must resolve. kmodcov: a declared ``source`` must hold the
    library. Warnings (lab, inventory, kmodcov drift) never fail the report.

    Raises:
        InitInputError: *root* is not a directory (``field="root"``).
    """
    from ..config.repo import validate_settings
    from ..models.jsonschema import schema_drift

    if not root.is_dir():
        raise InitInputError(f"{root} is not a directory", field="root")
    # One inventory per call, not per asker: the lab check, the warnings and
    # the labels each ask _inventory_for the same question. Owned here, never
    # a module global.
    cache: "dict[Path, Inventory | Exception | None]" = {}
    present = set(detect_areas(root))
    settings_problems = validate_settings(root) if "settings" in present else []
    lab_blocked = bool(settings_problems) or not _lab_compiles(root)
    verdicts: list[AreaVerdict] = []
    for name in AREA_NAMES:
        if name == "settings":
            verdict = _checked(name, settings_problems) if name in present else None
        elif name == "lab" and lab_blocked:
            verdict = AreaVerdict(name, "blocked", detail=_BLOCKED)
        elif name == "instructions":
            verdict = _instructions_verdict(root)
        elif name not in present:
            verdict = None
        elif name == "schemas":
            verdict = _checked(name, schema_drift(root / ".otto" / "schemas"))
        elif name == "lab":
            verdict = _checked(name, _validate_lab(root, cache))
        elif name == "tests":
            verdict = _checked(name, _validate_tests(root))
        else:
            verdict = _checked(name, _validate_kmodcov(root))
        verdicts.append(verdict or AreaVerdict(name, "absent", detail=_ABSENT[name]))
    warnings = [] if lab_blocked else _lab_warnings(root, cache)
    warnings.extend(_kmodcov_warnings(root))
    inventory_label, creds_label = _labels(root, cache)
    return DoctorReport(verdicts, warnings, inventory_label, creds_label)


def _checked(name: str, problems: list[str]) -> AreaVerdict:
    return AreaVerdict(name, "failed", problems) if problems else AreaVerdict(name, "ok")


def _lab_compiles(root: Path) -> bool:
    """Report whether :func:`lab_file_groups` can compile *root*'s lab sources.

    The settings compile already refuses every ``[lab]`` this one does, so
    today this is False only when the settings verdict has failed too. It is
    asked separately because the two compiles are separate code: should they
    ever disagree, the lab area reads blocked rather than ``check_repo``
    raising the compile error out of the lab check or the warnings pass.
    """
    try:
        lab_file_groups(root)
    except ValueError:
        return False
    return True


def _instructions_verdict(root: Path) -> AreaVerdict:
    """``absent`` when no init module is declared, else every declared one must resolve.

    A repo with no init modules is legitimate (spec 2026-10-03 init-library
    §2), so an omitted or empty ``init`` never fails. A settings file that is
    not valid TOML is the settings area's problem; this verdict reads
    ``blocked`` rather than repeating it, and so does an ``init`` that is not
    a list or holds anything but strings, which the settings compile refuses. Not gated on
    detection: a declared init that resolves nowhere is undetected, and it is
    exactly what this verdict must fail.
    """
    if not settings_path(root).is_file():
        return AreaVerdict("instructions", "absent", detail=_ABSENT["instructions"])
    data = settings_data(root)
    if data is None:
        return AreaVerdict("instructions", "blocked", detail=_BLOCKED)
    modules = data.get("init", [])
    if not isinstance(modules, list):
        return AreaVerdict("instructions", "blocked", detail=_BLOCKED)
    if not modules:
        return AreaVerdict("instructions", "absent", detail=_ABSENT["instructions"])
    declared = declared_init(root)
    if declared is None:
        return AreaVerdict("instructions", "blocked", detail=_BLOCKED)
    return _checked("instructions", _validate_instructions(root, declared))


def _inventory_for(
    root: Path, cache: "dict[Path, Inventory | Exception | None] | None" = None
) -> "Inventory | None":
    """Return the inventory bootstrap would build for *root*: its own override, else the user file.

    Raises ``InventoryError`` / ``ValueError`` for a broken declaration — the
    caller reports that as a problem naming the file; the doctor must never
    traceback on a broken ``[inventory]`` table or user settings file.

    *cache* memoises the result (inventory, ``None``, or the raised
    exception) per *root* — one :func:`check_repo` asks this up to three
    times (the "lab" area's own validation, the warnings pass, the labels),
    and while today's construction is I/O-free, a real backend (the netbox
    one, still to land) would otherwise pay a whole-set fetch three times
    over. A plain dict an owning caller creates and threads through by hand —
    never a module global, which would outlive one call and leak across
    processes or tests that import this module.
    """
    if cache is not None and root in cache:
        cached = cache[root]
        if isinstance(cached, Exception):
            raise cached
        return cached

    from ..config.user_settings import load_user_settings
    from ..inventory import InventoryDeclaration, InventoryError, build_inventory_from_declarations

    data = settings_data(root) or {}
    table = data.get("inventory") or {}
    creds_table = data.get("creds") or {}
    declarations = (
        [
            InventoryDeclaration(
                origin=str(settings_path(root)),
                anchor_dir=root,
                table=dict(table) if isinstance(table, dict) else {},
                creds_table=dict(creds_table) if isinstance(creds_table, dict) else {},
            )
        ]
        if (isinstance(table, dict) and table) or (isinstance(creds_table, dict) and creds_table)
        else []
    )
    try:
        result = build_inventory_from_declarations(declarations, user_settings=load_user_settings())
    except (InventoryError, ValueError) as e:
        if cache is not None:
            cache[root] = e
        raise
    if cache is not None:
        cache[root] = result
    return result


def _labels(root: Path, cache: "dict[Path, Inventory | Exception | None]") -> list[str | None]:
    """``[inventory label, creds-store label]`` when an inventory resolves, else ``[None, None]``.

    A broken declaration yields no labels — it is already a problem in the
    lab verdict via :func:`_validate_lab`, and repeating it would be noise.
    The creds label names the store when one resolves (spec 2026-09-06 §7.1).
    """
    from ..inventory import CredsOverlay, InventoryError

    try:
        inventory = _inventory_for(root, cache)
    except (InventoryError, ValueError):
        return [None, None]
    if inventory is None:
        return [None, None]
    creds = inventory.store.label if isinstance(inventory, CredsOverlay) else None
    return [inventory.label, creds]


_ParsedLab = tuple[str, dict[str, Any], list[Any], list[Any]]
"""One parsed lab file: ``(path, labs table, elements, raw links)``."""


def _parse_lab_documents(root: Path) -> tuple[list[str], list[_ParsedLab]]:
    """Parse every lab file the settings name: ``(problems, parsed documents)``.

    The section shape, the ``labs`` table, the ``elements`` entries and the
    in-source duplicate rules are all applied by the SAME code the runtime
    loader uses (:func:`otto.labs.json_repository.parse_lab_sections`,
    :func:`~otto.labs.json_repository.parse_lab_entries`,
    :func:`~otto.labs.json_repository.parse_elements`,
    :func:`~otto.labs.json_repository.check_in_source_duplicates`), so the
    doctor cannot drift from what otto accepts: an unknown ``routes`` section,
    a v1 top-level ``hosts`` array, a host entry carrying a hoisted key and a
    lab declared twice within one source are all rejected here exactly as they
    are at load. A file with a problem is reported and left out of the
    documents the warnings pass sees — a half-parsed file would only produce
    warnings about its own breakage.

    The duplicate state is threaded per SOURCE (:func:`lab_file_groups`), not
    over the flat file list: two sources may each declare the same lab, which
    is how ``[[lab.sources]]`` layering works, and rejecting that would fail
    repos otto loads happily.
    """
    from ..labs.errors import LabRepositoryError
    from ..labs.json_repository import (
        SeenElement,
        check_in_source_duplicates,
        parse_elements,
        parse_lab_entries,
        parse_lab_sections,
    )

    problems: list[str] = []
    documents: list[_ParsedLab] = []
    for group in lab_file_groups(root):
        # Reset per source: the duplicate rules are in-source rules.
        seen_labs: dict[str, Path] = {}
        seen_elements: dict[str, SeenElement] = {}
        for lab_file in group:
            if not lab_file.is_file():
                continue
            try:
                # Decoded as UTF-8 text, as the loader reads it: json.loads on
                # raw bytes would accept a BOM or UTF-16 the loader refuses.
                # ValueError covers malformed JSON and undecodable bytes.
                data = json.loads(lab_file.read_bytes().decode())
            except (OSError, ValueError) as e:
                problems.append(f"{lab_file}: {e}")
                continue
            try:
                sections = parse_lab_sections(data, str(lab_file))
                entries = parse_lab_entries(sections["labs"], str(lab_file))
                elements = parse_elements(sections["elements"], str(lab_file))
                check_in_source_duplicates(
                    entries,
                    elements,
                    lab_file,
                    seen_labs=seen_labs,
                    seen_elements=seen_elements,
                )
            except LabRepositoryError as e:
                problems.append(str(e))
                continue
            documents.append((str(lab_file), entries, elements, sections["links"]))
    return problems, documents


def _item_problem(validate: Callable[[Any], object], item: Any, prefix: str) -> list[str]:
    """Return ``[f"{prefix} <error>"]`` when *validate* rejects *item*, else ``[]``.

    A one-item helper rather than the loop body its callers would otherwise
    write: a ``try``/``except`` inside a per-item loop is ``PERF203``, and the
    repo's answer (``otto.labs.json_repository._parse_element``) is to move
    the ``try`` into a function the loop calls. ``ValueError`` covers both
    arms — pydantic's ``ValidationError`` is one; ``InventoryError`` covers a
    third — a host entry's :func:`~otto.inventory.resolve_host_entry` call
    hitting a dead key or an inventory-owned field declared inline.

    A ``ValidationError`` is rendered through :func:`compact_validation_error`
    rather than ``str(e)``: *item* here is the RESOLVED host dict, and a
    referenced host with no inline creds carries its store creds LAST (spec
    2026-09-06 creds-store §6.2), so ``str(ValidationError)``'s
    ``input_value=`` repr of the whole dict ends in a store password on the
    common "one bad field" mistake. ``compact_validation_error`` never reads
    ``input``.
    """
    from pydantic import ValidationError

    from ..inventory import InventoryError
    from ..models.base import compact_validation_error

    try:
        validate(item)
    except ValidationError as e:
        return [f"{prefix} {compact_validation_error(e)}"]
    except (ValueError, InventoryError) as e:
        return [f"{prefix} {e}"]
    return []


def _validate_lab(
    root: Path, cache: "dict[Path, Inventory | Exception | None] | None" = None
) -> list[str]:
    """Validate every lab file the settings' ``[[lab.sources]]`` name, via the real specs.

    The file shape is :func:`_parse_lab_documents`' job; what is left is the
    two payloads the wrapper models hold opaquely. Each element's host
    entries are resolved against this repo's inventory the way the loader
    resolves them (:func:`otto.inventory.resolve_host_entry`, spec §6, against
    the element :meth:`otto.models.lab.ElementSpec.to_element` builds), and
    handed to
    :func:`otto.host.factory.validate_host_dict`, so a bad ``os_type`` or
    field name, a dead inventory key, or an inventory-owned field declared
    inline all surface the same error the loader would raise. Each ``links``
    entry is validated structurally via :class:`~otto.models.link.LinkSpec`;
    endpoint cross-references (host ids, interface keys) are resolved at load
    time, not here.

    A broken ``[inventory]`` declaration (or user settings file) is reported
    ONCE, as its own problem, rather than once per referencing host entry —
    those entries are skipped for this pass and resolve once the declaration
    is fixed. "Those entries" means
    :func:`~otto.inventory.doctor.references_inventory`: a ``None`` or
    absent key references nothing and is validated as always regardless of
    the broken declaration, and a malformed key (the empty string, a
    non-string) is its own problem independent of the declaration — skipping
    on mere key PRESENCE would swallow both.
    """
    from ..host.factory import validate_host_dict
    from ..inventory import InventoryError, resolve_host_entry
    from ..inventory.doctor import references_inventory
    from ..models.link import LinkSpec

    problems, documents = _parse_lab_documents(root)
    inventory: "Inventory | None" = None
    inventory_broken = False
    try:
        inventory = _inventory_for(root, cache)
    except (InventoryError, ValueError) as e:
        problems.append(f"inventory: {e}")
        inventory_broken = True

    def _validate_entry(host_data: dict[str, Any], *, element: "Element") -> None:
        validate_host_dict(resolve_host_entry(host_data, inventory, element).host_data)

    for lab_file, _, elements, links in documents:
        for element in elements:
            # Bound rather than passed alongside: _item_problem calls its
            # validator with the item and nothing else.
            validate = partial(_validate_entry, element=element.to_element())
            for idx, host_data in enumerate(element.hosts):
                if inventory_broken and references_inventory(host_data):
                    continue  # reported once above; these resolve once it is fixed
                prefix = f"{lab_file}: element {element.name!r} hosts[{idx}]"
                problems.extend(_item_problem(validate, host_data, prefix))
        for idx, link_data in enumerate(links):
            problems.extend(
                _item_problem(LinkSpec.model_validate, link_data, f"{lab_file}: links[{idx}]")
            )
    return problems


def _lab_warnings(
    root: Path, cache: "dict[Path, Inventory | Exception | None] | None" = None
) -> list[str]:
    """Advisory findings across every lab file — never failing.

    Spec §8.3, §9 and §11, plus the shared-element protection rule of spec
    2026-08-28 three-level-reservations §7: two labs that share an element
    neither of them can reserve below the lab level, while their lab-level sets
    have nothing in common (:func:`otto.labs.doctor.lab_warnings`).

    Separate from :func:`_validate_lab` because the two answer different
    questions: a problem is "otto will not load this", a warning is "otto will
    load this and it is probably not what you meant". Only the first fails the
    report. Parsing runs again here rather than being threaded through the
    lab verdict, which carries problems and no warning channel.

    When an inventory resolves, its own advisory findings — a snapshot served
    because the backend was unreachable, orphan records
    (:func:`~otto.inventory.doctor.orphan_warning`), creds-store keys the
    inventory does not hold, and world-readable creds-store files
    (:func:`~otto.inventory.doctor.creds_mode_warnings`) — are appended. A
    broken inventory declaration contributes nothing here: it is already a
    problem in the lab verdict via :func:`_validate_lab`, and repeating it
    as a warning would be noise.

    The stale-snapshot notice is REPORTED rather than left to the cache's own
    ``logger.warning``, which fires once per snapshot per process and may
    already have been spent by an earlier resolution (``entry()``'s
    completion-cache write runs before the root callback installs a console
    handler at all). Spec §19.2 pitches ``otto init`` as the dead-reference
    gate to run in CI, and a green report against a days-old snapshot is
    exactly what that gate must not give.
    """
    from ..inventory import InventoryError, snapshot_cache_of
    from ..inventory.doctor import (
        creds_mode_warnings,
        orphan_creds_warning,
        orphan_warning,
        referenced_keys,
    )
    from ..labs.doctor import lab_warnings

    _, documents = _parse_lab_documents(root)
    warnings = lab_warnings([(src, entries, elements) for src, entries, elements, _ in documents])
    try:
        inventory = _inventory_for(root, cache)
    except (InventoryError, ValueError):
        return warnings  # the problem is already in the lab verdict
    if inventory is not None:
        try:
            orphan = orphan_warning(
                inventory, referenced=referenced_keys(elements for _, _, elements, _ in documents)
            )
            orphan_creds = orphan_creds_warning(inventory)
        except InventoryError as e:
            orphan = f"inventory '{inventory.label}': could not list records: {e}"
            orphan_creds = None
        # Read AFTER the orphan check, never before: the notice is set by the
        # resolution that check performs, and construction touches nothing.
        snapshot = snapshot_cache_of(inventory)
        stale = snapshot.stale_notice if snapshot is not None else None
        # Staleness first — it is the fact that qualifies every finding under
        # it, orphan list included.
        warnings.extend(
            w for w in (stale, orphan, orphan_creds, *creds_mode_warnings(inventory)) if w
        )
    return warnings


def _validate_tests(root: Path) -> list[str]:
    """Light check of configured test dirs: existence, ``test_*.py`` presence, syntax.

    Deliberately does NOT run pytest's collection (``otto test
    --list-tests``): that imports every test file and conftest, which is too
    heavy for a doctor check. ``ast.parse`` catches syntax errors without
    importing user code.
    """
    import ast

    paths = settings_paths(root)
    tests_dirs = paths["tests"] if paths is not None else [root / "tests"]
    problems: list[str] = []
    for tests_dir in tests_dirs:
        if not tests_dir.is_dir():
            problems.append(f"tests dir not found: {tests_dir}")
            continue
        test_files = sorted(tests_dir.glob("test_*.py"))
        if not test_files:
            problems.append(f"no test files found under {tests_dir}")
            continue
        for test_file in test_files:
            try:
                # Bytes, so a coding cookie is honoured as Python honours it;
                # an undecodable file is a SyntaxError, a NUL byte a ValueError,
                # and what glob lists but nothing can read (a dangling link, a
                # directory named test_*.py) an OSError.
                ast.parse(test_file.read_bytes(), filename=str(test_file))
            except (SyntaxError, ValueError, OSError) as e:  # noqa: PERF203 — per-file resilience, mirrors json_repository.py
                problems.append(f"{test_file}: {e}")
    return problems


def _validate_instructions(root: Path, modules: list[str]) -> list[str]:
    """Every declared init module must resolve the way the loader will import it.

    Resolution only (:func:`otto.config.repo.find_init_module`): no user code
    runs, because the doctor runs lab-free and may run before
    ``OTTO_SUT_DIRS`` is set.
    """
    from ..config.repo import find_init_module

    libs = instruction_libs(root)
    problems = [f"libs dir not found: {lib}" for lib in libs if not lib.is_dir()]
    searched = ", ".join(str(lib) for lib in libs) or "none declared"
    problems.extend(
        f"init module {module} not found under libs ({searched}) or on sys.path"
        for module in modules
        if find_init_module(module, libs) is None
    )
    return problems


# ── kmodcov ────────────────────────────────────────────────────────────────────


def _kmodcov_anchored(root: Path) -> list[Path | ValueError]:
    """Anchor each kmodcov entry's ``source`` under *root*, or keep why it cannot be.

    A ``source`` under an unknown user's home (``~nosuchuser/...``) cannot
    be anchored; the error is kept in its place so the kmodcov area can
    report it instead of the doctor raising.
    """
    from ..utils import anchor_path

    anchored: list[Path | ValueError] = []
    for entry in kmodcov_entries(root):
        if not isinstance(entry.get("source"), str):
            continue
        try:
            anchored.append(anchor_path(Path(entry["source"]), root))
        except ValueError as e:
            anchored.append(e)
    return anchored


def _kmodcov_sources(root: Path) -> list[Path]:
    """Return the vendored directories the kmodcov entries name via ``source``, under *root*."""
    return [source for source in _kmodcov_anchored(root) if isinstance(source, Path)]


def _validate_kmodcov(root: Path) -> list[str]:
    """Require a declared ``source`` to anchor and to hold the library.

    Drift from the installed otto is advisory, never a failure — see
    :func:`_kmodcov_warnings`.
    """
    from ..kmodcov import check_tree

    problems: list[str] = []
    for source in _kmodcov_anchored(root):
        if isinstance(source, ValueError):
            problems.append(f"a kmodcov dev tool's `source`: {source}")
        elif check_tree(source).state == "absent":
            problems.append(
                f"{source}: a kmodcov dev tool names it as `source`, but there is no "
                f"otto_kmodcov there — run `otto cov kmodcov export {source}`"
            )
    return problems


def _kmodcov_warnings(root: Path) -> list[str]:
    """Vendored copies that differ from the installed otto — reported, never a failure."""
    from ..kmodcov import check_tree

    warnings: list[str] = []
    for source in _kmodcov_sources(root):
        result = check_tree(source)
        if result.state != "differs":
            continue
        origin = f" (exported by otto {result.exported_by})" if result.exported_by else ""
        names = ", ".join([*result.differing, *result.missing])
        warnings.append(
            f"{source}{origin} is not the installed otto's library: {names} — "
            f"re-export with `otto cov kmodcov export {source}` and review the diff"
        )
    return warnings
