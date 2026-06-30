# Promote `@options` to a Discoverable Default — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `@options` the visible, recommended default for defining options on repos, instructions, and suites — via a new canonical guide page, a shipped `otto.examples` sample, lifecycle-framed cross-links, and conversion of every doc/fixture away from plain `@dataclass`.

**Architecture:** Documentation + examples + fixture edits only. No `src/otto` production behaviour changes — `@options` already exists as a lazy re-export of `pydantic.dataclasses.dataclass` (`src/otto/__init__.py:40`). One new sample module (`src/otto/examples/options.py`), one new guide page (`docs/guide/options.md`), edits to six existing doc pages, and a mechanical decorator swap across seven test-fixture Options classes.

**Tech Stack:** MyST Markdown + Sphinx (nitpicky, `-W`), `sphinx.ext.doctest`, pydantic dataclasses, Typer, pytest (`--doctest-modules`), ruff, `ty`.

**Spec:** [docs/superpowers/specs/2026-06-29-options-discoverability-design.md](../specs/2026-06-29-options-discoverability-design.md)

## Global Constraints

These apply to **every** task:

- **Stage only — never `git commit`.** otto's prepare-commit-msg hook needs `/dev/tty` and agent commits mis-tag AI assistance. Each task ends with `git add <paths>`. Chris commits at the end with the message in Task 5.
- **Ban `from __future__ import annotations`** in any new/edited Python — it trips otto's Sphinx-nitpicky docs gate. Use real 3.10+ annotations and module-top imports.
- **`@options` is described as *pydantic's* dataclass decorator**, never the standard library's `@dataclass`. Do not write "compatible with dataclasses" or "plain `@dataclass` still works / opt-in" anywhere.
- **Docs gate is strict:** `sphinx-build -E -a -W` (warnings = errors, `nitpicky = True`). Every `{doc}`/`{class}`/`{func}` xref must resolve; every new page must be in a toctree.
- **A `>>>` prompt may appear only inside a ```` ```{doctest} ```` fence** (enforced by `scripts/lint_markdown_doctests.py`). Never put `>>>` in a ```` ```python ```` block.
- **ruff import grouping:** `from otto import options` is first-party — it sorts at the top of the `otto.*` import group (because `otto` < `otto.cli` < `otto.suite` …).
- **No heavy/parallel test load on the dev VM** (no looped over-subscribed xdist); never power real VMs. A single `make coverage` pass is fine.
- **Gate vocabulary:** there is no `make test`. Per-task validation uses the narrowest relevant gate; the full gate (Task 5) is `make docs` + `make typecheck` + `make coverage` + `nox`.

---

### Task 1: Ship the `otto.examples.options` sample

Creates the copyable reference module and **de-risks the whole change**: its doctest proves that an `@options` class with `Annotated[T, typer.Option(...)]` fields and plain defaults (exactly the fixture pattern in Task 4) constructs and validates correctly.

**Files:**
- Create: `src/otto/examples/options.py`
- Modify: `src/otto/examples/__init__.py`
- Validated by: `pytest --doctest-modules src/otto/examples/options.py` and `make typecheck`

**Interfaces:**
- Produces: `otto.examples.options.RepoOptions` (fields `device_type: str = "router"`, `lab_env: str = "staging"`, `retries: int = Field(default=3, ge=0)`), `otto.examples.options.DeviceSuiteOptions(RepoOptions)` (adds `firmware: str = "latest"`), `otto.examples.options.DeployInstructionOptions(RepoOptions)` (adds `debug: bool = False`). Task 2's guide-page doctest imports `RepoOptions` from here.

- [ ] **Step 1: Write the sample module**

Create `src/otto/examples/options.py` with this exact content:

