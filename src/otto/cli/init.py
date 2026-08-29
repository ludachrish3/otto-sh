"""``otto init`` — scaffold a new otto repo or validate an existing one.

Each *area* (settings, schemas, lab, tests, instructions) can be detected,
validated (existing artifacts are checked via the SAME ingestion code
bootstrap uses — never modified, except the otto-owned schemas area, which
``--schemas`` refreshes even when already present), or scaffolded.
Interactive by default; ``--all`` or per-area flags skip prompts. See
docs/guide/configuration/settings.md.
"""

import dataclasses
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import tomli
import typer

from .init_templates import (
    CONFTEST_TEMPLATE,
    INSTRUCTIONS_TEMPLATE,
    LAB_JSON_TEMPLATE,
    LAB_README_TEMPLATE,
    OPTIONS_TEMPLATE,
    SETTINGS_TEMPLATE,
    TEST_EXAMPLE_TEMPLATE,
    VSCODE_EXTENSIONS_TEMPLATE,
    VSCODE_SETTINGS_TEMPLATE,
)

if TYPE_CHECKING:
    # Annotation-only: importing ``otto.inventory`` for real pulls ~77 otto
    # modules (see ``otto/inventory/config.py``'s module docstring), and
    # ``init.py`` sits on a budgeted CLI surface (scripts/import_budget.py).
    # Every real use below is a function-local import.
    from ..inventory import Inventory


@dataclasses.dataclass(frozen=True)
class InitConfig:
    """Values prompts/flags feed into the settings template."""

    name: str
    version: str

    @property
    def module_base(self) -> str:
        """``name`` sanitized into a valid module-name base (``my-repo`` -> ``my_repo``)."""
        base = re.sub(r"\W", "_", self.name)
        return f"_{base}" if base[:1].isdigit() else base


@dataclasses.dataclass(frozen=True)
class Area:
    """One scaffoldable/validatable unit of otto repo setup."""

    name: str
    detect: Callable[[Path], bool]
    validate: Callable[[Path], list[str]]
    scaffold: Callable[[Path, InitConfig], list[Path]]


def _settings_data(root: Path) -> dict[str, Any] | None:
    """Return the raw parsed ``.otto/settings.toml`` (``None`` if absent/unparseable).

    Returning ``None`` (rather than raising) is what lets every doctor check
    fall back to the conventional layout on a repo otto has not scaffolded
    yet; the settings area itself reports the parse error.
    """
    settings_path = root / ".otto" / "settings.toml"
    if not settings_path.is_file():
        return None
    try:
        return tomli.loads(settings_path.read_text())
    except (tomli.TOMLDecodeError, OSError):
        return None


def _settings_paths(root: Path) -> dict[str, list[Path]] | None:
    """Parse ``.otto/settings.toml`` and anchor its ``tests``/``libs`` lists to *root*.

    Returns ``None`` when the settings file is absent or fails to parse, so
    callers fall back to the conventional path instead of erroring.

    Applies phase 1 anchoring via :func:`otto.utils.anchor_path`: ``~`` expands
    to the user's home, and whatever is still relative afterwards is anchored
    to *root*. Host data is NOT one of these lists — it is declared as
    ``[[lab.sources]]`` entries and read through :func:`_lab_files`.
    """
    from ..utils import anchor_path

    data = _settings_data(root)
    if data is None:
        return None
    resolved: dict[str, list[Path]] = {}
    for key in ("tests", "libs"):
        values = data.get(key, [])
        if not isinstance(values, list):
            continue
        paths = [Path(str(v)) for v in values]
        resolved[key] = [anchor_path(p, root) for p in paths]
    return resolved


