"""``otto init`` — scaffold a new otto repo or validate an existing one.

Each *area* (settings, lab, tests, instructions) can be detected, validated
(existing artifacts are checked via the SAME ingestion code bootstrap uses —
never modified), or scaffolded. Interactive by default; ``--all`` or per-area
flags skip prompts. See docs/guide/repo-setup.md.
"""

import dataclasses
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import tomli
import typer

SETTINGS_TEMPLATE = """\
name = "{name}"
version = "{version}"

# Where otto looks for things, relative to this repo's root (${{sut_dir}}).
# These conventional paths are pre-wired so `otto init --lab` etc. can add
# areas later without editing this file.
labs = ["${{sut_dir}}/lab_data"]   # directories searched for hosts.json
tests = ["${{sut_dir}}/tests"]     # defines where test discovery happens
libs = ["${{sut_dir}}/pylib"]      # added to sys.path at startup
init = ["{init_module}"]           # modules imported at startup (register instructions)

# --- optional sections (uncomment to use; see docs/guide/repo-setup.md) ---
# [lab]                    # host-source backend selection (default: json)
# [host_preferences."*"]   # selector-scoped term/transfer preferences
# [os_profiles."my-os"]    # named OS-profile bundles for host entries
# [reservations]           # reservation gate configuration
# [coverage]               # remote gcov collection settings
# [docker]                 # image builds + compose stacks
"""

HOSTS_JSON_ENTRY = {
    "_comment": (
        "Example host - replace these values. Full host schema: "
        "docs/guide/host-database.md or `otto schema export`. The `labs` list "
        "names the labs this host belongs to (select with --lab/OTTO_LAB)."
    ),
    "ip": "192.0.2.1",
    "element": "example-device",
    "os_type": "unix",
    "valid_terms": ["ssh"],
    "valid_transfers": ["scp", "sftp"],
    "creds": {"admin": "CHANGE_ME"},
    "resources": ["example-device"],
    "labs": ["example_lab"],
}

LAB_README_TEMPLATE = """\
# lab_data/

This directory holds `hosts.json` — otto's host database for this repo. Every
entry describes one lab host and is validated against a pydantic spec before
otto will use it (`UnixHostSpec` / `EmbeddedHostSpec`, see
`docs/guide/host-database.md`). The scaffolded `hosts.json` has one example
entry; edit or replace it, and add as many more entries as your lab needs.

## Fields in the example entry

- **`ip`** — the host's IP address (or hostname), used to open term/transfer
  sessions.
- **`element`** — the host's unique id within this repo's host database. This
  is the name you pass to `--lab`-scoped commands and `get_host()`.
- **`os_type`** — `"unix"` for a UnixHost-backed entry (SSH/telnet-capable
  Linux/BSD-like systems) or `"embedded"` for an EmbeddedHost-backed entry
  (Zephyr and similar). Determines which spec class validates the rest of
  the entry.
- **`valid_terms`** — the ordered menu of term backends this host supports
  (e.g. `"ssh"`, `"telnet"`). The first entry is the default unless a
  `[host_preferences]` selector in `settings.toml` overrides it.
- **`valid_transfers`** — the ordered menu of file-transfer backends this
  host supports (e.g. `"scp"`, `"sftp"`, `"ftp"`, `"nc"`). Same
  first-entry-is-default rule as `valid_terms`.
- **`creds`** — a dict of username to password/secret, keyed by login user.
  Replace `"CHANGE_ME"` with a real credential (or point it at your secrets
  manager per your repo's convention) before connecting to a real host.
- **`resources`** — a set of resource names this host claims, used by
  reservations to prevent two sessions from using the same physical device
  at once. Usually just the host's own name.
- **`labs`** — the list of lab names this host belongs to. A host can belong
  to more than one lab; select which lab is active with `--lab`/`OTTO_LAB`.

## Keys starting with `_`

`hosts.json` is plain JSON, which has no comment syntax. Any key beginning
with `_` (like `_comment` above) is stripped before validation, so it is
otto's sanctioned way to leave a note inline. Use it freely.

## Where to go next

- Full host schema reference: `docs/guide/host-database.md`
- Machine-readable schema (for editor validation or codegen):
  `otto schema export`
- Confirm otto sees your hosts once you've edited this file:
  `otto --lab example_lab --list-hosts`
"""

TEST_EXAMPLE_TEMPLATE = '''\
"""Example otto test suite — runs hostless so it passes out of the box."""

from typing import Annotated

import typer

from otto import options
from otto.suite import OttoSuite


@options
class _Options:
    greeting: Annotated[str, typer.Option(help="Greeting the example test logs.")] = "hello"


class TestExample(OttoSuite[_Options]):
    """A minimal suite: `otto test TestExample` (auto-registered by its Test* name)."""

    Options = _Options

    async def test_greeting_has_a_default(self, suite_options: _Options, repo_marker: str) -> None:
        assert suite_options.greeting == "hello"
        assert repo_marker == "from-conftest"


def test_example_function() -> None:
    """Plain pytest functions run too: `otto test --tests test_example_function`."""
    assert True
'''

CONFTEST_TEMPLATE = '''\
"""Repo-wide fixtures — available to every test under tests/ (any depth)."""

import pytest


@pytest.fixture
def repo_marker() -> str:
    """Trivial example fixture the scaffolded suite consumes."""
    return "from-conftest"


# Fixtures can hand tests live lab hosts; uncomment once your lab_data/ is real:
# @pytest.fixture
# async def primary_host():
#     from otto.configmodule import get_host
#
#     host = get_host("example-device")
#     yield host
#     await host.close()
'''

