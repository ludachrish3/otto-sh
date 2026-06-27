# Adopt ty's `missing-override-decorator` Rule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-enable ty's `missing-override-decorator` rule at `error` by decorating every overriding method in `src/otto/` with `@override` (imported from `typing_extensions`), and removing the rule's `"ignore"` demotion from `pyproject.toml`.

**Architecture:** Purely mechanical, type-checker-driven sweep. The rule is *already* enabled by `[tool.ty.rules] all = "error"` but currently demoted to `"ignore"`. While demoted, `ty check` stays green, so each file-group can be swept and committed incrementally without breaking the gate. The verification oracle for each task is `ty check --error missing-override-decorator <paths>` (CLI override that re-enables the rule without editing config), driven to zero violations. The final task removes the `"ignore"` line so the rule is enforced permanently. `typing_extensions` is added as a direct dependency (it is already resolved transitively at v4.15.0, so this is a zero-footprint declaration of something we now import directly).

**Tech Stack:** Python 3.10+ (floor), `typing_extensions.override` (PEP 698), `ty` type checker, `ruff` (import sorting), `uv` (dependency + lock management), `nox`/`make` gates.

**Resolves:** GitHub issue #55. Note the issue's scope table is stale (written against an older tree): it lists **82 sites / 14 files** with `camelCase` filenames. The live count is **119 sites / 25 files** after the naming sweep, transfer-package split, and later workstreams. Always regenerate the live list (Step 1 of each sweep task) rather than trusting the issue's numbers.

## Global Constraints

- **Python floor:** `requires-python = ">=3.10"`. `typing.override` is 3.12+, so `override` MUST come from `typing_extensions`. Do not add a `sys.version_info` guard — import `typing_extensions` unconditionally (decision recorded for this work).
- **No `from __future__ import annotations`:** banned in this repo — it trips the Sphinx nitpicky (`-W`) docs gate with spurious unresolved-xref warnings. Do not add it to any file.
- **`@override` is a RUNTIME decorator:** it must be a normal top-level import, never placed inside a `if TYPE_CHECKING:` block.
- **`@override` placement rule (uniform):** `@override` is always the **topmost / outermost** decorator. For a bare method it sits immediately above `def`. When other decorators are present (`@cli_exposed(...)`, `@classmethod`, `@property`), `@override` goes **above** them.
- **Line length:** 120 (`.ruff.toml`). Import sorting is enforced by ruff rule `I`; new imports must land in the correct isort group (`typing_extensions` is third-party).
- **Annotations:** use real 3.10+ annotations + module-top imports for any new code (none expected here beyond the import line).
- **Gate commands:** scoped type check = `uv run --no-sync ty check --error missing-override-decorator <paths>`; full type gate = `make typecheck` (`uv run ty check`); affected-module smoke = `uv run --no-sync pytest <paths> -m "not integration and not embedded" -p no:cacheprovider` (the `--doctest-modules` addopt imports each module at collection, so a broken import/decorator fails fast).

## Live violation baseline (regenerate before starting)

```bash
uv run --no-sync ty check --error missing-override-decorator --output-format concise 2>&1 \
  | grep missing-override-decorator
```

Expected per-file counts at time of writing (119 total across 25 files):

| File | Count | Notes |
|------|------:|-------|
| `src/otto/host/command_frame.py` | 15 | plain methods |
| `src/otto/host/unix_host.py` | 14 | incl. 3 `@cli_exposed` (`get`, `put`, `shutdown`) |
| `src/otto/host/session.py` | 14 | plain methods |
| `src/otto/host/local_host.py` | 11 | incl. 2 `@cli_exposed` (`get`, `put`) |
| `src/otto/host/embedded_host.py` | 11 | incl. 2 `@cli_exposed` (`get`, `put`) |
| `src/otto/host/docker_host.py` | 9 | incl. 2 `@cli_exposed` (`get`, `put`) |
| `src/otto/monitor/parsers.py` | 5 | incl. 1 `@property` (`TopCpuParser.command`) |
| `src/otto/host/binary_loader.py` | 4 | plain methods |
| `src/otto/host/transfer/nc.py` | 4 | incl. 1 `@classmethod` (`create`) |
| `src/otto/host/transfer/tftp.py` | 3 | incl. 1 `@classmethod` (`create`) |
| `src/otto/host/transfer/sftp.py` | 3 | incl. 1 `@classmethod` (`create`) |
| `src/otto/host/transfer/scp.py` | 3 | incl. 1 `@classmethod` (`create`) |
| `src/otto/host/transfer/ftp.py` | 3 | incl. 1 `@classmethod` (`create`) |
| `src/otto/host/transfer/console.py` | 3 | incl. 1 `@classmethod` (`create`) |
| `src/otto/host/power.py` | 3 | plain methods |
| `src/otto/models/host.py` | 2 | plain methods (pydantic model) |
| `src/otto/logger/formatters.py` | 2 | plain methods |
| `src/otto/host/connections.py` | 2 | plain methods |
| `src/otto/cli/expose.py` | 2 | plain methods (`TyperGroup.list_commands`, `get_command`) |
| `src/otto/monitor/server.py` | 1 | plain method |
| `src/otto/host/transfer/embedded_base.py` | 1 | `@classmethod` (`create`) |
| `src/otto/host/remote_host.py` | 1 | plain method |
| `src/otto/host/product.py` | 1 | plain method |
| `src/otto/host/host.py` | 1 | plain method |
| `src/otto/configmodule/version.py` | 1 | plain method (`__repr__`) |

