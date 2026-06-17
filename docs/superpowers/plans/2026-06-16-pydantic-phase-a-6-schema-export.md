# Pydantic Phase A — Plan 6: JSON Schema export (`otto schema export`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a first-class `otto schema export` command that generates JSON Schema for the user-edited files (`hosts.json`, `settings.toml`, reservations JSON) from the live pydantic boundary models, so editor autocomplete + typo-catching always reflect the installed otto version and can never drift.

**Architecture:** A pure generation module `otto/models/jsonschema.py` builds the schema documents from the models and the host registry; a thin Typer subcommand `otto/cli/schema.py` writes them to `--out DIR`. The host array schema is assembled from the registry (not a single model) with `anyOf` + an `os_type` discriminator hint, using `pydantic.json_schema.models_json_schema` for a shared `$defs`. Custom-registered host specs are picked up automatically because `otto.configmodule` import already runs `apply_repo_settings()` (init-module load). Nothing is committed; correctness is guarded by validating the real `tests/lab_data` fixtures against the generated schema with the `jsonschema` library.

**Tech Stack:** pydantic v2 (`model_json_schema`, `pydantic.json_schema.models_json_schema`), Typer, `jsonschema` (new dev dep), pytest + Typer `CliRunner`, Sphinx/MyST docs.

**Spec:** `docs/superpowers/specs/2026-06-14-pydantic-phase-a-design.md` §6.

**Standing constraints (apply to every task):**
- **Stage only — do NOT commit.** Chris commits by hand (the prepare-commit-msg hook needs `/dev/tty`; agent commits mis-tag the AI-assist trailer). Where a task below says "Commit," **stage the files and STOP at a clean, staged checkpoint** — do not run `git commit`.
- **Never write test output inside the repo.** All file-writing tests use `tmp_path`, never `Path('.')` or anything under the repo tree.
- **Run `ruff --fix` on specific files**, never on a directory.
- Do not end any commit message with a `Co-Authored-By` trailer (the repo hook manages AI attribution).

---

## File Structure

- **Create** `src/otto/models/jsonschema.py` — pure generation: `build_schemas() -> dict[str, dict]` plus helpers. No Typer, no I/O. Imports the models + the registry accessor.
- **Create** `src/otto/cli/schema.py` — `schema_app = typer.Typer(...)` with the `export` command (writes files). The only I/O layer.
- **Modify** `src/otto/host/os_profile.py` — add public `registered_host_specs()` accessor.
- **Modify** `src/otto/cli/main.py` — register `'schema'` in `_SUBCOMMAND_MODULES`.
- **Modify** `pyproject.toml` — add `jsonschema` to the `dev` dependency group.
- **Modify** `.gitignore` — ignore the default `schemas/` output dir.
- **Modify** `Makefile` — add a `schema` convenience target.
- **Create** `docs/guide/editor-schemas.md` — user guide (command + VS Code + Neovim wiring); **Modify** `docs/guide/index.rst` toctree.
- **Create** tests: `tests/unit/host/test_registered_host_specs.py`, `tests/unit/models/test_jsonschema.py`, `tests/unit/models/test_jsonschema_validation.py`, `tests/unit/cli/test_schema_cli.py`.
- **Modify** `todo/registry_builtin_registration_symmetry.md` — note the new public accessor.

---

### Task 1: Public registry accessor + `jsonschema` dev dep

**Files:**
- Modify: `src/otto/host/os_profile.py`
- Modify: `pyproject.toml` (dev dependency group)
- Test: `tests/unit/host/test_registered_host_specs.py`

Context: `os_profile` keeps two private registries, `_HOST_CLASSES` (name→class) and `_HOST_SPECS` (name→spec class). Built-ins register `unix`, `embedded`, `zephyr` at module load; `_HOST_SPECS` currently maps `{'unix': UnixHostSpec, 'embedded': EmbeddedHostSpec, 'zephyr': EmbeddedHostSpec}`. The generator must read this without reaching into the private dict.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/host/test_registered_host_specs.py`:

```python
"""The public registry accessor backing JSON Schema export."""

from otto.host.os_profile import registered_host_specs
from otto.models import EmbeddedHostSpec, UnixHostSpec


