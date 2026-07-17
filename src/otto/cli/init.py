"""``otto init`` — scaffold a new otto repo or validate an existing one.

Each *area* (settings, schemas, lab, tests, instructions) can be detected,
validated (existing artifacts are checked via the SAME ingestion code
bootstrap uses — never modified, except the otto-owned schemas area, which
``--schemas`` refreshes even when already present), or scaffolded.
Interactive by default; ``--all`` or per-area flags skip prompts. See
docs/guide/setup/repo-setup.md.
"""

import dataclasses
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, cast

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


def _settings_paths(root: Path) -> dict[str, list[Path]] | None:
    """Parse ``.otto/settings.toml`` and resolve ``${sut_dir}`` in its path lists.

    Mirrors the substitution :meth:`otto.config.repo.Repo._expand_string`
    performs (plain ``str.replace``, no other variables). Returns ``None`` when
    the settings file is absent or fails to parse, so callers fall back to the
    conventional path instead of erroring.
    """
    settings_path = root / ".otto" / "settings.toml"
    if not settings_path.is_file():
        return None
    try:
        data = tomli.loads(settings_path.read_text())
    except (tomli.TOMLDecodeError, OSError):
        return None
    sut_dir = str(root)
    resolved: dict[str, list[Path]] = {}
    for key in ("labs", "tests", "libs"):
        values = data.get(key, [])
        if not isinstance(values, list):
            continue
        resolved[key] = [Path(str(v).replace("${sut_dir}", sut_dir)) for v in values]
    return resolved


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
    return created


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
                    "docs/guide/setup/editor-schemas.md for the schema associations"
                )
            continue
        vscode.mkdir(exist_ok=True)
        target.write_text(content)
        created.append(target)
    return created


def _validate_schemas(root: Path) -> list[str]:
    """Staleness doctor: regenerate in-memory and diff structurally against disk.

    Parsed-JSON comparison (never bytes) so a reformatted-but-equal file stays
    green. Missing, differing, orphaned, and unparsable ``*.schema.json`` files
    each get a problem naming both remedies. Mirrors the docs' "regenerate
    after upgrading otto" note, mechanically.
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
            problems.append(f"{path}: stale (differs from installed otto's models) — {remedy}")
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
    paths = _settings_paths(root)
    lab_dirs = paths["labs"] if paths is not None else [root / "lab_data"]
    # NB: `any(p.glob(...) for p in dirs)` is a bug trap — a Path.glob()
    # generator object is truthy even when empty, so `any()` would see it as
    # a hit regardless of matches. Force each generator to yield to check.
    return any(next(lab_dir.glob("lab.json"), None) is not None for lab_dir in lab_dirs)


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


def _validate_lab(root: Path) -> list[str]:
    """Validate every ``lab.json`` under the settings' ``labs`` dirs via the real specs.

    The top-level section shape (object guard, ``_``-comment allowance — also
    tolerating a top-level ``$schema`` key, the editor-wiring idiom —
    unknown-section rejection, per-section array check) is delegated to
    :func:`otto.labs.json_repository.parse_lab_sections` — the SAME helper the
    runtime loader uses — so the doctor cannot drift from what otto actually
    accepts (e.g. an unknown ``routes`` section is rejected here exactly as it
    is at load). Each ``hosts`` entry is then delegated to
    :func:`otto.host.factory.validate_host_dict` (a bad ``os_type`` or field
    name surfaces the same pydantic error the loader would raise), and each
    ``links`` entry is validated structurally via
    :class:`~otto.models.link.LinkSpec`; endpoint cross-references (host ids,
    interface keys) are resolved at load time, not here.
    """
    from pydantic import ValidationError

    from ..host.factory import validate_host_dict
    from ..labs.errors import LabRepositoryError
    from ..labs.json_repository import parse_lab_sections
    from ..models.link import LinkSpec

    paths = _settings_paths(root)
    lab_dirs = paths["labs"] if paths is not None else [root / "lab_data"]
    problems: list[str] = []
    for lab_dir in lab_dirs:
        lab_file = lab_dir / "lab.json"
        if not lab_file.is_file():
            continue
        try:
            data = json.loads(lab_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"{lab_file}: {e}")
            continue
        try:
            sections = parse_lab_sections(data, str(lab_file))
        except LabRepositoryError as e:
            problems.append(str(e))
            continue
        for idx, host_data in enumerate(sections["hosts"]):
            if not isinstance(host_data, dict):
                problems.append(
                    f"{lab_file}: hosts[{idx}] must be a JSON object, "
                    f"got {type(host_data).__name__}"
                )
                continue
            try:
                # JSON object keys are always str; the isinstance guard above
                # is the runtime check ty cannot see through.
                validate_host_dict(cast("dict[str, Any]", host_data))
            except ValueError as e:
                problems.append(f"{lab_file}: hosts[{idx}] {e}")
        for idx, link_data in enumerate(sections["links"]):
            try:
                LinkSpec.model_validate(link_data)
            except ValidationError as e:  # noqa: PERF203 — per-item resilience
                problems.append(f"{lab_file}: links[{idx}] {e}")
    return problems


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

    Registered as a bare-function loader (``"otto.cli.init:init_command"``),
    so ``resolve_spec_command`` wraps it with ``async_typer_command`` — same
    as every other otto leaf command — which requires a coroutine function.
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
    steps.append("otto --lab example_lab --list-hosts")
    steps.append("otto test --list-suites")
    steps.append("otto test TestExample")
    steps.append("otto test --tests test_example_function")
    steps.append("otto run smoke")
    rprint("\n[bold]Next steps[/bold]")
    for i, step in enumerate(steps, 1):
        rprint(f"  {i}. {step}")

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
            problems = area.validate(root)
            if problems:
                failed = True
                table.add_row(area.name, "[red]✗[/red]", "\n".join(problems))
            else:
                table.add_row(area.name, "[green]✓[/green]", "")
    rprint(table)
    if failed:
        raise typer.Exit(code=1)