INSTRUCTIONS_TEMPLATE = '''\
"""{name} instructions — functions exposed as `otto run` subcommands."""

from otto.cli.run import instruction
from otto.logger import get_logger

logger = get_logger()


@instruction()
async def smoke() -> None:
    """Log a greeting — replace with your first real instruction."""
    logger.info("hello from {name}")
'''


@dataclasses.dataclass(frozen=True)
class InitConfig:
    """Values prompts/flags feed into the settings template."""

    name: str
    version: str


@dataclasses.dataclass(frozen=True)
class Area:
    """One scaffoldable/validatable unit of otto repo setup."""

    name: str
    detect: Callable[[Path], bool]
    validate: Callable[[Path], list[str]]
    scaffold: Callable[[Path, InitConfig], list[Path]]


def _settings_paths(root: Path) -> dict[str, list[Path]] | None:
    """Parse ``.otto/settings.toml`` and resolve ``${sut_dir}`` in its path lists.

    Mirrors the substitution :meth:`otto.configmodule.repo.Repo._expand_string`
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


def _scaffold_settings(root: Path, cfg: InitConfig) -> list[Path]:
    target = root / ".otto" / "settings.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        SETTINGS_TEMPLATE.format(
            name=cfg.name, version=cfg.version, init_module=f"{cfg.name}_instructions"
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
    hosts = lab_dir / "hosts.json"
    hosts.write_text(json.dumps([HOSTS_JSON_ENTRY], indent=4) + "\n")
    readme = lab_dir / "README.md"
    readme.write_text(LAB_README_TEMPLATE)
    return [hosts, readme]


def _scaffold_tests(root: Path, cfg: InitConfig) -> list[Path]:  # noqa: ARG001 — cfg unused, uniform Area signature
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    example = tests_dir / "test_example.py"
    example.write_text(TEST_EXAMPLE_TEMPLATE)
    conftest = tests_dir / "conftest.py"
    conftest.write_text(CONFTEST_TEMPLATE)
    return [example, conftest]


def _scaffold_instructions(root: Path, cfg: InitConfig) -> list[Path]:
    module_dir = root / "pylib" / f"{cfg.name}_instructions"
    module_dir.mkdir(parents=True, exist_ok=True)
    init_file = module_dir / "__init__.py"
    init_file.write_text(INSTRUCTIONS_TEMPLATE.format(name=cfg.name))
    return [init_file]


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
    return any(next(lab_dir.glob("hosts.json"), None) is not None for lab_dir in lab_dirs)


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
    """Validate every ``hosts.json`` under the settings' ``labs`` dirs via the real host factory.

    Delegates each entry to :func:`otto.storage.factory.validate_host_dict` —
    the same per-entry validation :class:`~otto.storage.json_repository.JsonFileLabRepository`
    runs before ever constructing a host — so a bad ``os_type`` or field name
    surfaces exactly the pydantic error otto's own lab loader would raise.
    """
    from ..storage.factory import validate_host_dict

    paths = _settings_paths(root)
    lab_dirs = paths["labs"] if paths is not None else [root / "lab_data"]
    problems: list[str] = []
    for lab_dir in lab_dirs:
        hosts_file = lab_dir / "hosts.json"
        if not hosts_file.is_file():
            continue
        try:
            data = json.loads(hosts_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"{hosts_file}: {e}")
            continue
        if not isinstance(data, list):
            problems.append(f"{hosts_file}: must contain a JSON array, got {type(data).__name__}")
            continue
        for idx, host_data in enumerate(data):
            try:
                validate_host_dict(host_data)
            except ValueError as e:  # noqa: PERF203 — per-item resilience, mirrors json_repository.py
                problems.append(f"{hosts_file}: [{idx}] {e}")
    return problems


def _validate_tests(root: Path) -> list[str]:
    """Light check of configured test dirs: existence, ``test_*.py`` presence, syntax.

    Deliberately does NOT build a :class:`~otto.configmodule.repo.Repo` and
    run :meth:`~otto.configmodule.repo.Repo.collect_tests` — that spins an
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
    Area("lab", _detect_lab, _validate_lab, _scaffold_lab),
    Area("tests", _detect_tests, _validate_tests, _scaffold_tests),
    Area("instructions", _detect_instructions, _validate_instructions, _scaffold_instructions),
]


async def init_command(
    all_areas: Annotated[
        bool, typer.Option("--all", help="Scaffold every missing area without prompting.")
    ] = False,
    lab: Annotated[
        bool, typer.Option("--lab", help="Scaffold the lab area (lab_data/hosts.json).")
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

    requested = {"lab": lab, "tests": tests, "instructions": instructions}
    explicit = any(requested.values())
    interactive = not (all_areas or explicit)

    missing = [a for a in AREAS if not a.detect(root)]
    missing_names = {a.name for a in missing}

    if "settings" in missing_names and (all_areas or explicit):
        typer.echo("settings.toml is the repo marker — scaffolding it first.")

    if interactive and "settings" in missing_names:
        name = name or typer.prompt("Product name", default=root.name)
        version = typer.prompt("Version", default=version)
    cfg = InitConfig(name=name or _existing_settings_name(root) or root.name, version=version)

    scaffolded: list[str] = []
    for area in AREAS:
        if area.name not in missing_names:
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

    from otto.configmodule.env import SUT_DIRS_ENV_VAR

    steps: list[str] = []
    current = os.environ.get(SUT_DIRS_ENV_VAR, "")
    if str(root) not in current.split(","):
        steps.append(f"export {SUT_DIRS_ENV_VAR}={root}")
    steps.append("otto --install-completion")
    steps.append("otto --lab example_lab --list-hosts")
    steps.append("otto test --list-suites")
    steps.append("otto test TestExample")
    steps.append("otto test --tests test_example_function")
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