def test_returns_builtin_name_to_spec_mapping():
    specs = registered_host_specs()
    assert specs['unix'] is UnixHostSpec
    assert specs['embedded'] is EmbeddedHostSpec
    assert specs['zephyr'] is EmbeddedHostSpec  # zephyr shares the embedded spec


def test_returns_a_copy_not_the_live_registry():
    specs = registered_host_specs()
    specs['bogus'] = UnixHostSpec  # mutate the returned dict
    assert 'bogus' not in registered_host_specs()  # registry unaffected
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/host/test_registered_host_specs.py -q`
Expected: FAIL — `ImportError: cannot import name 'registered_host_specs'`.

- [ ] **Step 3: Add the accessor**

In `src/otto/host/os_profile.py`, near `build_host_spec` (around line 198), add:

```python
def registered_host_specs() -> dict[str, type[HostSpec]]:
    """Return a copy of the ``os_type`` name → :class:`HostSpec` subclass map.

    The public read accessor over the private ``_HOST_SPECS`` registry. Names
    are many-to-one over spec classes (``embedded`` and ``zephyr`` both resolve
    to :class:`EmbeddedHostSpec`). Used by the JSON Schema exporter to build the
    ``hosts.json`` array schema from whatever host classes are registered,
    including custom ones loaded via init modules.
    """
    return dict(_HOST_SPECS)
```

Confirm `HostSpec` is importable in that module (it is — used in the existing `build_host_spec` signature / `_nearest_registered_spec`).

- [ ] **Step 4: Add the dev dependency**

In `pyproject.toml`, add `jsonschema` to the `[dependency-groups]` `dev` list (alphabetical position; pin loosely):

```toml
    "jsonschema>=4.0",
```

Then sync: `uv sync` (refreshes the lock + venv so `import jsonschema` works).

- [ ] **Step 5: Run the test to confirm it passes**

Run: `uv run pytest tests/unit/host/test_registered_host_specs.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Stage the checkpoint (do NOT commit)**

```bash
git add src/otto/host/os_profile.py pyproject.toml uv.lock tests/unit/host/test_registered_host_specs.py
```
Stop here — Chris commits.

---

### Task 2: Pure generation module `otto/models/jsonschema.py`

**Files:**
- Create: `src/otto/models/jsonschema.py`
- Test: `tests/unit/models/test_jsonschema.py`

Context: This module turns the models + registry into the schema documents. It produces, by default: one self-contained file per *distinct* host spec, the `hosts` array wrapper, `settings`, and `reservations`. File stems are derived from the spec class name (`UnixHostSpec → "unix-host"`, `EmbeddedHostSpec → "embedded-host"`) so the many-to-one `os_type → spec` collapse yields one file per class. The wrapper uses `anyOf` (NOT `oneOf`) + an `os_type` `discriminator` hint, built via `pydantic.json_schema.models_json_schema` for a shared deduped `$defs`. (Verified during planning: `oneOf` rejects real fixtures because minimal hosts that omit `os_type` validate against both specs; `anyOf` accepts them while still failing unknown keys.)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/models/test_jsonschema.py`:

```python
"""Unit tests for the JSON Schema generation module."""

import otto.host.os_profile as op
from otto.models import EmbeddedHostSpec, UnixHostSpec
from otto.models.host import HostSpec
from otto.models.jsonschema import build_schemas


def test_default_set_of_documents():
    docs = build_schemas()
    assert set(docs) >= {
        'unix-host', 'embedded-host', 'hosts', 'settings', 'reservations'
    }


def test_each_doc_is_a_self_describing_json_schema():
    for stem, doc in build_schemas().items():
        assert doc['$schema'] == 'https://json-schema.org/draft/2020-12/schema'
        assert '$id' in doc and stem in doc['$id']
        assert 'title' in doc


def test_host_specs_forbid_unknown_keys():
    docs = build_schemas()
    assert docs['unix-host']['additionalProperties'] is False
    assert docs['embedded-host']['additionalProperties'] is False


