# Coverage Manual-Testing Overrides Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A commented TOML file (`.otto/coverage-overrides.toml`) that (a) declares tickets/commits as manually tested so their attributed lines count toward a named manual tier — visibly marked as "asserted", never confusable with recorded runs — and (b) break-glass reattributes a commit's ticket ids everywhere.

**Architecture:** Overrides load and validate loud at settings-resolve time (`coverage/overrides.py`). Reattribution replaces the parsed ticket set at the single extraction site inside `attribute_tickets`. Asserted entries resolve to line sets against the existing attribution outputs (line→sha map + ticket→commits map), bounded by an `as_of` sha via one `git rev-list --first-parent` order, and add hits + provenance refs to the store (v6). The SPA and `tickets.json` read the new store fields; a "hide asserted" ⋮-menu toggle subtracts Python-precomputed asserted counts client-side.

**Tech Stack:** Python 3.12 (dataclasses, tomllib, pydantic settings specs), git plumbing via `capture/gitio.py`, React/TypeScript covapp (wouter, vendored Untitled UI dropdown), pytest + Playwright + vitest.

**Spec:** `docs/superpowers/specs/2026-07-30-coverage-manual-overrides-design.md` — read it first.

## Global Constraints

- Never `from __future__ import annotations` (breaks Sphinx nitpicky `-W`).
- Public APIs prefer `list` over `tuple`; callables return dataclasses.
- All work happens in a git worktree (superpowers:using-git-worktrees). Fresh worktrees need `uv sync` and `cd web && npm ci` before gates.
- Commit per task, conventional prefix, trailer line: `Assisted-by: Claude Fable 5`.
- Never `git push`.
- Gates: `nox -s lint` (ruff check + format) after every Python task; `nox -s typecheck` after src edits (ty runs ONLY there); `pytest` does NOT build the web dist — run `make web` before any browser test after TS changes.
- Store v5 → v6 is exact-match loud-fail, no migration shim (established policy).
- `OTTO_COV_DATA_FORMAT` (spa_data.py:39) and `EXPECTED_DATA_FORMAT` (web/src/covapp/types.ts:11) bump together 1 → 2, in the same task, or never.
- `TICKET_EXPORT_FORMAT` bumps 1 → 2 (spec §7 ruling: added fields are a shape change).
- Reserved strings: a top-level override table may not be named `reattribute`-as-a-tier; asserted/reattributed ticket ids may not equal `(no ticket)` / `(uncommitted)` (import `NO_TICKET`/`UNCOMMITTED_TICKET` from `otto.coverage.attribution`, don't retype the literals).
- Feature-absent invariant (pinned in Task 7): no override file + no `[coverage.overrides]` key → report byte-identical to today.

## File Structure

| File | Role |
| --- | --- |
| `src/otto/models/settings.py` | +`CoverageOverridesSpec` (`file` key), wired into `CoverageSettingsSpec` |
| `src/otto/coverage/capture/gitio.py` | +`rev_parse_commit`, +`rev_list_first_parent` |
| `src/otto/coverage/overrides.py` (new) | dataclasses, TOML load + all validation, `apply_asserted_entries` (pure, no git) |
| `src/otto/coverage/attribution.py` | `attribute_tickets(..., reattributions=...)` |
| `src/otto/coverage/store/model.py` | v6: `OverrideRecord`, `CoverageStore.overrides`, `LineRecord.asserted` |
| `src/otto/coverage/reporter.py` | `_annotate_tickets` returns attribution products; new `_apply_overrides`; params threaded |
| `src/otto/cli/cov.py`, `src/otto/suite/run.py` | resolve + thread `OverrideConfig` |
| `src/otto/coverage/ticket_export.py` | format 2, per-ticket `asserted`, `overrides_active` |
| `src/otto/coverage/renderer/spa_data.py` | format 2, per-line `asserted`, index `overrides` table, `Stats.lines.asserted_per_tier`, ticket summary/totals `asserted` |
| `web/src/covapp/types.ts`, `format.ts`, `focus.tsx`, `chrome/AppShell.tsx`, `pages/FilePage.tsx`, `pages/TicketsPage.tsx` | marker, expander chip, toggle, badge, recompute |
| `tests/_fixtures/covapp_ticket_contract.json` | +`asserted` keys (both contract suites) |
| `tests/_fixtures/_report_fixture.py`, `tests/e2e/cov/report_browser/test_spa_asserted.py` (new) | browser pins |
| `docs/guide/coverage.md`, `docs/guide/cli-reference.md`, `docs/architecture/subsystems/coverage/attribution.md` | docs |

---

### Task 1: `[coverage.overrides]` settings spec

**Files:**
- Modify: `src/otto/models/settings.py` (after `CoverageTicketsSpec`, ~line 350)
- Test: `tests/unit/models/test_settings.py`

**Interfaces:**
- Produces: `CoverageOverridesSpec` pydantic model with `file: str | None = None`; `CoverageSettingsSpec.overrides: CoverageOverridesSpec | None = None`. Validation-only, like `CoverageTicketsSpec` — runtime re-reads the raw dict (Task 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/models/test_settings.py` (match its existing style for building a minimal settings dict — reuse whatever helper the `[coverage.tickets]` tests there use; if none exist for coverage, construct `CoverageSettingsSpec` directly):

```python
def test_coverage_overrides_block_accepts_file_key():
    spec = CoverageSettingsSpec.model_validate({"overrides": {"file": "custom/overrides.toml"}})
    assert spec.overrides is not None
    assert spec.overrides.file == "custom/overrides.toml"


def test_coverage_overrides_block_defaults_file_to_none():
    spec = CoverageSettingsSpec.model_validate({"overrides": {}})
    assert spec.overrides is not None
    assert spec.overrides.file is None


def test_coverage_overrides_block_absent_is_none():
    spec = CoverageSettingsSpec.model_validate({})
    assert spec.overrides is None


def test_coverage_overrides_unknown_key_fails():
    with pytest.raises(ValidationError):
        CoverageSettingsSpec.model_validate({"overrides": {"path": "x"}})
```

(`OttoModel` forbids extra keys — the last test pins that posture holds here. Import `CoverageSettingsSpec` alongside the module's existing imports; `ValidationError` from pydantic.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/models/test_settings.py -k overrides -v`
Expected: FAIL — `overrides` is not a `CoverageSettingsSpec` field (pydantic raises on the first test).

- [ ] **Step 3: Implement**

In `src/otto/models/settings.py`, after `CoverageTicketsSpec`:

```python
class CoverageOverridesSpec(OttoModel):
    """``[coverage.overrides]`` — manual-testing override file location.

    Validation-only, like the other coverage specs — the runtime value is
    re-read from the raw settings dict by
    ``otto.coverage.overrides.load_override_config``, which parses and
    validates the override file itself.
    """

    file: str | None = None
```

and in `CoverageSettingsSpec` add the field after `tickets`:

```python
    overrides: CoverageOverridesSpec | None = None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/models/test_settings.py -v`
Expected: PASS (all, including pre-existing).

- [ ] **Step 5: Lint + commit**

```bash
nox -s lint
git add src/otto/models/settings.py tests/unit/models/test_settings.py
git commit -m "feat(cov): add [coverage.overrides] settings spec" -m "Assisted-by: Claude Fable 5"
```

---

### Task 2: gitio helpers — sha resolution and first-parent order

**Files:**
- Modify: `src/otto/coverage/capture/gitio.py` (after `head_commit`, ~line 172)
- Test: `tests/unit/cov/test_gitio.py`

**Interfaces:**
- Produces: `rev_parse_commit(repo_root: Path, rev: str) -> str` — full sha for any resolvable commit-ish (abbreviated shas included); raises `GitUnavailableError` if unresolvable/ambiguous. `rev_list_first_parent(repo_root: Path) -> list[str]` — full shas, HEAD first (newest → oldest), first-parent only.

- [ ] **Step 1: Write the failing tests**

`tests/unit/cov/test_gitio.py` already builds throwaway repos — follow its local repo-helper pattern (it has one; if the helper is module-private, replicate the minimal `git init/commit` subprocess calls the way `test_attribution.py`'s `_repo` does):

```python
def test_rev_parse_commit_resolves_abbreviated_sha(tmp_path):
    repo = _make_repo(tmp_path)  # helper: init + one commit, returns (path, full_sha)
    path, full = repo
    assert gitio.rev_parse_commit(path, full[:8]) == full


def test_rev_parse_commit_unresolvable_raises(tmp_path):
    path, _ = _make_repo(tmp_path)
    with pytest.raises(gitio.GitUnavailableError):
        gitio.rev_parse_commit(path, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")


def test_rev_parse_commit_non_commit_raises(tmp_path):
    path, _ = _make_repo(tmp_path)
    with pytest.raises(gitio.GitUnavailableError):
        gitio.rev_parse_commit(path, "not-a-ref")


def test_rev_list_first_parent_newest_first(tmp_path):
    path, first = _make_repo(tmp_path)
    (path / "b.txt").write_text("x")
    second = _commit_all(path)  # helper returning new HEAD sha
    assert gitio.rev_list_first_parent(path) == [second, first]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/cov/test_gitio.py -k "rev_parse or rev_list" -v`
Expected: FAIL with `AttributeError: ... has no attribute 'rev_parse_commit'`.

- [ ] **Step 3: Implement**

In `gitio.py` after `head_commit`:

```python
def rev_parse_commit(repo_root: Path, rev: str) -> str:
    """Resolve *rev* (full/abbreviated sha, ref) to a full commit sha.

    ``^{commit}`` peels tags and rejects non-commit objects; ``--verify``
    makes an unknown or ambiguous *rev* a loud :class:`GitUnavailableError`
    instead of echoing the input back.
    """
    return _run(["rev-parse", "--verify", f"{rev}^{{commit}}"], repo_root).strip()


def rev_list_first_parent(repo_root: Path) -> list[str]:
    """Every first-parent commit sha reachable from HEAD, newest first.

    One process for the whole list; callers index it to answer "is commit A
    at/before commit B on the mainline" without per-pair subprocesses.
    """
    return _run(["rev-list", "--first-parent", "HEAD"], repo_root).split()
```

(`_run` is the module's existing pinned-config runner; both helpers inherit its `GitUnavailableError` behavior.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/cov/test_gitio.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
nox -s lint
git add src/otto/coverage/capture/gitio.py tests/unit/cov/test_gitio.py
git commit -m "feat(cov): gitio rev_parse_commit + rev_list_first_parent helpers" -m "Assisted-by: Claude Fable 5"
```

---

### Task 3: `coverage/overrides.py` — load and validate the override file

**Files:**
- Create: `src/otto/coverage/overrides.py`
- Test: `tests/unit/cov/test_overrides.py` (new)

**Interfaces:**
- Consumes: `TierConfig` (`otto.coverage.tiers`), `gitio.rev_parse_commit` (Task 2), `NO_TICKET`/`UNCOMMITTED_TICKET` (`otto.coverage.attribution`).
- Produces:

```python
class OverrideConfigError(ValueError): ...

DEFAULT_OVERRIDES_RELPATH = Path(".otto") / "coverage-overrides.toml"

@dataclass(frozen=True)
class AssertedEntry:
    id: int                      # index into the file's entry order; stable per report
    tier: str                    # a declared kind="manual" tier name
    reason: str
    ticket: str | None = None    # exactly one of ticket/commit is set
    commit: str | None = None    # resolved FULL sha
    as_of: str | None = None     # resolved FULL sha; set iff ticket is set

    @property
    def key(self) -> str:        # "ticket:PROJ-412" or "commit:<full sha>"

@dataclass(frozen=True)
class OverrideConfig:
    path: Path
    asserted: list[AssertedEntry]
    reattributions: dict[str, list[str]]   # full sha -> replacement ticket ids

def load_override_config(
    cov_config: dict[str, Any], sut_dir: Path, tiers: list[TierConfig]
) -> OverrideConfig | None: ...
```

Semantics of `load_override_config` (each rule is spec §2, each loud via `OverrideConfigError` whose message names the file, the table, and the entry index):
1. Path: `cov_config.get("overrides", {}).get("file")` → `sut_dir / that` (relative allowed); else `sut_dir / DEFAULT_OVERRIDES_RELPATH`. Explicit key + missing file → error. Default path missing → `None` (feature off).
2. File present but `cov_config.get("tickets")` falsy → error ("an override file requires [coverage.tickets]").
3. Parse with `tomllib` (binary read); TOML syntax error → wrapped in `OverrideConfigError`.
4. Every top-level key must be `"reattribute"` or the name of a tier in *tiers* with `kind == "manual"`; each value must be a list of tables (`[[name]]`). A tier named `reattribute` in *tiers* is itself an error (reserved).
5. Asserted entry: keys ⊆ {`ticket`, `commit`, `as_of`, `reason`}; exactly one of `ticket`/`commit`; non-empty `reason`; `ticket` ⇒ `as_of` required; `commit` ⇒ `as_of` forbidden; ticket id not in the reserved sentinels.
6. Reattribute entry: keys ⊆ {`commit`, `tickets`, `reason`}; all three required (`tickets` may be `[]`); ids must be strings, none reserved; duplicate `commit` across reattribute entries → error (ambiguous).
7. Every `commit`/`as_of` resolved through `gitio.rev_parse_commit(sut_dir, ...)`; failure wrapped in `OverrideConfigError` naming the entry. Resolved full shas are what the dataclasses carry (walk shas are full).
8. Entry `id` = running index over asserted entries in file order (across tiers, in TOML document order — iterate the parsed dict in insertion order).

The "ticket must appear at/before `as_of`" rule needs the walk and is checked in Task 6, not here.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/cov/test_overrides.py`. Use a real tiny git repo per test (mirror `test_attribution.py`'s subprocess pattern) since sha resolution is part of loading:

```python
"""load_override_config: the override file's full validation surface."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.overrides import (
    DEFAULT_OVERRIDES_RELPATH,
    OverrideConfigError,
    load_override_config,
)
from otto.coverage.tiers import TierConfig

_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        env={**_ENV, "HOME": str(root)},
    ).stdout


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "sut"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.c").write_text("line1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c1 #1")
    return root, _git(root, "rev-parse", "HEAD").strip()


def _manual_tier(name: str = "bench") -> TierConfig:
    return TierConfig(name=name, kind="manual", precedence=5, color="purple")


_TICKETS_CFG = {"tickets": {"pattern": "#(?P<num>[0-9]+)"}}


def _write(root: Path, text: str) -> None:
    path = root / DEFAULT_OVERRIDES_RELPATH
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)


def test_absent_default_file_and_absent_key_is_none(tmp_path):
    root, _ = _repo(tmp_path)
    assert load_override_config(_TICKETS_CFG, root, [_manual_tier()]) is None


def test_explicit_key_with_missing_file_fails_loud(tmp_path):
    root, _ = _repo(tmp_path)
    cfg = {**_TICKETS_CFG, "overrides": {"file": "nope.toml"}}
    with pytest.raises(OverrideConfigError, match="nope.toml"):
        load_override_config(cfg, root, [_manual_tier()])


def test_file_without_coverage_tickets_fails_loud(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[bench]]\ncommit = "{sha}"\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match=r"\[coverage.tickets\]"):
        load_override_config({}, root, [_manual_tier()])


def test_commit_entry_loads_with_resolved_full_sha(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[bench]]\ncommit = "{sha[:8]}"\nreason = "hand-tested"\n')
    cfg = load_override_config(_TICKETS_CFG, root, [_manual_tier()])
    assert cfg is not None
    (entry,) = cfg.asserted
    assert (entry.id, entry.tier, entry.commit, entry.as_of) == (0, "bench", sha, None)
    assert entry.key == f"commit:{sha}"


def test_ticket_entry_requires_as_of(tmp_path):
    root, _ = _repo(tmp_path)
    _write(root, '[[bench]]\nticket = "#1"\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match="as_of"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_ticket_entry_loads_with_resolved_as_of(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[bench]]\nticket = "#1"\nas_of = "{sha[:8]}"\nreason = "r"\n')
    cfg = load_override_config(_TICKETS_CFG, root, [_manual_tier()])
    (entry,) = cfg.asserted
    assert (entry.ticket, entry.as_of, entry.key) == ("#1", sha, "ticket:#1")


def test_commit_entry_with_as_of_fails(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[bench]]\ncommit = "{sha}"\nas_of = "{sha}"\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match="as_of"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


@pytest.mark.parametrize(
    "body",
    [
        '[[bench]]\nreason = "r"\n',                                  # neither key
        '[[bench]]\nticket = "#1"\ncommit = "HEAD"\nreason = "r"\n',  # both keys
        '[[bench]]\nticket = "#1"\nas_of = "HEAD"\n',                 # missing reason
        '[[bench]]\nticket = "#1"\nas_of = "HEAD"\nreason = ""\n',    # empty reason
        '[[bench]]\nticket = "#1"\nas_of = "HEAD"\nreason = "r"\nbogus = 1\n',  # unknown key
    ],
)
def test_malformed_asserted_entries_fail_loud(tmp_path, body):
    root, _ = _repo(tmp_path)
    _write(root, body)
    with pytest.raises(OverrideConfigError):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_unknown_table_name_fails_loud(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[bnech]]\ncommit = "{sha}"\nreason = "r"\n')  # typo'd tier
    with pytest.raises(OverrideConfigError, match="bnech"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_non_manual_tier_table_fails_loud(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[system]]\ncommit = "{sha}"\nreason = "r"\n')
    tiers = [_manual_tier(), TierConfig(name="system", kind="e2e", precedence=1, color="green")]
    with pytest.raises(OverrideConfigError, match="manual"):
        load_override_config(_TICKETS_CFG, root, tiers)


def test_manual_tier_named_reattribute_is_reserved(tmp_path):
    root, _ = _repo(tmp_path)
    _write(root, "")
    with pytest.raises(OverrideConfigError, match="reserved"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier("reattribute")])


def test_unresolvable_sha_fails_loud(tmp_path):
    root, _ = _repo(tmp_path)
    _write(root, '[[bench]]\ncommit = "deadbeef"\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match="deadbeef"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_reattribute_entry_loads(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[reattribute]]\ncommit = "{sha[:8]}"\ntickets = ["#9"]\nreason = "wrong id"\n')
    cfg = load_override_config(_TICKETS_CFG, root, [_manual_tier()])
    assert cfg.reattributions == {sha: ["#9"]}


def test_reattribute_empty_tickets_is_legal(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[reattribute]]\ncommit = "{sha}"\ntickets = []\nreason = "none"\n')
    cfg = load_override_config(_TICKETS_CFG, root, [_manual_tier()])
    assert cfg.reattributions == {sha: []}


def test_reattribute_duplicate_commit_fails(tmp_path):
    root, sha = _repo(tmp_path)
    _write(
        root,
        f'[[reattribute]]\ncommit = "{sha}"\ntickets = ["#9"]\nreason = "a"\n'
        f'[[reattribute]]\ncommit = "{sha[:8]}"\ntickets = ["#8"]\nreason = "b"\n',
    )
    with pytest.raises(OverrideConfigError, match="duplicate"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_reserved_sentinel_ticket_id_fails(tmp_path):
    root, sha = _repo(tmp_path)
    _write(root, f'[[reattribute]]\ncommit = "{sha}"\ntickets = ["(no ticket)"]\nreason = "r"\n')
    with pytest.raises(OverrideConfigError, match="reserved"):
        load_override_config(_TICKETS_CFG, root, [_manual_tier()])


def test_entry_ids_are_file_order_across_tiers(tmp_path):
    root, sha = _repo(tmp_path)
    _write(
        root,
        f'[[bench]]\ncommit = "{sha}"\nreason = "a"\n'
        f'[[field]]\ncommit = "{sha}"\nreason = "b"\n'
        f'[[bench]]\nticket = "#1"\nas_of = "{sha}"\nreason = "c"\n',
    )
    cfg = load_override_config(_TICKETS_CFG, root, [_manual_tier(), _manual_tier("field")])
    assert [(e.id, e.tier) for e in cfg.asserted] == [(0, "bench"), (1, "field"), (2, "bench")]
```

Note on the id-ordering test: `tomllib` groups array-of-tables by key in the parsed dict; document order across *different* tables is not preserved by the dict. The rule the implementation follows (and this test pins) is therefore **dict insertion order of the tables** (first-appearance order), entries within a table in file order — adjust the expected list to `[(0, "bench"), (1, "bench"), (2, "field")]`… no: first-appearance order of tables is `bench`, `field`; `bench` holds two entries → ids `(0, "bench"), (1, "bench"), (2, "field")` with reasons `a, c, b`. **Use exactly that expectation** — deterministic and documented in the module docstring.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/cov/test_overrides.py -v`
Expected: FAIL — `ModuleNotFoundError: otto.coverage.overrides`.

- [ ] **Step 3: Implement `src/otto/coverage/overrides.py`**

```python
"""``.otto/coverage-overrides.toml`` — manual-testing coverage overrides.

Two capabilities, one commented TOML file (design spec 2026-07-30): asserted
manual coverage (top-level tables named after ``kind="manual"`` tiers) and
break-glass ticket reattribution (the reserved ``[[reattribute]]`` table).
This module loads and validates the file — every rule loud at load, never
rendered around — and applies asserted entries to a store (Task 6).

Entry ids are assigned in table first-appearance order, entries within a
table in file order (``tomllib`` groups array-of-tables by key, so
cross-table document order is not observable).
"""

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .attribution import NO_TICKET, UNCOMMITTED_TICKET
from .capture import gitio
from .tiers import TierConfig

logger = logging.getLogger(__name__)


class OverrideConfigError(ValueError):
    """The override file (or its settings key) is malformed — raised loud."""


DEFAULT_OVERRIDES_RELPATH = Path(".otto") / "coverage-overrides.toml"

_RESERVED_TABLE = "reattribute"
_RESERVED_IDS = frozenset({NO_TICKET, UNCOMMITTED_TICKET})
_ASSERTED_KEYS = frozenset({"ticket", "commit", "as_of", "reason"})
_REATTRIBUTE_KEYS = frozenset({"commit", "tickets", "reason"})


@dataclass(frozen=True)
class AssertedEntry:
    """One "this was manually tested" declaration, resolved to full shas."""

    id: int
    tier: str
    reason: str
    ticket: str | None = None
    commit: str | None = None
    as_of: str | None = None

    @property
    def key(self) -> str:
        """Stable display key: ``ticket:<id>`` or ``commit:<full sha>``."""
        if self.ticket is not None:
            return f"ticket:{self.ticket}"
        return f"commit:{self.commit}"


@dataclass(frozen=True)
class OverrideConfig:
    """The parsed, validated override file."""

    path: Path
    asserted: list[AssertedEntry]
    reattributions: dict[str, list[str]]
```

Then the loader (same file). Write it as small helpers — `_resolve_path`, `_parse_toml`, `_validate_tables`, `_load_asserted_entry`, `_load_reattribute_entry` — with this exact behavior:

```python
def _resolve_sha(sut_dir: Path, rev: str, *, where: str) -> str:
    try:
        return gitio.rev_parse_commit(sut_dir, rev)
    except gitio.GitUnavailableError as exc:
        raise OverrideConfigError(f"{where}: cannot resolve {rev!r} to a commit: {exc}") from exc


def load_override_config(
    cov_config: dict[str, Any], sut_dir: Path, tiers: list[TierConfig]
) -> OverrideConfig | None:
    """Load and validate the override file, or None when the feature is off.

    Raises:
        OverrideConfigError: any spec §2 rule violated. The absent-default
            case (no ``[coverage.overrides]`` key, no file at the default
            path) is the only silent path — an *explicitly configured* path
            that does not exist is an error, not a no-op.
    """
    raw_key = (cov_config.get("overrides") or {}).get("file")
    path = sut_dir / raw_key if raw_key else sut_dir / DEFAULT_OVERRIDES_RELPATH
    if not path.is_file():
        if raw_key:
            raise OverrideConfigError(f"[coverage.overrides] file does not exist: {path}")
        return None
    if not cov_config.get("tickets"):
        raise OverrideConfigError(
            f"{path.name}: an override file requires [coverage.tickets] to be "
            "configured — both asserted coverage and reattribution operate on "
            "the ticket-attribution walk"
        )
    manual_tiers = {t.name for t in tiers if t.kind == "manual"}
    if _RESERVED_TABLE in manual_tiers:
        raise OverrideConfigError(
            f"a manual tier may not be named {_RESERVED_TABLE!r} (reserved table name)"
        )
    try:
        doc = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise OverrideConfigError(f"{path}: not valid TOML: {exc}") from exc

    asserted: list[AssertedEntry] = []
    reattributions: dict[str, list[str]] = {}
    next_id = 0
    for table, entries in doc.items():
        if table != _RESERVED_TABLE and table not in manual_tiers:
            raise OverrideConfigError(
                f"{path.name}: unknown table {table!r} — top-level tables must be "
                f"'{_RESERVED_TABLE}' or a declared kind=\"manual\" tier "
                f"(have: {sorted(manual_tiers)})"
            )
        if not isinstance(entries, list):
            raise OverrideConfigError(
                f"{path.name}: [{table}] must be an array of tables ([[{table}]])"
            )
        for i, entry in enumerate(entries):
            where = f"{path.name}: [[{table}]] entry {i + 1}"
            if table == _RESERVED_TABLE:
                sha, ids = _load_reattribute_entry(entry, sut_dir, where)
                if sha in reattributions:
                    raise OverrideConfigError(f"{where}: duplicate reattribution for {sha}")
                reattributions[sha] = ids
            else:
                asserted.append(_load_asserted_entry(entry, table, next_id, sut_dir, where))
                next_id += 1
    return OverrideConfig(path=path, asserted=asserted, reattributions=reattributions)
```

`_load_asserted_entry(entry, tier, entry_id, sut_dir, where) -> AssertedEntry` enforces: unknown keys (`set(entry) - _ASSERTED_KEYS` → error naming them); exactly one of `ticket`/`commit`; `reason` a non-empty `str` (`.strip()`); `ticket` present ⇒ `as_of` present, `commit` present ⇒ `as_of` absent; `ticket` not in `_RESERVED_IDS`; shas through `_resolve_sha`. `_load_reattribute_entry(entry, sut_dir, where) -> tuple[str, list[str]]` enforces: keys exactly `_REATTRIBUTE_KEYS` (all required), `tickets` a list of non-reserved non-empty strings (empty list legal), non-empty `reason`, `commit` through `_resolve_sha`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/cov/test_overrides.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
nox -s lint && nox -s typecheck
git add src/otto/coverage/overrides.py tests/unit/cov/test_overrides.py
git commit -m "feat(cov): load and validate .otto/coverage-overrides.toml" -m "Assisted-by: Claude Fable 5"
```

---

### Task 4: reattribution inside `attribute_tickets`

**Files:**
- Modify: `src/otto/coverage/attribution.py:329-366`
- Test: `tests/unit/cov/test_attribution.py`

**Interfaces:**
- Produces: `attribute_tickets(repo_root, line_counts, spec, *, first_parent=True, reattributions: dict[str, list[str]] | None = None)` — same triple return. For a sha in *reattributions*, the entry's list replaces message extraction entirely (empty list ⇒ the commit names no ticket ⇒ its lines land in `(no ticket)` downstream, exactly like a no-match commit).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cov/test_attribution.py` (reuse its `_repo`-style helpers — see `test_tickets_map_lines_and_collect_commits` at :363 for the exact idiom):

```python
def test_reattribution_replaces_the_parsed_ticket_set(tmp_path):
    repo = _make_ticket_repo(tmp_path, "fix #1")   # one commit, message names #1
    sha = _head(repo)
    spec = build_ticket_spec("#(?P<n>[0-9]+)", None)
    lines, commits, _ = attribute_tickets(
        repo, {"a.c": 1}, spec, reattributions={sha: ["#9"]}
    )
    assert lines["a.c"][1] == ["#9"]
    assert "#1" not in commits
    assert commits["#9"] == [sha]


def test_reattribution_to_empty_list_means_no_ticket(tmp_path):
    repo = _make_ticket_repo(tmp_path, "fix #1")
    sha = _head(repo)
    spec = build_ticket_spec("#(?P<n>[0-9]+)", None)
    lines, commits, _ = attribute_tickets(repo, {"a.c": 1}, spec, reattributions={sha: []})
    assert lines["a.c"][1] == []
    assert commits == {}


def test_reattribution_leaves_other_commits_alone(tmp_path):
    repo = _make_two_commit_ticket_repo(tmp_path)  # c1 "#1" touches line 1, c2 "#2" touches line 2
    c2 = _head(repo)
    spec = build_ticket_spec("#(?P<n>[0-9]+)", None)
    lines, _, _ = attribute_tickets(repo, {"a.c": 2}, spec, reattributions={c2: ["#7"]})
    assert lines["a.c"][1] == ["#1"]
    assert lines["a.c"][2] == ["#7"]
```

Write the two tiny repo helpers next to the file's existing ones if no direct fit exists (they follow the `_ENV`/`subprocess` pattern already at the top of the module).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/cov/test_attribution.py -k reattribution -v`
Expected: FAIL — `TypeError: attribute_tickets() got an unexpected keyword argument`.

- [ ] **Step 3: Implement**

In `attribute_tickets`, add the keyword and change the `tickets_of` comprehension:

```python
def attribute_tickets(
    repo_root: Path,
    line_counts: dict[str, int],
    spec: TicketSpec,
    *,
    first_parent: bool = True,
    reattributions: dict[str, list[str]] | None = None,
) -> tuple[dict[str, dict[int, list[str]]], dict[str, list[str]], dict[str, dict[int, str]]]:
```

and replace the `tickets_of = {...}` line with:

```python
    # Break-glass reattribution (overrides spec §3): a listed commit's ids
    # replace message extraction entirely, at this single site, so every
    # consumer (store, gutter, tickets page, export, asserted entries) sees
    # the same corrected mapping. Load-time validation already rejected
    # reserved sentinel ids, so no _extract_real_tickets-style filter here.
    reattr = reattributions or {}
    tickets_of = {
        c.sha: (
            list(reattr[c.sha])
            if c.sha in reattr
            else _extract_real_tickets(spec, f"{c.subject}\n{c.body}", c.sha)
        )
        for c in walk
    }
```

Extend the docstring's Returns section with one sentence about *reattributions*.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/cov/test_attribution.py -v`
Expected: PASS (including the spawn-budget test — the change adds no subprocess).

- [ ] **Step 5: Lint + commit**

```bash
nox -s lint
git add src/otto/coverage/attribution.py tests/unit/cov/test_attribution.py
git commit -m "feat(cov): commit-level ticket reattribution in attribute_tickets" -m "Assisted-by: Claude Fable 5"
```

---

### Task 5: store v6 — `OverrideRecord`, `CoverageStore.overrides`, `LineRecord.asserted`

**Files:**
- Modify: `src/otto/coverage/store/model.py`
- Test: `tests/unit/cov/test_model.py`

**Interfaces:**
- Produces: `STORE_FORMAT_VERSION = 6`. `OverrideRecord` dataclass (`id: int`, `tier: str`, `key: str`, `reason: str`, `as_of: str | None = None`, `to_dict()`). `CoverageStore.overrides: list[OverrideRecord]` (init `[]`, saved as a list, loaded back). `LineRecord.asserted: dict[str, list[int]]` (tier → sorted override-entry ids; default `{}`; serialized under `"asserted"` only when non-empty; merged by per-tier id union; loaded back).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cov/test_model.py` (follow its existing round-trip test style):

```python
def test_store_v6_round_trips_overrides_and_asserted(tmp_path):
    store = CoverageStore(tier_order=["bench"])
    store.overrides.append(
        OverrideRecord(id=0, tier="bench", key="ticket:#1", reason="legacy", as_of="a" * 40)
    )
    rec = store.get_or_create_file(tmp_path / "a.c")
    line = rec.get_or_create_line(1)
    line.hits.add("bench", 1)
    line.asserted = {"bench": [0]}
    store.save(tmp_path / "store.json")
    loaded = CoverageStore.load(tmp_path / "store.json")
    (ov,) = loaded.overrides
    assert (ov.id, ov.tier, ov.key, ov.reason, ov.as_of) == (
        0, "bench", "ticket:#1", "legacy", "a" * 40
    )
    (fr,) = list(loaded.files())
    assert fr.lines[1].asserted == {"bench": [0]}


def test_asserted_is_omitted_from_json_when_empty(tmp_path):
    store = CoverageStore(tier_order=["bench"])
    rec = store.get_or_create_file(tmp_path / "a.c")
    rec.get_or_create_line(1).hits.add("bench", 1)
    store.save(tmp_path / "store.json")
    raw = (tmp_path / "store.json").read_text()
    assert '"asserted"' not in raw
    assert json.loads(raw)["overrides"] == []


def test_line_merge_unions_asserted_ids_per_tier():
    a = LineRecord(line_number=1, asserted={"bench": [0, 2]})
    b = LineRecord(line_number=1, asserted={"bench": [2, 3], "field": [1]})
    a.merge(b)
    assert a.asserted == {"bench": [0, 2, 3], "field": [1]}


def test_v5_store_fails_loud(tmp_path):
    (tmp_path / "store.json").write_text('{"format": 5}')
    with pytest.raises(ValueError, match="v6 required"):
        CoverageStore.load(tmp_path / "store.json")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/cov/test_model.py -k "v6 or asserted" -v`
Expected: FAIL — `ImportError`/`NameError: OverrideRecord`.

- [ ] **Step 3: Implement**

In `model.py`:

1. `STORE_FORMAT_VERSION = 6`; append to its docstring:
   ```
   Version 6 adds the manual-override surface: a top-level ``overrides``
   table (one row per asserted entry from ``.otto/coverage-overrides.toml``)
   and a per-line ``asserted`` map (tier -> override-entry ids) marking lines
   whose only hits in that tier are override-sourced.
   ```
2. After `TicketRecord`:
   ```python
   @dataclass
   class OverrideRecord:
       """One asserted manual-coverage entry from the override file.

       ``key`` is the entry's display identity (``ticket:PROJ-412`` /
       ``commit:<full sha>``); ``id`` is the index per-line ``asserted``
       refs point at, so the reason is stored once, not per line.
       """

       id: int
       tier: str
       key: str
       reason: str
       as_of: str | None = None

       def to_dict(self) -> dict[str, Any]:
           """Return a JSON-serialisable dict representation of this entry."""
           return {
               "id": self.id, "tier": self.tier, "key": self.key,
               "reason": self.reason, "as_of": self.as_of,
           }
   ```
3. `LineRecord`: field `asserted: dict[str, list[int]] = field(default_factory=dict)` with the comment `# Override provenance (manual-overrides spec §3/§5): tier -> ids of the override entries that asserted this line; present only while the tier's sole hits are override-sourced.` In `LineRecord.merge`, after the ticket union:
   ```python
        for tier, ids in other.asserted.items():
            mine = self.asserted.setdefault(tier, [])
            for entry_id in ids:
                if entry_id not in mine:
                    mine.append(entry_id)
   ```
4. `FileRecord._line_to_dict`: after the `ticket` guard: `if rec.asserted: d["asserted"] = {tier: list(ids) for tier, ids in rec.asserted.items()}`.
5. `CoverageStore.__init__`: `self.overrides: list[OverrideRecord] = []`. `save()`: `"overrides": [o.to_dict() for o in self.overrides],`. `load()`: after the tickets loop:
   ```python
        for od in data.get("overrides") or []:
            store.overrides.append(
                OverrideRecord(
                    id=od["id"], tier=od["tier"], key=od["key"],
                    reason=od["reason"], as_of=od.get("as_of"),
                )
            )
   ```
   and in the per-line loader pass `asserted={t: list(v) for t, v in (ld.get("asserted") or {}).items()}` into the `LineRecord(...)` constructor call.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/cov/test_model.py tests/unit/cov/test_store_dir.py -v`
Expected: PASS. Then `uv run pytest tests/unit/cov tests/integration/cov -x -q` — any test that hand-writes `"format": 5` fixtures will fail; update those fixtures to 6 in this task (they are part of the version bump).

- [ ] **Step 5: Lint + commit**

```bash
nox -s lint
git add -A src tests
git commit -m "feat(cov)!: store v6 — override records and per-line asserted provenance" -m "Assisted-by: Claude Fable 5"
```

---

### Task 6: `apply_asserted_entries` + prune-signal logging

**Files:**
- Modify: `src/otto/coverage/overrides.py`
- Test: `tests/unit/cov/test_overrides_apply.py` (new)

**Interfaces:**
- Consumes: `AssertedEntry`, `CoverageStore`/`OverrideRecord` (Task 5), attribution products.
- Produces (in `overrides.py`):

```python
def apply_asserted_entries(
    store: CoverageStore,
    entries: list[AssertedEntry],
    *,
    repo_root: Path,
    per_line_sha: dict[str, dict[int, str]],     # attribute_tickets 3rd return
    ticket_commits: dict[str, list[str]],        # attribute_tickets 2nd return
    fp_index: dict[str, int],                    # sha -> index in rev_list_first_parent (0 = HEAD)
) -> None
```

Behavior (spec §3):
- Line set per entry: commit entry → store lines whose owning sha == `entry.commit`; ticket entry → owning sha ∈ `{s in ticket_commits.get(entry.ticket, []) if fp_index[s] >= fp_index[entry.as_of]}` (rev-list is newest-first, so *older or equal* means a **greater-or-equal** index).
- Loud checks (all `OverrideConfigError`): `entry.as_of` not in `fp_index` → "as_of … is not in the first-parent history"; ticket entry whose ticket has **no** commit at/before `as_of` → "ticket … never appears in a commit at/before as_of … — a typo, or the wrong as_of". (Zero *current lines* is legal aging, distinct from zero *commits*.)
- Snapshot-then-apply: first compute every entry's line list and snapshot `hits.is_hit(tier)` per involved (relpath, lineno, tier) **before any mutation** — so entry A's added hit never makes entry B read "already covered", and two same-tier entries on one line both get refs but the hit counter increments once.
- Application per (entry, line): snapshot-hit → contributes nothing; else append `entry.id` to `line.asserted[tier]` and, only when this is the tier's first asserted ref on the line, `line.hits.add(tier, 1)`.
- Every entry appends `OverrideRecord(id=entry.id, tier=entry.tier, key=entry.key, reason=entry.reason, as_of=entry.as_of)` to `store.overrides` (inert entries included — the SPA badge lists them all), and `store.register_tier(entry.tier)`.
- Prune signal, per inert entry, exactly one `logger.info`:
  - empty line list → `"override %s (tier %r) is fully aged out — no current line is attributed to it; prune it from %s (reason: %s)"`
  - non-empty but all snapshot-hit → `"override %s (tier %r) is fully covered by recorded runs — every line is proven; prune it from %s (reason: %s)"`
  (args: `entry.key`, `entry.tier`, the overrides file name, `entry.reason`; pass the file `Path` in via a `path: Path` keyword — add it to the signature.)

Store lines are matched the way `_annotate_tickets` matches them: resolve `repo_root`, iterate `store.files()`, skip files not under it, key by `rec.path.relative_to(repo_root).as_posix()`, and skip line numbers not in `rec.lines`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/cov/test_overrides_apply.py`. No git needed — construct the attribution products by hand:

```python
"""apply_asserted_entries: line-set resolution, as_of bounding, provenance, prune logs."""

import logging
from pathlib import Path

import pytest

from otto.coverage.overrides import AssertedEntry, OverrideConfigError, apply_asserted_entries
from otto.coverage.store.model import CoverageStore

SHA_NEW, SHA_MID, SHA_OLD = "n" * 40, "m" * 40, "o" * 40
FP_INDEX = {SHA_NEW: 0, SHA_MID: 1, SHA_OLD: 2}


def _store(tmp_path: Path, lines: dict[int, list[str]]) -> CoverageStore:
    """A store with one file a.c; *lines* maps lineno -> tiers already hit."""
    store = CoverageStore(tier_order=["bench"])
    rec = store.get_or_create_file(tmp_path / "a.c")
    for lineno, tiers in lines.items():
        lr = rec.get_or_create_line(lineno)
        for tier in tiers:
            lr.hits.add(tier, 1)
    return store


def _apply(store, tmp_path, entries, per_line_sha, ticket_commits=None):
    apply_asserted_entries(
        store, entries,
        repo_root=tmp_path,
        per_line_sha=per_line_sha,
        ticket_commits=ticket_commits or {},
        fp_index=FP_INDEX,
        path=tmp_path / "coverage-overrides.toml",
    )


def _line(store, tmp_path, lineno):
    return store.get_or_create_file(tmp_path / "a.c").lines[lineno]


def test_commit_entry_asserts_its_unhit_lines(tmp_path):
    store = _store(tmp_path, {1: [], 2: []})
    entry = AssertedEntry(id=0, tier="bench", reason="r", commit=SHA_MID)
    _apply(store, tmp_path, [entry], {"a.c": {1: SHA_MID, 2: SHA_NEW}})
    assert _line(store, tmp_path, 1).hits.for_tier("bench") == 1
    assert _line(store, tmp_path, 1).asserted == {"bench": [0]}
    assert _line(store, tmp_path, 2).hits.for_tier("bench") == 0
    assert _line(store, tmp_path, 2).asserted == {}
    (ov,) = store.overrides
    assert (ov.id, ov.key, ov.tier) == (0, f"commit:{SHA_MID}", "bench")


def test_already_hit_line_gets_no_mark_and_no_extra_hit(tmp_path):
    store = _store(tmp_path, {1: ["bench"]})
    entry = AssertedEntry(id=0, tier="bench", reason="r", commit=SHA_MID)
    _apply(store, tmp_path, [entry], {"a.c": {1: SHA_MID}})
    assert _line(store, tmp_path, 1).hits.for_tier("bench") == 1
    assert _line(store, tmp_path, 1).asserted == {}


def test_hit_in_another_tier_still_gets_asserted_in_its_own(tmp_path):
    store = _store(tmp_path, {1: ["unit"]})
    store.register_tier("unit")
    entry = AssertedEntry(id=0, tier="bench", reason="r", commit=SHA_MID)
    _apply(store, tmp_path, [entry], {"a.c": {1: SHA_MID}})
    assert _line(store, tmp_path, 1).asserted == {"bench": [0]}


def test_ticket_entry_respects_as_of_bound(tmp_path):
    store = _store(tmp_path, {1: [], 2: []})
    entry = AssertedEntry(id=0, tier="bench", reason="r", ticket="#1", as_of=SHA_MID)
    _apply(
        store, tmp_path, [entry],
        {"a.c": {1: SHA_OLD, 2: SHA_NEW}},          # line 2's commit is NEWER than as_of
        ticket_commits={"#1": [SHA_OLD, SHA_NEW]},
    )
    assert _line(store, tmp_path, 1).asserted == {"bench": [0]}
    assert _line(store, tmp_path, 2).asserted == {}   # after as_of: not blessed


def test_as_of_not_in_first_parent_history_fails_loud(tmp_path):
    store = _store(tmp_path, {1: []})
    entry = AssertedEntry(id=0, tier="bench", reason="r", ticket="#1", as_of="x" * 40)
    with pytest.raises(OverrideConfigError, match="first-parent"):
        _apply(store, tmp_path, [entry], {"a.c": {1: SHA_OLD}}, {"#1": [SHA_OLD]})


def test_ticket_with_no_commit_at_or_before_as_of_fails_loud(tmp_path):
    store = _store(tmp_path, {1: []})
    entry = AssertedEntry(id=0, tier="bench", reason="r", ticket="#1", as_of=SHA_MID)
    with pytest.raises(OverrideConfigError, match="at/before"):
        _apply(store, tmp_path, [entry], {"a.c": {1: SHA_NEW}}, {"#1": [SHA_NEW]})


def test_two_entries_same_line_both_ref_one_hit(tmp_path):
    store = _store(tmp_path, {1: []})
    entries = [
        AssertedEntry(id=0, tier="bench", reason="a", commit=SHA_MID),
        AssertedEntry(id=1, tier="bench", reason="b", ticket="#1", as_of=SHA_NEW),
    ]
    _apply(store, tmp_path, entries, {"a.c": {1: SHA_MID}}, {"#1": [SHA_MID]})
    assert _line(store, tmp_path, 1).asserted == {"bench": [0, 1]}
    assert _line(store, tmp_path, 1).hits.for_tier("bench") == 1


def test_fully_aged_out_entry_logs_prune_signal(tmp_path, caplog):
    store = _store(tmp_path, {1: []})
    entry = AssertedEntry(id=0, tier="bench", reason="old bench pass", commit=SHA_OLD)
    with caplog.at_level(logging.INFO, logger="otto.coverage.overrides"):
        _apply(store, tmp_path, [entry], {"a.c": {1: SHA_NEW}})
    assert "fully aged out" in caplog.text
    assert f"commit:{SHA_OLD}" in caplog.text
    assert "old bench pass" in caplog.text
    assert len(store.overrides) == 1  # inert entries still listed


def test_fully_covered_entry_logs_prune_signal(tmp_path, caplog):
    store = _store(tmp_path, {1: ["bench"]})
    entry = AssertedEntry(id=0, tier="bench", reason="r", commit=SHA_MID)
    with caplog.at_level(logging.INFO, logger="otto.coverage.overrides"):
        _apply(store, tmp_path, [entry], {"a.c": {1: SHA_MID}})
    assert "fully covered" in caplog.text


def test_contributing_entry_logs_nothing(tmp_path, caplog):
    store = _store(tmp_path, {1: []})
    entry = AssertedEntry(id=0, tier="bench", reason="r", commit=SHA_MID)
    with caplog.at_level(logging.INFO, logger="otto.coverage.overrides"):
        _apply(store, tmp_path, [entry], {"a.c": {1: SHA_MID}})
    assert "prune" not in caplog.text


def test_snapshot_ordering_entry_a_hit_does_not_hide_line_from_entry_b(tmp_path, caplog):
    """Entry 1's added hit must not make entry 2 read 'fully covered'."""
    store = _store(tmp_path, {1: []})
    entries = [
        AssertedEntry(id=0, tier="bench", reason="a", commit=SHA_MID),
        AssertedEntry(id=1, tier="bench", reason="b", commit=SHA_MID),
    ]
    with caplog.at_level(logging.INFO, logger="otto.coverage.overrides"):
        _apply(store, tmp_path, entries, {"a.c": {1: SHA_MID}})
    assert "fully covered" not in caplog.text
    assert _line(store, tmp_path, 1).asserted == {"bench": [0, 1]}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/cov/test_overrides_apply.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_asserted_entries'`.

- [ ] **Step 3: Implement**

Append to `overrides.py` (import `CoverageStore, OverrideRecord` from `.store.model` at module top):

```python
def _entry_shas(
    entry: AssertedEntry, ticket_commits: dict[str, list[str]], fp_index: dict[str, int]
) -> set[str]:
    """The commit shas whose lines *entry* covers (spec §3)."""
    if entry.commit is not None:
        return {entry.commit}
    assert entry.as_of is not None  # noqa: S101 — loader invariant: ticket entries carry as_of
    bound = fp_index.get(entry.as_of)
    if bound is None:
        raise OverrideConfigError(
            f"override {entry.key}: as_of {entry.as_of} is not in the "
            "first-parent history of HEAD"
        )
    shas = {s for s in ticket_commits.get(entry.ticket or "", []) if fp_index.get(s, -1) >= bound}
    if not shas:
        raise OverrideConfigError(
            f"override {entry.key}: ticket never appears in a commit at/before "
            f"as_of {entry.as_of} — a typo'd id, or the wrong as_of"
        )
    return shas


def apply_asserted_entries(  # noqa: PLR0913 — attribution products are independent inputs
    store: CoverageStore,
    entries: list[AssertedEntry],
    *,
    repo_root: Path,
    per_line_sha: dict[str, dict[int, str]],
    ticket_commits: dict[str, list[str]],
    fp_index: dict[str, int],
    path: Path,
) -> None:
    """Fold asserted entries into *store*: hits + provenance + prune signals.

    Snapshot-then-apply: every entry's line set and every involved line's
    pre-existing hit state are computed before any mutation, so one entry's
    added hit can never make a later entry read "already covered", and two
    entries asserting the same line both get a provenance ref while the hit
    counter moves exactly once.
    """
    if not entries:
        return
    resolved_root = repo_root.resolve()
    records = {
        rec.path.relative_to(resolved_root).as_posix(): rec
        for rec in store.files()
        if rec.path.is_relative_to(resolved_root)
    }
    # Pass 1: resolve line sets against the immutable attribution products.
    lines_of: dict[int, list[tuple[str, int]]] = {}
    for entry in entries:
        shas = _entry_shas(entry, ticket_commits, fp_index)
        lines_of[entry.id] = [
            (relpath, lineno)
            for relpath, per_line in per_line_sha.items()
            if relpath in records
            for lineno, sha in per_line.items()
            if sha in shas and lineno in records[relpath].lines
        ]
    # Snapshot real hit state before mutating anything.
    already_hit = {
        (relpath, lineno, entry.tier): records[relpath].lines[lineno].hits.is_hit(entry.tier)
        for entry in entries
        for relpath, lineno in lines_of[entry.id]
    }
    # Pass 2: apply, and log the prune signal for inert entries.
    for entry in entries:
        store.register_tier(entry.tier)
        marked = 0
        for relpath, lineno in lines_of[entry.id]:
            if already_hit[(relpath, lineno, entry.tier)]:
                continue
            line = records[relpath].lines[lineno]
            refs = line.asserted.setdefault(entry.tier, [])
            if not refs:
                line.hits.add(entry.tier, 1)
            refs.append(entry.id)
            marked += 1
        if not lines_of[entry.id]:
            logger.info(
                "override %s (tier %r) is fully aged out — no current line is "
                "attributed to it; prune it from %s (reason: %s)",
                entry.key, entry.tier, path.name, entry.reason,
            )
        elif marked == 0:
            logger.info(
                "override %s (tier %r) is fully covered by recorded runs — every "
                "line is proven; prune it from %s (reason: %s)",
                entry.key, entry.tier, path.name, entry.reason,
            )
        store.overrides.append(
            OverrideRecord(
                id=entry.id, tier=entry.tier, key=entry.key,
                reason=entry.reason, as_of=entry.as_of,
            )
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/cov/test_overrides_apply.py tests/unit/cov/test_overrides.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
nox -s lint && nox -s typecheck
git add src/otto/coverage/overrides.py tests/unit/cov/test_overrides_apply.py
git commit -m "feat(cov): apply asserted override entries with as_of bounding and prune signals" -m "Assisted-by: Claude Fable 5"
```

---

### Task 7: reporter + CLI/suite wiring, end-to-end pins

**Files:**
- Modify: `src/otto/coverage/reporter.py` (`CoverageReporter.__init__` ~:228, `run()` §3c ~:451, `_annotate_tickets` ~:682, `generate/run_coverage_report` ~:840-988)
- Modify: `src/otto/cli/cov.py` (`_resolve_cov_settings` ~:195-235, `report` ~:315-352)
- Modify: `src/otto/suite/run.py` (~:300-348)
- Test: `tests/integration/cov/test_overrides_report.py` (new)

**Interfaces:**
- Consumes: `OverrideConfig`, `load_override_config`, `apply_asserted_entries` (Tasks 3/6), `gitio.rev_list_first_parent` (Task 2).
- Produces: `CoverageReporter(..., overrides: "OverrideConfig | None" = None)`; `run_coverage_report(..., overrides=None)` / `_run_collection_report(..., overrides=None)`; `_annotate_tickets` returns `tuple[dict[str, dict[int, str]], dict[str, list[str]]] | None` (per_line_sha, raw commits map; `None` on its existing early-outs); `_resolve_cov_settings` returns a 6-tuple ending in `overrides`.

- [ ] **Step 1: Write the failing integration tests**

Create `tests/integration/cov/test_overrides_report.py`, modeled line-for-line on `tests/integration/cov/test_capture_report_cycle.py` (same `ENV`, same inner `git()` closure, same `pytest.mark.asyncio` + `run_coverage_report([], report_dir, repo_root=repo, tier_configs=load_tiers(COV), ...)` idiom; store lines are materialized by a committed manual capture, which may carry 0-hit lines to register uncovered lines):

```python
"""Override file end-to-end: asserted coverage, as_of bounding, reattribution."""

import json
import logging
import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import blob_sha, head_commit
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.capture.store_dir import write_manual_capture
from otto.coverage.overrides import DEFAULT_OVERRIDES_RELPATH, load_override_config
from otto.coverage.reporter import run_coverage_report
from otto.coverage.tickets import build_ticket_spec
from otto.coverage.tiers import load_tiers

ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
    "PATH": "/usr/bin:/bin",
}

COV = {
    "tiers": {
        "seed": {"kind": "manual", "precedence": 1},
        "bench": {"kind": "manual", "precedence": 2},
    },
    "tickets": {"pattern": "#(?P<n>[0-9]+)"},
}

SPEC = build_ticket_spec("#(?P<n>[0-9]+)", None)


def _mk_repo(tmp_path: Path):
    repo = tmp_path / "sut"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True,
            env={**ENV, "HOME": str(tmp_path)},
        )

    git("init", "-q", "-b", "main")
    return repo, git


def _seed_lines(repo: Path, rel: str, linenos: list[int]) -> None:
    """Materialize *linenos* in the store, uncovered, via a 0-hit capture."""
    cap = Capture(
        tier="seed",
        base_commit=head_commit(repo),
        captured_at="2026-07-01T00:00:00Z",
        board="b1",
        files={rel: CaptureFileCov(blob=blob_sha(repo, Path(rel)), lines=dict.fromkeys(linenos, 0))},
    )
    write_manual_capture(cap, repo)


def _overrides(repo: Path, text: str) -> None:
    path = repo / DEFAULT_OVERRIDES_RELPATH
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)


async def _report(repo: Path, out: Path):
    tier_configs = load_tiers(COV)
    return await run_coverage_report(
        [], out, repo_root=repo, tier_configs=tier_configs, ticket_spec=SPEC,
        overrides=load_override_config(COV, repo, tier_configs),
    )


def _file_rec(store, name: str):
    (rec,) = [f for f in store.files() if f.path.name == name]
    return rec


@pytest.mark.asyncio
async def test_asserted_lines_count_in_their_tier_and_carry_provenance(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\nl2\nl3\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    sha = head_commit(repo)
    _seed_lines(repo, "a.c", [1, 2, 3])
    _overrides(repo, f'[[bench]]\nticket = "#1"\nas_of = "{sha}"\nreason = "legacy pass"\n')
    store = await _report(repo, tmp_path / "r")
    rec = _file_rec(store, "a.c")
    for n in (1, 2, 3):
        assert rec.lines[n].hits.for_tier("bench") == 1
        assert rec.lines[n].asserted == {"bench": [0]}
    (ov,) = store.overrides
    assert (ov.key, ov.tier, ov.as_of) == ("ticket:#1", "bench", sha)
    assert json.loads((tmp_path / "r" / "store.json").read_text())["format"] == 6


@pytest.mark.asyncio
async def test_commit_after_as_of_under_same_ticket_stays_uncovered(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\nl2\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    as_of = head_commit(repo)
    (repo / "a.c").write_text("l1\nl2\nl3\n")
    git("commit", "-aqm", "more work #1")
    _seed_lines(repo, "a.c", [1, 2, 3])
    _overrides(repo, f'[[bench]]\nticket = "#1"\nas_of = "{as_of}"\nreason = "r"\n')
    store = await _report(repo, tmp_path / "r")
    rec = _file_rec(store, "a.c")
    assert rec.lines[1].asserted == {"bench": [0]}
    assert rec.lines[2].asserted == {"bench": [0]}
    assert rec.lines[3].asserted == {}
    assert rec.lines[3].hits.for_tier("bench") == 0


@pytest.mark.asyncio
async def test_rewritten_line_drops_out_whitespace_edit_survives(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\nl2\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    as_of = head_commit(repo)
    (repo / "a.c").write_text("REWRITTEN\n    l2\n")  # l1 rewritten, l2 reindented
    git("commit", "-aqm", "later edit, no ticket")
    _seed_lines(repo, "a.c", [1, 2])
    _overrides(repo, f'[[bench]]\nticket = "#1"\nas_of = "{as_of}"\nreason = "r"\n')
    store = await _report(repo, tmp_path / "r")
    rec = _file_rec(store, "a.c")
    assert rec.lines[1].asserted == {}          # superseded — aged out by content
    assert rec.lines[2].asserted == {"bench": [0]}  # -w: whitespace never re-attributes


@pytest.mark.asyncio
async def test_reattribution_reaches_store_tickets(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\n")
    git("add", "-A")
    git("commit", "-qm", "fix #1")
    sha = head_commit(repo)
    _seed_lines(repo, "a.c", [1])
    _overrides(repo, f'[[reattribute]]\ncommit = "{sha}"\ntickets = ["#9"]\nreason = "wrong id"\n')
    store = await _report(repo, tmp_path / "r")
    rec = _file_rec(store, "a.c")
    assert rec.lines[1].ticket == ["#9"]
    assert "#9" in store.tickets
    assert "#1" not in store.tickets


@pytest.mark.asyncio
async def test_real_manual_run_clears_the_asserted_mark(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\nl2\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    sha = head_commit(repo)
    _seed_lines(repo, "a.c", [1, 2])
    # A real bench-tier capture proves line 1.
    cap = Capture(
        tier="bench", base_commit=head_commit(repo), captured_at="2026-07-02T00:00:00Z",
        board="b1",
        files={"a.c": CaptureFileCov(blob=blob_sha(repo, Path("a.c")), lines={1: 3})},
    )
    write_manual_capture(cap, repo)
    _overrides(repo, f'[[bench]]\nticket = "#1"\nas_of = "{sha}"\nreason = "r"\n')
    store = await _report(repo, tmp_path / "r")
    rec = _file_rec(store, "a.c")
    assert rec.lines[1].hits.for_tier("bench") == 3   # the real run's count, untouched
    assert rec.lines[1].asserted == {}                # proven — no mark
    assert rec.lines[2].asserted == {"bench": [0]}    # still asserted


@pytest.mark.asyncio
async def test_absent_file_and_key_is_identical(tmp_path):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    _seed_lines(repo, "a.c", [1])
    s1 = await _report(repo, tmp_path / "r1")   # no override file exists
    s2 = await run_coverage_report(              # overrides never resolved at all
        [], tmp_path / "r2", repo_root=repo, tier_configs=load_tiers(COV), ticket_spec=SPEC,
    )
    d1 = json.loads((tmp_path / "r1" / "store.json").read_text())
    d2 = json.loads((tmp_path / "r2" / "store.json").read_text())
    assert d1 == d2
    assert d1["overrides"] == []
    assert s1.overrides == [] and s2.overrides == []


@pytest.mark.asyncio
async def test_prune_signal_reaches_the_report_log(tmp_path, caplog):
    repo, git = _mk_repo(tmp_path)
    (repo / "a.c").write_text("l1\n")
    git("add", "-A")
    git("commit", "-qm", "work #1")
    sha = head_commit(repo)
    cap = Capture(
        tier="bench", base_commit=head_commit(repo), captured_at="2026-07-02T00:00:00Z",
        board="b1",
        files={"a.c": CaptureFileCov(blob=blob_sha(repo, Path("a.c")), lines={1: 1})},
    )
    write_manual_capture(cap, repo)
    _overrides(repo, f'[[bench]]\ncommit = "{sha}"\nreason = "old pass"\n')
    with caplog.at_level(logging.INFO, logger="otto.coverage.overrides"):
        await _report(repo, tmp_path / "r")
    assert "fully covered" in caplog.text
    assert "old pass" in caplog.text
```

(If `Capture`/`write_manual_capture` reject 0-hit lines, seed with `lines={n: 0}` replaced by a hit in the low-precedence `seed` tier and assert `bench` columns only — adjust `_seed_lines` accordingly; the sibling module shows which shapes `apply_manual_capture` accepts.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/cov/test_overrides_report.py -v`
Expected: FAIL — `run_coverage_report() got an unexpected keyword argument 'overrides'`.

- [ ] **Step 3: Implement reporter changes**

1. `CoverageReporter.__init__`: add keyword `overrides: "OverrideConfig | None" = None` (TYPE_CHECKING import from `.overrides`), store on `self.overrides`; document in the class docstring next to `ticket_spec`.
2. `_annotate_tickets`: pass `reattributions=self.overrides.reattributions if self.overrides else None` into `attribute_tickets`; change its `return`s to `return None` on the early-outs and, at the end, `return per_line_sha, commits` (the **raw** commits map from `attribute_tickets`, not the coverable-filtered `store.tickets` view — override bounding needs every walked commit of a ticket).
3. New method after `_annotate_tickets`:

```python
    def _apply_overrides(
        self,
        store: CoverageStore,
        repo_root: Path,
        per_line_sha: dict[str, dict[int, str]],
        ticket_commits: dict[str, list[str]],
    ) -> None:
        """Fold the override file's asserted entries into the store (spec §3).

        One extra constant-cost subprocess (`rev-list --first-parent`) for
        the as_of ordering — never per entry or per file.
        """
        from .capture.gitio import rev_list_first_parent
        from .overrides import apply_asserted_entries

        assert self.overrides is not None  # noqa: S101 — caller gates on it
        fp_index = {sha: i for i, sha in enumerate(rev_list_first_parent(repo_root))}
        apply_asserted_entries(
            store,
            self.overrides.asserted,
            repo_root=repo_root,
            per_line_sha=per_line_sha,
            ticket_commits=ticket_commits,
            fp_index=fp_index,
            path=self.overrides.path,
        )
```

4. `run()` §3c becomes:

```python
            if self.repo_root is not None:
                attribution = self._annotate_tickets(store, self.repo_root)
                # 3d. Manual-testing overrides (overrides spec §3): asserted
                # entries fold in after attribution so the line->sha map and
                # ticket->commits map they resolve against are final.
                if self.overrides is not None and attribution is not None:
                    self._apply_overrides(store, self.repo_root, *attribution)
```

5. Thread `overrides: "OverrideConfig | None" = None` through `run_coverage_report` → `_run_collection_report` → the `CoverageReporter(...)` construction (mirror how `ticket_spec` flows, including the docstring sentence noting the legacy branch ignores it).

- [ ] **Step 4: Implement CLI + suite wiring**

`src/otto/cli/cov.py` — `_resolve_cov_settings`: import `load_override_config` from `..coverage.overrides`; return 6-tuple:

```python
    tier_cfgs = load_tiers(cov_config, cov_repo.sut_dir)
    return (
        cov_repo.sut_dir,
        tier_cfgs,
        extra_markers,
        load_report_thresholds(cov_config),
        load_ticket_spec(cov_config),
        load_override_config(cov_config, cov_repo.sut_dir, tier_cfgs),
    )
```

(update the no-coverage fallback to `(None, None, [], None, None, None)` and the docstring). In `report`, extend the unpack (`..., ticket_spec, overrides = _resolve_cov_settings()`), init `overrides = None` beside the other pre-decls, and pass `overrides=overrides` to `run_coverage_report`. `OverrideConfigError` is a `ValueError`, so `report`'s existing `except ValueError` handler already prints it clean — pin that in Step 5's CLI test.

`src/otto/suite/run.py` (~:323): after the `ticket_spec` line:

```python
        from ..coverage.overrides import load_override_config

        overrides = (
            load_override_config(cov_config, repo_root, tier_configs)
            if cov_repo is not None
            else None
        )
```

…but wrapped so a bad override file cannot fail an otherwise-successful test run (this block's standing contract): put the `load_override_config` call **inside** the existing `try:` alongside `prepare_empty_dir` — move the assignment there, defaulting `overrides = None` above the try — and pass `overrides=overrides` to `run_coverage_report`.

- [ ] **Step 5: CLI-level regression test**

Add to `tests/unit/cli/test_cov.py` (mirror its existing `_resolve_cov_settings`/report invocation fixtures): a settings tree with `[coverage.tickets]` + a malformed overrides file → `otto cov report` exits 1 with the `OverrideConfigError` message, no traceback.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/integration/cov/test_overrides_report.py tests/unit/cli/test_cov.py tests/unit/cov -q`
Expected: PASS.

- [ ] **Step 7: Lint, typecheck, commit**

```bash
nox -s lint && nox -s typecheck
git add -A src tests
git commit -m "feat(cov): thread manual overrides through reporter, CLI, and suite run" -m "Assisted-by: Claude Fable 5"
```

---

### Task 8: `tickets.json` v2 — `asserted` counts + `overrides_active`

**Files:**
- Modify: `src/otto/coverage/ticket_export.py`
- Test: `tests/unit/cov/test_ticket_export.py`

**Interfaces:**
- Produces: `TICKET_EXPORT_FORMAT = 2`. Each ticket object gains `"asserted": {tier: n}` (lines owned by the ticket whose hits in that tier are override-sourced, i.e. `tier in line.asserted`) placed directly after `"per_tier"`. Top-level gains `"overrides_active": bool(store.overrides)` after `"traversal"`. Determinism rules unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cov/test_ticket_export.py`, following its store-building helpers:

```python
def test_export_format_is_2():
    # extend the existing minimal-payload test, or:
    assert TICKET_EXPORT_FORMAT == 2


def test_asserted_counts_per_ticket_and_overrides_active(tmp_path):
    # store: one file, line 1 ticket ["#1"] hit in "bench" via override
    # (line.asserted={"bench":[0]}), line 2 ticket ["#1"] hit in "bench"
    # by a real run (asserted empty); store.overrides has one record.
    payload = build_ticket_export(store, repo_root=..., project="p",
                                  otto_version="0", generated="g")
    assert payload["overrides_active"] is True
    (ticket,) = [t for t in payload["tickets"] if t["id"] == "#1"]
    assert ticket["per_tier"]["bench"] == 2      # both lines count as covered
    assert ticket["asserted"]["bench"] == 1      # only line 1 is asserted


def test_overrides_absent_emits_false_and_zero_asserted(tmp_path):
    payload = build_ticket_export(store_without_overrides, ...)
    assert payload["overrides_active"] is False
    assert all(set(t["asserted"].values()) <= {0} for t in payload["tickets"])


def test_determinism_still_byte_stable(tmp_path):
    # regenerate twice with fixed `generated`; assert byte equality —
    # extend/duplicate the module's existing determinism test to a store
    # carrying asserted data.
```

Flesh the store construction out with the module's existing helpers (it already builds ticket-annotated stores).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/cov/test_ticket_export.py -v`
Expected: FAIL (format assertion + missing keys).

- [ ] **Step 3: Implement**

In `ticket_export.py`: `TICKET_EXPORT_FORMAT = 2` (extend its docstring: v2 adds `asserted` + `overrides_active`, additive). In `build_ticket_export`'s accumulation loop, beside `per_tier_of` add `asserted_of: dict[str, dict[str, int]] = {}` and inside the tier loop:

```python
                for tier in store.tier_order:
                    if line.hits.is_hit(tier):
                        tiers[tier] = tiers.get(tier, 0) + 1
                    if tier in line.asserted:
                        a = asserted_of.setdefault(ticket_id, {})
                        a[tier] = a.get(tier, 0) + 1
```

(restructure so `asserted_of.setdefault` mirrors `per_tier_of`'s hoisting). In the per-ticket emit, after `"per_tier": per_tier,`:

```python
                "asserted": {
                    tier: asserted_of.get(ticket_id, {}).get(tier, 0)
                    for tier in store.tier_order
                },
```

Top-level payload, after `"traversal"`: `"overrides_active": bool(store.overrides),`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/cov/test_ticket_export.py tests/unit/cli/test_cov.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
nox -s lint
git add src/otto/coverage/ticket_export.py tests/unit/cov/test_ticket_export.py
git commit -m "feat(cov)!: tickets.json v2 — per-tier asserted counts and overrides_active" -m "Assisted-by: Claude Fable 5"
```

---

### Task 9: SPA data contract — Python emission + TS types (format 2)

**Files:**
- Modify: `src/otto/coverage/renderer/spa_data.py`
- Modify: `web/src/covapp/types.ts`
- Modify: `tests/_fixtures/covapp_ticket_contract.json`
- Test: `tests/unit/cov/test_spa_data.py`, `tests/unit/cov/test_covapp_ticket_contract.py` (auto — reads emitted payloads), `web/src/covapp/contract.test.ts`

**Interfaces (the wire contract every later task reads):**
- `OTTO_COV_DATA_FORMAT = 2` (spa_data.py:39) and `EXPECTED_DATA_FORMAT = 2` (types.ts:11) — same commit.
- `LineJson` gains `asserted?: Record<string, number[]>` (tier → override-entry ids; omitted when empty) — emitted by `_line_to_json`.
- `IndexPayload` gains `overrides: OverrideJson[]` where `OverrideJson = { id: number; tier: string; key: string; reason: string; as_of: string | null }` — from `store.overrides`, in id order.
- `Stats.lines` gains `asserted_per_tier: Record<string, number>` (count of lines whose hits in that tier are override-sourced), rolled up through `_empty_stats`/`_add_stats`/`_file_stats` exactly like `per_tier`.
- `TicketSummary` and `TicketTotals` each gain `asserted: Record<string, number>` (per-tier counts; totals deduped like the rest of `tickets_totals`).
- Contract JSON: `"asserted"` added to `ticket_summary_keys` and `ticket_totals_keys`.

- [ ] **Step 1: Write the failing Python tests**

Append to `tests/unit/cov/test_spa_data.py` (reuse its store builders):

```python
def test_line_json_carries_asserted_map_and_omits_when_empty(...):
    # line with asserted={"bench":[0]} -> {"asserted": {"bench": [0]}} present;
    # plain line -> no "asserted" key.

def test_index_payload_carries_overrides_table_in_id_order(...):
    # store.overrides = two records (ids 1, 0 appended out of order) ->
    # payload["overrides"] == sorted by id, each
    # {"id","tier","key","reason","as_of"}.

def test_stats_roll_up_asserted_per_tier(...):
    # file with 2 bench-asserted lines + 1 really-hit ->
    # tree stats lines.asserted_per_tier["bench"] == 2 at file and root.

def test_ticket_summary_and_totals_carry_asserted(...):
    # per-ticket "asserted" counts; deduped tickets_totals["asserted"].

def test_data_format_is_2(...):
    # payload["format"] == 2
```

Write real bodies against the module's existing fixtures.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/cov/test_spa_data.py tests/unit/cov/test_covapp_ticket_contract.py -q`
Expected: new tests FAIL.

- [ ] **Step 3: Implement Python side**

In `spa_data.py`:
1. `OTTO_COV_DATA_FORMAT: int = 2`.
2. `_line_to_json`: after the `ticket` guard — `if lr.asserted: d["asserted"] = {tier: list(ids) for tier, ids in lr.asserted.items()}`.
3. `_empty_stats`: `"lines"` gains `"asserted_per_tier": dict.fromkeys(tier_order, 0)`.
4. `_add_stats`: after the lines-per_tier loop —
   ```python
    for tier, n in source["lines"]["asserted_per_tier"].items():
        target["lines"]["asserted_per_tier"][tier] = (
            target["lines"]["asserted_per_tier"].get(tier, 0) + n
        )
   ```
5. `_file_stats`: after the per_tier loop —
   ```python
    for tier in tier_order:
        stats["lines"]["asserted_per_tier"][tier] = sum(
            1 for lr in lines if tier in lr.asserted
        )
   ```
6. `_build_ticket_summaries`: add `asserted_of: dict[str, dict[str, int]] = {}` and `total_asserted: dict[str, int] = dict.fromkeys(store.tier_order, 0)`; in the dedup branch (`if line.ticket:`) count `total_asserted[tier] += 1` for `tier in store.tier_order if tier in line.asserted`; in the per-ticket branch mirror `per_tier_of` with `tier in line.asserted`; emit `"asserted": {tier: asserted_of.get(ticket_id, {}).get(tier, 0) for tier in store.tier_order}` in each summary (after `per_tier`) and `"asserted": total_asserted` in `tickets_totals`.
7. `build_index_payload`: after `"runs"`: `"overrides": [o.to_dict() for o in sorted(store.overrides, key=lambda o: o.id)],`.

- [ ] **Step 4: Implement TS side + contract**

`web/src/covapp/types.ts`:
1. `EXPECTED_DATA_FORMAT = 2`.
2. `LineJson`: `asserted?: Record<string, number[]>;` with a doc comment (`tier -> override-entry ids into IndexPayload.overrides; present only while the tier's sole hits are override-sourced`).
3. New interface + `IndexPayload.overrides: OverrideJson[]`:
   ```ts
   /** One asserted manual-override entry (`store.overrides`, v6). */
   export interface OverrideJson {
     id: number;
     tier: string;
     key: string;
     reason: string;
     as_of: string | null;
   }
   ```
4. `Stats["lines"]` gains `asserted_per_tier: Record<string, number>;`; `TicketSummary` and `TicketTotals` gain `asserted: Record<string, number>;`.

`tests/_fixtures/covapp_ticket_contract.json`: insert `"asserted"` (alphabetical) into `ticket_summary_keys` and `ticket_totals_keys`.

Check `web/src/covapp/contract.test.ts` and `tests/unit/cov/test_covapp_ticket_contract.py` — both read the JSON, so they should now pass against the updated emitters/types without edits; if the TS test enumerates interface keys manually, update it to include `asserted`.

- [ ] **Step 5: Run both suites**

Run: `uv run pytest tests/unit/cov/test_spa_data.py tests/unit/cov/test_covapp_ticket_contract.py tests/unit/cov/test_spa_renderer.py -q`
Run: `cd web && npm run test -- --run src/covapp/contract.test.ts src/covapp/data.test.ts && cd ..`
Expected: PASS. (`data.test.ts` guards the format check — it will catch a missed `EXPECTED_DATA_FORMAT` bump; other vitest suites may fail on the new required `Stats`/`IndexPayload` fields in fixtures — update `web/src/covapp/testUtils.tsx` fixture builders with `asserted_per_tier: {}`-style defaults **in this task**, since the type change is what forces them.)

- [ ] **Step 6: Full vitest + lint + commit**

```bash
cd web && npm run test && cd ..
nox -s lint && make lint-ts
git add -A src web tests
git commit -m "feat(cov)!: SPA data format 2 — asserted provenance, overrides table, rollups" -m "Assisted-by: Claude Fable 5"
```

---

### Task 10: file page — asserted marker + expander reason chip

**Files:**
- Modify: `web/src/covapp/pages/FilePage.tsx`
- Test: `web/src/covapp/pages/FilePage.test.tsx`

**Interfaces:**
- Consumes: `LineJson.asserted`, `IndexPayload.overrides` (Task 9).
- Produces: helper `lineAssertedIn(line: LineJson | undefined, tier: string): boolean` (exported for tests); `HitCell` gains `asserted?: boolean` — asserted cells render a **hollow** marker (`◌`-style: the hit number wrapped in a dashed ring) visually distinct from the solid count; `AssertedChip` in the expander showing key + reason; asserted-only lines are expandable.

- [ ] **Step 1: Write the failing tests**

Append to `FilePage.test.tsx` (reuse its render helpers/fixtures from `testUtils.tsx`; extend the fixture chunk with an asserted line):

```tsx
test("asserted line renders the hollow marker, proven line the solid count", ...);
  // line with asserted:{bench:[0]} and hits:{bench:1} -> cell has
  // data-testid "hit-asserted"; a really-hit line renders the plain count
  // and no "hit-asserted" testid.

test("expanding an asserted line shows the override reason", ...);
  // index.overrides = [{id:0, tier:"bench", key:"ticket:#1",
  // reason:"legacy bench pass", as_of:null}] -> expander panel contains
  // "ticket:#1" and "legacy bench pass" (data-testid "asserted-chip").

test("asserted-only line is expandable", ...);
  // line with no run/stale_run but asserted -> the expander chevron renders.
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npm run test -- --run src/covapp/pages/FilePage.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `FilePage.tsx`:

1. Helper near `collectRunIds`:
   ```tsx
   /** Whether `tier`'s hits on this line are override-sourced (spec §3: the
    * marker exists only while the tier's SOLE hits come from an override —
    * the emitter enforces that, so presence of the key is the whole test). */
   export function lineAssertedIn(line: LineJson | undefined, tier: string): boolean {
     return (line?.asserted?.[tier]?.length ?? 0) > 0;
   }
   ```
2. `HitCell`:
   ```tsx
   function HitCell({ value, asserted = false }: { value: number | null; asserted?: boolean }) {
     if (value === null || value === 0) {
       return (
         <span aria-hidden className="text-quaternary opacity-45 tabular-nums">
           ·
         </span>
       );
     }
     if (asserted) {
       return (
         <span
           data-testid="hit-asserted"
           title="asserted by a manual-testing override — not proven by a recorded run"
           className="inline-flex items-center justify-center rounded-full border border-dashed
             border-current px-1 text-tertiary tabular-nums opacity-80"
         >
           {value}
         </span>
       );
     }
     return <span className="text-tertiary tabular-nums">{value}</span>;
   }
   ```
3. `buildCells`: `cells.push(<HitCell key={tier} value={...} asserted={lineAssertedIn(line, tier)} />);` (leave `buildCellsFocused` alone — a focused context shows run-scoped numbers, and an asserted line has no member-run hits by construction).
4. `AssertedChip` beside `RunChip`:
   ```tsx
   /** Expander chip for one asserted override ref: dashed-border variant of
    * RunChip so provenance reads as "declared", never "recorded". */
   function AssertedChip({ entryId, index }: { entryId: number; index: IndexPayload }) {
     const entry = index.overrides.find((o) => o.id === entryId);
     if (!entry) return null;
     return (
       <span
         data-testid="asserted-chip"
         className="inline-flex items-center gap-1.5 rounded-full border border-dashed
           border-secondary bg-primary px-2.5 py-1 text-xs font-medium text-secondary"
       >
         <span
           aria-hidden
           className="size-2 shrink-0 rounded-sm border border-current"
           style={{ borderColor: index.tier_colors[entry.tier] ?? "currentColor" }}
         />
         {entry.key}
         <span className="text-quaternary">{entry.reason}</span>
       </span>
     );
   }
   ```
5. `renderExpansionFor`: collect asserted ids too —
   ```tsx
     const line = chunk.lines[String(codeLine.number)];
     if (!line) return null;
     const runIds = collectRunIds(line);
     const assertedIds = [...new Set(Object.values(line.asserted ?? {}).flat())];
     if (runIds.size === 0 && assertedIds.length === 0) return null;
     return (
       <>
         {[...runIds].map((id) => (
           <RunChip key={id} id={id} line={line} index={index} />
         ))}
         {assertedIds.map((id) => (
           <AssertedChip key={`a${id}`} entryId={id} index={index} />
         ))}
       </>
     );
   ```
6. The `expandable:` computation (~:529): `expandable: collectRunIds(line).size > 0 || Object.keys(line?.asserted ?? {}).length > 0` (match the existing expression's exact shape at that site).

- [ ] **Step 4: Run tests**

Run: `cd web && npm run test -- --run src/covapp/pages/FilePage.test.tsx`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
make lint-ts
git add web/src/covapp
git commit -m "feat(web): asserted-coverage marker and reason chip on the coverage file page" -m "Assisted-by: Claude Fable 5"
```

---

### Task 11: "hide asserted" toggle (⋮ menu) + overrides badge + recompute

**Files:**
- Modify: `web/src/covapp/focus.tsx`, `web/src/covapp/chrome/AppShell.tsx`, `web/src/covapp/format.ts`, `web/src/covapp/tickets.ts`, `web/src/covapp/pages/FilePage.tsx`, `web/src/covapp/pages/DirectoryPage.tsx`, `web/src/covapp/pages/TicketsPage.tsx`
- Test: `web/src/covapp/focus.test.tsx`, `chrome/AppShell.test.tsx`, `format.test.ts`, page tests

**Interfaces:**
- Produces: `useFocus()` gains `hideAsserted: boolean` and `setHideAsserted(v: boolean): void`, following the existing `ticket` slot exactly (`?asserted=1` hash param `ASSERTED_PARAM = "asserted"`, storage key `otto-cov:<stamp>:asserted`, same entry-stamping/adopt-vs-reassert contract, toast on change). Stats helpers gain a `hideAsserted` parameter: `tierRows(index, stats, hideAsserted=false)`, `chunkTierRows(index, chunk, hideAsserted=false)`, `ticketTreeRow`/`ticketFileRow` likewise.
- Semantics of hiding: a line/count "asserted in tier T" is **subtracted from T's hit count and from the all-tiers hit count when no other tier really hits it** — tree pages subtract `stats.lines.asserted_per_tier[tier]` (and `TicketSummary.asserted` / `tickets_totals.asserted` on the tickets page); the file page recomputes per line via `lineAssertedIn`. Totals (denominators) never change. The scope line of the StatsCard appends " · asserted hidden" while active (never silent).

- [ ] **Step 1: Write the failing tests**

`format.test.ts`:

```ts
test("chunkTierRows subtracts asserted lines when hideAsserted", ...);
  // chunk: line1 asserted bench, line2 real bench hit, line3 unhit.
  // default -> bench [2,3]; hideAsserted -> bench [1,3]; "all" row drops
  // line1 too when no other tier hits it.

test("tierRows subtracts asserted_per_tier when hideAsserted", ...);
```

`focus.test.tsx`: mirror the existing `ticket` param/localStorage/back-forward tests for `asserted` (copy the nearest `ticket` describe block, adjust param + storage key + boolean shape).

`AppShell.test.tsx`:

```ts
test("overflow menu shows the hide-asserted toggle when overrides exist", ...);
  // index.overrides non-empty -> menu item data-testid "toggle-hide-asserted";
  // clicking flips useFocus().hideAsserted (assert via rendered ✓ / callback).

test("no overrides -> no toggle and no badge", ...);

test("overrides badge lists entries", ...);
  // badge data-testid "overrides-badge" shows count; opening lists
  // key + tier + reason rows (data-testid "override-entry").
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npm run test -- --run src/covapp/format.test.ts src/covapp/focus.test.tsx src/covapp/chrome/AppShell.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement `focus.tsx` slot**

Clone the `ticket` slot end-to-end: `ASSERTED_PARAM = "asserted"` (serialized as `"1"`/absent), `assertedStorageKey(stamp)` → `` `otto-cov:${stamp}:asserted` ``, `initialHideAsserted`, hashchange adopt/reassert handling, `setHideAsserted` (toast "Asserted coverage hidden"/"shown"), extend `UseFocusResult` with `hideAsserted: boolean; setHideAsserted: (v: boolean) => void`. Follow the file's own doc-comment conventions — every exported symbol there is documented.

- [ ] **Step 4: Implement recompute helpers**

`format.ts` — `chunkTierRows(index, chunk, hideAsserted = false)`: inside the line loop:

```ts
    const assertedTiers = hideAsserted ? Object.keys(line.asserted ?? {}) : [];
    const really = (tier: string) =>
      (line.hits[tier] ?? 0) > 0 && !assertedTiers.includes(tier);
    if (index.tier_order.some(really)) lineHitAll++;
    for (const tier of index.tier_order) {
      if (really(tier)) lineHitByTier[tier] = (lineHitByTier[tier] ?? 0) + 1;
    }
```

(replacing the current two hit computations; when `hideAsserted` is false `assertedTiers` is `[]` and the behavior is byte-identical — keep branch coverage untouched, asserted never contributes branches). `tierRows(index, stats, hideAsserted = false)`: per-tier `hit - (hideAsserted ? (stats.lines.asserted_per_tier[tier] ?? 0) : 0)`; the "all"-row hit uses the store's deduped equivalent — the tree stats have no cross-tier dedup for asserted, so compute the all-row as `stats.lines.hit - (hideAsserted ? assertedOnlyTotal(stats) : 0)` where `assertedOnlyTotal` needs a per-node count of lines whose *every* hit tier is asserted. **Add that as a second rollup field in Task 9's `Stats.lines`: `asserted_only: number`** (Python: `sum(1 for lr in lines if lr.asserted and all(t in lr.asserted for t in lr.hits.counts if lr.hits.counts[t] > 0))` — a line is asserted-only when every tier that hits it is asserted) — go back and add it to Task 9's emitters/types/tests if executing in order; if Task 9 is already committed, add it here as an amendment to `spa_data.py`/`types.ts` with its own unit test. `ticketFileRow`/`ticketTreeRow` (`tickets.ts`): same subtraction from `TicketSummary.asserted` / per-file `asserted` — mirror the pattern.

- [ ] **Step 5: Implement AppShell menu + badge**

In `AppShell.tsx`'s `Dropdown` menu, after the Focus-context section and only when `index.overrides.length > 0`, add a section header "Overrides" with:
- a `FocusMenuItem`-shaped toggle row (`testId="toggle-hide-asserted"`, label "Hide asserted coverage", `active={hideAsserted}`, `onAction={() => setHideAsserted(!hideAsserted)}`, no dot color);
- one disabled `KeyRow`-style row per entry (`data-testid="override-entry"`): dashed tier dot + `entry.key` + truncated `entry.reason`, `title={entry.reason}`.

Badge: next to the nav links, when overrides exist —

```tsx
<span
  data-testid="overrides-badge"
  title="manual-testing overrides are active — open the ⋮ menu for the list"
  className="rounded-full border border-dashed border-secondary px-2 text-xs text-tertiary"
>
  {index.overrides.length} override{index.overrides.length === 1 ? "" : "s"}
</span>
```

(The ⋮ menu holds the listing — the badge is the always-visible indicator; this satisfies spec §6's badge + list with one source of truth.)

- [ ] **Step 6: Thread through pages**

`FilePage.tsx`: `const { hideAsserted } = useFocus();` → pass to `chunkTierRows`/`ticketFileRow`; when hiding, render asserted cells as HitCell value 0 (pass `asserted && !hideAsserted` and `value={hideAsserted && lineAssertedIn(line, tier) ? 0 : ...}`) and suppress row tinting from asserted-only tiers by feeding the same predicate into `rowClassFor`'s inputs (adjust at the call site, not inside `rowClassFor`). `DirectoryPage.tsx`/`TicketsPage.tsx`: pass `hideAsserted` into their `tierRows`/`ticketTreeRow`/totals reads; append " · asserted hidden" to the StatsCard scope line while active (find the scope-line construction in `format.ts` — `statsScopeLabel` or the nearest equivalent — and give it the flag).

- [ ] **Step 7: Run tests, lint, commit**

```bash
cd web && npm run test && cd ..
make lint-ts && nox -s lint
git add -A web src tests
git commit -m "feat(web): hide-asserted toggle, overrides badge, and asserted-aware stat recompute" -m "Assisted-by: Claude Fable 5"
```

---

### Task 12: browser e2e pins

**Files:**
- Modify: `tests/_fixtures/_report_fixture.py`
- Create: `tests/e2e/cov/report_browser/test_spa_asserted.py`

**Interfaces:**
- Consumes: everything above; `build_fixture_report(base_dir) -> Path`.

- [ ] **Step 1: Extend the fixture**

In `_report_fixture.py`, extend the built store with: a `bench` manual tier; one line asserted in `bench` (`line.asserted = {"bench": [0]}`, `hits.add("bench", 1)`); one line really hit in `bench`; `store.overrides = [OverrideRecord(id=0, tier="bench", key="ticket:PROJ-204", reason="legacy bench regression pass", as_of=None)]`. Keep every existing count assertion in the spa test modules green — adjust their expected totals where the new lines shift them (do it in the same commit; the fixture is shared).

- [ ] **Step 2: Write the browser tests**

`test_spa_asserted.py`, module shape copied from `test_spa_tickets.py` (`pytestmark = [pytest.mark.hostless, pytest.mark.browser]`, local `_goto`):

```python
def test_asserted_marker_renders_distinct_from_proven(page, report_dir):
    # file page: locator('[data-testid="hit-asserted"]') visible on the
    # asserted line; the really-hit line's cell has no such testid.

def test_expanding_asserted_line_shows_reason(page, report_dir):
    # click the expander -> [data-testid="asserted-chip"] contains
    # "legacy bench regression pass".

def test_overrides_badge_and_menu_listing(page, report_dir):
    # [data-testid="overrides-badge"] shows "1 override"; open ⋮ ->
    # [data-testid="override-entry"] lists ticket:PROJ-204.

def test_hide_asserted_recomputes_and_announces(page, report_dir):
    # read the bench StatsCard cell, toggle via ⋮ menu, assert the number
    # dropped and the scope line contains "asserted hidden"; toggle back.

def test_hide_asserted_composes_with_run_focus(page, report_dir):
    # pin a focus context, then hide asserted — both chips/labels active,
    # no crash, file page renders.
```

Write full bodies with explicit locators (mirror the waiting/locator discipline in the sibling modules — Playwright `count()` doesn't retry; use `expect`).

- [ ] **Step 3: Build + run (chromium first)**

```bash
make web
uv run pytest tests/e2e/cov/report_browser/test_spa_asserted.py --browser chromium -v
uv run pytest tests/e2e/cov/report_browser -q --browser chromium
```
Expected: PASS (new + existing spa modules against the extended fixture).

- [ ] **Step 4: Commit**

```bash
nox -s lint
git add tests/_fixtures/_report_fixture.py tests/e2e/cov/report_browser
git commit -m "test(cov): browser pins for asserted markers, badge, and hide-asserted toggle" -m "Assisted-by: Claude Fable 5"
```

(The full `nox -s dashboard` matrix runs once in Task 14 — bare pytest here is the chromium-only lane by design.)

---

### Task 13: docs

**Files:**
- Modify: `docs/guide/coverage.md` (new "Manual-testing overrides" section + `tickets.json` v2 compat note ~:1015)
- Modify: `docs/guide/cli-reference.md` (`--tickets-json` notes ~:188/:386 if they enumerate payload keys)
- Modify: `docs/architecture/subsystems/coverage/attribution.md` (reattribution hook + asserted application)

- [ ] **Step 1: Guide section**

Add after the per-ticket section in `coverage.md`: the file format (the spec §2 example verbatim), every validation rule as a bulleted contract, the `as_of` ruling and its silent-drift rationale, aging-by-content ("a rewritten line migrates to the newer commit and drops out — no cache"), the honesty model (marked marker, hide-asserted toggle in the ⋮ menu, a real run clears the mark), the prune-signal log lines with both causes quoted, and `[coverage.overrides] file` for the custom path. Update the `tickets.json` section: format 2, `asserted`, `overrides_active`, compat-policy bullet noting the additive v1→v2 change.

- [ ] **Step 2: Architecture page**

`attribution.md`: one subsection — reattribution replaces extraction at `attribute_tickets(reattributions=...)`; asserted entries resolve against `per_line_sha` + `ticket_commits` bounded by one `rev-list --first-parent` (constant subprocess count preserved).

- [ ] **Step 3: Build docs clean**

Run: `make docs` (or the repo's clean-rebuild docs gate — incremental sphinx misses broken refs; use the clean build).
Expected: zero warnings (`-W`).

- [ ] **Step 4: Commit**

```bash
git add docs
git commit -m "docs(cov): manual-testing overrides guide, tickets.json v2, attribution notes" -m "Assisted-by: Claude Fable 5"
```

---

### Task 14: full gates

- [ ] `nox -s lint && nox -s typecheck`
- [ ] `uv run pytest tests/unit -q`
- [ ] `uv run pytest tests/integration/cov -q`
- [ ] `cd web && npm run test && cd .. && make lint-ts`
- [ ] `make web && nox -s dashboard` (full chromium/firefox/webkit matrix — the browser gate; bare pytest is chromium-only)
- [ ] `make coverage` (the repo's standing merge gate; scoped pytest passing is not sufficient evidence)
- [ ] Fix anything that falls out; commit fixes with conventional prefixes.

---

## Self-review notes (already applied)

- Spec §3 "real run clears the mark": enforced at application time (snapshot-hit lines get no ref) **and** pinned end-to-end in Task 7's `test_real_manual_run_clears_the_asserted_mark`; store never carries a ref for a really-hit line, so renderer/export need no clearing logic.
- Spec §2 "ticket must appear at/before as_of" lives in Task 6 (`_entry_shas`), not the loader — it needs the walk.
- The all-tiers recompute under hide-asserted needs `Stats.lines.asserted_only` — flagged inside Task 11 Step 4 with instructions for both execution orders.
- Constant-subprocess invariant: `rev_parse_commit` is per-entry at **load** time (bounded by file size, not repo/file count) and `rev_list_first_parent` is one call at apply time; `test_git_subprocess_count_is_constant_in_file_count` is unaffected because attribution itself gains no spawn.
