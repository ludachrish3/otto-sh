# Sphinx Nitpicky (Zero Ignores) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn on Sphinx `nitpicky` (`-n`) alongside the existing `-W` so unresolved doc cross-references fail the build, with **zero** `nitpick_ignore` entries and no private members documented (resolves [issue #56](https://github.com/ludachrish3/otto-sh/issues/56)).

**Architecture:** The dominant warning source (441 of 682) is `sphinx_autodoc_typehints`, whose attribute monkeypatch is broken on Sphinx 8.1.3 — it leaks an `--is-rst--` sentinel into rendered HTML *and* strips types from function signatures. We **drop the extension** entirely (native Sphinx 8 autodoc renders types correctly and emits resolvable xref nodes; proven zero regression across 1018 documented objects) and add `sphinx.ext.intersphinx` for stdlib/third-party resolution. The remaining ~250 warnings are internal cross-reference hygiene: qualify short-name refs, de-link private members, document genuinely-undocumented-but-referenced public modules, and fix a few typos. The live `sphinx-build -nW` output is the convergence checklist; each task drives a category of those warnings to zero. `nitpicky=True` is committed only in the final task, so `make docs` stays green throughout.

**Tech Stack:** Sphinx 8.1.3, `sphinx.ext.autodoc` + `napoleon` + `intersphinx` + `myst_parser` + `sphinx_immaterial`, uv, ty (typecheck), pytest.

## Global Constraints

- **Zero ignores:** no `nitpick_ignore` or `nitpick_ignore_regex` entries anywhere. Verify with `grep -rn "nitpick_ignore" docs/` → empty.
- **Keep `-W`:** the build promotes warnings to errors. Final state runs `sphinx-build -n -a -W`.
- **No private members documented:** any doc/docstring that links a private member (leading `_`) must be de-linked to an inline literal (``` ``_name`` ```), never documented.
- **Native autodoc only:** `sphinx_autodoc_typehints` is removed, not reconfigured. `autodoc_typehints = 'signature'` stays.
- **Stage only — do not commit.** Chris commits (prepare-commit-msg hook needs `/dev/tty`; agent commits mis-tag AI attribution). Leave all work staged with `git add`.
- **Probe builds go to scratchpad,** never `docs/_build`, to avoid clobbering: `sphinx-build -n -b html docs/ <scratchpad>/probe`.
- **Detection command (the checklist):** `uv run --no-sync sphinx-build -n -b html docs/ <scratchpad>/probe 2>&1 | grep "reference target not found"` prints every remaining unresolved ref as `file:line: WARNING: py:ROLE reference target not found: TARGET`.

---

### Task 1: Config foundation — drop the broken extension, add intersphinx, delete the dead filter

**Files:**
- Modify: `docs/conf.py` (extensions list lines 12-19; delete filter block lines 77-103; add intersphinx mapping near line 75)
- Modify: `pyproject.toml:105` (remove `sphinx-autodoc-typehints` dependency)

**Interfaces:**
- Produces: a native-autodoc docs build with intersphinx configured and `nitpicky` still OFF. Later tasks rely on `intersphinx_mapping` containing `python`, `typer`, `rich`, `pydantic`.

- [ ] **Step 1: Edit `docs/conf.py` extensions** — remove `"sphinx_autodoc_typehints",`, add `"sphinx.ext.intersphinx",`:

```python
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.doctest",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "myst_parser",
    "sphinx_immaterial",
]
```

- [ ] **Step 2: Add the intersphinx mapping** immediately after `autodoc_typehints = 'signature'` (line 75). Only the four inventories that resolve real references — asyncssh/click pulled zero, so their refs are de-linked in Task 5 instead:

```python
autodoc_typehints = 'signature'

# -- intersphinx --------------------------------------------------------------
# Resolve stdlib + third-party type targets so nitpicky can follow them.
intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
    'typer': ('https://typer.tiangolo.com', None),
    'rich': ('https://rich.readthedocs.io/en/stable', None),
    'pydantic': ('https://docs.pydantic.dev/latest', None),
}
```