def test_hosts_wrapper_is_an_anyof_array_with_discriminator():
    hosts = build_schemas()['hosts']
    assert hosts['type'] == 'array'
    items = hosts['items']
    # anyOf, not oneOf — minimal hosts validate against >1 spec.
    assert 'anyOf' in items and 'oneOf' not in items
    assert {ref['$ref'] for ref in items['anyOf']} == {
        '#/$defs/UnixHostSpec', '#/$defs/EmbeddedHostSpec'
    }
    disc = items['discriminator']
    assert disc['propertyName'] == 'os_type'
    # Every registered os_type name is mapped to its spec's $def.
    assert disc['mapping'] == {
        'unix': '#/$defs/UnixHostSpec',
        'embedded': '#/$defs/EmbeddedHostSpec',
        'zephyr': '#/$defs/EmbeddedHostSpec',
    }
    assert 'UnixHostSpec' in hosts['$defs'] and 'EmbeddedHostSpec' in hosts['$defs']


def test_custom_registered_spec_appears(monkeypatch):
    # A custom host class + spec registered at runtime must flow into both its
    # own file and the hosts wrapper, without touching the real registry.
    class AcmeSpec(HostSpec):
        pass

    monkeypatch.setitem(op._HOST_SPECS, 'acme', AcmeSpec)
    docs = build_schemas()
    assert 'acme' in docs['hosts']['items']['discriminator']['mapping']
    assert docs['hosts']['items']['discriminator']['mapping']['acme'] == '#/$defs/AcmeSpec'
    assert 'acme' in docs  # its own per-spec file (stem from the class name)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/models/test_jsonschema.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.models.jsonschema'`.

- [ ] **Step 3: Write the generation module**

Create `src/otto/models/jsonschema.py`:

```python
"""Generate JSON Schema for the user-edited otto files from the boundary models.

The schemas are a *generated* product of the live models and the host registry,
never a committed artifact — so they cannot drift from the code. The CLI
(:mod:`otto.cli.schema`) writes them to disk; this module is pure (no I/O) so it
is trivially testable and importable.

Emitted documents (default):

- one self-contained file per *distinct* registered host spec
  (``unix-host``, ``embedded-host``, …),
- ``hosts`` — the array schema for the whole ``hosts.json`` file, assembled from
  the registry with ``anyOf`` + an ``os_type`` discriminator hint,
- ``settings`` — for ``settings.toml``,
- ``reservations`` — for the reservations JSON file.
"""

from __future__ import annotations

import re

from pydantic.json_schema import models_json_schema

from ..host.os_profile import registered_host_specs
from . import HostSpec, ReservationFile, SettingsModel

_SCHEMA_DIALECT = 'https://json-schema.org/draft/2020-12/schema'
_ID_BASE = 'https://otto-sh.readthedocs.io/schemas'


def _stem(spec_cls: type) -> str:
    """File stem for a host spec class: ``UnixHostSpec`` -> ``unix-host``."""
    name = re.sub(r'Spec$', '', spec_cls.__name__)          # UnixHost
    return re.sub(r'(?<!^)(?=[A-Z])', '-', name).lower()    # unix-host


def _decorate(doc: dict, stem: str, title: str) -> dict:
    """Prepend the dialect / id / title metadata to a generated schema doc."""
    return {
        '$schema': _SCHEMA_DIALECT,
        '$id': f'{_ID_BASE}/{stem}.schema.json',
        'title': title,
        **doc,
    }


def _host_array_schema(distinct: list[type[HostSpec]],
                       names: dict[str, type[HostSpec]]) -> dict:
    """The ``hosts.json`` array schema: ``anyOf`` over the distinct specs with a
    shared ``$defs`` and an ``os_type`` discriminator mapping covering every
    registered name."""
    defs_map, top = models_json_schema(
        [(s, 'validation') for s in distinct],
        ref_template='#/$defs/{model}',
    )
    return {
        'type': 'array',
        'items': {
            'anyOf': [defs_map[(s, 'validation')] for s in distinct],
            'discriminator': {
                'propertyName': 'os_type',
                'mapping': {
                    name: defs_map[(spec, 'validation')]['$ref']
                    for name, spec in names.items()
                },
            },
        },
        '$defs': top['$defs'],
    }


def build_schemas() -> dict[str, dict]:
    """Return ``{stem: schema_document}`` for every generated schema.

    Reads whatever host classes are currently registered, so custom specs
    loaded via init modules are included automatically.
    """
    names = registered_host_specs()
    distinct: list[type[HostSpec]] = list(dict.fromkeys(names.values()))

    docs: dict[str, dict] = {}
    for spec in distinct:
        stem = _stem(spec)
        docs[stem] = _decorate(spec.model_json_schema(), stem, f'otto {stem}')

    docs['hosts'] = _decorate(
        _host_array_schema(distinct, names), 'hosts', 'otto hosts.json'
    )
    docs['settings'] = _decorate(
        SettingsModel.model_json_schema(), 'settings', 'otto settings.toml'
    )
    docs['reservations'] = _decorate(
        ReservationFile.model_json_schema(), 'reservations', 'otto reservations'
    )
    return docs
```