def _lab_file_groups(root: Path) -> list[list[Path]]:
    """Every lab file this repo's json ``[[lab.sources]]`` entries name, ONE LIST PER SOURCE.

    THE single reader of a repo's host-data declaration inside ``otto init``:
    detection and validation both go through it (via :func:`_lab_files`), so
    the doctor can never disagree with the runtime — or with itself — about
    which files hold this repo's hosts. Compiles the entries with the SAME
    :func:`otto.labs.sources.compile_lab_sources` ``Repo.parse_settings``
    uses, then asks each json source for its files (a directory entry
    contributes its ``lab.json``; a ``.json`` entry IS the file; a glob
    contributes every match).

    The grouping is load-bearing for the duplicate rules, which are per SOURCE:
    two files of ONE source declaring the same lab is a typo, the same
    declaration in two SOURCES is the documented override seam (spec §2.4).
    Callers that only need "which files exist" flatten it through
    :func:`_lab_files`.

    Falls back to the conventional ``lab_data/lab.json`` only when there is no
    readable ``settings.toml`` at all — init must work on a repo it has not
    scaffolded yet. Settings that parse but declare no ``[lab]`` table declare
    no host data, so they yield no files; a malformed ``[lab]`` yields none
    either, because the settings area validates the very same file through
    ``SettingsModel`` and reports the pydantic error itself.
    """
    from ..labs.json_repository import LAB_FILENAME
    from ..labs.sources import compile_lab_sources
    from ..models.settings import LabConfigSpec

    data = _settings_data(root)
    if data is None:
        return [[root / "lab_data" / LAB_FILENAME]]
    lab = data.get("lab")
    if lab is None:
        return []
    try:
        # pydantic's ValidationError IS a ValueError, so one arm covers both
        # the envelope check and compile_lab_sources' own shape errors.
        sources = compile_lab_sources(
            LabConfigSpec.model_validate(lab),
            repo_name=str(data.get("name") or root.name),
            sut_dir=root,
        )
    except ValueError:
        return []
    return [src.lab_files() for src in sources if src.backend == "json"]


def _lab_files(root: Path) -> list[Path]:
    """Every lab file this repo's json sources name, flattened in source order.

    The "does this repo have host data, and where" view, for detection and for
    any caller that does not care which source a file came from. See
    :func:`_lab_file_groups` for the per-source view the duplicate rules need.
    """
    return [lab_file for group in _lab_file_groups(root) for lab_file in group]


def _ensure_options_module(root: Path, cfg: InitConfig) -> list[Path]:
    """Create ``pylib/<module_base>_options.py`` if absent; never overwrite.

    Shared plumbing between the tests and instructions areas: both samples
    inherit ``RepoOptions``, so whichever scaffold runs first creates it and
    the other reuses it (idempotent — the module is user-owned once written).
    """
    pylib = root / "pylib"
    pylib.mkdir(parents=True, exist_ok=True)
    target = pylib / f"{cfg.module_base}_options.py"
    if target.exists():
        return []
    target.write_text(OPTIONS_TEMPLATE.format(name=cfg.name))
    return [target]


def _schemas_dir(root: Path) -> Path:
    return root / ".otto" / "schemas"


def _detect_schemas(root: Path) -> bool:
    return next(_schemas_dir(root).glob("*.schema.json"), None) is not None


def _scaffold_schemas(root: Path, cfg: InitConfig) -> list[Path]:  # noqa: ARG001 — cfg unused, uniform Area signature
    """Write the generated editor schemas — same product as ``otto schema export``."""
    from ..models.jsonschema import build_schemas

    out = _schemas_dir(root)
    out.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for stem, doc in build_schemas().items():
        target = out / f"{stem}.schema.json"
        target.write_text(json.dumps(doc, indent=2) + "\n")
        created.append(target)
    created.extend(_scaffold_editor_wiring(root))
    created.extend(_scaffold_snippets(root))
    return created


def _scaffold_snippets(root: Path) -> list[Path]:
    """Write the generated VS Code snippets — otto-owned, so ALWAYS refreshed.

    ``.vscode/*.code-snippets`` is auto-loaded by VS Code, so this file needs
    no wiring in ``settings.json``. It is generated from the live models
    (spec 2026-08-27 lab-definition-v2 §12), which makes it otto's to
    overwrite — unlike the user-owned ``.vscode/settings.json``, which
    :func:`_scaffold_editor_wiring` only ever creates when absent. It is
    deliberately NOT checked by :func:`_validate_schemas`: an editor
    convenience going stale is not a broken repo.
    """
    from ..models.snippets import build_snippets

    vscode = root / ".vscode"
    vscode.mkdir(parents=True, exist_ok=True)
    target = vscode / "otto.code-snippets"
    target.write_text(json.dumps(build_snippets(), indent=2) + "\n")
    return [target]