**Decorator-stacking sites that need the placement rule (17 total: 9 `@cli_exposed` + 7 `@classmethod` + 1 `@property`):**
- `@cli_exposed` (9): `unix_host.get/put/shutdown`, `local_host.get/put`, `embedded_host.get/put`, `docker_host.get/put`. `@override` goes **above** `@cli_exposed`.
- `@classmethod create(cls, ...)` (7): `transfer/{console,embedded_base,ftp,nc,scp,sftp,tftp}.py`. `@override` goes **above** `@classmethod`.
- `@property` (1): `monitor/parsers.py` → `TopCpuParser.command`. `@override` goes **above** `@property`. Leave the existing `# type: ignore[override]` comments untouched (they are mypy/pyright-oriented; ty does not honor them, has no `invalid-override` rule, and removing them is out of scope).

All other sites are plain methods: `@override` immediately above `def` / `async def`.

---

### Task 1: Add `typing_extensions` as a direct dependency

**Files:**
- Modify: `pyproject.toml` (`[project.dependencies]` list)
- Modify: `uv.lock` (regenerated by `uv lock`)

**Interfaces:**
- Produces: a guaranteed-present `typing_extensions` import surface for all later tasks: `from typing_extensions import override`.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, inside the `[project] dependencies = [ ... ]` array, add an entry in alphabetical position (after `telnetlib3`, before `tomli` — i.e. keep the list ordered):

```toml
    "telnetlib3>=4.0.1",
    # `override` (PEP 698) lives in `typing` only from 3.12; our floor is 3.10,
    # so it is imported from typing_extensions. Already resolved transitively
    # (pydantic/typer); declared directly because we now import it directly.
    "typing_extensions>=4.12.0",
    "tomli>=2.4.0",
```