Note: the `from . import HostSpec, ...` works because `models/__init__.py` re-exports these and is fully initialized by the time this module is imported (it is not imported during `models/__init__` execution). If a circular-import surfaces, import from the leaf modules instead (`from .host import HostSpec`, `from .settings import SettingsModel, ReservationFile`).

- [ ] **Step 4: Run the test to confirm it passes**

Run: `uv run pytest tests/unit/models/test_jsonschema.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Stage the checkpoint (do NOT commit)**

```bash
git add src/otto/models/jsonschema.py tests/unit/models/test_jsonschema.py
```
Stop — Chris commits.

---

### Task 3: Correctness round-trip test against real fixtures

**Files:**
- Test: `tests/unit/models/test_jsonschema_validation.py`

Context: This is the guard that replaces the rejected "drift snapshot" — it proves the generated `hosts` schema actually matches real host data and rejects garbage, using the `jsonschema` library. Verified during planning: `tests/lab_data/tech1` (11 hosts) and `tech2` (3 hosts) both validate clean under the `anyOf` wrapper; an unknown key fails.

- [ ] **Step 1: Write the test**

Create `tests/unit/models/test_jsonschema_validation.py`:

```python
"""The generated hosts schema validates real lab data and rejects bad data."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from otto.models.jsonschema import build_schemas

_LAB_DATA = Path(__file__).resolve().parents[2] / 'tests' / 'lab_data'
_HOST_FILES = sorted(_LAB_DATA.glob('*/hosts.json'))


@pytest.fixture(scope='module')
def hosts_validator() -> Draft202012Validator:
    schema = build_schemas()['hosts']
    Draft202012Validator.check_schema(schema)  # the schema itself is well-formed
    return Draft202012Validator(schema)


def test_lab_data_fixtures_exist():
    assert _HOST_FILES, 'expected at least one tests/lab_data/*/hosts.json fixture'


@pytest.mark.parametrize('hosts_file', _HOST_FILES, ids=lambda p: p.parent.name)
def test_real_hosts_json_validates(hosts_validator, hosts_file):
    data = json.loads(hosts_file.read_text())
    errors = list(hosts_validator.iter_errors(data))
    assert errors == [], [e.message for e in errors]


def test_unknown_host_key_is_rejected(hosts_validator):
    base = json.loads(_HOST_FILES[0].read_text())
    bad = copy.deepcopy(base)
    bad[0]['totally_unknown_key'] = 'x'
    assert list(hosts_validator.iter_errors(bad)), 'unknown key should fail validation'
```

Note the path: this test file lives at `tests/unit/models/`, so `parents[2]` is the repo's `tests/` parent — i.e. repo root; `_LAB_DATA` resolves to `<repo>/tests/lab_data`. Confirm by running; if the path is off, adjust the `parents[N]` index (the fixtures are at `tests/lab_data/*/hosts.json` from the repo root).

- [ ] **Step 2: Run the test to confirm it passes**

Run: `uv run pytest tests/unit/models/test_jsonschema_validation.py -q`
Expected: PASS (fixtures parametrized — `tech1`, `tech2`, … all green; unknown-key test green).

- [ ] **Step 3: Stage the checkpoint (do NOT commit)**

```bash
git add tests/unit/models/test_jsonschema_validation.py
```
Stop — Chris commits.

---

### Task 4: CLI command `otto schema export`

**Files:**
- Create: `src/otto/cli/schema.py`
- Modify: `src/otto/cli/main.py` (`_SUBCOMMAND_MODULES`)
- Test: `tests/unit/cli/test_schema_cli.py`

Context: The CLI is the only I/O layer. `otto/cli/main.py` auto-attaches subcommand Typers from `_SUBCOMMAND_MODULES: dict[str, tuple[modpath, attr]]` (line 359). Adding `'schema': ('.schema', 'schema_app')` wires it up; the completion/placeholder machinery handles the rest. Custom host specs are already loaded by the time a real (non-completion) `otto schema export` runs, because importing `otto.configmodule` runs `apply_repo_settings()`; the `--builtins-only` flag exists for running outside a project (it imports the models directly and does not depend on a resolved repo). Default output dir is `schemas/` (git-ignored, Task 6).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/cli/test_schema_cli.py`:

```python
"""`otto schema export` writes the generated schema files."""

import json

from typer.testing import CliRunner

from otto.cli.schema import schema_app

runner = CliRunner()


def test_export_writes_all_documents(tmp_path):
    out = tmp_path / 'schemas'
    result = runner.invoke(schema_app, ['export', '--out', str(out)])
    assert result.exit_code == 0, result.output

    written = {p.name for p in out.glob('*.schema.json')}
    assert {
        'unix-host.schema.json', 'embedded-host.schema.json',
        'hosts.schema.json', 'settings.schema.json', 'reservations.schema.json',
    } <= written

    # Every emitted file is valid JSON carrying the schema dialect.
    for path in out.glob('*.schema.json'):
        doc = json.loads(path.read_text())
        assert doc['$schema'] == 'https://json-schema.org/draft/2020-12/schema'


def test_export_reports_what_it_wrote(tmp_path):
    out = tmp_path / 'schemas'
    result = runner.invoke(schema_app, ['export', '--out', str(out), '--builtins-only'])
    assert result.exit_code == 0
    assert 'hosts.schema.json' in result.output
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/unit/cli/test_schema_cli.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.cli.schema'`.

- [ ] **Step 3: Write the CLI module**

Create `src/otto/cli/schema.py`:

```python
"""otto schema — export JSON Schema for the user-edited otto files.