def _scaffold_editor_wiring(root: Path) -> list[Path]:
    """Write ``.vscode`` schema wiring, strictly only-if-absent.

    VS Code settings are JSONC (comments, trailing commas) — merging
    programmatically risks corrupting a user file, so an existing
    ``settings.json`` is never touched; the docs snippet covers manual
    wiring. These files are scaffold-only: `_validate_schemas` must never
    look at them (user-owned editor config once created).
    """
    created: list[Path] = []
    vscode = root / ".vscode"
    targets = [
        (vscode / "settings.json", VSCODE_SETTINGS_TEMPLATE),
        (vscode / "extensions.json", VSCODE_EXTENSIONS_TEMPLATE),
    ]
    for target, content in targets:
        if target.exists():
            if target.name == "settings.json":
                typer.echo(
                    "existing .vscode/settings.json left untouched — see "
                    "docs/guide/cli/schema/editors.md for the schema associations"
                )
            continue
        vscode.mkdir(exist_ok=True)
        target.write_text(content)
        created.append(target)
    return created


def _drift_problem(path: Path, data: object, doc: dict[str, Any], remedy: str) -> str:
    """Describe one on-disk schema that differs from the freshly generated one.

    An otto upgrade is the overwhelmingly common cause — schemas are written
    once, at ``otto init``, and then drift as otto moves on (spec 2026-08-27
    lab-definition-v2 §12) — so when the ``x-otto-version`` stamps disagree the
    problem names both versions rather than leaving the reader to wonder
    whether their own edit or their upgrade caused it. A file with no stamp
    (written by an otto from before the stamp existed) reads as
    ``<unstamped>``. *data* is whatever the file parsed to and need not be a
    JSON object at all, so it is probed defensively — a malformed-but-parsable
    schema is still just a problem line, never a traceback.
    """
    stamped = data.get("x-otto-version") if isinstance(data, dict) else None
    if stamped != doc["x-otto-version"]:
        return (
            f"{path}: generated by otto {stamped or '<unstamped>'}, "
            f"installed otto is {doc['x-otto-version']} — {remedy}"
        )
    return f"{path}: stale (differs from installed otto's models) — {remedy}"


def _validate_schemas(root: Path) -> list[str]:
    """Staleness doctor: regenerate in-memory and diff structurally against disk.

    Parsed-JSON comparison (never bytes) so a reformatted-but-equal file stays
    green. Missing, differing, orphaned, and unparsable ``*.schema.json`` files
    each get a problem naming both remedies; a differing file is split by its
    ``x-otto-version`` stamp (see :func:`_drift_problem`). Mirrors the docs'
    "regenerate after upgrading otto" note, mechanically.
    """
    from ..models.jsonschema import build_schemas

    out = _schemas_dir(root)
    remedy = "re-run `otto init --schemas` or `otto schema export`"
    expected = build_schemas()
    on_disk = {p.name: p for p in out.glob("*.schema.json")}
    problems: list[str] = []
    for stem, doc in expected.items():
        name = f"{stem}.schema.json"
        path = on_disk.pop(name, None)
        if path is None:
            problems.append(f"{out / name}: missing — {remedy}")
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:  # per-file resilience
            problems.append(f"{path}: unparsable ({e}) — {remedy}")
            continue
        if data != doc:
            problems.append(_drift_problem(path, data, doc, remedy))
    problems.extend(
        f"{path}: orphaned (installed otto emits no such schema) — {remedy}"
        for _, path in sorted(on_disk.items())
    )
    return problems


def _scaffold_settings(root: Path, cfg: InitConfig) -> list[Path]:
    target = root / ".otto" / "settings.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        SETTINGS_TEMPLATE.format(
            name=cfg.name, version=cfg.version, init_module=f"{cfg.module_base}_instructions"
        )
    )
    # Pre-wired paths must exist so later area scaffolds (and bootstrap) never
    # trip over a missing conventional dir.
    for d in ("lab_data", "tests", "pylib"):
        (root / d).mkdir(exist_ok=True)
    return [target]