(Adjust ordering to match the existing list's convention; the comment placement is what matters.)

- [ ] **Step 2: Regenerate the lock without dirtying via `uv run`**

Run: `uv lock`
Expected: success; the `uv.lock` diff adds `typing-extensions` to `otto-sh`'s direct dependency list. The **resolved version must stay `4.15.0`** (it was already present transitively) — confirm no other package versions move.

Verify the diff is minimal:

```bash
git diff uv.lock | grep -E "^\+|^-" | grep -iv "typing-extensions" | grep -vE "^(\+\+\+|---)" | head
```

Expected: no unrelated version changes (only the `otto-sh` dependency-list addition).

- [ ] **Step 3: Materialize the environment**

Run: `uv sync`
Expected: success; environment unchanged except the now-direct `typing-extensions`.

- [ ] **Step 4: Smoke-test the import surface**

Run: `uv run --no-sync python -c "from typing_extensions import override; print('ok', override.__name__)"`
Expected: `ok override`

- [ ] **Step 5: Confirm the type gate is still green (rule still demoted)**

Run: `make typecheck`
Expected: PASS (the `missing-override-decorator` rule is still `"ignore"`, so no override violations yet).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: declare typing_extensions as a direct dependency

Prep for adopting ty's missing-override-decorator rule (#55): override
(PEP 698) is typing-only from 3.12 and our floor is 3.10. typing_extensions
is already resolved transitively, so this only makes the direct import
explicit."
```

> NOTE: This repo does not self-commit via the agent (prepare-commit-msg hook needs /dev/tty). If running interactively, hand the commit message to the maintainer instead of committing.

---

### Task 2: Sweep `src/otto/host/` core files

**Files (modify each; add the import + decorate every overriding method):**
- `src/otto/host/command_frame.py` (15)
- `src/otto/host/session.py` (14)
- `src/otto/host/unix_host.py` (14 — incl. `@cli_exposed`: `get`, `put`, `shutdown`)
- `src/otto/host/local_host.py` (11 — incl. `@cli_exposed`: `get`, `put`)
- `src/otto/host/embedded_host.py` (11 — incl. `@cli_exposed`: `get`, `put`)
- `src/otto/host/docker_host.py` (9 — incl. `@cli_exposed`: `get`, `put`)
- `src/otto/host/binary_loader.py` (4)
- `src/otto/host/power.py` (3)
- `src/otto/host/connections.py` (2)
- `src/otto/host/remote_host.py` (1)
- `src/otto/host/product.py` (1)
- `src/otto/host/host.py` (1)

**Interfaces:**
- Consumes: `from typing_extensions import override` (Task 1).
- Produces: zero `missing-override-decorator` violations in `src/otto/host/*.py` (excluding the `transfer/` package, which is Task 3).

- [ ] **Step 1: Get the live per-file site list for this group**

```bash
uv run --no-sync ty check --error missing-override-decorator --output-format concise 2>&1 \
  | grep -E "src/otto/host/[a-z_]+\.py:" | grep -v "src/otto/host/transfer/"
```

Expected: a list of `file:line:col: error[missing-override-decorator] Method X overrides Y...`. Use it as the worklist.

- [ ] **Step 2: Add the runtime import to each file in the group**

For every file that does not already import it, add (top-level, in the third-party import group; do not place in a `TYPE_CHECKING` block):

```python
from typing_extensions import override
```

- [ ] **Step 3: Decorate each overriding method per the placement rule**

Plain method — `@override` immediately above `def`/`async def`:

```python
    @override
    def handshake(self) -> bytes:
        ...
```

`@cli_exposed(...)` site — `@override` **above** `@cli_exposed` (confirmed safe: `cli_exposed` in `src/otto/utils.py:118` is a pass-through marker that returns the same function):

```python
    @override
    @cli_exposed(success="Transfer complete.")
    async def put(self, ...):
        ...
```

`@cli_exposed` bare (e.g. `unix_host.shutdown`):

```python
    @override
    @cli_exposed
    async def shutdown(self) -> tuple[Status, str]:
        ...
```

- [ ] **Step 4: Sort imports for the group**

Run: `uv run --no-sync ruff check --select I --fix src/otto/host/command_frame.py src/otto/host/session.py src/otto/host/unix_host.py src/otto/host/local_host.py src/otto/host/embedded_host.py src/otto/host/docker_host.py src/otto/host/binary_loader.py src/otto/host/power.py src/otto/host/connections.py src/otto/host/remote_host.py src/otto/host/product.py src/otto/host/host.py`
Expected: import statements reordered if needed; no other changes.

- [ ] **Step 5: Verify zero remaining violations in the group**

```bash
uv run --no-sync ty check --error missing-override-decorator --output-format concise 2>&1 \
  | grep -E "src/otto/host/[a-z_]+\.py:" | grep -v "src/otto/host/transfer/" | wc -l
```

Expected: `0`

- [ ] **Step 6: Smoke-test that the modules still import + decorate cleanly**

Run: `uv run --no-sync pytest tests/unit/host -m "not integration and not embedded" -p no:cacheprovider -q`
Expected: PASS (no collection/import errors; the decorator is a runtime no-op for behavior).

- [ ] **Step 7: Commit**

```bash
git add src/otto/host/command_frame.py src/otto/host/session.py src/otto/host/unix_host.py src/otto/host/local_host.py src/otto/host/embedded_host.py src/otto/host/docker_host.py src/otto/host/binary_loader.py src/otto/host/power.py src/otto/host/connections.py src/otto/host/remote_host.py src/otto/host/product.py src/otto/host/host.py
git commit -m "style(host): add @override to host core overriding methods (#55)"
```

---

### Task 3: Sweep `src/otto/host/transfer/` package

**Files:**
- `src/otto/host/transfer/nc.py` (4 — incl. `@classmethod create`)
- `src/otto/host/transfer/tftp.py` (3 — incl. `@classmethod create`)
- `src/otto/host/transfer/sftp.py` (3 — incl. `@classmethod create`)
- `src/otto/host/transfer/scp.py` (3 — incl. `@classmethod create`)
- `src/otto/host/transfer/ftp.py` (3 — incl. `@classmethod create`)
- `src/otto/host/transfer/console.py` (3 — incl. `@classmethod create`)
- `src/otto/host/transfer/embedded_base.py` (1 — `@classmethod create`)

**Interfaces:**
- Consumes: `from typing_extensions import override` (Task 1).
- Produces: zero `missing-override-decorator` violations under `src/otto/host/transfer/`.

- [ ] **Step 1: Get the live site list for the package**

```bash
uv run --no-sync ty check --error missing-override-decorator --output-format concise 2>&1 \
  | grep "src/otto/host/transfer/"
```

- [ ] **Step 2: Add the runtime import to each transfer file**

```python
from typing_extensions import override
```

- [ ] **Step 3: Decorate. `@classmethod create` site — `@override` ABOVE `@classmethod`:**

```python
    @override
    @classmethod
    def create(cls, ctx: "TransferContext") -> "ScpFileTransfer":
        ...
```

Plain methods in these files (e.g. `nc.py` has 3 non-`create` overrides) — `@override` immediately above `def`/`async def`.

- [ ] **Step 4: Sort imports for the package**

Run: `uv run --no-sync ruff check --select I --fix src/otto/host/transfer/`
Expected: imports reordered if needed; no other changes.

- [ ] **Step 5: Verify zero remaining violations in the package**

```bash
uv run --no-sync ty check --error missing-override-decorator --output-format concise 2>&1 \
  | grep -c "src/otto/host/transfer/"
```

Expected: `0`

- [ ] **Step 6: Smoke-test**

Run: `uv run --no-sync pytest tests/unit/host -k "transfer" -m "not integration and not embedded" -p no:cacheprovider -q`
Expected: PASS. (If `-k "transfer"` selects nothing, run the full `tests/unit/host` subset instead.)

- [ ] **Step 7: Commit**

```bash
git add src/otto/host/transfer/
git commit -m "style(host): add @override to transfer backend overrides (#55)"
```

---

### Task 4: Sweep the remaining modules (monitor, logger, models, configmodule, cli)

**Files:**
- `src/otto/monitor/parsers.py` (5 — incl. `@property` `TopCpuParser.command`)
- `src/otto/monitor/server.py` (1)
- `src/otto/logger/formatters.py` (2)
- `src/otto/models/host.py` (2)
- `src/otto/configmodule/version.py` (1 — `__repr__`)
- `src/otto/cli/expose.py` (2 — `list_commands`, `get_command`)

**Interfaces:**
- Consumes: `from typing_extensions import override` (Task 1).
- Produces: zero `missing-override-decorator` violations anywhere in `src/` (this is the last file group).

- [ ] **Step 1: Get the live site list for these files**

```bash
uv run --no-sync ty check --error missing-override-decorator --output-format concise 2>&1 \
  | grep -E "monitor/parsers|monitor/server|logger/formatters|models/host|configmodule/version|cli/expose"
```

- [ ] **Step 2: Add the runtime import to each file**

```python
from typing_extensions import override
```

- [ ] **Step 3: Decorate. The `@property` site in `parsers.py` (`TopCpuParser.command`) — `@override` ABOVE `@property`; leave the `# type: ignore[override]` comments in place:**

```python
    @override
    @property  # type: ignore[override]
    def command(self) -> str:  # type: ignore[override]
        return f'top -d {self._delay} -bn2'
```

All other sites are plain methods (`__repr__`, `list_commands`, `get_command`, etc.) — `@override` immediately above `def`.

- [ ] **Step 4: Sort imports for these files**

Run: `uv run --no-sync ruff check --select I --fix src/otto/monitor/parsers.py src/otto/monitor/server.py src/otto/logger/formatters.py src/otto/models/host.py src/otto/configmodule/version.py src/otto/cli/expose.py`
Expected: imports reordered if needed; no other changes.

- [ ] **Step 5: Verify zero remaining violations across ALL of `src/`**

```bash
uv run --no-sync ty check --error missing-override-decorator --output-format concise 2>&1 \
  | grep -c missing-override-decorator
```

Expected: `0` (every site is now decorated).

- [ ] **Step 6: Smoke-test the affected packages**

Run: `uv run --no-sync pytest tests/unit/monitor tests/unit/logger tests/unit -k "version or expose or models" -m "not integration and not embedded" -p no:cacheprovider -q`
Expected: PASS. (Fall back to `uv run --no-sync pytest tests/unit -m "not integration and not embedded" -p no:cacheprovider -q` if the `-k` filter is awkward.)

- [ ] **Step 7: Commit**

```bash
git add src/otto/monitor/parsers.py src/otto/monitor/server.py src/otto/logger/formatters.py src/otto/models/host.py src/otto/configmodule/version.py src/otto/cli/expose.py
git commit -m "style(monitor,logger,models,cli): add @override to remaining overrides (#55)"
```

---

### Task 5: Re-enable the rule and run the full gate

**Files:**
- Modify: `pyproject.toml` (`[tool.ty.rules]` — remove the demotion line + comment)

**Interfaces:**
- Consumes: zero remaining violations (Tasks 2–4).
- Produces: `missing-override-decorator` enforced at `error` permanently (via `all = "error"`).

- [ ] **Step 1: Remove the demotion**

In `pyproject.toml`, delete the `missing-override-decorator = "ignore"` line **and** its preceding comment block. The `[tool.ty.rules]` table should collapse to just:

```toml
[tool.ty.rules]
all = "error"
```

(Remove the 5-line `# Demoted: ...` comment that references the GitHub issue, since the issue is now resolved.)

- [ ] **Step 2: Confirm the rule is now enforced by the default gate**

Run: `make typecheck`
Expected: PASS — `ty check` (no `--error` override) now enforces `missing-override-decorator` at `error` and finds zero violations. If it FAILS, a site was missed; re-run the live list from Task 4 Step 5 and fix.

- [ ] **Step 3: Run the full type gate exactly as the issue's acceptance criteria specify**

Run: `uv run nox -s typecheck`
Expected: PASS. (This is the literal acceptance command from issue #55. It uses `uv run nox`, which is the documented gate invocation; if it dirties `uv.lock`, restore with `git checkout uv.lock` — the lock was already finalized in Task 1.)

- [ ] **Step 4: Run the unit suite to confirm no runtime regression from the decorators/imports**

Run: `make coverage`
Expected: PASS with the coverage gate satisfied (the `@override` decorator and `typing_extensions` import are runtime no-ops for behavior; this confirms every touched module still imports and all tests pass).

- [ ] **Step 5: Confirm the change is lint-clean (so it adds nothing to the ruff debt)**

Run: `uv run nox -s lint`
Expected: ruff `check` + `format --check` report **no new violations introduced by this change**. (Pre-existing debt elsewhere may still report — confirm the new `from typing_extensions import override` lines and `@override` decorators are not among the reported items. If `format --check` flags a touched file, run `uv run --no-sync ruff format <file>` and re-commit.)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml
git commit -m "build(ty): enforce missing-override-decorator at error (#55)

All 119 overriding methods in src/otto are now decorated with @override;
remove the rule's ignore demotion so all = \"error\" enforces it. Closes #55."
```

---

## Self-Review notes

- **Spec coverage (issue #55 acceptance criteria):**
  - ☑ "Add `typing_extensions` dependency and import `override`" → Task 1 + the per-file import steps (Tasks 2–4). Unconditional import (no version guard) per the recorded decision.
  - ☑ "Decorate all overriding methods with `@override`" → Tasks 2–4 cover all 119 live sites (regenerated, not the stale 82).
  - ☑ "Remove the `missing-override-decorator = "ignore"` line (and its comment)" → Task 5 Step 1.
  - ☑ "`uv run nox -s typecheck` passes with the rule back at `error`" → Task 5 Step 3.
- **Drift guard:** every sweep task regenerates the live violation list first (Step 1), so the plan survives further code drift between writing and execution. The per-file table is a baseline, not a hardcoded worklist.
- **Edge cases enumerated, not hand-waved:** `@cli_exposed` (above), `@classmethod` (above), `@property` (above, keep `# type: ignore` comments). No "handle other decorators appropriately" placeholders.
- **Why no new pytest tests:** the rule is enforced by the type checker, not runtime behavior; `ty check` is the oracle and the existing suite (run via `--doctest-modules` collection + `make coverage`) guards against import/decoration regressions. Adding bespoke unit tests would violate YAGNI.
- **Relationship to the broader ruff effort:** intentionally kept separate. `missing-override-decorator` is a *type-checker* rule (ty resolves base classes; ruff is AST-only and cannot enforce it), so it stays in the `typecheck` gate and never moves to ruff. This plan deliberately lands lint-clean (Task 5 Step 5) so it adds nothing to the ruff debt that the separate, rule-by-rule ruff sweep will burn down.