- [ ] **Step 3: Delete the dead `_PydanticDataclassTypehintFilter` block** (conf.py lines ~77-103, the whole comment + `import logging as _logging` + class + `getLogger(...).addFilter(...)`). It exists only to suppress `sphinx_autodoc_typehints` warnings; native autodoc never hits the pydantic `_typeshed` path (verified: zero pydantic/forward-ref warnings under native). Remove from the `autodoc_typehints` line down to just before the `# -- napoleon` header.

- [ ] **Step 4: Remove the dependency** from `pyproject.toml` (delete the `"sphinx-autodoc-typehints>=2.0",` line).

- [ ] **Step 5: Verify the non-nitpicky `-W` build still passes** (no regression to the current gate):

Run: `make docs-html`
Expected: `build succeeded` with exit 0 (warnings are nitpick-only and only fire under `-n`, so the non-nitpicky build is clean).

- [ ] **Step 6: Probe nitpicky and confirm the artifact is gone**

Run: `uv run --no-sync sphinx-build -n -b html docs/ "$SCRATCH/probe" 2>&1 | grep -c -- "--is-rst--"`
Expected: `0` (was 441).
Run: `uv run --no-sync sphinx-build -n -b html docs/ "$SCRATCH/probe" 2>&1 | grep -c "reference target not found"`
Expected: ~250 (down from 682), all now genuine resolvable/qualifiable refs.

- [ ] **Step 7: Spot-check signature rendering improved** (types now present):

Run: `python3 -c "import re,html; t=open('$SCRATCH/probe/api/utils.html').read(); m=re.search(r'id=\"otto.utils.split_on_commas\".*?</dt>',t,re.S); print(re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]+>',' ',m.group(0)))))"`
Expected: shows `split_on_commas ( values : list [ str ] | str ) → list [ str ]` (types present, not stripped).

- [ ] **Step 8: Stage**

```bash
git add docs/conf.py pyproject.toml
```

---

### Task 2: Document the undocumented-but-referenced public modules

**Files:**
- Create: `docs/api/models/index.rst`, `docs/api/models/base.rst`, `docs/api/models/host.rst`, `docs/api/models/settings.rst`, `docs/api/models/monitor.rst`
- Create: `docs/api/context.rst`
- Create: `docs/api/host/product.rst`, `docs/api/host/power.rst`, `docs/api/host/privilege.rst`, `docs/api/host/file_ops.rst`, `docs/api/host/binary_loader.rst`
- Modify: `docs/api/index.rst` (add `models/index`, `context`), `docs/api/host/index.rst` (add the 5 host pages)

**Interfaces:**
- Consumes: nothing.
- Produces: `py:class`/`py:func` targets for `otto.context.OttoContext`, `otto.models.*` (HostSpec, EmbeddedHostSpec, OttoModel, MetricPoint, settings), `otto.host.product.Product`, `otto.host.power.PowerController`, `otto.host.privilege.PosixPrivilege`, `otto.host.file_ops.PosixFileOps`, `otto.host.binary_loader.BinaryLoader`/`register_binary_loader`. Task 4's qualified refs depend on these existing.