```python
"""Reference repo-wide ``@options`` classes (sample).

Options classes are the contract that threads otto's lifecycle: a repo-wide
base is defined once (in any module named in your ``init`` setting), then
inherited by every test suite's inner ``Options`` class and every
``@instruction(options=...)`` so the same flags appear on ``otto test`` and
``otto run``.

``@options`` (``from otto import options``) is otto's name for pydantic's
dataclass decorator: decorating a class with it makes the class a pydantic
dataclass, so its fields are validated at construction. It is not the standard
library's ``@dataclass``.

Copy this module as a starting point, or import these classes directly:

>>> from otto.examples.options import RepoOptions, DeviceSuiteOptions
>>> RepoOptions().device_type
'router'
>>> DeviceSuiteOptions(device_type="switch", firmware="2.1").firmware
'2.1'
>>> from pydantic import ValidationError
>>> try:
...     RepoOptions(retries=-1)
... except ValidationError:
...     print("rejected")
rejected
"""

from typing import Annotated

import typer
from pydantic import Field

from otto import options


@options
class RepoOptions:
    """Repo-wide options shared by every suite and instruction.

    Inherit this from a suite's inner ``Options`` class or from the class you
    pass to ``@instruction(options=...)`` and every field becomes a CLI flag on
    both ``otto test`` and ``otto run`` subcommands.
    """

    device_type: Annotated[
        str,
        typer.Option(help="Type of device under test (e.g. 'router', 'switch')."),
    ] = "router"
    lab_env: Annotated[
        str,
        typer.Option(help="Lab environment to target (e.g. 'staging', 'production')."),
    ] = "staging"
    retries: Annotated[
        int,
        typer.Option(help="Connection retries (must be >= 0)."),
    ] = Field(default=3, ge=0)


@options
class DeviceSuiteOptions(RepoOptions):
    """Suite options: inherits the repo-wide flags and adds ``--firmware``."""

    firmware: Annotated[
        str,
        typer.Option(help="Firmware version to validate against."),
    ] = "latest"


@options
class DeployInstructionOptions(RepoOptions):
    """Instruction options: inherits the repo-wide flags and adds ``--field/--debug``."""

    debug: Annotated[
        bool,
        typer.Option("--field/--debug", help="Use field or debug products."),
    ] = False
```

- [ ] **Step 2: Run the doctest to verify it passes**

Run: `uv run pytest -p no:cacheprovider -o addopts="--doctest-modules" src/otto/examples/options.py -v`
Expected: PASS (1 doctest item). If it errors at *collection/import* time, pydantic could not model a field — stop and investigate before continuing (this is the Task 4 risk surfacing early).

- [ ] **Step 3: Add the sample to the examples package index**

In `src/otto/examples/__init__.py`, the docstring lists the existing samples as a bullet list. Add a third bullet so it reads:

```python
- :mod:`otto.examples.lab_repository` — an in-memory host source.
- :mod:`otto.examples.reservations` — an in-memory reservation backend.
- :mod:`otto.examples.options` — repo-wide ``@options`` classes for suites and instructions.
```

- [ ] **Step 4: Typecheck the new module**

Run: `make typecheck`
Expected: PASS, no new errors in `src/otto/examples/options.py`.

- [ ] **Step 5: Lint the new module**

Run: `uv run ruff check src/otto/examples/options.py && uv run ruff format --check src/otto/examples/options.py`
Expected: PASS. If `format --check` fails, run `uv run ruff format src/otto/examples/options.py` then re-run `ruff check` (formatting is not lint-neutral).

- [ ] **Step 6: Stage**

```bash
git add src/otto/examples/options.py src/otto/examples/__init__.py
```

---

### Task 2: Create the canonical guide page `docs/guide/options.md`

The single hub everything links to. Leads with the lifecycle framing, then mechanics, then a `{doctest}` that imports the Task 1 sample.

**Files:**
- Create: `docs/guide/options.md`
- Modify: `docs/guide/index.rst` (toctree)
- Validated by: `make docs`

