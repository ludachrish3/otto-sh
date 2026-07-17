# otto init Sample Plumbing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `otto init` scaffolds imitable options→suite→instruction plumbing, a settings.toml showing the entire configuration surface, and auto-exported editor schemas with self-wiring.

**Architecture:** Spec: `docs/superpowers/specs/2026-07-17-otto-init-sample-plumbing-design.md`. Templates move to a new `src/otto/cli/init_templates.py`; `init.py` gains a fifth "schemas" area (detect / staleness-validate / scaffold via the existing `build_schemas()`), a shared idempotent options-module scaffold, and module-name sanitization. Two one-line-ish core changes: `parse_lab_sections` tolerates `$schema`, and `models/jsonschema.py` emits the `_`-key/`$schema` allowances the runtime already has. Drift between the settings template and `SettingsModel` is caught by two unit tests, not discipline.

**Tech Stack:** Python 3.10+, typer, pydantic v2, tomli, pytest (+`jsonschema` lib in tests).

## Global Constraints

- Execute on a worktree (superpowers:using-git-worktrees). Fresh worktree setup: `uv sync`, and `npm ci` in `web/` (make coverage self-heals the web dist but needs node_modules).
- Commits: conventional prefix, `Assisted-by: Claude (Fable 5)` trailer (worktree ⇒ self-commit OK).
- NEVER `from __future__ import annotations` (breaks Sphinx nitpicky `-W`).
- `@options` is *pydantic's* dataclass re-export (`from otto import options`), never stdlib `@dataclass`.
- Prefer lists over tuples in APIs.
- `ty` runs only at `nox -s typecheck` — budget a typecheck round after each src/ edit wave.
- Per-task test runs are scoped pytest; the whole-branch gate is `make coverage` (there is no `make test`). Triage failures with `scripts/junit_failures.py` (`make coverage | tail` eats make's exit code).
- Substring asserts on rich table output need pinned COLUMNS — `tests/unit/cli/test_init_validate.py` already has an autouse `COLUMNS=300` fixture; new doctor-output tests belong there.
- Docs gate is a CLEAN `make docs` rebuild (incremental `-W` misses docstring `:doc:` refs). Termynal help blocks regenerate from the live CLI during the docs build — no committed artifacts.
- settings.toml comment convention (load-bearing for the drift test): commented-out TOML is `#key = value` / `#[section]` (no space after `#`); prose is `# text`; the `#:schema` directive is excluded by its `#:` prefix. All commented-out *top-level* keys must appear before the first `[section]` header (TOML scoping).

---

### Task 1: Extract templates into `init_templates.py`

Pure mechanical move so later tasks edit templates in a focused file.

**Files:**
- Create: `src/otto/cli/init_templates.py`
- Modify: `src/otto/cli/init.py` (delete lines 19–206, the seven template constants; add import)
- Test: existing `tests/unit/cli/test_init_scaffold.py` (unchanged, proves the move)

**Interfaces:**
- Produces: `otto.cli.init_templates` exporting `SETTINGS_TEMPLATE`, `EXAMPLE_HOST_ENTRY`, `LAB_JSON_TEMPLATE`, `LAB_README_TEMPLATE`, `TEST_EXAMPLE_TEMPLATE`, `CONFTEST_TEMPLATE`, `INSTRUCTIONS_TEMPLATE` — same names/values as today's `init.py:19-206`.

- [ ] **Step 1: Create the module**

Create `src/otto/cli/init_templates.py` with this docstring, then paste `SETTINGS_TEMPLATE`, `EXAMPLE_HOST_ENTRY`, `LAB_JSON_TEMPLATE`, `LAB_README_TEMPLATE`, `TEST_EXAMPLE_TEMPLATE`, `CONFTEST_TEMPLATE`, `INSTRUCTIONS_TEMPLATE` **verbatim** from `src/otto/cli/init.py:19-206` (including the `from typing import Any` needs — the file needs `from typing import Any` for `LAB_JSON_TEMPLATE`'s annotation):

```python
"""Templates ``otto init`` scaffolds into a new repo.

String constants only — all scaffolding logic stays in :mod:`otto.cli.init`.
``SETTINGS_TEMPLATE`` follows the sshd_config comment convention: prose
comments are ``# text`` (hash-space), commented-out TOML is ``#key = value``
(no space), and the ``#:schema`` editor directive is neither. The drift tests
in ``tests/unit/cli/test_init_templates.py`` rely on that convention to
uncomment and validate the whole surface against ``SettingsModel``.
"""

from typing import Any
```

- [ ] **Step 2: Rewire init.py**

In `src/otto/cli/init.py`, delete the moved constants and add after the existing imports:

```python
from .init_templates import (
    CONFTEST_TEMPLATE,
    EXAMPLE_HOST_ENTRY,
    INSTRUCTIONS_TEMPLATE,
    LAB_JSON_TEMPLATE,
    LAB_README_TEMPLATE,
    SETTINGS_TEMPLATE,
    TEST_EXAMPLE_TEMPLATE,
)
```

(`EXAMPLE_HOST_ENTRY` is only referenced by `LAB_JSON_TEMPLATE`; if nothing in `init.py` uses it after the move, don't import it.)

- [ ] **Step 3: Run the existing init tests**

Run: `uv run pytest tests/unit/cli/test_init_scaffold.py tests/unit/cli/test_init_prompts.py tests/unit/cli/test_init_validate.py -q`
Expected: all PASS (behavior unchanged).

- [ ] **Step 4: Commit**

```bash
git add src/otto/cli/init.py src/otto/cli/init_templates.py
git commit -m "refactor(cli): extract otto init templates into init_templates.py

Assisted-by: Claude (Fable 5)"
```

---

### Task 2: Full-surface settings.toml template + drift tests

**Files:**
- Modify: `src/otto/cli/init_templates.py` (replace `SETTINGS_TEMPLATE`)
- Create: `tests/unit/cli/test_init_templates.py`

**Interfaces:**
- Consumes: `SETTINGS_TEMPLATE` with format fields `{name}`, `{version}`, `{init_module}` (unchanged signature — `_scaffold_settings` keeps working).
- Produces: template whose commented-out lines, when uncommented, form a `SettingsModel`-valid document; test helper `_uncommented()` used by both drift tests.

- [ ] **Step 1: Write the failing drift tests**

Create `tests/unit/cli/test_init_templates.py`:

```python
"""Drift guards: the scaffolded settings.toml must cover SettingsModel exactly."""

import re

import tomli

from otto.cli.init_templates import SETTINGS_TEMPLATE
from otto.models.settings import (
    CoverageSettingsSpec,
    DockerSettingsSpec,
    LabConfigSpec,
    LoggingConfigSpec,
    ReservationConfigSpec,
    SettingsModel,
)

# Commented-out TOML is "#key" / "#[table]" (no space after #); prose is "# ".
# The "#:schema" editor directive is excluded by its ":".
_COMMENTED = re.compile(r"^#(?![ :])")

# Intentionally omitted from the template: legacy passthrough consumed by nobody.
_OMITTED_TOP_LEVEL = {"lab_data_type"}
# Per-section omissions: free-form sub-tables pointed at docs instead.
_SECTION_SPECS = {
    "lab": (LabConfigSpec, set()),
    "logging": (LoggingConfigSpec, set()),
    "reservations": (ReservationConfigSpec, set()),
    "coverage": (CoverageSettingsSpec, {"embedded"}),
    "docker": (DockerSettingsSpec, set()),
}


def _uncommented() -> dict:
    rendered = SETTINGS_TEMPLATE.format(
        name="widget", version="0.1.0", init_module="widget_instructions"
    )
    text = "\n".join(_COMMENTED.sub("", line) for line in rendered.splitlines())
    return tomli.loads(text)


def test_uncommented_template_is_settings_model_valid() -> None:
    model = SettingsModel.model_validate(_uncommented())
    assert model.name == "widget"
    # spot-check each section survived into the model, not just parsed
    assert model.lab.backend == "json"
    assert model.reservations.backend == "none"
    assert "nightly" in model.coverage.tiers
    assert model.docker.images[0].name == "widget-test"
    assert model.host_preferences[".*"]["term"] == ["ssh", "telnet"]
    assert model.os_profiles["my-os"].base == "unix"


def test_template_mentions_every_top_level_settings_field() -> None:
    data = _uncommented()
    model_fields = set(SettingsModel.model_fields)
    assert model_fields - set(data) == _OMITTED_TOP_LEVEL
    assert set(data) <= model_fields  # no keys the model doesn't know


def test_template_mentions_every_fixed_section_field() -> None:
    data = _uncommented()
    for section, (spec, omitted) in _SECTION_SPECS.items():
        assert set(spec.model_fields) - set(data[section]) == omitted, section
        assert set(data[section]) <= set(spec.model_fields), section
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/cli/test_init_templates.py -q`
Expected: FAIL — today's template has no commented-out sections, so `data["lab"]` etc. raise `KeyError` / the top-level diff is larger than the allowlist.

- [ ] **Step 3: Replace `SETTINGS_TEMPLATE`**

In `src/otto/cli/init_templates.py`:

```python
SETTINGS_TEMPLATE = """\
#:schema ./schemas/settings.schema.json
# {name} — otto repo settings. Reference: docs/guide/setup/repo-setup.md.
# Lines starting "#key" or "#[section]" are optional settings: remove the
# leading "#" to enable them. Your editor autocompletes every field from the
# schema line above (regenerate with `otto schema export`).

name = "{name}"
version = "{version}"

# Where otto looks for things, relative to this repo's root (${{sut_dir}}).
# These conventional paths are pre-wired so `otto init --lab` etc. can add
# areas later without editing this file.
labs = ["${{sut_dir}}/lab_data"]   # directories searched for lab.json
tests = ["${{sut_dir}}/tests"]     # defines where test discovery happens
libs = ["${{sut_dir}}/pylib"]      # added to sys.path at startup
init = ["{init_module}"]           # modules imported at startup (register instructions)

# Restrict --lab/OTTO_LAB to an allowlist (default: any lab found in labs dirs).
#valid_labs = ["example_lab"]

# --- [lab] — host-source backend selection (default: built-in "json") --------
# Backend-specific settings live in [lab.<backend>]; see docs/guide/setup/host-database.md.
#[lab]
#backend = "json"

# --- [logging] — extra top-level logger prefixes routed into otto's sinks ----
#[logging]
#capture = ["my_library"]

# --- [host_preferences."<selector>"] — scoped term/transfer preferences ------
# The quoted selector is a regex fullmatched against host ids; ".*" = all.
# Ordered lists are intersected with each host's own menu at build time.
#[host_preferences.".*"]
#term = ["ssh", "telnet"]
#transfer = ["scp", "sftp"]
#impairer = ["tc"]
# Six per-protocol option tables may also sit under a selector: ssh_options,
# telnet_options, sftp_options, scp_options, ftp_options, nc_options. Their
# fields are not listed here — the schema autocompletes them. Example:
#[host_preferences.".*".ssh_options]
#port = 22

# --- [os_profiles.<name>] — named OS-profile bundles for lab.json hosts ------
# `base` is the host class the profile builds on; any host field may follow
# as a default applied to every host that selects this profile.
#[os_profiles.my-os]
#base = "unix"
#valid_terms = ["ssh"]

# --- [reservations] — reservation gate; see docs/guide/reservations.md -------
# Backend-specific settings live in [reservations.<backend>].
#[reservations]
#backend = "none"
#url = ""

# --- [coverage] — coverage tiers + remote gcov collection --------------------
# Embedded build settings live in [coverage.embedded] (see the coverage docs).
#[coverage]
#hosts = "example-device"
#gcda_remote_dir = "/tmp/gcda"
#[coverage.tiers.nightly]
#kind = "e2e"
#precedence = 10
#color = "#22c55e"
#harvest_dirs = ["cov/nightly"]
#max_age = "180d"
#[coverage.exclusions]
#markers = ["GCOV_EXCL"]

# --- [docker] — image builds + compose stacks --------------------------------
#[docker]
#registry_url = "docker.io"
#[[docker.images]]
#name = "{name}-test"
#dockerfile = "docker/Dockerfile"
#context = "."
#target = "test"
#[docker.images.build_args]
#PORT = 8080
#[[docker.composes]]
#path = "docker/compose.yaml"
#default_host = "{name}-svc"
#services = ["{name}-svc"]
"""
```

Notes for the implementer:
- `${{sut_dir}}` is `.format()` escaping for a literal `${sut_dir}` — keep it.
- If `test_uncommented_template_is_settings_model_valid` rejects `color = "#22c55e"`, check `otto.coverage.colors.validate_color` and substitute a value it accepts (the test failure message will show the constraint) — do NOT loosen the validator.
- `[docker.images.build_args]` after `[[docker.images]]` is valid TOML (sub-table of the latest array element).

- [ ] **Step 4: Run the drift tests + existing scaffold tests**

Run: `uv run pytest tests/unit/cli/test_init_templates.py tests/unit/cli/test_init_scaffold.py -q`
Expected: PASS (scaffold test `test_settings_scaffold_parses_via_settings_model` still passes — active lines unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/otto/cli/init_templates.py tests/unit/cli/test_init_templates.py
git commit -m "feat(cli): otto init settings.toml template covers the full settings surface

Every optional field commented out sshd_config-style; two drift tests pin
the template to SettingsModel (uncomment-and-validate + completeness diff).

Assisted-by: Claude (Fable 5)"
```

---

### Task 3: Sanitized module names + shared RepoOptions module + sample rewrites

**Files:**
- Modify: `src/otto/cli/init.py` (InitConfig, `_ensure_options_module`, `_scaffold_settings`, `_scaffold_tests`, `_scaffold_instructions`, epilogue)
- Modify: `src/otto/cli/init_templates.py` (`OPTIONS_TEMPLATE` new; `TEST_EXAMPLE_TEMPLATE`, `INSTRUCTIONS_TEMPLATE` rewritten)
- Test: `tests/unit/cli/test_init_scaffold.py`

**Interfaces:**
- Consumes: `InitConfig(name, version)` frozen dataclass; `Area.scaffold(root, cfg) -> list[Path]`.
- Produces: `InitConfig.module_base: str` property (sanitized identifier base); `_ensure_options_module(root: Path, cfg: InitConfig) -> list[Path]` (returns `[path]` when created, `[]` when pre-existing); templates `OPTIONS_TEMPLATE.format(name=...)`, `TEST_EXAMPLE_TEMPLATE.format(options_module=...)`, `INSTRUCTIONS_TEMPLATE.format(name=..., options_module=...)`. Scaffolded module names: `pylib/<module_base>_options.py`, `pylib/<module_base>_instructions/`. Task 10's e2e relies on: suite flag `--message` (default `hello from <name>`), suite flag `--greeting`, instruction `smoke` flags `--message`/`--loud`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cli/test_init_scaffold.py`:

```python
def test_tests_scaffold_creates_shared_options_module(tmp_path: Path) -> None:
    created = BY_NAME["tests"].scaffold(tmp_path, CFG)
    options_mod = tmp_path / "pylib" / "widget_options.py"
    assert options_mod in created
    src = options_mod.read_text()
    assert "class RepoOptions" in src
    assert "hello from widget" in src
    suite_src = (tmp_path / "tests" / "test_example.py").read_text()
    assert "from widget_options import RepoOptions" in suite_src
    assert "class _Options(RepoOptions)" in suite_src


def test_instructions_scaffold_creates_shared_options_module(tmp_path: Path) -> None:
    created = BY_NAME["instructions"].scaffold(tmp_path, CFG)
    assert tmp_path / "pylib" / "widget_options.py" in created
    src = (tmp_path / "pylib" / "widget_instructions" / "__init__.py").read_text()
    assert "from widget_options import RepoOptions" in src
    assert "@instruction(options=_Options)" in src


def test_options_module_scaffold_is_idempotent_either_order(tmp_path: Path) -> None:
    first = BY_NAME["tests"].scaffold(tmp_path, CFG)
    options_mod = tmp_path / "pylib" / "widget_options.py"
    assert options_mod in first
    marker = "# user edited\n" + options_mod.read_text()
    options_mod.write_text(marker)
    second = BY_NAME["instructions"].scaffold(tmp_path, CFG)
    assert options_mod not in second  # not re-created...
    assert options_mod.read_text() == marker  # ...and never overwritten
    # reverse order in a fresh tree
    other = tmp_path / "other"
    other.mkdir()
    assert other / "pylib" / "widget_options.py" in BY_NAME["instructions"].scaffold(other, CFG)
    assert other / "pylib" / "widget_options.py" not in BY_NAME["tests"].scaffold(other, CFG)


def test_module_names_are_sanitized_identifiers(tmp_path: Path) -> None:
    cfg = InitConfig(name="my-repo 2.0", version="0.1.0")
    assert cfg.module_base == "my_repo_2_0"
    BY_NAME["settings"].scaffold(tmp_path, cfg)
    BY_NAME["instructions"].scaffold(tmp_path, cfg)
    import tomli

    data = tomli.loads((tmp_path / ".otto" / "settings.toml").read_text())
    assert data["name"] == "my-repo 2.0"  # display name keeps the raw value
    assert data["init"] == ["my_repo_2_0_instructions"]
    assert (tmp_path / "pylib" / "my_repo_2_0_instructions" / "__init__.py").exists()
    assert (tmp_path / "pylib" / "my_repo_2_0_options.py").exists()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/cli/test_init_scaffold.py -q`
Expected: new tests FAIL (`module_base` attribute missing; no options module created).

- [ ] **Step 3: Implement the templates**

In `src/otto/cli/init_templates.py`, add `OPTIONS_TEMPLATE` and **replace** `TEST_EXAMPLE_TEMPLATE` and `INSTRUCTIONS_TEMPLATE`:

```python
OPTIONS_TEMPLATE = '''\
"""Repo-wide options shared by every suite and instruction.

``@options`` (``from otto import options``) is pydantic's dataclass
decorator: fields declared here become validated CLI flags on every
``otto test`` suite and every ``otto run`` instruction whose options class
inherits ``RepoOptions``. See docs/guide/options.md.
"""

from typing import Annotated

import typer

from otto import options


@options
class RepoOptions:
    """Inherit me from a suite's inner Options or an @instruction options class."""

    message: Annotated[
        str, typer.Option(help="Message the sample suite and instruction log.")
    ] = "hello from {name}"
'''

TEST_EXAMPLE_TEMPLATE = '''\
"""Example otto test suite — runs hostless so it passes out of the box."""

from typing import Annotated

import typer

from otto import options
from otto.suite import OttoSuite

from {options_module} import RepoOptions


@options
class _Options(RepoOptions):
    """This suite's options: the repo-wide flags plus its own ``--greeting``."""

    greeting: Annotated[str, typer.Option(help="Greeting the example test logs.")] = "hello"


class TestExample(OttoSuite[_Options]):
    """A minimal suite: `otto test TestExample` (auto-registered by its Test* name)."""

    Options = _Options

    async def test_logs_message(self, suite_options: _Options, repo_marker: str) -> None:
        self.logger.info("%s (%s)", suite_options.message, suite_options.greeting)
        assert repo_marker == "from-conftest"


def test_example_function() -> None:
    """Plain pytest functions run too: `otto test --tests test_example_function`."""
    assert True
'''

INSTRUCTIONS_TEMPLATE = '''\
"""{name} instructions — functions exposed as `otto run` subcommands."""

import logging
from typing import Annotated

import typer

from otto import options
from otto.cli.run import instruction

from {options_module} import RepoOptions

logger = logging.getLogger(__name__)


@options
class _Options(RepoOptions):
    """This instruction's options: the repo-wide flags plus its own ``--loud``."""

    loud: Annotated[bool, typer.Option(help="Uppercase the message.")] = False


@instruction(options=_Options)
async def smoke(opts: _Options) -> None:
    """Log the repo-wide message — replace with your first real instruction."""
    logger.info(opts.message.upper() if opts.loud else opts.message)
'''
```

(`CONFTEST_TEMPLATE` stays exactly as-is — the `repo_marker` fixture remains consumed.)

- [ ] **Step 4: Implement the init.py changes**

In `src/otto/cli/init.py`:

a. Add `import re` to the top-level imports (it is currently function-local in the epilogue).

b. Extend `InitConfig`:

```python
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
```

c. Add the shared helper (near the scaffold functions), and import `OPTIONS_TEMPLATE`:

```python
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
```

d. Rewire the three scaffolds:

```python
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


def _scaffold_tests(root: Path, cfg: InitConfig) -> list[Path]:
    created = _ensure_options_module(root, cfg)
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    example = tests_dir / "test_example.py"
    example.write_text(
        TEST_EXAMPLE_TEMPLATE.format(options_module=f"{cfg.module_base}_options")
    )
    conftest = tests_dir / "conftest.py"
    conftest.write_text(CONFTEST_TEMPLATE)
    return [*created, example, conftest]


def _scaffold_instructions(root: Path, cfg: InitConfig) -> list[Path]:
    created = _ensure_options_module(root, cfg)
    module_dir = root / "pylib" / f"{cfg.module_base}_instructions"
    module_dir.mkdir(parents=True, exist_ok=True)
    init_file = module_dir / "__init__.py"
    init_file.write_text(
        INSTRUCTIONS_TEMPLATE.format(
            name=cfg.name, options_module=f"{cfg.module_base}_options"
        )
    )
    return [*created, init_file]
```

(The `# noqa: ARG001` on `_scaffold_tests` goes away — `cfg` is now used.)

e. Epilogue: after `steps.append("otto test --tests test_example_function")` add:

```python
    steps.append("otto run smoke")
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/cli/test_init_scaffold.py tests/unit/cli/test_init_prompts.py tests/unit/cli/test_init_templates.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/otto/cli/init.py src/otto/cli/init_templates.py tests/unit/cli/test_init_scaffold.py
git commit -m "feat(cli): otto init scaffolds shared RepoOptions plumbing into suite + instruction

pylib/<name>_options.py created idempotently by either area; samples log one
statement and add one field each so --message rides both otto test and
otto run; module names sanitized to importable identifiers.

Assisted-by: Claude (Fable 5)"
```

---

### Task 4: `$schema` tolerance in the lab loader + lab.json self-wiring

**Files:**
- Modify: `src/otto/labs/json_repository.py:60-65`
- Modify: `src/otto/cli/init_templates.py` (`LAB_JSON_TEMPLATE`)
- Test: `tests/unit/cli/test_init_validate.py`, `tests/unit/cli/test_init_scaffold.py`

**Interfaces:**
- Produces: `parse_lab_sections` accepts a top-level `"$schema"` key (still rejects other unknown keys); scaffolded lab.json carries `"$schema": "../.otto/schemas/lab.schema.json"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cli/test_init_validate.py`:

```python
def test_parse_lab_sections_tolerates_dollar_schema() -> None:
    from otto.labs.errors import LabRepositoryError
    from otto.labs.json_repository import parse_lab_sections

    data = {"$schema": "../.otto/schemas/lab.schema.json", "hosts": [], "links": []}
    assert parse_lab_sections(data, "lab.json")["hosts"] == []
    with pytest.raises(LabRepositoryError, match="unknown section"):
        parse_lab_sections({"routes": []}, "lab.json")
```

(Add `import pytest` to the file's imports if not present.) Append to `tests/unit/cli/test_init_scaffold.py::test_lab_scaffold_passes_hostspec_ingest`, after the existing asserts:

```python
    assert data["$schema"] == "../.otto/schemas/lab.schema.json"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/cli/test_init_validate.py -q tests/unit/cli/test_init_scaffold.py -q`
Expected: FAIL — `parse_lab_sections` raises on `$schema`; template has no `$schema` key.

- [ ] **Step 3: Implement**

`src/otto/labs/json_repository.py` — replace the unknown-key line (currently line 60) and touch the docstring's comment-space sentence:

```python
    # `$schema` is the standard editor-wiring key (VS Code / jsonls) — treated
    # as comment space alongside `_`-prefixed keys.
    unknown = {
        k
        for k in data
        if not (isinstance(k, str) and (k.startswith("_") or k == "$schema"))
    } - _LAB_SECTIONS
```

`src/otto/cli/init_templates.py` — put `$schema` first in `LAB_JSON_TEMPLATE`:

```python
LAB_JSON_TEMPLATE: dict[str, Any] = {
    "$schema": "../.otto/schemas/lab.schema.json",
    "_comment": (
        "otto lab database: 'hosts' lists every lab host; 'links' declares "
        "data-plane routes between them (see docs/guide/setup/lab-config.md). "
        "Keys starting with _ are comments; $schema wires editor autocomplete."
    ),
    "hosts": [EXAMPLE_HOST_ENTRY],
    "links": [],
}
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/cli/test_init_validate.py tests/unit/cli/test_init_scaffold.py tests/unit/labs -q`
Expected: PASS (including the existing labs loader suite — the tolerance must not regress unknown-section rejection).

- [ ] **Step 5: Commit**

```bash
git add src/otto/labs/json_repository.py src/otto/cli/init_templates.py tests/unit/cli/test_init_validate.py tests/unit/cli/test_init_scaffold.py
git commit -m "feat(labs): tolerate a top-level \$schema key in lab.json; scaffold wires it

Assisted-by: Claude (Fable 5)"
```

---

### Task 5: Generated schemas allow what the runtime tolerates

**Files:**
- Modify: `src/otto/models/jsonschema.py`
- Test: `tests/unit/models/test_jsonschema_validation.py`

**Interfaces:**
- Consumes: `build_schemas()` → `{stem: doc}`; `HostSpec._strip_comment_keys` / `LinkSpec._strip_comment_keys` (runtime `_`-key strip at host and link entry level; lab top level already has `patternProperties {"^_": {}}` — `jsonschema.py:261`).
- Produces: `_allow_comment_keys(schema: dict) -> None` helper; lab schema top-level `properties` gains `"$schema": {"type": "string"}`; host-spec docs (standalone + lab `$defs`) and link docs (standalone + lab-embedded) carry `patternProperties {"^_": {}}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/models/test_jsonschema_validation.py` (the module already builds `lab_validator` from `build_schemas()["lab"]`):

```python
def test_lab_schema_accepts_scaffolded_lab_json(lab_validator, tmp_path):
    """The very file `otto init` writes must validate against the emitted schema."""
    from otto.cli.init import AREAS, InitConfig

    lab_area = next(a for a in AREAS if a.name == "lab")
    lab_area.scaffold(tmp_path, InitConfig(name="widget", version="0.1.0"))
    doc = json.loads((tmp_path / "lab_data" / "lab.json").read_text())
    lab_validator.validate(doc)  # $schema + top-level/_ and host-level _comment


def test_lab_schema_accepts_comment_keys_in_host_and_link(lab_validator):
    doc = {
        "$schema": "../.otto/schemas/lab.schema.json",
        "hosts": [{**_VALID_HOST, "_note": "runtime strips me"}],
        "links": [{**_VALID_LINK, "_note": "and me"}],
    }
    lab_validator.validate(doc)


def test_lab_schema_still_rejects_unknown_top_level_key(lab_validator):
    from jsonschema.exceptions import ValidationError

    doc = {"hosts": [], "links": [], "routes": []}
    with pytest.raises(ValidationError):
        lab_validator.validate(doc)


def test_standalone_host_and_link_schemas_accept_comment_keys():
    from jsonschema import Draft202012Validator

    docs = build_schemas()
    Draft202012Validator(docs["unix-host"]).validate({**_VALID_HOST, "_note": "x"})
    Draft202012Validator(docs["link"]).validate({**_VALID_LINK, "_note": "x"})
```

`_VALID_LINK` in that module uses hosts `carrot`/`tomato` — reference validation happens at load, not in the schema, so it validates standalone.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/models/test_jsonschema_validation.py -q`
Expected: new tests FAIL with `additionalProperties` violations (`_comment` in host entry, `$schema` at top level).

- [ ] **Step 3: Implement in `src/otto/models/jsonschema.py`**

a. Add the helper after `_decorate`:

```python
def _allow_comment_keys(schema: dict[str, Any]) -> None:
    """Allow ``_``-prefixed keys on an entry schema, mirroring the runtime strip.

    ``HostSpec._strip_comment_keys`` / ``LinkSpec._strip_comment_keys`` drop
    ``_``-prefixed keys before validation (the JSON comment idiom), so the
    emitted schema must not squiggle them — `otto init` itself scaffolds a
    host-level ``_comment``.
    """
    schema.setdefault("patternProperties", {})["^_"] = {}
```

b. In `_hosts_array_schema`, inside the existing `for s in distinct:` loop that post-processes `top["$defs"][key]`, add after `_inject_interface_shorthand(...)`:

```python
            _allow_comment_keys(top["$defs"][key])
```

c. In `_lab_schema`, allow the link entries and the editor-wiring key:

```python
def _lab_schema(hosts_array: dict[str, Any]) -> dict[str, Any]:
    """Build the ``lab.json`` object schema: ``hosts``/``links`` sections + ``_`` comments."""
    link_doc = LinkSpec.model_json_schema(ref_template="#/$defs/{model}")
    _allow_comment_keys(link_doc)
    defs = {**hosts_array.pop("$defs", {}), **link_doc.pop("$defs", {})}
    return {
        "type": "object",
        "properties": {
            "$schema": {"type": "string"},
            "hosts": hosts_array,
            "links": {"type": "array", "items": link_doc},
        },
        "patternProperties": {"^_": {}},
        "additionalProperties": False,
        "$defs": defs,
    }
```

d. In `build_schemas`, decorate the standalone per-host docs and the standalone link doc:

```python
    for spec in distinct:
        stem = _stem(spec)
        doc = spec.model_json_schema()
        _inject_selector_enums(doc, spec)
        _inject_interface_shorthand(doc)
        _allow_comment_keys(doc)
        docs[stem] = _decorate(doc, stem, f"otto {stem}")
```

and for the link:

```python
    link_doc = LinkSpec.model_json_schema()
    _allow_comment_keys(link_doc)
    docs["link"] = _decorate(link_doc, "link", "otto link")
```

- [ ] **Step 4: Run the schema test modules**

Run: `uv run pytest tests/unit/models/test_jsonschema_validation.py tests/unit/models/test_jsonschema.py -q`
Expected: PASS (existing rejection tests must stay green — `additionalProperties: false` still rejects non-`_`, non-`$schema` keys).

- [ ] **Step 5: Commit**

```bash
git add src/otto/models/jsonschema.py tests/unit/models/test_jsonschema_validation.py
git commit -m "fix(models): emitted schemas allow the _-comment and \$schema keys the runtime accepts

Assisted-by: Claude (Fable 5)"
```

---

### Task 6: The schemas area (detect + scaffold) and `--schemas` refresh flag

**Files:**
- Modify: `src/otto/cli/init.py`
- Test: `tests/unit/cli/test_init_scaffold.py`, `tests/unit/cli/test_init_prompts.py`

**Interfaces:**
- Consumes: `otto.models.jsonschema.build_schemas()` → `{stem: doc}`; `Area` dataclass.
- Produces: `_schemas_dir(root) -> Path` (= `root/".otto"/"schemas"`), `_detect_schemas`, `_scaffold_schemas` (validate arrives in Task 7 — wire `_validate_schemas` as a stub returning `[]` so the Area tuple is complete); `AREAS` order `["settings", "schemas", "lab", "tests", "instructions"]`; `--schemas` flag that also REFRESHES a detected area (generated artifacts are otto-owned; this is what makes the doctor's "re-run `otto init --schemas`" remedy true — `--all` and interactive keep missing-only semantics).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/cli/test_init_scaffold.py`, **replace** `test_area_order_is_settings_first`:

```python
def test_area_order_is_settings_first() -> None:
    assert [a.name for a in AREAS] == ["settings", "schemas", "lab", "tests", "instructions"]
```

Append:

```python
def test_schemas_scaffold_writes_schema_files(tmp_path: Path) -> None:
    created = BY_NAME["schemas"].scaffold(tmp_path, CFG)
    out = tmp_path / ".otto" / "schemas"
    for stem in ("settings", "lab", "link", "reservations"):
        assert out / f"{stem}.schema.json" in created
    data = json.loads((out / "lab.schema.json").read_text())
    assert data["title"] == "otto lab.json"
```

In `tests/unit/cli/test_init_prompts.py`:

- `test_interactive_prompts_per_missing_area`: change the input to cover five areas (settings=y, schemas=y, lab=y, tests=n, instructions=n):

```python
    result = runner.invoke(
        _app(), ["--path", str(tmp_path)], input="widget\n0.1.0\ny\ny\ny\nn\nn\n"
    )
```

(keep its existing asserts; add `assert (tmp_path / ".otto" / "schemas" / "settings.schema.json").is_file()`)

- `test_all_flag_scaffolds_everything_without_prompts`: extend the artifact list with:

```python
        ".otto/schemas/settings.schema.json",
        ".otto/schemas/lab.schema.json",
        "pylib/widget_options.py",
```

- Append a refresh-semantics test:

```python
def test_schemas_flag_refreshes_stale_files(tmp_path: Path) -> None:
    result = runner.invoke(_app(), ["--all", "--name", "widget", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    lab_schema = tmp_path / ".otto" / "schemas" / "lab.schema.json"
    lab_schema.write_text("{}")  # simulate stale/tampered
    result = runner.invoke(_app(), ["--schemas", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(lab_schema.read_text()).get("title") == "otto lab.json"
```

(add `import json` to the prompts module imports.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/cli/test_init_scaffold.py tests/unit/cli/test_init_prompts.py -q`
Expected: FAIL — no schemas area, no `--schemas` flag.

- [ ] **Step 3: Implement in `src/otto/cli/init.py`**

a. Area functions (place near the other `_detect_*`/`_scaffold_*`):

```python
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
    return created


def _validate_schemas(root: Path) -> list[str]:  # noqa: ARG001 — staleness doctor lands in the next commit
    return []
```

b. Register (order matters — doctor table + scaffold order):

```python
AREAS: list[Area] = [
    Area("settings", _detect_settings, _validate_settings, _scaffold_settings),
    Area("schemas", _detect_schemas, _validate_schemas, _scaffold_schemas),
    Area("lab", _detect_lab, _validate_lab, _scaffold_lab),
    Area("tests", _detect_tests, _validate_tests, _scaffold_tests),
    Area("instructions", _detect_instructions, _validate_instructions, _scaffold_instructions),
]
```

c. `init_command` signature — add after the `all_areas` parameter:

```python
    schemas: Annotated[
        bool,
        typer.Option(
            "--schemas",
            help="Scaffold (or refresh, if present) the schemas area: .otto/schemas + editor wiring.",
        ),
    ] = False,
```

d. Body — extend `requested`, add refresh semantics:

```python
    requested = {"schemas": schemas, "lab": lab, "tests": tests, "instructions": instructions}
    explicit = any(requested.values())
    interactive = not (all_areas or explicit)

    missing = [a for a in AREAS if not a.detect(root)]
    missing_names = {a.name for a in missing}
    # Generated artifacts are otto-owned, so the explicit flag REFRESHES a
    # detected schemas area (the doctor's "re-run `otto init --schemas`"
    # remedy). --all / interactive keep missing-only semantics.
    refresh_names: set[str] = {"schemas"} if schemas else set()
```

and in the scaffold loop change the skip line:

```python
        if area.name not in missing_names and area.name not in refresh_names:
            continue
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/cli/test_init_scaffold.py tests/unit/cli/test_init_prompts.py tests/unit/cli/test_init_validate.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/otto/cli/init.py tests/unit/cli/test_init_scaffold.py tests/unit/cli/test_init_prompts.py
git commit -m "feat(cli): otto init grows a schemas area; --schemas refreshes generated files

Assisted-by: Claude (Fable 5)"
```

---

### Task 7: Schemas staleness doctor

**Files:**
- Modify: `src/otto/cli/init.py` (replace the `_validate_schemas` stub)
- Test: `tests/unit/cli/test_init_validate.py`

**Interfaces:**
- Consumes: `_schemas_dir`, `build_schemas()`.
- Produces: `_validate_schemas(root) -> list[str]` — structural (parsed-JSON) comparison; problems for missing / stale / orphaned / unparsable `*.schema.json`, each naming both remedies.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cli/test_init_validate.py` (module already has the autouse COLUMNS fixture; `AREAS`/`InitConfig` import style: match the file's existing imports):

```python
def test_schemas_validate_green_after_scaffold_and_reformat(tmp_path: Path) -> None:
    by_name = {a.name: a for a in AREAS}
    by_name["schemas"].scaffold(tmp_path, InitConfig(name="widget", version="0.1.0"))
    assert by_name["schemas"].validate(tmp_path) == []
    # reformat-only change stays green: comparison is structural, not bytes
    lab = tmp_path / ".otto" / "schemas" / "lab.schema.json"
    lab.write_text(json.dumps(json.loads(lab.read_text()), indent=4, sort_keys=True))
    assert by_name["schemas"].validate(tmp_path) == []


def test_schemas_validate_flags_stale_missing_orphaned(tmp_path: Path) -> None:
    by_name = {a.name: a for a in AREAS}
    by_name["schemas"].scaffold(tmp_path, InitConfig(name="widget", version="0.1.0"))
    out = tmp_path / ".otto" / "schemas"
    stale = json.loads((out / "lab.schema.json").read_text())
    stale["title"] = "tampered"
    (out / "lab.schema.json").write_text(json.dumps(stale))
    (out / "settings.schema.json").unlink()
    (out / "ghost.schema.json").write_text("{}")
    problems = "\n".join(by_name["schemas"].validate(tmp_path))
    assert "lab.schema.json" in problems and "stale" in problems
    assert "settings.schema.json" in problems and "missing" in problems
    assert "ghost.schema.json" in problems and "orphaned" in problems
    assert "otto schema export" in problems  # remedy named
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/cli/test_init_validate.py -q`
Expected: the stale/missing/orphaned test FAILS (stub returns `[]`).

- [ ] **Step 3: Implement**

Replace the stub in `src/otto/cli/init.py`:

```python
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
        except (OSError, json.JSONDecodeError) as e:  # noqa: PERF203 — per-file resilience
            problems.append(f"{path}: {e} — {remedy}")
            continue
        if data != doc:
            problems.append(f"{path}: stale (differs from installed otto's models) — {remedy}")
    problems.extend(
        f"{path}: orphaned (installed otto emits no such schema) — {remedy}"
        for _, path in sorted(on_disk.items())
    )
    return problems
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/cli/test_init_validate.py tests/unit/cli/test_init_scaffold.py -q`
Expected: PASS (including `test_detect_flips_after_scaffold` — scaffold then detect stays true, validate green).

- [ ] **Step 5: Commit**

```bash
git add src/otto/cli/init.py tests/unit/cli/test_init_validate.py
git commit -m "feat(cli): otto init doctor flags stale/missing/orphaned generated schemas

Assisted-by: Claude (Fable 5)"
```

---

### Task 8: Editor wiring — `.vscode` only-if-absent

**Files:**
- Modify: `src/otto/cli/init_templates.py` (two new constants), `src/otto/cli/init.py` (`_scaffold_editor_wiring`, called from `_scaffold_schemas`)
- Test: `tests/unit/cli/test_init_scaffold.py`, `tests/unit/cli/test_init_prompts.py`

**Interfaces:**
- Produces: `VSCODE_SETTINGS_TEMPLATE`, `VSCODE_EXTENSIONS_TEMPLATE` constants; `_scaffold_editor_wiring(root: Path) -> list[Path]`; `_scaffold_schemas` return value now includes any wiring files created. NEVER doctor-validated (`_validate_schemas` untouched).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cli/test_init_scaffold.py`:

```python
def test_schemas_scaffold_writes_vscode_wiring_when_absent(tmp_path: Path) -> None:
    created = BY_NAME["schemas"].scaffold(tmp_path, CFG)
    settings = tmp_path / ".vscode" / "settings.json"
    extensions = tmp_path / ".vscode" / "extensions.json"
    assert settings in created and extensions in created
    wiring = json.loads(settings.read_text())
    urls = [entry["url"] for entry in wiring["json.schemas"]]
    assert "./.otto/schemas/lab.schema.json" in urls
    assert "./.otto/schemas/reservations.schema.json" in urls
    assert "evenBetterToml.schema.associations" in wiring
    assert "tamasfe.even-better-toml" in json.loads(extensions.read_text())["recommendations"]


def test_existing_vscode_settings_left_byte_for_byte_untouched(tmp_path: Path) -> None:
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    original = '// user file with comments\n{ "editor.rulers": [88] }\n'  # JSONC on purpose
    (vscode / "settings.json").write_text(original)
    created = BY_NAME["schemas"].scaffold(tmp_path, CFG)
    assert (vscode / "settings.json").read_text() == original
    assert vscode / "settings.json" not in created
    assert vscode / "extensions.json" in created  # independent only-if-absent check
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/cli/test_init_scaffold.py -q`
Expected: new tests FAIL (no `.vscode` output).

- [ ] **Step 3: Implement**

`src/otto/cli/init_templates.py` — add (raw string keeps the JSON regex escape literal):

```python
VSCODE_SETTINGS_TEMPLATE = r"""{
  "json.schemas": [
    { "fileMatch": ["**/lab.json"], "url": "./.otto/schemas/lab.schema.json" },
    { "fileMatch": ["**/reservations.json"], "url": "./.otto/schemas/reservations.schema.json" }
  ],
  "evenBetterToml.schema.associations": {
    ".*/settings\\.toml$": "./.otto/schemas/settings.schema.json"
  }
}
"""

VSCODE_EXTENSIONS_TEMPLATE = """\
{
  "recommendations": ["tamasfe.even-better-toml"]
}
"""
```

`src/otto/cli/init.py` — import the two constants, add the helper, and call it at the end of `_scaffold_schemas`:

```python
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
```

and in `_scaffold_schemas`, before `return created`:

```python
    created.extend(_scaffold_editor_wiring(root))
```

- [ ] **Step 4: Run the tests; update the `--all` artifact list**

Run: `uv run pytest tests/unit/cli/test_init_scaffold.py tests/unit/cli/test_init_prompts.py -q`
Then extend `test_all_flag_scaffolds_everything_without_prompts`'s artifact list with `".vscode/settings.json"` and `".vscode/extensions.json"`, and re-run.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/otto/cli/init.py src/otto/cli/init_templates.py tests/unit/cli/test_init_scaffold.py tests/unit/cli/test_init_prompts.py
git commit -m "feat(cli): otto init writes .vscode schema wiring, strictly only-if-absent

Assisted-by: Claude (Fable 5)"
```

---

### Task 9: `otto schema export` default `--out` → `.otto/schemas`

**Files:**
- Modify: `src/otto/cli/schema.py:39`
- Test: `tests/unit/cli/test_schema_export.py` (create if absent — check with `ls tests/unit/cli/ | grep -i schema` first; if a schema-export CLI test module already exists, add the test there instead)

**Interfaces:**
- Produces: `otto schema export` with no `--out` writes into `.otto/schemas/` under the CWD — the same files `_validate_schemas` checks, making the doctor's remedy true by default.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/cli/test_schema_export.py`:

```python
"""`otto schema export` writes to .otto/schemas by default (shared with otto init)."""

from pathlib import Path

from typer.testing import CliRunner

from otto.cli.schema import schema_app

runner = CliRunner()


def test_export_defaults_to_dot_otto_schemas(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(schema_app, ["export"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".otto" / "schemas" / "settings.schema.json").is_file()
    assert (tmp_path / ".otto" / "schemas" / "lab.schema.json").is_file()


def test_export_out_flag_still_honored(tmp_path: Path) -> None:
    out = tmp_path / "elsewhere"
    result = runner.invoke(schema_app, ["export", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "settings.schema.json").is_file()
```

- [ ] **Step 2: Run to verify the first test fails**

Run: `uv run pytest tests/unit/cli/test_schema_export.py -q`
Expected: first test FAILS (files land in `tmp_path/schemas/`), second PASSES.

- [ ] **Step 3: Implement**

`src/otto/cli/schema.py` — change the default and help:

```python
    out: Annotated[
        Path,
        typer.Option("--out", "-o", help="Directory to write *.schema.json into."),
    ] = Path(".otto/schemas"),
```

Also update the module docstring's usage line to `otto schema export [--out DIR] [--builtins-only]` context: mention the default (`--out` defaults to `.otto/schemas`, the same location `otto init` scaffolds and its doctor checks).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/cli/test_schema_export.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/otto/cli/schema.py tests/unit/cli/test_schema_export.py
git commit -m "feat(cli)!: otto schema export defaults --out to .otto/schemas

Shared location with the otto init schemas area/doctor. Pass --out schemas
to keep the old root-level directory.

Assisted-by: Claude (Fable 5)"
```

---

### Task 10: E2E — the plumbing proven end-to-end

**Files:**
- Modify: `tests/e2e/test_init_e2e.py`

**Interfaces:**
- Consumes: `run_otto(argv, xdir=..., sut_dirs=..., lab=...)` from `tests.e2e._otto_subprocess`; scaffolded flags from Task 3 (`--message`, `--greeting`, `--loud`).

- [ ] **Step 1: Extend the flow test**

In `test_init_then_full_verification_flow`, after the existing `otto run smoke` block, append:

```python
    # The repo-wide RepoOptions flag rides BOTH surfaces (the whole point of
    # the scaffolded plumbing): an unknown flag would exit 2 at parse time.
    r = run_otto(
        ["test", "TestExample", "--message", "hi-from-e2e", "--greeting", "yo"],
        xdir=xdir,
        sut_dirs=repo,
        lab="example_lab",
    )
    assert r.returncode == 0, r.stdout + r.stderr

    r = run_otto(
        ["run", "smoke", "--message", "hi-from-e2e"], xdir=xdir, sut_dirs=repo, lab="example_lab"
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "hi-from-e2e" in (r.stdout + r.stderr)

    r = run_otto(["run", "smoke", "--loud"], xdir=xdir, sut_dirs=repo, lab="example_lab")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "HELLO FROM WIDGET" in (r.stdout + r.stderr)

    # Second init run: everything detected, doctor green (exit 0) — including
    # schema freshness, since the same otto generated them moments ago.
    r = run_otto(["init", "--all", "--name", "widget", "--path", str(repo)], xdir=xdir)
    assert r.returncode == 0, r.stdout + r.stderr

    # The scaffolded lab.json carries $schema and still loads (tolerance is
    # in the runtime loader, proven by --list-hosts above; sanity-check disk).
    assert (repo / ".otto" / "schemas" / "lab.schema.json").is_file()
    assert (repo / ".vscode" / "settings.json").is_file()
```

- [ ] **Step 2: Run the e2e module**

Run: `uv run pytest tests/e2e/test_init_e2e.py -q`
Expected: PASS. (Hostless; runs the real `otto` binary via the subprocess harness.)

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_init_e2e.py
git commit -m "test(e2e): prove the scaffolded RepoOptions plumbing rides otto test and otto run

Assisted-by: Claude (Fable 5)"
```

---

### Task 11: Docs + full gate

**Files:**
- Modify: `docs/guide/setup/repo-setup.md`, `docs/guide/setup/editor-schemas.md`
- Verify: clean `make docs`, `nox -s typecheck`, `make coverage`

- [ ] **Step 1: repo-setup.md**

a. In the team-setup checklist item 1 (currently "scaffolds `.otto/settings.toml` … an example lab host, an example test suite, and an example instructions module in one step"), replace the sentence body with:

```markdown
1. **Run `otto init`** — scaffolds `.otto/settings.toml` (`name`, `version`, and
   the `labs` / `libs` / `tests` / `init` paths — this page, above) with every
   optional section present but commented out, the generated editor schemas
   (`.otto/schemas/` + `.vscode` wiring, see {doc}`editor-schemas`), an example
   lab host, and a shared `RepoOptions` class inherited by both an example test
   suite and an example instructions module — so `otto test TestExample` and
   `otto run smoke` share a `--message` flag out of the box. `otto init --all`
   scaffolds everything with no prompts; bare `otto init` asks per missing
   area; `otto init --schemas` also *refreshes* the generated schemas after an
   otto upgrade. See {doc}`../../getting-started` and {doc}`../cli-reference`.
```

b. In "## Defining shared options", after the sentence ending "`otto.examples.options` for a copyable example.", add:

```markdown
`otto init` scaffolds exactly this shape: a `pylib/<name>_options.py` with a
repo-wide `RepoOptions` that the example suite and instruction both inherit.
```

- [ ] **Step 2: editor-schemas.md**

a. Replace every `schemas/` path with `.otto/schemas/` (the `--out` example becomes `otto schema export` with a note that it defaults to `.otto/schemas`; VS Code/Neovim snippet URLs become `./.otto/schemas/...`; the taplo directive example becomes `#:schema ./schemas/settings.schema.json` **with a sentence noting it is relative to `.otto/settings.toml`**).

b. After the intro paragraph, add:

```markdown
New repos get all of this automatically: `otto init` exports the schemas to
`.otto/schemas/`, stamps the scaffolded `settings.toml` (`#:schema` directive)
and `lab.json` (`$schema` key) so single files self-wire, and writes
`.vscode/settings.json` + `.vscode/extensions.json` when they don't already
exist (an existing file is never modified — add the snippets below by hand).
The `otto init` doctor also flags stale schemas after an upgrade. The manual
steps below are for existing repos or other editors.
```

c. Replace the "Note on drift" paragraph with:

```markdown
The schemas reflect the otto version that generated them. There is no
committed copy in the otto repo — the `otto init` doctor flags a stale
`.otto/schemas/` after an upgrade; refresh with `otto init --schemas` or
`otto schema export`.
```

- [ ] **Step 3: Clean docs build**

Run: `make docs` (from a clean build dir — remove the sphinx build cache first if the Makefile doesn't already; check `make -n docs` for a clean target).
Expected: exit 0, no `-W` warnings. The termynal `help-init.html` regenerates from the live CLI during the build and picks up `--schemas`; the capture script itself scaffolds via `otto init --all`, so a template regression fails the docs build.

- [ ] **Step 4: Typecheck + full gate**

Run: `nox -s typecheck`
Expected: clean.
Run: `make coverage; echo "exit=$?"` and verify via `uv run python scripts/junit_failures.py` (never trust `| tail`).
Expected: suite green, coverage threshold met.

- [ ] **Step 5: Commit**

```bash
git add docs/guide/setup/repo-setup.md docs/guide/setup/editor-schemas.md
git commit -m "docs: otto init scaffolds full settings surface, schemas + editor wiring

Assisted-by: Claude (Fable 5)"
```