**Why:** These public modules (Pydantic Phase A models, WS#1 OttoContext, host-ergonomics product/power/privilege/file_ops/binary_loader) ship in code but are absent from the API toctree — referencing them is unresolvable. Documenting is the issue-aligned fix (catch the rot), not de-linking. Pages follow the existing one-line `automodule` pattern.

- [ ] **Step 1: Create each page** using the established pattern (see `docs/api/host/host.rst`). Example `docs/api/host/power.rst`:

```rst
host.power
==========

.. automodule:: otto.host.power
```

Repeat for each module path: `otto.host.product`, `otto.host.privilege`, `otto.host.file_ops`, `otto.host.binary_loader`, `otto.context`, `otto.models.base`, `otto.models.host`, `otto.models.settings`, `otto.models.monitor`. Title = the page heading (e.g. `models.host`), underline length matching.

- [ ] **Step 2: Create `docs/api/models/index.rst`**:

```rst
models
======

Pydantic data models for hosts, settings, and monitor records.

.. toctree::

   base
   host
   settings
   monitor
```

- [ ] **Step 3: Wire into toctrees.** Add `models/index` and `context` to `docs/api/index.rst`'s `.. toctree::`. Add `product`, `power`, `privilege`, `file_ops`, `binary_loader` to `docs/api/host/index.rst`'s `.. toctree::`.

- [ ] **Step 4: Verify the new pages build and resolve their targets**

Run: `uv run --no-sync sphinx-build -n -b html docs/ "$SCRATCH/probe" 2>&1 | grep -E "OttoContext|HostSpec|PowerController|PosixPrivilege|PosixFileOps|BinaryLoader|otto.host.product"`
Expected: those fully-qualified targets no longer appear as "not found". NOTE: newly-documented modules may surface NEW short-name refs from their own docstrings — that is expected convergence; those are handled in Tasks 4/5.

- [ ] **Step 5: Re-measure the remaining set** (new baseline for Tasks 4/5):

Run: `uv run --no-sync sphinx-build -n -b html docs/ "$SCRATCH/probe" 2>&1 | grep "reference target not found" | grep -oP "not found: \K[^ ]+" | sort | uniq -c | sort -rn`

- [ ] **Step 6: Stage**

```bash
git add docs/api/
```

---

### Task 3: Modernize `Optional[X]` → `X | None` and fix the `types.Annotated` typo

**Files:**
- Modify: ~96 sites across `src/otto/**` containing `Optional[` (15 modules import it). Find with `grep -rln "Optional\[" src/`.
- Modify: the `types.Annotated` reference site (should be `typing.Annotated`). Find with `grep -rn "types.Annotated" src/ docs/`.

**Interfaces:**
- Consumes: nothing. Behavior-neutral type-annotation rewrite.
- Produces: no deprecated `Optional`/`typing.Optional` forms remain in `src/`.

**Why:** Issue item 3 (hygiene). Not load-bearing for nitpicky (native autodoc already renders `Optional[X]` as `X | None`), but the issue requests it and Chris opted in. `List`/`Dict`/`Tuple` forms are already gone (0 in src).

- [ ] **Step 1: Per module, rewrite annotations.** `Optional[X]` → `X | None`. Where a module used `Optional` only for this, drop it from the `from typing import ...` line. Ensure `from __future__ import annotations` is present in any module where a `|` union appears in a runtime-evaluated position (most otto modules already have it; verify per file). Work module-by-module so each diff is reviewable.

- [ ] **Step 2: Fix the typo** — change `types.Annotated` to `typing.Annotated` at the reference site.

- [ ] **Step 3: Verify typecheck clean** (the real test for a type-only change):

Run: `make typecheck`
Expected: ty reports no new errors.

- [ ] **Step 4: Verify no deprecated forms remain**

Run: `grep -rn "Optional\[" src/`
Expected: empty.

- [ ] **Step 5: Verify tests still pass** (behavior-neutral):

Run: `make coverage`
Expected: green (≈91%+ per current baseline).

- [ ] **Step 6: Stage**

```bash
git add src/
```

---

> **REVISION 2026-06-25 — Tasks 4–6 below are SUPERSEDED by the strategy in this block.** Execution discovered that ~73 of the remaining refs are *signature-derived* stdlib/third-party type annotations (`Path`, `datetime`, `SSHClientConnection`, `_pytest.*`, `rich.Panel`) that render unresolved in the signature itself — docstring edits can't reach them, and `autodoc_typehints_format='fully-qualified'`/`autodoc_type_aliases` did not fix them. The validated fix (Chris's call) is to standardize `from __future__ import annotations`, which makes autodoc emit qualified targets that intersphinx resolves. Revised task order:
>
> - **Task 4 (revised, 2026-06-25 #2) — conf.py resolver + exclude `model_config`:** the `from __future__` idea was WRONG (it is the *cause*: it stringifies annotations so autodoc emits unresolvable short targets; verified). Chris's call: resolve the ~52 signature-derived stdlib/3rd-party refs with a `missing-reference` event handler in conf.py that maps curated EXTERNAL short names (`Path`→`pathlib.Path`, `datetime`→`datetime.datetime`, `timedelta`, `re.Pattern`, `Panel`→`rich.panel.Panel`, `Progress`→`rich.progress.Progress`, `types.Annotated`→`typing.Annotated`) to qualified intersphinx targets and re-dispatches via `sphinx.ext.intersphinx.missing_reference` — producing real clickable links (NOT a silence; functionally distinct from `nitpick_ignore`). It is a reliable stand-in for `autodoc_type_aliases` (which is buggy under postponed evaluation). Also add `'exclude-members': 'model_config'` to `autodoc_default_options` (drops ~25 pydantic-boilerplate `ConfigDict` refs). Add an intersphinx inventory for any external lib whose public type the resolver maps and that isn't yet mapped (e.g. try asyncssh/pytest for `SSHClientConnection`/`_pytest.*`→`pytest.*`); REPORT any external type that still won't resolve (those get de-linked/excluded in Task 5). Config-only, no source/runtime change. Verify the non-nitpicky `-W` build stays green and re-measure.
> - **Task 5 (revised) — Resolve remaining docstring references to zero:** (a) add `'exclude-members': 'model_config'` to `autodoc_default_options` (drops the ~26 pydantic-boilerplate `ConfigDict` refs); (b) qualify internal short-name refs using the name→path map below (`:class:`UnixHost`` → `:class:`~otto.host.unix_host.UnixHost``); (c) de-link private members / TypeVars to ``` ``literal`` ```; (d) for third-party not resolvable via an inventory (asyncssh `SSHClientConnection`/`SFTPClient`, `aioftp.Client`, `telnetlib3.*`, `_pytest.*`): qualify if an inventory entry exists (`Panel`→`~rich.panel.Panel`), else de-link, else exclude the member (e.g. pytest plugin hooks) — report any you can't cleanly resolve. Work per package; drive the package's bucket to 0.
> - **Task 6 — Enable nitpicky + gate:** unchanged, except enforce nitpicky via `nitpicky = True` in `conf.py` only (it is authoritative; the `-W` gate already runs through conf.py) — **do NOT edit the Makefile** (resolves the Step-2 hedge).
>
> The name→path map and de-link target lists in the (superseded) Task 4/5 text below remain the reference data for Task 5(b)/(c)/(d).

### Task 4: Qualify internal short-name cross-references in docstrings

**Files:**
- Modify: docstrings across `src/otto/**` that use bare `:class:`/`:func:`/`:meth:`/`:attr:` roles. Partition by package for parallel work: `configmodule`, `host`, `monitor`, `coverage`, `cli`, `suite`, `reservations`, `storage`, `models`.

**Interfaces:**
- Consumes: documented targets from Task 2.
- Produces: every internal short-name ref resolves.

**Transformation rule:** `:class:`UnixHost`` → `:class:`~otto.host.unix_host.UnixHost`` (the `~` keeps the displayed text short). Bare `Path` in docstrings → `:class:`~pathlib.Path`` (or ``` ``Path`` ``` if it's prose, not a real reference). Method refs like `run` → `:meth:`~otto.host.unix_host.UnixHost.run`` when they mean a specific class method; attribute refs like `RemoteHost.interfaces` → `:attr:`~otto.host.remote_host.RemoteHost.interfaces``.

**Name → qualified path map** (generated from `src/`; use these exact targets):

```
UnixHost            -> otto.host.unix_host.UnixHost
Host                -> otto.host.host.Host
RemoteHost          -> otto.host.remote_host.RemoteHost
LocalHost           -> otto.host.local_host.LocalHost
EmbeddedHost        -> otto.host.embedded_host.EmbeddedHost
ZephyrHost          -> otto.host.embedded_host.ZephyrHost
DockerContainerHost -> otto.host.docker_host.DockerContainerHost
HostSession         -> otto.host.session.HostSession
ConnectionManager   -> otto.host.connections.ConnectionManager
PowerController      -> otto.host.power.PowerController
Product             -> otto.host.product.Product
Toolchain           -> otto.host.toolchain.Toolchain
CommandFrame        -> otto.host.command_frame.CommandFrame
BinaryLoader        -> otto.host.binary_loader.BinaryLoader
NoFileSystem        -> otto.host.embedded_filesystem.NoFileSystem
LittleFsFileSystem  -> otto.host.embedded_filesystem.LittleFsFileSystem
FatRamFileSystem    -> otto.host.embedded_filesystem.FatRamFileSystem
RunResult           -> otto.host.host.RunResult
ShellCommand        -> otto.host.host.ShellCommand
CoverageStore       -> otto.coverage.store.model.CoverageStore
ReservationBackend  -> otto.reservations.protocol.ReservationBackend
Repo                -> otto.configmodule.repo.Repo
CommandStatus       -> otto.utils.CommandStatus
Status              -> otto.utils.Status
OttoContext         -> otto.context.OttoContext
OttoModel           -> otto.models.base.OttoModel
HostSpec            -> otto.models.host.HostSpec
EmbeddedHostSpec    -> otto.models.host.EmbeddedHostSpec
MetricPoint         -> otto.models.monitor.MetricPoint
MonitorTarget       -> otto.monitor.collector.MonitorTarget
MetricCollector     -> otto.monitor.collector.MetricCollector
```

Names not in this map (`OsType`, `NcPortStrategy`, `NcListenerCheck`, `TransferProgressHandler`, `TransferProgressFactory`, `TierSpec`, `Origin`, `Expect`, `SFTPClient`) are type-aliases/re-exports/attrs — resolve each case-by-case: link the canonical definition if one exists, otherwise de-link to a literal (Task 5 rule).

- [ ] **Step 1: For one package at a time**, run the detection command filtered to that package's source paths, and for each `bare-name` warning apply the qualification rule using the map above. Use the warning's `file:line` to locate the exact docstring.

- [ ] **Step 2: Verify the package's bucket is empty** after edits, e.g. for `host`:

Run: `uv run --no-sync sphinx-build -n -b html docs/ "$SCRATCH/probe" 2>&1 | grep "reference target not found" | grep "src/otto/host/"`
Expected: empty for the package just completed.

- [ ] **Step 3: Stage after each package**

```bash
git add src/otto/<package>/
```

---

### Task 5: De-link private members, TypeVars, and unresolvable third-party references

**Files:**
- Modify: docstrings across `src/otto/**` referencing private members, TypeVars, or third-party internals.

**Interfaces:**
- Consumes: nothing.
- Produces: zero remaining nitpick warnings (combined with Tasks 1-4).

**Transformation rule:** turn the cross-reference into an inline literal — `:func:`_apply_option_overrides`` → ``` ``_apply_option_overrides`` ```. This satisfies "private members remain undocumented; any doc that referenced one is fixed to not link it."

- [ ] **Step 1: De-link private members.** Targets observed: `_apply_option_overrides`, `_FRAME_CLASSES`, `_FILESYSTEM_CLASSES`, `_elevate`, `_warmup_for_transfer`, `_soft_reboot`, `_interact`, `__post_init__`, `RemoteHost._build_hop_transport`, `otto.host.session.ShellSession._ensure_initialized`, `otto.host.binary_loader.BinaryLoader.is_fully_unloaded`. Convert each to an inline literal. (Re-run detection for the authoritative current list.)

- [ ] **Step 2: De-link TypeVars** `T`, `R`, `P`, and the generic `otto.suite.suite.TOptions` if it is a TypeVar — TypeVars have no documentable target; render as ``` ``T`` ```.

- [ ] **Step 3: Resolve / de-link third-party internals not in any public inventory.** `asyncssh.connection.SSHClientConnection`, `SFTPClient`, `aioftp.Client`, `telnetlib3.open_connection`, `_pytest.nodes.Item`, `_pytest.config.Config`, `_pytest.reports.TestReport`, `_pytest.stash.StashKey`, `_pytest.runner.CallInfo`, `_pytest.main.Session`, bare `Panel`/`Plotly`. For each: if a public inventory entry exists (e.g. bare `Panel` → `:class:`~rich.panel.Panel``, `ConfigDict` → `:class:`~pydantic.ConfigDict``), qualify it; otherwise de-link to a literal. The `_pytest.*` names are pytest internals leaking from test-helper signatures — de-link (do not add a pytest inventory; it documents the public `pytest.*` API only).

- [ ] **Step 4: Sweep prose-noise false targets.** A few warnings have targets like `the`, `For`, `get`, `put`, `repo1's` — these are malformed inline-role usages in docstrings (e.g. `:func:`get`` meant as a word). Fix the markup so they are plain text.

- [ ] **Step 5: Verify ZERO remaining**

Run: `uv run --no-sync sphinx-build -n -b html docs/ "$SCRATCH/probe" 2>&1 | grep -c "reference target not found"`
Expected: `0`.

- [ ] **Step 6: Stage**

```bash
git add src/
```

---

### Task 6: Enable `nitpicky`, final clean build, full gate

**Files:**
- Modify: `docs/conf.py` (add `nitpicky = True`)

- [ ] **Step 1: Enable nitpicky** in `docs/conf.py` (near the top config block, after `version = release`):

```python
nitpicky = True
```

- [ ] **Step 2: Add `-n` to the Sphinx build invocations** in the `Makefile` so the gate enforces it. Update `docs-html` (line ~262) and `doctest` (line ~265) targets to include `-n`:

```make
docs/_build/html/index.html: $(SPHINX_SRCS)
	uv run sphinx-build -E -a -n -W -b html docs/ docs/_build/html
```

(With `nitpicky = True` in conf.py the `-n` flag is redundant but explicit; keeping conf.py authoritative is sufficient — apply whichever the reviewer prefers, but ensure the committed gate is nitpicky.)

- [ ] **Step 3: Confirm zero ignores** (acceptance):

Run: `grep -rn "nitpick_ignore" docs/`
Expected: empty.

- [ ] **Step 4: Full clean `make docs`** (docs-lint + docs-html + doctest + doctest-src):

Run: `make docs`
Expected: exit 0, `build succeeded` with **0 warnings**.

- [ ] **Step 5: Full local gate** (per project gate targets; live `make nox` is Chris's to run on the dev VM):

Run: `make typecheck && make coverage`
Expected: ty clean; coverage green.

- [ ] **Step 6: Stage everything for Chris's commit** (do NOT commit):

```bash
git add docs/ pyproject.toml src/ Makefile
git status
```

Report the staged tree and the suggested commit message to Chris.

---

## Self-Review notes

- **Spec coverage:** item 1 (intersphinx) → Task 1; item 2 (resolve typehints artifact, no regression) → Task 1 (drop extension, verified zero regression across 1018 objects); item 3 (modernize hints) → Task 3; item 4 (enable nitpicky, zero-ignore clean build) → Task 6. Acceptance "private members undocumented / de-linked" → Task 5. The undocumented-public-module gap (not in the original issue buckets but blocking zero-ignore) → Task 2.
- **Ordering:** Task 2 before 4 (qualified refs need their targets documented). Task 3 is independent. nitpicky committed only in Task 6 so intermediate `make docs` stays green.
- **Convergence:** the live `sphinx-build -nW` warning list is the source of truth; the embedded counts/targets are the 2026-06-24 snapshot and shrink as tasks land — always re-run detection for the current set.