def _scaffold_lab(root: Path, cfg: InitConfig) -> list[Path]:  # noqa: ARG001 — cfg unused, uniform Area signature
    lab_dir = root / "lab_data"
    lab_dir.mkdir(parents=True, exist_ok=True)
    lab_file = lab_dir / "lab.json"
    lab_file.write_text(json.dumps(LAB_JSON_TEMPLATE, indent=4) + "\n")
    readme = lab_dir / "README.md"
    readme.write_text(LAB_README_TEMPLATE)
    return [lab_file, readme]


def _scaffold_tests(root: Path, cfg: InitConfig) -> list[Path]:
    created = _ensure_options_module(root, cfg)
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    example = tests_dir / "test_example.py"
    example.write_text(TEST_EXAMPLE_TEMPLATE.format(options_module=f"{cfg.module_base}_options"))
    conftest = tests_dir / "conftest.py"
    conftest.write_text(CONFTEST_TEMPLATE)
    return [*created, example, conftest]


def _scaffold_instructions(root: Path, cfg: InitConfig) -> list[Path]:
    created = _ensure_options_module(root, cfg)
    module_dir = root / "pylib" / f"{cfg.module_base}_instructions"
    module_dir.mkdir(parents=True, exist_ok=True)
    init_file = module_dir / "__init__.py"
    init_file.write_text(
        INSTRUCTIONS_TEMPLATE.format(name=cfg.name, options_module=f"{cfg.module_base}_options")
    )
    return [*created, init_file]


def _existing_settings_name(root: Path) -> str | None:
    """Read ``name`` from an already-scaffolded ``.otto/settings.toml``, if any.

    Used so later area scaffolds (e.g. ``--instructions`` run after settings
    already exists) derive module names from the repo's recorded ``name``
    rather than falling back to the directory basename. Error-tolerant like
    :func:`_detect_instructions`: any parse failure yields ``None`` so callers
    fall back to ``root.name`` as before.
    """
    settings_path = root / ".otto" / "settings.toml"
    if not settings_path.is_file():
        return None
    try:
        data = tomli.loads(settings_path.read_text())
    except (tomli.TOMLDecodeError, OSError):
        return None
    name = data.get("name")
    return name if isinstance(name, str) and name else None


def _detect_settings(root: Path) -> bool:
    return (root / ".otto" / "settings.toml").is_file()


def _detect_lab(root: Path) -> bool:
    return any(lab_file.is_file() for lab_file in _lab_files(root))


def _detect_tests(root: Path) -> bool:
    paths = _settings_paths(root)
    tests_dirs = paths["tests"] if paths is not None else [root / "tests"]
    return any(next(tests_dir.glob("test_*.py"), None) is not None for tests_dir in tests_dirs)


def _detect_instructions(root: Path) -> bool:
    settings_path = root / ".otto" / "settings.toml"
    if not settings_path.is_file():
        return False
    try:
        data = tomli.loads(settings_path.read_text())
    except (tomli.TOMLDecodeError, OSError):
        return False
    init_modules = data.get("init", [])
    if not isinstance(init_modules, list) or not init_modules:
        return False
    paths = _settings_paths(root)
    lib_dirs = paths["libs"] if paths is not None else [root / "pylib"]
    return any((lib_dir / str(mod)).is_dir() for lib_dir in lib_dirs for mod in init_modules)


def _validate_settings(root: Path) -> list[str]:
    """Parse+validate ``.otto/settings.toml`` the same way :meth:`Repo.parse_settings` does.

    Reuses :class:`otto.models.settings.SettingsModel` directly (the same
    model ``Repo.parse_settings`` calls ``model_validate`` on) rather than
    re-implementing the schema. Problems are the ``pydantic.ValidationError``
    text, one block per file, prefixed with the settings path.
    """
    from pydantic import ValidationError

    from ..models.settings import SettingsModel

    settings_path = root / ".otto" / "settings.toml"
    try:
        data = tomli.loads(settings_path.read_text())
    except (tomli.TOMLDecodeError, OSError) as e:
        return [f"{settings_path}: {e}"]
    try:
        SettingsModel.model_validate(data)
    except ValidationError as e:
        return [f"{settings_path}: {e}"]
    return []