**Interfaces:**
- Consumes: `otto.examples.options.RepoOptions` from Task 1 (the page's doctest imports it).
- Produces: the doc target `guide/options`, linked to by Task 3.

- [ ] **Step 1: Write the page**

Create `docs/guide/options.md` with this exact content:

````markdown
# Options classes

Options classes are how you add command-line flags to your `otto run`
instructions and `otto test` suites. They are otto's through-line: define a set
of options once, and the same definition surfaces on every command that
inherits it.

## Options across otto's lifecycle

Options appear at three points in a project's lifecycle:

- **Project definition** — you define repo-wide options once, in a module named
  in your `init` setting. These are the common flags every instruction and suite
  shares (device type, lab environment, …).
- **Instruction execution** — {func}`@instruction() <otto.cli.run.instruction>`
  expands an options class into `otto run` flags and hands your function a
  populated instance.
- **Test suite runs** — {class}`~otto.suite.suite.OttoSuite` with
  {func}`@register_suite() <otto.suite.register.register_suite>` expands an
  options class into `otto test` flags and passes them to each test method as
  `suite_options`.

Defining the options once and inheriting them keeps `otto run` and `otto test`
in lock-step.

## Anatomy of an options class

An options class has fields annotated with `Annotated[T, typer.Option(...)]`.
Each field becomes a CLI flag; the `typer.Option(...)` carries the help text and
any flag spelling.

```python
from typing import Annotated

import typer

from otto import options


@options
class RepoOptions:
    device_type: Annotated[
        str, typer.Option(help="Type of device under test (e.g. 'router', 'switch').")
    ] = "router"
    lab_env: Annotated[
        str, typer.Option(help="Lab environment to target.")
    ] = "staging"
```

`--device-type` and `--lab-env` now appear in `--help` wherever this class is
used.

## `@options` is a pydantic dataclass

`@options` (`from otto import options`) is otto's ergonomic name for
**pydantic's** dataclass decorator — `pydantic.dataclasses.dataclass`,
re-exported under otto's namespace. Decorating a class with `@options` makes it
a *pydantic dataclass*: its fields are validated when the class is constructed.

```{important}
`@options` is **pydantic's** dataclass, not the standard library's
`@dataclass`. Use `@options` for every options class so your flags are
validated and consistent.
```

Importing `from otto import options` — rather than reaching for pydantic
directly — keeps every options class on one standard import and gives otto a
single seam to evolve options behaviour in the future.

## Validating fields

Add pydantic constraints with `Field(...)`. An out-of-range value is rejected at
construction — before the suite or instruction runs — and otto turns the error
into a clean CLI failure (exit code 2, naming the offending flag) instead of
silently accepting it.

```python
from typing import Annotated

import typer
from pydantic import Field

from otto import options


@options
class RepoOptions:
    retries: Annotated[
        int, typer.Option(help="Connection retries (must be >= 0).")
    ] = Field(default=3, ge=0)
```

```bash
otto test TestDevice --retries -1
# error: Invalid value: retries: Input should be greater than or equal to 0
```

Validation runs at construction time, so the bad value never reaches your test.
A copyable example ships in otto as `otto.examples.options`
(`src/otto/examples/options.py`):

```{doctest}
>>> from otto.examples.options import RepoOptions
>>> RepoOptions().retries
3
>>> from pydantic import ValidationError
>>> try:
...     RepoOptions(retries=-1)
... except ValidationError:
...     print("rejected")
rejected
```

## Sharing repo-wide options

Define a base options class once and inherit it everywhere you want the same
flags. Put the base in **any module named in your repo's `init` setting** — the
location is yours. A `libs` directory such as `pylib/` is a common place to keep
it, but the only rule is that the module is importable and listed in `init` (see
{doc}`repo-setup`).

`otto.examples.options` bundles a complete example: a `RepoOptions` base plus a
suite options class and an instruction options class that both inherit it.

### In a test suite

```python
from typing import Annotated

import typer
from otto import options
from otto.suite import OttoSuite, register_suite

from my_shared.options import RepoOptions  # your base, listed in `init`


@options
class _Options(RepoOptions):              # inherits --device-type, --lab-env, --retries
    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"


@register_suite()
class TestDevice(OttoSuite[_Options]):
    Options = _Options

    async def test_version(self, suite_options: _Options) -> None:
        self.logger.info(f"device={suite_options.device_type} fw={suite_options.firmware}")
```

`otto test TestDevice --help` shows `--device-type`, `--lab-env`, `--retries`,
and `--firmware`.

### In an instruction

```python
from typing import Annotated

import typer
from otto import options
from otto.cli.run import instruction

from my_shared.options import RepoOptions  # your base, listed in `init`


@options
class _DeployOpts(RepoOptions):           # inherits --device-type, --lab-env, --retries
    debug: Annotated[bool, typer.Option("--field/--debug")] = False


@instruction(options=_DeployOpts)
async def deploy(opts: _DeployOpts):
    ...
```

`otto run deploy --help` shows the same repo-wide flags plus `--field/--debug`.

See {doc}`run` and {doc}`test` for the full instruction and suite guides, and
[Inheriting shared options](../cookbook/suite-recipes.md#inheriting-shared-options)
in the cookbook.
````

- [ ] **Step 2: Register the page in the guide toctree**

In `docs/guide/index.rst`, the `.. toctree::` lists pages starting with `repo-setup`. Insert `options` immediately after `repo-setup` so the block begins:

```rst
.. toctree::

   repo-setup
   options
   lab-config
```

- [ ] **Step 3: Build the docs and run doctests**

Run: `make docs`
Expected: PASS — 0 Sphinx warnings, doctest builder green (the new `{doctest}` block passes), `docs-lint` clean. If nitpicky reports an unresolved `{doc}`/`{class}`/`{func}` xref, fix the role (compare against working examples in `docs/overview.md`). If `docs-lint` flags a `>>>` prompt, it is in a `python` fence — move it into the `{doctest}` fence or remove it.

- [ ] **Step 4: Stage**

```bash
git add docs/guide/options.md docs/guide/index.rst
```

---

### Task 3: Lifecycle-frame and cross-link the existing docs

Convert every options code block to `@options`, add a framing sentence tying options to each page's lifecycle stage, and link to `guide/options`. `docs/guide/os-profiles.md` is **audited and left unchanged** (its `@dataclass` is a host-profile struct, not an options class).

**Files:**
- Modify: `docs/getting-started.md`, `docs/guide/repo-setup.md`, `docs/guide/run.md`, `docs/guide/test.md`, `docs/cookbook/suite-recipes.md`, `docs/overview.md`
- Validated by: `make docs`

**Interfaces:**
- Consumes: the `guide/options` doc target from Task 2.

> For each edit below: Read the file first, locate the quoted region, and apply the change. Markdown source wrapping may differ slightly from these quotes — match on the distinctive text.

- [ ] **Step 1: `docs/getting-started.md` — rewrite the inline `@options` explanation (≈ lines 424–429)**

Replace the paragraph that currently begins ``` `@options` (`from otto import options`) is a re-export of ``` and ends ``` ...for `otto run` subcommands.``` with:

```markdown
`@options` (`from otto import options`) is otto's name for **pydantic's**
dataclass decorator: decorating an Options class with it makes the class a
pydantic dataclass, so its fields are validated. `otto test TestExample
--retries -1` fails with a clean CLI error (exit code 2) instead of being
silently accepted. The same `@options` classes power `@instruction(options=...)`
for `otto run` subcommands. See {doc}`guide/options` for the full picture.
```

(Leave the surrounding example code and the `{doctest}` block that follow it unchanged.)

- [ ] **Step 2: `docs/guide/repo-setup.md` — add a "Defining shared options" section**

Immediately **after** the "## What happens at startup" numbered list (the item ending "…making hosts available to the zero-argument accessors … in all commands.") and **before** "## Multiple repos", insert:

```markdown
## Defining shared options

Most repos want a common set of CLI flags — device type, lab environment, and so
on — on every `otto run` instruction and `otto test` suite. Define them once as a
shared **options class** in any module named in your `init` setting (a `libs`
directory like `pylib/` is a common home, but any importable module works), then
inherit it from each suite and instruction. Options are a first-class part of
project definition: declared here at setup, they thread through instruction
execution and test runs.

Use the `@options` decorator — otto's name for a pydantic dataclass — so the
flags are validated. See {doc}`options` for the full treatment, and
`otto.examples.options` for a copyable example.
```

- [ ] **Step 3: `docs/guide/run.md` — convert the options section to `@options` + link**

In the "## Sharing repo-wide options across instructions and suites" section:

(a) In the intro paragraph, change the clause "define a shared / base dataclass in your pylib." to:

```markdown
define a shared base **options class** (with `@options`) in any module listed in
your `init` setting — a `libs` path like `pylib/` is one common choice. See
{doc}`options` for the full treatment. The *same* class can be inherited by
```

(so it still flows into the existing "- a suite's inner `Options` class …" list).

(b) In the **### 1. Define repo-wide options** code block, change the imports and decorator:
- Replace `from dataclasses import dataclass` with `from otto import options` (move it below the `import typer` line, as the first-party import group).
- Replace `@dataclass` with `@options`.

(c) In the **### 2. Inherit and extend in each instruction** code block:
- Replace `from dataclasses import dataclass` with nothing (delete the line) and add `from otto import options` at the top of the `from otto...` import group.
- Replace `@dataclass` with `@options`.

(d) In the **### 2b. Inherit the same base in a suite** code block:
- Replace `from dataclasses import dataclass` (delete) and add `from otto import options` at the top of the `from otto...` group (before `from otto.suite import ...`).
- Replace `@dataclass` with `@options`.

- [ ] **Step 4: `docs/guide/test.md` — convert the options section to `@options` + link**

In the "## Options dataclass" section (≈ lines 78–104):

(a) Rename the heading to `## Options classes`.

(b) Add a framing sentence at the top of the section:

```markdown
A suite's options class is expanded into `otto test <Suite>` flags and handed to
each test method as `suite_options` — the test-suite stage of otto's options
lifecycle ({doc}`options`).
```

(c) In each code block in this section, replace `@dataclass` with `@options`, and ensure each block that uses `@options` has `from otto import options` imported (add it to the existing example imports; if a block shows no imports, add a one-line `from otto import options` at its top).

(d) Leave the closing line "Import the base from a shared module listed in your `init` setting." in place (it is already correct).

- [ ] **Step 5: `docs/cookbook/suite-recipes.md` — convert the recipe to `@options` + link**

In the "## Inheriting shared options" recipe (≈ lines 70–133):

(a) In the `# pylib/my_shared/options.py` code block, replace `from dataclasses import dataclass` with `from otto import options` (placed after `import typer`) and `@dataclass` with `@options`.

(b) In the `# tests/test_device.py` code block, delete `from dataclasses import dataclass`, add `from otto import options` at the top of the `from otto...`/third-party group, and replace `@dataclass` with `@options`.

(c) After the recipe's existing "see [Sharing repo-wide options](../guide/run.md#…)" sentence, add:

```markdown
For the complete options reference — validation, the lifecycle, and the
`@options` decorator — see [Options classes](../guide/options.md).
```

- [ ] **Step 6: `docs/overview.md` — add a nav link**

In the "## Where to go next" list, add a bullet immediately after the `{doc}`guide/index`` line:

```markdown
- {doc}`guide/options` — Shared options classes for instructions and suites
```

- [ ] **Step 7: Confirm `docs/guide/os-profiles.md` needs no change**

Run: `grep -n "dataclass\|@options" docs/guide/os-profiles.md`
Expected: the only hits are the `MyRtosHost(EmbeddedHost)` host-profile struct (a host subclass, not an options class). Make **no** edit. This step is a recorded verification, not a change.

- [ ] **Step 8: Build the docs**

Run: `make docs`
Expected: PASS — 0 warnings, all new `{doc}`guide/options`` / `../guide/options.md` links resolve, `docs-lint` clean.

- [ ] **Step 9: Stage**

```bash
git add docs/getting-started.md docs/guide/repo-setup.md docs/guide/run.md docs/guide/test.md docs/cookbook/suite-recipes.md docs/overview.md
```

---

### Task 4: Convert the test-fixture Options classes to `@options`

Seven Options classes in `tests/repo1` and `tests/repo3` are plain `@dataclass`. Convert each to `@options`. In every file the `from dataclasses import dataclass` import is used **only** for the Options decorator, so it is removed and replaced by `from otto import options`.

**Files:**
- Modify: `tests/repo1/pylib/repo1_common/options.py`, `tests/repo1/pylib/repo1_instructions/install.py`, `tests/repo1/pylib/repo1_instructions/nc_smoke.py`, `tests/repo1/tests/test_device.py`, `tests/repo1/tests/test_stability_fixture.py`, `tests/repo1/tests/test_coverage_product.py`, `tests/repo3/tests/test_embedded_coverage.py`
- Validated by: import smoke (below) + `make coverage` in Task 5

**Interfaces:**
- Consumes: nothing new. `RepoOptions` stays the base; its subclasses must become `@options` too (homogeneous pydantic-dataclass inheritance chain).

- [ ] **Step 1: Convert `tests/repo1/pylib/repo1_common/options.py` (the base)**

- Delete the line `from dataclasses import dataclass`.
- Below `import typer`, add a first-party import group:

```python
from otto import options
```

- Replace `@dataclass` with `@options` on `RepoOptions`.

Resulting import block:

```python
from typing import Annotated

import typer

from otto import options
```

- [ ] **Step 2: Convert the two instruction Options classes**

In `tests/repo1/pylib/repo1_instructions/install.py` **and** `tests/repo1/pylib/repo1_instructions/nc_smoke.py`:
- Delete `from dataclasses import dataclass`.
- Add `from otto import options` as the **first** line of the existing `from otto...` import group (above `from otto.cli.run import instruction`).
- Replace `@dataclass` with `@options` on `_Options`.

- [ ] **Step 3: Convert the three suite Options classes**

In `tests/repo1/tests/test_device.py`, `tests/repo1/tests/test_stability_fixture.py`, and `tests/repo1/tests/test_coverage_product.py`:
- Delete `from dataclasses import dataclass`.
- Add `from otto import options` at the top of the `from otto...` import group (e.g. above `from otto.suite import OttoSuite, register_suite` / above `from otto.configmodule import ...`).
- Replace `@dataclass` with `@options` on `_Options`.

(Note: `test_stability_fixture.py` keeps `from typing import ClassVar`; `test_coverage_product.py`'s `_Options` is `pass` — an empty `@options` class is valid.)

- [ ] **Step 4: Convert the repo3 suite Options class**

In `tests/repo3/tests/test_embedded_coverage.py`:
- Delete `from dataclasses import dataclass`.
- Add `from otto import options` at the top of the `from otto...` import group (above `from otto.configmodule import get_repos`).
- Replace `@dataclass` with `@options` on `_Options`.

- [ ] **Step 5: Import-smoke the pylib modules (catches any pydantic-modeling failure)**

Run:
```bash
cd /home/vagrant/otto-sh && PYTHONPATH=tests/repo1/pylib uv run python -c "import repo1_common.options, repo1_instructions.install, repo1_instructions.nc_smoke; print('pylib ok')"
```
Expected: `pylib ok`. An error here means an `@options` class could not be built — investigate the offending field (adjust it or set `ConfigDict(arbitrary_types_allowed=True)`; do **not** revert to `@dataclass`).

- [ ] **Step 6: Collection-smoke the fixture test modules**

Run:
```bash
cd /home/vagrant/otto-sh && uv run pytest tests/repo1/tests/test_device.py tests/repo1/tests/test_stability_fixture.py tests/repo1/tests/test_coverage_product.py tests/repo3/tests/test_embedded_coverage.py --collect-only -q
```
Expected: collection succeeds (modules import, `@options` classes build, suites register). Live-bed execution is not required here — that happens in Task 5.

- [ ] **Step 7: Lint the changed fixtures**

Run: `uv run ruff check tests/repo1 tests/repo3`
Expected: PASS (imports correctly grouped). If isort complains, fix the placement of `from otto import options`.

- [ ] **Step 8: Stage**

```bash
git add tests/repo1/pylib/repo1_common/options.py tests/repo1/pylib/repo1_instructions/install.py tests/repo1/pylib/repo1_instructions/nc_smoke.py tests/repo1/tests/test_device.py tests/repo1/tests/test_stability_fixture.py tests/repo1/tests/test_coverage_product.py tests/repo3/tests/test_embedded_coverage.py
```

---

### Task 5: Full verification and commit handoff

Run the complete gate and hand Chris a paste-able commit message. No new code.

**Files:** none (verification only).

- [ ] **Step 1: Bed-free gates (always run these)**

Run, expecting PASS for each:
```bash
make docs                                   # sphinx -W, doctests, docs-lint
make typecheck                              # ty: all=error
uv run pytest -p no:cacheprovider -o addopts="--doctest-modules" src/otto    # doctest-src incl. examples/options.py
uv run pytest tests/unit/suite/test_options_validation.py -v                 # existing @options + plain-dataclass coverage stays green
```

- [ ] **Step 2: Full suite + nox (single pass; mind live-bed timing)**

These exercise the converted `tests/repo1` / `tests/repo3` fixtures against the live bed. Run **one** pass each (no looped/over-subscribed xdist), when the bed is free; do not kill a live-bed run at a tight timeout.
```bash
make coverage
nox
```
Expected: green. If a converted fixture newly *rejects* a value some test passes, that test was relying on an out-of-range default — fix the test data (this is unlikely: the fixtures carry valid defaults and no `Field` constraints).

- [ ] **Step 3: Confirm the working tree is fully staged**

Run: `git status`
Expected: only the files staged across Tasks 1–4, nothing unintended.

- [ ] **Step 4: Hand off the commit to Chris**

Do **not** commit. Provide Chris this message:

```
docs+examples: make @options the standard options decorator

New docs/guide/options.md hub framed around otto's lifecycle (project
definition -> instruction execution -> suite runs); a copyable
otto.examples.options sample; lifecycle-framed cross-links from
getting-started, repo-setup, run, test, cookbook, and overview; and
conversion of every doc code block and the tests/repo* fixtures from
plain @dataclass to @options. @options is documented as pydantic's
dataclass decorator (re-exported). No production code change.
```

---

## Self-Review

**1. Spec coverage**

| Spec element | Task |
|---|---|
| New `docs/guide/options.md` hub, in toctree | Task 2 |
| Lifecycle framing (project def / instruction exec / suite runs) | Task 2 (page §"lifecycle"), Task 3 (repo-setup/run/test framing sentences) |
| `@options` described precisely as *pydantic's* decorator, not stdlib | Task 1 docstring, Task 2 §"is a pydantic dataclass", Task 3 step 1 (getting-started rewrite) |
| `otto.examples` sample | Task 1 |
| Cross-links at every authoring touchpoint | Task 3 (getting-started, repo-setup, run, test, cookbook, overview) |
| `init`-importability precision (pylib = convention) | Task 2 §"Sharing repo-wide options", Task 3 steps 2–3 |
| Convert doc code blocks + 7 fixtures | Task 3, Task 4 |
| os-profiles audited, not an options class | Task 3 step 7 |
| No runtime enforcement / no production change | (none — by construction) |
| No stdlib-`@dataclass` guard test (existing tests already cover both paths) | Task 5 step 1 references `test_options_validation.py` |
| Stage-only, full gate before done | Global Constraints, Task 5 |

**2. Placeholder scan:** The `...` inside the suite/instruction *illustrative* `python` blocks (Task 2) are intentional Python `Ellipsis` bodies in non-executed display code, not plan placeholders. No "TBD/TODO/handle errors" placeholders elsewhere.

**3. Type/name consistency:** `RepoOptions`, `DeviceSuiteOptions`, `DeployInstructionOptions` defined in Task 1 and imported (only `RepoOptions`) in Task 2's doctest — names match. Fixture `_Options` / `RepoOptions` names match the verbatim source. `from otto import options` import spelling consistent across all tasks.