Commands:
    otto schema export [--out DIR] [--builtins-only]

The schemas are generated from the installed otto's pydantic models, so they
always match the running version. Point your editor at the emitted files for
autocomplete + typo-catching on ``hosts.json``, ``settings.toml``, and the
reservations JSON. See the "Editor schemas" user guide.
"""

import json
from pathlib import Path
from typing import Annotated

import typer
from rich import print as rprint

schema_app = typer.Typer(
    name='schema',
    help='Export JSON Schema for hosts.json / settings.toml / reservations.',
    context_settings={'help_option_names': ['-h', '--help']},
)


@schema_app.command('export')
def export(
    out: Annotated[Path, typer.Option(
        '--out', '-o', help='Directory to write *.schema.json into.',
    )] = Path('schemas'),
    builtins_only: Annotated[bool, typer.Option(
        '--builtins-only',
        help='Emit only the in-tree host specs; skip project config / init '
             'modules (use when running outside an otto project).',
    )] = False,
) -> None:
    """Generate the schema files into ``out``."""
    if not builtins_only:
        # Importing configmodule runs apply_repo_settings() -> import_init_modules(),
        # so any custom-registered host classes are present before generation.
        import otto.configmodule  # noqa: F401

    from ..models.jsonschema import build_schemas

    out.mkdir(parents=True, exist_ok=True)
    for stem, doc in build_schemas().items():
        path = out / f'{stem}.schema.json'
        path.write_text(json.dumps(doc, indent=2) + '\n')
        rprint(f'  wrote [cyan]{path.name}[/cyan]')
    rprint(f'[green]Wrote schemas to[/green] {out}')
```

- [ ] **Step 4: Register the subcommand**

In `src/otto/cli/main.py`, add the entry to `_SUBCOMMAND_MODULES` (around line 359):

```python
    'schema':      ('.schema',      'schema_app'),
```

- [ ] **Step 5: Run the test to confirm it passes**

Run: `uv run pytest tests/unit/cli/test_schema_cli.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Smoke-test the wired command (writes to a temp dir, NOT the repo)**

Run: `uv run otto schema export --out /tmp/otto_schema_smoke && ls /tmp/otto_schema_smoke`
Expected: prints `wrote unix-host.schema.json` … and lists 5 `*.schema.json` files. Then `rm -rf /tmp/otto_schema_smoke`.

- [ ] **Step 7: Stage the checkpoint (do NOT commit)**

```bash
git add src/otto/cli/schema.py src/otto/cli/main.py tests/unit/cli/test_schema_cli.py
```
Stop — Chris commits.

---

### Task 5: Docs page (command + VS Code + Neovim) + Makefile + .gitignore

**Files:**
- Create: `docs/guide/editor-schemas.md`
- Modify: `docs/guide/index.rst` (toctree)
- Modify: `Makefile` (add `schema` target)
- Modify: `.gitignore`
- Modify: `todo/registry_builtin_registration_symmetry.md`

Context: The docs build is warnings-as-errors and every page must be in a toctree, so the new MyST page must be added to `docs/guide/index.rst`. `getting-started.md` confirms MyST markdown is accepted. Keep code blocks fenced with a language for the lint.

- [ ] **Step 1: Write the user guide page**

Create `docs/guide/editor-schemas.md`:

````markdown
# Editor schemas (autocomplete for `hosts.json` & `settings.toml`)

otto can generate [JSON Schema](https://json-schema.org/) for the files you edit
by hand — `hosts.json`, `settings.toml`, and the reservations JSON — so your
editor offers field autocomplete and flags typos. The schemas are generated from
the pydantic models inside the otto you have installed, so they always match your
version. There is nothing to download and nothing that can go stale.

## Generate the schemas

```bash
otto schema export --out .schemas
```

This writes (into `.schemas/`):

| File | Describes |
| --- | --- |
| `hosts.schema.json` | the whole `hosts.json` array (any registered `os_type`) |
| `unix-host.schema.json`, `embedded-host.schema.json` | a single host of one type |
| `settings.schema.json` | `settings.toml` |
| `reservations.schema.json` | the reservations JSON file |

Run it again after upgrading otto, or after adding a custom host class, to pick
up new fields. (Custom host classes registered via an init module in
`.otto/settings.toml` appear automatically; pass `--builtins-only` to emit just
the built-in types when running outside a project.)

## VS Code

`hosts.json` and the reservations JSON are covered by the built-in JSON
language server. Add to your workspace `.vscode/settings.json`:

```json
{
  "json.schemas": [
    { "fileMatch": ["**/hosts.json"], "url": "./.schemas/hosts.schema.json" },
    { "fileMatch": ["**/reservations.json"], "url": "./.schemas/reservations.schema.json" }
  ]
}
```

For `settings.toml`, install the
[Even Better TOML](https://marketplace.visualstudio.com/items?itemName=tamasfe.even-better-toml)
extension and add:

```json
{
  "evenBetterToml.schema.associations": {
    ".*/settings\\.toml$": "./.schemas/settings.schema.json"
  }
}
```

## Neovim

With the JSON language server (`jsonls`, from `vscode-json-languageserver`) via
`nvim-lspconfig`:

```lua
require('lspconfig').jsonls.setup({
  settings = {
    json = {
      schemas = {
        { fileMatch = { 'hosts.json' }, url = './.schemas/hosts.schema.json' },
        { fileMatch = { 'reservations.json' }, url = './.schemas/reservations.schema.json' },
      },
    },
  },
})
```

For `settings.toml`, the [taplo](https://taplo.tamasfe.dev/) language server
honours schema directives. Either add a directive at the top of the file:

```toml
#:schema ./.schemas/settings.schema.json
```

or associate it in the taplo config (`.taplo.toml`):

```toml
[[rule]]
include = ["settings.toml"]
[rule.schema]
path = ".schemas/settings.schema.json"
```

## Note on drift

The schemas reflect the otto version that generated them. There is no committed
copy in the otto repo — regenerate with `otto schema export` whenever you
upgrade so the fields stay in sync with your installed models.
````

- [ ] **Step 2: Add the page to the guide toctree**

Open `docs/guide/index.rst` and add `editor-schemas` to its `toctree` directive (match the existing entry style/indentation). Verify the entry name has no extension.

- [ ] **Step 3: Add the Makefile target**

In `Makefile`, add (place near other dev/utility targets, keep the `##` help comment so it shows in `make help`):

```make
schema: ## Generate JSON Schema for hosts.json / settings.toml / reservations into schemas/ (git-ignored; for editor autocomplete)
	uv run otto schema export --out schemas
```

- [ ] **Step 4: Ignore the default output dir**

Append to `.gitignore`:

```gitignore
# Generated JSON Schema (otto schema export default output)
/schemas/
```

- [ ] **Step 5: Note the accessor in the registry-hygiene todo**

In `todo/registry_builtin_registration_symmetry.md`, add a short line under the relevant section noting that `os_profile.registered_host_specs()` is now the public read accessor over `_HOST_SPECS` (added by Plan 6 for schema export), so a future registry-hygiene pass should keep/extend it rather than re-expose the private dict.

- [ ] **Step 6: Build the docs to confirm no warnings**

Run: `make docs`
Expected: `docs-lint`, `docs-html` (warnings-as-errors), and `doctest` all pass. If the build complains the new page isn't in a toctree, fix Step 2.

- [ ] **Step 7: Stage the checkpoint (do NOT commit)**

```bash
git add docs/guide/editor-schemas.md docs/guide/index.rst Makefile .gitignore todo/registry_builtin_registration_symmetry.md
```
Stop — Chris commits.

---

### Task 6: Full gate

**Files:** none (verification + lint fixes only).

- [ ] **Step 1: Lint the touched source/test files**

Run (explicit files, never a directory):

```bash
uv run ruff check --fix \
  src/otto/host/os_profile.py src/otto/models/jsonschema.py src/otto/cli/schema.py \
  src/otto/cli/main.py \
  tests/unit/host/test_registered_host_specs.py tests/unit/models/test_jsonschema.py \
  tests/unit/models/test_jsonschema_validation.py tests/unit/cli/test_schema_cli.py
```
Expected: clean (no remaining diagnostics). Re-stage any files ruff modified.

- [ ] **Step 2: Type-check**

Run: `make typecheck`
Expected: `ty` reports 0 diagnostics on the new modules (advisory gate, keep it clean).

- [ ] **Step 3: Docs**

Run: `make docs`
Expected: clean (lint + html-warnings-as-errors + doctests).

- [ ] **Step 4: Coverage gate**

Run: `make coverage`
Expected: suite green and total coverage ≥ 90%. The new modules should be near-fully covered by Tasks 1–4; if `otto/cli/schema.py` or `otto/models/jsonschema.py` dip coverage, add a focused unit test (e.g. the `--builtins-only` branch, the custom-spec path) rather than a pragma.

- [ ] **Step 5: Full matrix**

Run: `make nox`
Expected: all supported Pythons (3.10–3.14) "was successful". `jsonschema` resolves on every Python (pure-Python, universal wheel). **Do not kill the run at a tight timeout** — let it finish (a wedged live-bed run is recovered with `make qemu-restart`, not by SIGTERM).

- [ ] **Step 6: Final staged checkpoint (do NOT commit)**

```bash
git add -A
git status   # confirm only Plan-6 files are staged; working tree otherwise clean
```
Stop — report the green gate results to Chris for his single commit.

---

## Self-Review (planner)

**Spec coverage:** §6 maps to tasks — generator command (T4), per-spec + `anyOf` array wrapper from the registry (T2), `settings` + `reservations` (T2), registry-derived via public accessor (T1), custom host types incl. config bootstrap + `--builtins-only` (T2 test + T4), `jsonschema` correctness test replacing the snapshot (T3), VS Code + Neovim docs (T5), `make schema` + git-ignored output (T5), full gate incl. nox/coverage/docs (T6). ✅

**Placeholder scan:** every code/test/doc step shows complete content; no TBD/"add validation"/"similar to". ✅

**Type/name consistency:** `registered_host_specs()` (T1) consumed in T2; `build_schemas() -> dict[str, dict]` defined T2, consumed in T3/T4; `schema_app` defined T4, registered in `main.py` T4 and imported in the T4 test; file stems (`unix-host`, `embedded-host`, `hosts`, `settings`, `reservations`) consistent across T2/T3/T4/T5; `anyOf` (not `oneOf`) consistent in spec + T2 assertions. ✅

**Known soft spots flagged inline:** the `parents[N]` index in T3 (verify on run); the `from . import …` in T2 (leaf-import fallback noted). Both are caught by the task's own run-to-pass step.