def _inventory_for(
    root: Path, cache: "dict[Path, Inventory | Exception | None] | None" = None
) -> "Inventory | None":
    """Return the inventory bootstrap would build for *root*: its own override, else the user file.

    Raises ``InventoryError`` / ``ValueError`` for a broken declaration — the
    caller reports that as a problem naming the file; the doctor must never
    traceback on a broken ``[inventory]`` table or user settings file.

    *cache* memoises the result (inventory, ``None``, or the raised
    exception) per *root* — one ``otto init`` run asks this up to three times
    (the "lab" area's own validation, the warnings pass, the label line), and
    while today's construction is I/O-free, a real backend (the netbox one,
    still to land) would otherwise pay a whole-set fetch three times over. A
    plain dict an owning caller creates and threads through by hand — never a
    module global, which would outlive one invocation and leak across
    processes or tests that import this module.
    """
    if cache is not None and root in cache:
        cached = cache[root]
        if isinstance(cached, Exception):
            raise cached
        return cached

    from ..config.user_settings import load_user_settings
    from ..inventory import InventoryDeclaration, InventoryError, build_inventory_from_declarations

    data = _settings_data(root) or {}
    table = data.get("inventory") or {}
    declarations = (
        [
            InventoryDeclaration(
                origin=str(root / ".otto" / "settings.toml"), anchor_dir=root, table=dict(table)
            )
        ]
        if isinstance(table, dict) and table
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


def _print_inventory_label(
    root: Path, cache: "dict[Path, Inventory | Exception | None] | None" = None
) -> None:
    """Print ``inventory: <label>`` when one resolves for *root*; silent otherwise.

    A separate function (rather than inline in :func:`init_command`) so the
    try/except does not count against that function's own cyclomatic budget.
    A broken declaration prints nothing here — it is already a problem in the
    verdict table via :func:`_validate_lab`, and repeating it would be noise.
    """
    from rich import print as rprint
    from rich.markup import escape

    from ..inventory import InventoryError

    try:
        inventory = _inventory_for(root, cache)
    except (InventoryError, ValueError):
        return
    if inventory is not None:
        rprint(f"inventory: {escape(inventory.label)}")


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

    The duplicate state is threaded per SOURCE (:func:`_lab_file_groups`), not
    over the flat file list: two sources may each declare the same lab, which
    is how ``[[lab.sources]]`` layering works, and rejecting that would fail
    repos otto loads happily.
    """
    from ..labs.errors import LabRepositoryError
    from ..labs.json_repository import (
        check_in_source_duplicates,
        parse_elements,
        parse_lab_entries,
        parse_lab_sections,
    )

    problems: list[str] = []
    documents: list[_ParsedLab] = []
    for group in _lab_file_groups(root):
        # Reset per source: the duplicate rules are in-source rules.
        seen_labs: dict[str, Path] = {}
        seen_elements: dict[Any, Path] = {}
        for lab_file in group:
            if not lab_file.is_file():
                continue
            try:
                data = json.loads(lab_file.read_text())
            except (OSError, json.JSONDecodeError) as e:
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
    """
    from ..inventory import InventoryError

    try:
        validate(item)
    except (ValueError, InventoryError) as e:
        return [f"{prefix} {e}"]
    return []


def _validate_lab(
    root: Path, cache: "dict[Path, Inventory | Exception | None] | None" = None
) -> list[str]:
    """Validate every lab file the settings' ``[[lab.sources]]`` name, via the real specs.

    The file shape is :func:`_parse_lab_documents`' job; what is left is the
    two payloads the wrapper models hold opaquely. Each element's host
    entries are flattened the way the loader flattens them
    (:meth:`otto.models.lab.ElementSpec.flatten` stamps the element identity
    on), resolved against this repo's inventory the way the loader resolves
    them (:func:`otto.inventory.resolve_host_entry`, spec §6), and handed to
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
    :func:`~otto.inventory.doctor.references_inventory` (R7): a ``None`` or
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

    def _validate_entry(host_data: dict[str, Any]) -> None:
        validate_host_dict(resolve_host_entry(host_data, inventory).host_data)

    for lab_file, _, elements, links in documents:
        for element in elements:
            for idx, host_data in enumerate(element.flatten()):
                if inventory_broken and references_inventory(host_data):
                    continue  # reported once above; these resolve once it is fixed
                prefix = f"{lab_file}: element {element.name!r} hosts[{idx}]"
                problems.extend(_item_problem(_validate_entry, host_data, prefix))
        for idx, link_data in enumerate(links):
            problems.extend(
                _item_problem(LinkSpec.model_validate, link_data, f"{lab_file}: links[{idx}]")
            )
    return problems


def _lab_warnings(
    root: Path, cache: "dict[Path, Inventory | Exception | None] | None" = None
) -> list[str]:
    """Advisory findings across every lab file (spec §8.3, §9, §11) — never failing.

    Separate from :func:`_validate_lab` because the two answer different
    questions: a problem is "otto will not load this", a warning is "otto will
    load this and it is probably not what you meant". Only the first sets the
    exit code. Parsing runs again here rather than being threaded through the
    ``Area`` protocol, which has one validate hook and no warning channel.

    When an inventory resolves, its own advisory findings — a snapshot served
    because the backend was unreachable, orphan records
    (:func:`~otto.inventory.doctor.orphan_warning`) and a world-readable
    ``creds_file`` (:func:`~otto.inventory.doctor.creds_mode_warning`) — are
    appended. A broken inventory declaration contributes nothing here: it is
    already a problem in the verdict table via :func:`_validate_lab`, and
    repeating it as a warning would be noise.

    The stale-snapshot notice is REPORTED rather than logged because this
    command is ``lab_free``: ``init_cli_logging`` never runs for such a group
    and otto's ``NullHandler`` defeats ``logging.lastResort``, so the cache's
    own ``logger.warning`` reaches nobody here. Spec §19.2 pitches
    ``otto init`` as the dead-reference gate to run in CI, and a green table
    against a days-old snapshot is exactly what that gate must not print.
    """
    from ..inventory import InventoryError, snapshot_cache_of
    from ..inventory.doctor import creds_mode_warning, orphan_warning, referenced_keys
    from ..labs.doctor import lab_warnings

    _, documents = _parse_lab_documents(root)
    warnings = lab_warnings([(src, entries, elements) for src, entries, elements, _ in documents])
    try:
        inventory = _inventory_for(root, cache)
    except (InventoryError, ValueError):
        return warnings  # the problem is already in the verdict table
    if inventory is not None:
        try:
            orphan = orphan_warning(
                inventory, referenced=referenced_keys(elements for _, _, elements, _ in documents)
            )
        except InventoryError as e:
            orphan = f"inventory '{inventory.label}': could not list records: {e}"
        # Read AFTER the orphan check, never before: the notice is set by the
        # resolution that check performs, and construction touches nothing.
        snapshot = snapshot_cache_of(inventory)
        stale = snapshot.stale_notice if snapshot is not None else None
        # Staleness first — it is the fact that qualifies every finding under
        # it, orphan list included.
        warnings.extend(w for w in (stale, orphan, creds_mode_warning(inventory)) if w)
    return warnings


def _validate_tests(root: Path) -> list[str]:
    """Light check of configured test dirs: existence, ``test_*.py`` presence, syntax.

    Deliberately does NOT build a :class:`~otto.config.repo.Repo` and
    run :meth:`~otto.config.repo.Repo.collect_tests` — that spins an
    inner pytest collection pass (module-cache save/restore, event-loop
    bookkeeping) which is too heavy for a doctor check. ``ast.parse`` catches
    syntax errors without importing user code.
    """
    import ast

    paths = _settings_paths(root)
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
                ast.parse(test_file.read_text(), filename=str(test_file))
            except SyntaxError as e:  # noqa: PERF203 — per-file resilience, mirrors json_repository.py
                problems.append(f"{test_file}: {e}")
    return problems


def _validate_instructions(root: Path) -> list[str]:
    """Check each configured ``init`` module resolves under some ``libs`` dir.

    Path/module-layout checks only — never imports user code (init runs
    lab-free and may run before ``OTTO_SUT_DIRS`` is set, so importing
    arbitrary user modules from a doctor command would be a surprising
    side effect).
    """
    settings_path = root / ".otto" / "settings.toml"
    try:
        data = tomli.loads(settings_path.read_text())
    except (tomli.TOMLDecodeError, OSError) as e:
        return [f"{settings_path}: {e}"]
    init_modules = data.get("init", [])
    if not isinstance(init_modules, list):
        return [f"{settings_path}: 'init' must be a list"]
    paths = _settings_paths(root)
    lib_dirs = paths["libs"] if paths is not None else [root / "pylib"]
    problems: list[str] = [
        f"libs dir not found: {lib_dir}" for lib_dir in lib_dirs if not lib_dir.is_dir()
    ]
    for mod in init_modules:
        mod_name = str(mod)
        found = any(
            (lib_dir / mod_name / "__init__.py").is_file() or (lib_dir / f"{mod_name}.py").is_file()
            for lib_dir in lib_dirs
        )
        if not found:
            searched = ", ".join(str(lib_dir) for lib_dir in lib_dirs)
            problems.append(f"init module {mod_name} not found under libs ({searched})")
    return problems


AREAS: list[Area] = [
    Area("settings", _detect_settings, _validate_settings, _scaffold_settings),
    Area("schemas", _detect_schemas, _validate_schemas, _scaffold_schemas),
    Area("lab", _detect_lab, _validate_lab, _scaffold_lab),
    Area("tests", _detect_tests, _validate_tests, _scaffold_tests),
    Area("instructions", _detect_instructions, _validate_instructions, _scaffold_instructions),
]


async def init_command(
    all_areas: Annotated[
        bool, typer.Option("--all", help="Scaffold every missing area without prompting.")
    ] = False,
    schemas: Annotated[
        bool,
        typer.Option(
            "--schemas",
            help=(
                "Scaffold (or refresh, if present) the schemas area: .otto/schemas + editor wiring."
            ),
        ),
    ] = False,
    lab: Annotated[
        bool, typer.Option("--lab", help="Scaffold the lab area (lab_data/lab.json).")
    ] = False,
    tests: Annotated[
        bool, typer.Option("--tests", help="Scaffold the tests area (example suite + conftest).")
    ] = False,
    instructions: Annotated[
        bool, typer.Option("--instructions", help="Scaffold the instructions area (pylib module).")
    ] = False,
    name: Annotated[
        str,
        typer.Option("--name", help="Product name for settings.toml (default: directory name)."),
    ] = "",
    version: Annotated[
        str, typer.Option("--version", help="Product version for settings.toml.")
    ] = "0.1.0",
    path: Annotated[
        Path, typer.Option("--path", file_okay=False, help="Repo root to operate on.")
    ] = Path(),
) -> None:
    """Scaffold a new otto repo, or validate an existing one's setup.

    Registered as a bare-function loader (``"otto.cli.init:init_command"``);
    as a plain ``async def`` it runs under the full command lifecycle via the
    leaf-invoke wrapper's coroutine bridge (``cli/invoke._wrap_invoke``) —
    registration is the only opt-in.
    """
    root = path.resolve()
    if not root.is_dir():
        raise typer.BadParameter(f"{root} is not a directory", param_hint="--path")

    requested = {"schemas": schemas, "lab": lab, "tests": tests, "instructions": instructions}
    explicit = any(requested.values())
    interactive = not (all_areas or explicit)

    missing = [a for a in AREAS if not a.detect(root)]
    missing_names = {a.name for a in missing}
    # Generated artifacts are otto-owned, so the explicit flag REFRESHES a
    # detected schemas area (the doctor's "re-run `otto init --schemas`"
    # remedy). --all / interactive keep missing-only semantics.
    refresh_names: set[str] = {"schemas"} if schemas else set()

    if "settings" in missing_names and (all_areas or explicit):
        typer.echo("settings.toml is the repo marker — scaffolding it first.")

    if interactive and "settings" in missing_names:
        name = name or typer.prompt("Product name", default=root.name)
        version = typer.prompt("Version", default=version)
    cfg = InitConfig(name=name or _existing_settings_name(root) or root.name, version=version)

    scaffolded: list[str] = []
    for area in AREAS:
        if area.name not in missing_names and area.name not in refresh_names:
            continue
        if interactive:
            wanted = typer.confirm(f"Scaffold the {area.name} area?", default=True)
        elif area.name == "settings":
            wanted = True  # prerequisite: always accompanies any explicit/all request
        else:
            wanted = all_areas or requested[area.name]
        if not wanted:
            continue
        for created in area.scaffold(root, cfg):
            typer.echo(f"created {created.relative_to(root)}")
        scaffolded.append(area.name)

    from rich import print as rprint
    from rich.markup import escape
    from rich.table import Table

    from otto.config.env import SUT_DIRS_ENV_VAR

    steps: list[str] = []
    current = os.environ.get(SUT_DIRS_ENV_VAR, "")
    # Split on comma OR os.pathsep (colon on Linux), matching config.env
    # and settings.OttoEnvSettings convention, then strip each segment
    current_sep = re.compile(rf"[,{re.escape(os.pathsep)}]")
    current_dirs = [p.strip() for p in current_sep.split(current) if p.strip()]
    if str(root) not in current_dirs:
        steps.append(f"export {SUT_DIRS_ENV_VAR}={root}")
    steps.append("otto --install-completion")
    # --install-completion WRITES the script; it does not activate it in the
    # shell already running, so the pair has to be printed together or the
    # user concludes completion is broken (spec §12).
    steps.append("source ~/.bash_completions/otto.sh")
    steps.append("otto --lab example_lab --list-hosts")
    steps.append("otto test --list-suites")
    steps.append("otto test TestExample")
    steps.append("otto test --tests test_example_function")
    steps.append("otto run smoke")
    rprint("\n[bold]Next steps[/bold]")
    for i, step in enumerate(steps, 1):
        rprint(f"  {i}. {step}")

    # One inventory per invocation, not per asker: _validate_lab (below),
    # _print_inventory_label and _lab_warnings each ask _inventory_for the
    # same question about the same root. A plain dict, owned here and threaded
    # through by hand — never a module global (see _inventory_for).
    inventory_cache: "dict[Path, Inventory | Exception | None]" = {}

    table = Table(title=f"otto init — {root}", show_header=True)
    table.add_column("area")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    failed = False
    for area in AREAS:
        if area.name in scaffolded:
            table.add_row(area.name, "[green]scaffolded[/green]", "")
        elif not area.detect(root):
            table.add_row(area.name, "[yellow]skipped[/yellow]", "not requested")
        else:
            # The "lab" area's validate hook is _validate_lab, specifically —
            # it is the one area whose problems come from asking
            # _inventory_for, so it is the one call routed through the shared
            # cache rather than the uniform Area.validate(root) the others use.
            problems = (
                _validate_lab(root, inventory_cache) if area.name == "lab" else area.validate(root)
            )
            if problems:
                failed = True
                # escape(): a problem quotes pydantic (`[type=extra_forbidden,
                # …]`) and the author's own regexes — both tag-shaped, and both
                # silently swallowed by rich markup if handed over raw.
                table.add_row(area.name, "[red]✗[/red]", escape("\n".join(problems)))
            else:
                table.add_row(area.name, "[green]✓[/green]", "")
    rprint(table)

    _print_inventory_label(root, inventory_cache)

    # Advisory only — printed after the verdict table, never folded into it,
    # and deliberately not part of `failed`.
    warnings = _lab_warnings(root, inventory_cache)
    if warnings:
        rprint("\n[bold yellow]Warnings[/bold yellow]")
        for warning in warnings:
            # escape(): a warning quotes the author's own regex, and a
            # tag-shaped one (`[a-z]+`) would otherwise be swallowed as rich
            # markup — the pattern is the whole point of the message.
            rprint(f"  [yellow]•[/yellow] {escape(warning)}")

    if failed:
        raise typer.Exit(code=1)
