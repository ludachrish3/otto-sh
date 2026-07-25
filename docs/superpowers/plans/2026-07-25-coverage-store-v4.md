# Coverage Store v4 Implementation Plan (Plan B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bump the coverage store to format v4: explicit per-run host identity, report-level thresholds + a type-extensible stats vocabulary, a reserved per-line ticket slot, and `[coverage.report]` settings plumbed end-to-end into the rendered report.

**Architecture:** All changes land in the persisted `store.json` schema (`src/otto/coverage/store/model.py`) and the settings → reporter → renderer thread that feeds it. The capture format (`capture.json`, v2) is untouched; one capture is already exactly one host, so per-host per-line attribution needs no new per-line data — grouping the existing `LineRecord.run_hits` by the new `RunRecord.host` reconstructs it (pinned by test). This is the data contract Plan C's SPA will read; no UI work here.

**Tech Stack:** Python dataclasses (store models), pydantic v2 (`OttoModel` settings specs), Jinja renderer (existing, kept until Plan D), pytest.

**Spec:** `docs/superpowers/specs/2026-07-24-coverage-report-ui-rework-design.md` §6 (data contract), §11 (Migration: "Settings additions: `[coverage.report]` high, medium. Store v4."), §4 (thresholds semantics + the host-pill/per-host UX this data feeds).

## Global Constraints

- `STORE_FORMAT_VERSION` becomes exactly `4`. The loader stays **exact-match, no migration shim**: any other value raises `ValueError` with the existing message shape (`"coverage store format v4 required; found v3 — regenerate with otto cov get/report"`). Never add version tolerance.
- `CAPTURE_FORMAT_VERSION` stays `2` — `capture.json` and `src/otto/coverage/capture/model.py` are not modified by this plan.
- Threshold defaults: `high = 80.0`, `medium = 70.0`. Bucketing semantics (spec §4): `pct >= high → "pct-high"`, `pct >= medium → "pct-mid"`, else `"pct-low"`. (This intentionally replaces the renderer's old hard-coded 75/50 — no test pins those values; verified 2026-07-25.)
- Stats vocabulary: exactly `("line", "branch", "decision")`, emitted as a JSON list `["line", "branch", "decision"]` (spec §6). No producer for `decision` exists — it is a declared slot only.
- New store keys: top-level `"thresholds"` and `"stat_types"` (beside the existing top-level `"tier_order"`/`"tier_colors"` — spec §6's "config block" is this report-level key group, not a nested object); per-run `"host"` (always emitted, `""` when unattributed); per-line `"ticket"` **omitted when absent**, matching the existing `"run"`/`"stale_run"` convention.
- New optional keys are read with `.get(..., default)` in `CoverageStore.load` (existing pattern) — the version gate is the only hard failure.
- `src/otto/models/settings.py` must NOT import `otto.config` at module top (leaf-isolation rule in its module docstring). Runtime resolution of raw settings dicts belongs in `otto.coverage`, mirroring `otto/coverage/tiers.py::load_tiers`.
- Never add `from __future__ import annotations` (breaks Sphinx nitpicky builds).
- Public APIs prefer lists over tuples; callables return dataclasses (the `STAT_TYPES` module constant is a tuple for immutability, but it is serialized and exposed as a list).
- **Dev-VM test discipline:** per task, run only the scoped pytest commands given in that task, plus `uv run nox -s lint`; tasks that touch `src/` also run `uv run nox -s typecheck`. The single full gate (`make coverage`) runs once, in Task 6, from the session's main context. Never pass `-n auto` beyond what the Makefile already does.
- Commits: conventional prefix, end the message with the trailer `Assisted-by: Claude Fable 5`.

---

### Task 1: `[coverage.report]` settings spec + init template

**Files:**
- Modify: `src/otto/models/settings.py` (insert after `CoverageExclusionsSpec`, ~line 306; add field on `CoverageSettingsSpec`, ~line 310-326)
- Modify: `src/otto/cli/init_templates.py` (coverage block, after the `#markers = ["GCOV_EXCL"]` line ~90)
- Test: `tests/unit/models/test_settings_coverage.py`

**Interfaces:**
- Consumes: existing `OttoModel`, `SettingsModel`, `CoverageSettingsSpec` in `src/otto/models/settings.py`; the test file's `_settings(coverage: dict) -> SettingsModel` helper (line 11).
- Produces: `CoverageReportSpec(OttoModel)` with `high: float = 80.0`, `medium: float = 70.0` (both `ge=0.0, le=100.0`, plus `medium <= high` model validator); `CoverageSettingsSpec.report: CoverageReportSpec = CoverageReportSpec()`. Task 5's raw-dict loader mirrors these defaults; the pydantic spec is validation-only (no `to_runtime()`), like its `CoverageTierSpec`/`CoverageExclusionsSpec` siblings.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/models/test_settings_coverage.py`:

```python
def test_report_defaults() -> None:
    s = SettingsModel.model_validate(BASE)
    assert s.coverage.report.high == 80.0
    assert s.coverage.report.medium == 70.0


def test_report_parses_values() -> None:
    s = _settings({"report": {"high": 90, "medium": 75}})
    assert s.coverage.report.high == 90.0
    assert s.coverage.report.medium == 75.0


def test_report_rejects_medium_above_high() -> None:
    with pytest.raises(ValidationError, match="medium"):
        _settings({"report": {"high": 70, "medium": 80}})


def test_report_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        _settings({"report": {"high": 101}})
    with pytest.raises(ValidationError):
        _settings({"report": {"medium": -1}})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/models/test_settings_coverage.py -q`
Expected: the four new tests FAIL (`report` is an extra/unknown field under `extra="forbid"`, or `AttributeError` on `s.coverage.report`).

- [ ] **Step 3: Implement `CoverageReportSpec`**

In `src/otto/models/settings.py`, directly after `CoverageExclusionsSpec`:

```python
class CoverageReportSpec(OttoModel):
    """``[coverage.report]`` — report rendering thresholds (design §4/§11).

    gcovr-style percentage cutoffs: ``pct >= high`` renders green,
    ``pct >= medium`` yellow, below red.  Validation-only, like the other
    coverage specs — the runtime value is re-read from the raw settings
    dict by :func:`otto.coverage.report_config.load_report_thresholds`.
    """

    high: float = Field(default=80.0, ge=0.0, le=100.0)
    medium: float = Field(default=70.0, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def _medium_not_above_high(self) -> "CoverageReportSpec":
        if self.medium > self.high:
            raise ValueError(
                f"[coverage.report] medium ({self.medium}) must not exceed high ({self.high})"
            )
        return self
```

Add `report: CoverageReportSpec = CoverageReportSpec()` to `CoverageSettingsSpec` (beside `exclusions`). Check the module's existing pydantic imports and add `model_validator` if absent.

Ruling recorded here so nobody "fixes" it later: `has_cov_config` (`src/otto/coverage/config.py:21`) deliberately does NOT gain `report` in its `or`-chain — a repo declaring only render thresholds with no collection config is not thereby "the coverage repo"; a report-only block simply falls back to defaults.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/models/test_settings_coverage.py -q`
Expected: PASS (all, including the pre-existing tests).

- [ ] **Step 5: Add the init-template sample lines**

In `src/otto/cli/init_templates.py`, after the `#markers = ["GCOV_EXCL"]` line and before the `# --- [docker]` separator:

```
#[coverage.report]
#high = 80
#medium = 70
```

Grep `tests/` for assertions pinning the init template's coverage block (`grep -rn "coverage.exclusions\|GCOV_EXCL" tests/unit/cli/`) and extend any such pin to cover the new lines.

- [ ] **Step 6: Verify, lint, commit**

Run: `uv run pytest tests/unit/models/test_settings_coverage.py tests/unit/cli -q` then `uv run nox -s lint` and `uv run nox -s typecheck`.
Commit: `feat(cov): add [coverage.report] threshold settings spec`

---

### Task 2: Store v4 — version bump, thresholds, stats vocabulary

**Files:**
- Modify: `src/otto/coverage/store/model.py` (`STORE_FORMAT_VERSION` ~line 29; new `Thresholds` dataclass + `STAT_TYPES` constant near it; `CoverageStore.__init__` ~line 332; `save()` ~line 452; `load()` ~line 493)
- Test: `tests/unit/cov/test_model.py`, `tests/unit/cov/test_renderer.py` (one `"format": 3` fixture literal ~line 80)

**Interfaces:**
- Consumes: existing `CoverageStore` save/load.
- Produces: `STORE_FORMAT_VERSION = 4`; `STAT_TYPES: tuple[str, ...] = ("line", "branch", "decision")`; `@dataclass Thresholds` with `high: float = 80.0`, `medium: float = 70.0`, `to_dict() -> dict[str, float]`; `CoverageStore.thresholds: Thresholds` (instance attribute, default `Thresholds()`). Tasks 3–5 build on this exact naming.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cov/test_model.py` (extend its existing `from otto.coverage.store.model import ...` line with `Thresholds`; `json` is already used by this file):

```python
class TestStoreV4Config:
    def test_save_emits_v4_thresholds_and_stat_types(self, tmp_path) -> None:
        store = CoverageStore(tier_order=["system"])
        p = tmp_path / "store.json"
        store.save(p)
        raw = json.loads(p.read_text())
        assert raw["format"] == 4
        assert raw["thresholds"] == {"high": 80.0, "medium": 70.0}
        assert raw["stat_types"] == ["line", "branch", "decision"]

    def test_thresholds_roundtrip(self, tmp_path) -> None:
        store = CoverageStore(tier_order=["system"])
        store.thresholds = Thresholds(high=90.0, medium=75.0)
        p = tmp_path / "store.json"
        store.save(p)
        loaded = CoverageStore.load(p)
        assert loaded.thresholds == Thresholds(high=90.0, medium=75.0)

    def test_load_defaults_thresholds_when_absent(self, tmp_path) -> None:
        p = tmp_path / "store.json"
        p.write_text('{"format": 4, "tier_order": ["system"], "files": []}')
        loaded = CoverageStore.load(p)
        assert loaded.thresholds == Thresholds()

    def test_load_rejects_v3(self, tmp_path) -> None:
        p = tmp_path / "store.json"
        p.write_text('{"format": 3, "tier_order": [], "files": []}')
        with pytest.raises(ValueError, match="found v3"):
            CoverageStore.load(p)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cov/test_model.py -q`
Expected: the new class FAILS (`ImportError: Thresholds` first); pre-existing tests still pass.

- [ ] **Step 3: Implement the v4 core**

In `src/otto/coverage/store/model.py`:

1. `STORE_FORMAT_VERSION = 4`, and append to its docstring:

```
Version 4 adds the report-level config surface: top-level
``thresholds`` (high/medium render cutoffs from ``[coverage.report]``)
and ``stat_types`` (the type-extensible stats vocabulary — ``line``,
``branch``, and the reserved ``decision`` slot).
```

2. Below the constant:

```python
STAT_TYPES: tuple[str, ...] = ("line", "branch", "decision")
"""The type-extensible stats vocabulary (design §6), emitted into
``store.json`` for UI consumers.  ``decision`` is a declared slot with no
producer yet — consumers render "no decision data" until one exists."""


@dataclass
class Thresholds:
    """Render thresholds (design §4): ``pct >= high`` green, ``>= medium`` yellow."""

    high: float = 80.0
    medium: float = 70.0

    def to_dict(self) -> dict[str, float]:
        return {"high": self.high, "medium": self.medium}
```

3. `CoverageStore.__init__`: add `self.thresholds: Thresholds = Thresholds()` beside `self.runs`.
4. `save()`: add to the `data` dict, after `"tier_colors"`: `"thresholds": self.thresholds.to_dict(),` and `"stat_types": list(STAT_TYPES),`.
5. `load()`: after `store.tier_colors = dict(tier_colors)`:

```python
th = data.get("thresholds") or {}
store.thresholds = Thresholds(
    high=float(th.get("high", 80.0)), medium=float(th.get("medium", 70.0))
)
```

(`stat_types` is declarative output for consumers; `load()` does not carry it — `save()` always re-emits the current constant.)

- [ ] **Step 4: Update the five existing `"format": 3` literals**

- `tests/unit/cov/test_model.py`: fixture literals at ~lines 232 and 347 (`"format": 3` → `4`), assertion at ~line 328 (`raw["format"] == 3` → `== 4`).
- `tests/unit/cov/test_renderer.py`: fixture literal at ~line 80 (`"format": 3` → `4`).
- `tests/unit/cov/test_model.py::test_load_rejects_old_format` (~line 17) keeps its literal `1` — still a wrong version — and is now joined by `test_load_rejects_v3`.

- [ ] **Step 5: Run the scoped suites, lint, commit**

Run: `uv run pytest tests/unit/cov -q` — expected PASS.
Run: `uv run nox -s lint` and `uv run nox -s typecheck`.
Commit: `feat(cov): store v4 — thresholds + stats vocabulary in store.json`

---

### Task 3: `RunRecord.host` — explicit run host identity

**Files:**
- Modify: `src/otto/coverage/store/model.py` (`RunRecord` ~line 153, its `to_dict`, `add_run` ~line 358, `load()` run reconstruction ~line 500)
- Modify: `src/otto/coverage/validity.py` (`register_capture_run`, lines 25-43)
- Modify: `src/otto/coverage/capture/supersede.py` (docstring + `_key` readability, lines 1-17)
- Test: `tests/unit/cov/test_model.py`; the file that already tests `register_capture_run` (find it: `grep -rln register_capture_run tests/` — add there; if none exists, add to `tests/unit/cov/test_model.py` with the imports shown below)

**Interfaces:**
- Consumes: Task 2's v4 store (`STORE_FORMAT_VERSION = 4`).
- Produces: `RunRecord.host: str = ""` (dataclass field after `board`, JSON key `"host"` after `"board"`, always emitted); `CoverageStore.add_run(..., host: str = "")` keyword; `register_capture_run` sets `host=capture.board`. Semantics: `host` is the **host id** (the capture pipeline's `board` field is literally `host.id` — the per-board cov-dir name). It stays `""` for runs with no host attribution: synthetic tier runs (`reporter.py` lines ~379/~393/~575) and the legacy multi-host `.gcda`-merge run, whose lcov merge destroys per-host identity (documented limitation, see Task 6 docs). `label` derivation is unchanged (`display_name or board or tier`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cov/test_model.py`:

```python
class TestRunHost:
    def test_add_run_host_roundtrip(self, tmp_path) -> None:
        store = CoverageStore(tier_order=["nightly"])
        rid = store.add_run(tier="nightly", label="Rig One", board="rig-1", host="rig-1")
        p = tmp_path / "store.json"
        store.save(p)
        raw = json.loads(p.read_text())
        assert raw["runs"][0]["host"] == "rig-1"
        loaded = CoverageStore.load(p)
        assert loaded.runs[rid].host == "rig-1"

    def test_add_run_defaults_host_empty(self) -> None:
        store = CoverageStore(tier_order=["system"])
        rid = store.add_run(tier="system")
        assert store.runs[rid].host == ""

    def test_per_host_lines_derivable_from_run_hits(self) -> None:
        """Plan C's per-host breakdown needs no new per-line data: grouping
        LineRecord.run_hits by RunRecord.host reconstructs per-host line
        counts, because one capture == one host == one run."""
        store = CoverageStore(tier_order=["nightly"])
        r1 = store.add_run(tier="nightly", label="Rig One", host="rig-1")
        r2 = store.add_run(tier="nightly", label="Rig Two", host="rig-2")
        f = FileRecord(path=Path("src/a.c"))
        l1 = f.get_or_create_line(1)
        l1.hits.add("nightly", 1)
        l1.run_hits = {r1: 1}
        l2 = f.get_or_create_line(2)
        l2.hits.add("nightly", 1)
        l2.run_hits = {r1: 1, r2: 1}
        store.merge_file(f)
        per_host: dict[str, int] = {}
        for rec in store.files():
            for line in rec.lines.values():
                for rid in line.run_hits:
                    host = store.runs[rid].host
                    per_host[host] = per_host.get(host, 0) + 1
        assert per_host == {"rig-1": 2, "rig-2": 1}
```

And in the `register_capture_run` test location:

```python
from otto.coverage.capture.model import Capture
from otto.coverage.validity import register_capture_run


def test_register_capture_run_sets_host() -> None:
    cap = Capture(tier="nightly", base_commit="deadbeef", board="rig-1", display_name="Rig One")
    store = CoverageStore(tier_order=["nightly"])
    rid = register_capture_run(store, cap)
    run = store.runs[rid]
    assert run.host == "rig-1"
    assert run.label == "Rig One"
    assert run.board == "rig-1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cov/test_model.py -q` (plus the register test's file)
Expected: FAIL — `add_run() got an unexpected keyword argument 'host'` / `RunRecord has no attribute 'host'`.

- [ ] **Step 3: Implement**

1. `RunRecord`: add `host: str = ""` after `board: str = ""`, with the field comment `# Host id (the capture's board dir name == host.id); "" = no host attribution.` Extend the class docstring's label sentence with: `` `host` is the explicit host identity (v4); label derivation is unchanged.``
2. `RunRecord.to_dict()`: add `"host": self.host,` after `"board": self.board,`.
3. `add_run()`: add keyword `host: str = ""` after `board: str = ""`; pass `host=host` into the `RunRecord(...)` construction.
4. `load()`: add `host=rd.get("host", ""),` after the `board=` line.
5. `validity.py::register_capture_run`: add `host=capture.board,` to the `store.add_run(` call.
6. `supersede.py`: replace the docstring sentence `Plan B's explicit ``host`` field will replace the board component of the key.` with `The third key component is the capture's ``board`` field — the host id — the same value carried into ``RunRecord.host`` at report time (store v4).` and make `_key` self-documenting:

```python
def _key(cap: Capture) -> tuple[str, str, str]:
    host = cap.board
    return (cap.tier, cap.display_name or host, host)
```

(No behavior change in supersede — `tests/unit/cov/test_supersede.py` must pass untouched.)

Extend the `STORE_FORMAT_VERSION` docstring's v4 paragraph with: `` , plus an explicit per-run ``host`` identity.``

- [ ] **Step 4: Run the scoped suites**

Run: `uv run pytest tests/unit/cov tests/integration/cov/test_capture_report_cycle.py -q`
Expected: PASS (the integration cycle exercises `register_capture_run` end-to-end through save/load).

- [ ] **Step 5: Lint, typecheck, commit**

Run: `uv run nox -s lint` and `uv run nox -s typecheck`.
Commit: `feat(cov): store v4 — explicit RunRecord.host identity`

---

### Task 4: Reserved per-line ticket slot

**Files:**
- Modify: `src/otto/coverage/store/model.py` (`LineRecord` ~line 195, `merge()` ~line 214, `FileRecord._line_to_dict` ~line 309, `load()` line reconstruction ~line 532)
- Test: `tests/unit/cov/test_model.py`

**Interfaces:**
- Consumes: Task 2's v4 store.
- Produces: `LineRecord.ticket: str | None = None` — reserved (spec §6: "absent until the per-commit plumbing exists"). JSON key `"ticket"`, emitted only when set. Merge policy: first-set wins (`if self.ticket is None: self.ticket = other.ticket`) — no producer exists, so conflicts are impossible today; the comment says the per-commit plumbing must revisit the policy when it arrives.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cov/test_model.py`:

```python
class TestLineTicketSlot:
    def test_ticket_roundtrip(self, tmp_path) -> None:
        store = CoverageStore(tier_order=["system"])
        f = FileRecord(path=Path("a.c"))
        line = f.get_or_create_line(1)
        line.hits.add("system", 1)
        line.ticket = "PROJ-123"
        store.merge_file(f)
        p = tmp_path / "store.json"
        store.save(p)
        loaded = CoverageStore.load(p)
        assert loaded.files()[0].lines[1].ticket == "PROJ-123"

    def test_ticket_absent_when_unset(self, tmp_path) -> None:
        store = CoverageStore(tier_order=["system"])
        f = FileRecord(path=Path("a.c"))
        f.get_or_create_line(1).hits.add("system", 1)
        store.merge_file(f)
        p = tmp_path / "store.json"
        store.save(p)
        raw = json.loads(p.read_text())
        (line_dict,) = raw["files"][0]["lines"].values()
        assert "ticket" not in line_dict

    def test_merge_keeps_first_set_ticket(self) -> None:
        a = LineRecord(line_number=1, ticket="PROJ-1")
        b = LineRecord(line_number=1, ticket="PROJ-2")
        a.merge(b)
        assert a.ticket == "PROJ-1"
        c = LineRecord(line_number=1)
        c.merge(b)
        assert c.ticket == "PROJ-2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cov/test_model.py -q`
Expected: FAIL — `LineRecord.__init__() got an unexpected keyword argument 'ticket'`.

- [ ] **Step 3: Implement**

1. `LineRecord`: after `stale_runs`, add:

```python
    # Reserved per-line ticket slot (design §6): absent until the
    # per-commit ticket plumbing exists — nothing writes it yet.
    ticket: str | None = None
```

2. `LineRecord.merge()`: add at the end:

```python
        # First-set wins; revisit when a ticket producer exists.
        if self.ticket is None:
            self.ticket = other.ticket
```

3. `FileRecord._line_to_dict()`: after the `stale_run` block:

```python
        if rec.ticket is not None:
            d["ticket"] = rec.ticket
```

4. `load()`: add `ticket=ld.get("ticket"),` to the `LineRecord(` construction.
5. Extend the `STORE_FORMAT_VERSION` docstring's v4 paragraph with: `` and a reserved per-line ``ticket`` slot.``

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/unit/cov -q`, then `uv run nox -s lint` and `uv run nox -s typecheck`.
Commit: `feat(cov): store v4 — reserved per-line ticket slot`

---

### Task 5: Thresholds end-to-end — settings → reporter → store → renderer

**Files:**
- Create: `src/otto/coverage/report_config.py`
- Modify: `src/otto/coverage/reporter.py` (`CoverageReporter.__init__` ~line 220; `run()` where `store = CoverageStore(tier_order=tier_order)` is built ~line 326; `run_coverage_report` ~line 678; `_run_legacy_report` ~line 740; `_run_collection_report` ~line 778)
- Modify: `src/otto/cli/cov.py` (`_resolve_cov_settings` lines 182-205; the `report` command's call ~lines 286-300)
- Modify: `src/otto/suite/run.py` (settings resolution ~lines 301-317; `run_coverage_report` call ~line 333)
- Modify: `src/otto/coverage/renderer/html_renderer.py` (constants lines 51-53; `__init__` ~line 86; `render()` ~line 113; `_pct_class` lines 537-544)
- Test: Create `tests/unit/cov/test_report_config.py`; extend `tests/unit/cov/test_renderer.py`

**Interfaces:**
- Consumes: Task 2's `Thresholds` (in `otto.coverage.store.model`); Task 1's validated `[coverage.report]` block (raw dict shape `{"report": {"high": ..., "medium": ...}}`).
- Produces: `load_report_thresholds(cov_config: dict[str, Any]) -> Thresholds` in `otto.coverage.report_config`; `CoverageReporter(..., thresholds: Thresholds | None = None)` and `run_coverage_report(..., thresholds: Thresholds | None = None)` keywords (`None` → `Thresholds()`); the reporter stamps `store.thresholds` before render/save; `HtmlRenderer._pct_class` reads the store's thresholds (module constants `_PCT_HIGH_THRESHOLD`/`_PCT_MID_THRESHOLD` are deleted).

- [ ] **Step 1: Write the failing loader tests**

Create `tests/unit/cov/test_report_config.py`:

```python
"""[coverage.report] runtime resolution — render thresholds."""

from otto.coverage.report_config import load_report_thresholds
from otto.coverage.store.model import Thresholds


def test_defaults_when_absent() -> None:
    assert load_report_thresholds({}) == Thresholds(high=80.0, medium=70.0)


def test_reads_report_block() -> None:
    cfg = {"report": {"high": 90, "medium": 75}}
    assert load_report_thresholds(cfg) == Thresholds(high=90.0, medium=75.0)


def test_partial_block_keeps_other_default() -> None:
    assert load_report_thresholds({"report": {"high": 95}}) == Thresholds(high=95.0, medium=70.0)
```

Run: `uv run pytest tests/unit/cov/test_report_config.py -q` — expected FAIL (`ModuleNotFoundError`).

- [ ] **Step 2: Implement the loader**

Create `src/otto/coverage/report_config.py`:

```python
"""``[coverage.report]`` runtime resolution — render thresholds.

Mirrors :mod:`otto.coverage.tiers`: the pydantic spec
(:class:`otto.models.settings.CoverageReportSpec`) validates the block at
settings-parse time; this module re-reads the raw dict at report time.
"""

from typing import Any

from .store.model import Thresholds


def load_report_thresholds(cov_config: dict[str, Any]) -> Thresholds:
    """Build render thresholds from a raw ``[coverage]`` settings dict."""
    report = cov_config.get("report") or {}
    return Thresholds(
        high=float(report.get("high", 80.0)),
        medium=float(report.get("medium", 70.0)),
    )
```

Run: `uv run pytest tests/unit/cov/test_report_config.py -q` — expected PASS.

- [ ] **Step 3: Write the failing renderer tests**

Append to `tests/unit/cov/test_renderer.py` (extend its store-model import with `FileRecord, Thresholds`):

```python
class TestThresholdBucketing:
    def test_pct_class_boundaries_at_defaults(self, tmp_path):
        r = HtmlRenderer(tmp_path / "report")
        assert r._pct_class(80.0) == "pct-high"
        assert r._pct_class(79.9) == "pct-mid"
        assert r._pct_class(70.0) == "pct-mid"
        assert r._pct_class(69.9) == "pct-low"

    def test_render_uses_store_thresholds(self, tmp_path):
        src = _write(tmp_path, "half.c", "int a;\nint b;\n")
        store = CoverageStore(tier_order=["system"])
        store.thresholds = Thresholds(high=40.0, medium=30.0)
        fr = store.get_or_create_file(src)
        fr.lines[1] = LineRecord(line_number=1, hits=LineHits(counts={"system": 1}))
        fr.lines[2] = LineRecord(line_number=2)  # uncovered → 50% file pct
        out_dir = tmp_path / "report"
        HtmlRenderer(out_dir).render(store)
        html = (out_dir / "index.html").read_text()
        # 50% >= high(40) → pct-high; impossible under the defaults (80/70 → pct-low).
        assert "pct-high" in html
```

Run: `uv run pytest tests/unit/cov/test_renderer.py -q` — expected FAIL (`_pct_class` is a staticmethod on old constants; store thresholds unused).

- [ ] **Step 4: Rewire the renderer**

In `src/otto/coverage/renderer/html_renderer.py`:

1. Delete the `_PCT_HIGH_THRESHOLD` / `_PCT_MID_THRESHOLD` constants (lines 51-53) and their comment.
2. Extend the existing store-model import (line 44) with `Thresholds`.
3. In `__init__`, before the Jinja env creation: `self._thresholds = Thresholds()`.
4. At the top of `render()`: `self._thresholds = store.thresholds`.
5. Replace the staticmethod:

```python
    def _pct_class(self, pct: float) -> str:
        """Return a CSS class name based on a coverage percentage."""
        if pct >= self._thresholds.high:
            return "pct-high"
        if pct >= self._thresholds.medium:
            return "pct-mid"
        return "pct-low"
```

(The `__init__` Jinja-global registration at line 107 already binds `self._pct_class`; a bound method works unchanged.)

Run: `uv run pytest tests/unit/cov/test_renderer.py -q` — expected PASS.

- [ ] **Step 5: Thread thresholds through the reporter and both entry points**

1. `CoverageReporter.__init__`: add keyword `thresholds: "Thresholds | None" = None` (after `prefix`); set `self.thresholds: Thresholds = thresholds or Thresholds()`; document in the class docstring Args (`thresholds: Render thresholds from [coverage.report]; defaults to Thresholds().`). Import `Thresholds` beside the module's existing store-model imports.
2. In `run()`, immediately after `store = CoverageStore(tier_order=tier_order)`: `store.thresholds = self.thresholds`.
3. `run_coverage_report`: add keyword `thresholds: "Thresholds | None" = None`; pass `thresholds=thresholds` to both `_run_legacy_report` and `_run_collection_report`; each wrapper gains the same keyword and forwards it to `CoverageReporter(...)`. Document in the docstring.
4. `src/otto/cli/cov.py::_resolve_cov_settings`: return a 4-tuple — change the signature to `-> "tuple[Path | None, list[TierConfig] | None, list[str], Thresholds | None]"`, return `None, None, [], None` in the no-cov-repo branch, and `..., load_report_thresholds(cov_config)` otherwise (import `load_report_thresholds` beside the function's existing lazy imports; import `Thresholds` under `TYPE_CHECKING` if needed for the annotation). Update the docstring's tuple description. In the `report` command, unpack the fourth element and pass `thresholds=thresholds` to `run_coverage_report`. The explicit `--tier` branch (the git-less escape hatch that never calls `_resolve_cov_settings`) passes nothing — defaults apply; add a one-line comment saying so.
5. `src/otto/suite/run.py`: beside the lazy `load_tiers` import (~line 301) add `from ..coverage.report_config import load_report_thresholds`; after the `extra_markers = ...` line (~317) add `thresholds = load_report_thresholds(cov_config) if cov_repo is not None else None`; pass `thresholds=thresholds` in the `run_coverage_report` call (~line 333).

- [ ] **Step 6: Reporter-level round-trip test**

Append to `tests/unit/cov/test_report_config.py` (the repo's async convention is `@pytest.mark.asyncio`, `asyncio_mode = "strict"`):

```python
import json

import pytest

from otto.coverage.reporter import run_coverage_report
from otto.coverage.tiers import load_tiers


@pytest.mark.asyncio
async def test_run_coverage_report_stamps_thresholds_into_store_json(tmp_path) -> None:
    out = tmp_path / "report"
    tier_configs = load_tiers({"tiers": {"nightly": {"kind": "e2e", "precedence": 1}}}, None)
    store = await run_coverage_report(
        [],
        out,
        tier_configs=tier_configs,
        thresholds=Thresholds(high=90.0, medium=75.0),
    )
    assert store is not None
    raw = json.loads((out / "store.json").read_text())
    assert raw["thresholds"] == {"high": 90.0, "medium": 75.0}
```

Design note on this construction: passing `tier_configs` (without `repo_root`) selects the collection-model path, which **always** produces a store — no git repo, gcda dirs, or captures needed — so this is git-free and also proves the new kwarg threads through `run_coverage_report` → `_run_collection_report` → `CoverageReporter` → `store.thresholds` → `save()`. (A direct `CoverageReporter` with all-default `CollectionInputs` would take the legacy path and can early-out without writing `store.json` — don't use that shape.) `tests/integration/cov/test_capture_report_cycle.py` line 66 is the working precedent for the `run_coverage_report([], out, tier_configs=load_tiers(...))` call shape; check `load_tiers`'s second parameter (`sut_dir`) signature there if the call errs.

- [ ] **Step 7: Run scoped suites, lint, typecheck, commit**

Run: `uv run pytest tests/unit/cov tests/integration/cov -q`
Run: `uv run nox -s lint` and `uv run nox -s typecheck`.
Commit: `feat(cov): thread [coverage.report] thresholds settings→store→renderer`

---

### Task 6: Docs + full gate

**Files:**
- Modify: `docs/guide/coverage.md` (settings walkthrough, `[coverage]` section ~lines 86-155)
- Modify: `docs/architecture/subsystems/coverage.md` (add a store subsection; "Where the code lives" list ~lines 181-194)

**Interfaces:**
- Consumes: everything shipped in Tasks 1-5.
- Produces: user + architecture documentation for store v4; the plan's single full gate.

- [ ] **Step 1: Guide — `[coverage.report]`**

In `docs/guide/coverage.md`, after the `[coverage.exclusions]` walkthrough, add a `[coverage.report]` subsection documenting: the two keys with defaults (`high = 80`, `medium = 70`), the gcovr-style semantics (≥ high renders green, ≥ medium yellow, below red — applied to every percentage in the report), validation (0-100, `medium ≤ high`, rejected at settings parse), and a TOML example:

```toml
[coverage.report]
high = 80
medium = 70
```

- [ ] **Step 2: Architecture — the store contract**

In `docs/architecture/subsystems/coverage.md`, add a subsection (near the run-table/supersede prose ~line 155) titled `The store (v4)` covering, in prose matching the page's existing density:

- `store.json` is the canonical versioned artifact (`STORE_FORMAT_VERSION = 4`); the loader is exact-match with a loud regenerate error — no migration shims, by design.
- v4 additions: per-run `host` (host id; context identity is (run label, host)); report-level `thresholds` + `stat_types` vocabulary (`line`, `branch`, reserved `decision`); reserved per-line `ticket` slot.
- Per-host breakdowns are **derived**, not stored: one capture == one host == one run, so grouping `LineRecord.run_hits` by `RunRecord.host` reconstructs per-host line counts (pinned by test).
- Known limitation: the legacy multi-host `.gcda`-merge path collapses all hosts into one synthetic run with `host = ""` — lcov merging destroys per-host identity; host attribution needs the per-board capture path.
- Add `otto.coverage.store` (with one line: versioned store models + save/load) and `otto.coverage.report_config` to the "Where the code lives" list — the store module is currently missing from it.

- [ ] **Step 3: Docs gate (clean rebuild)**

Run: `rm -rf docs/_build/html && make docs`
(Clean rebuild is mandatory — incremental Sphinx `-W` misses broken `:doc:` refs. If Playwright screenshot steps time out, the web dist is stale: run `make web` first.)

- [ ] **Step 4: Commit docs**

Commit: `docs(cov): document store v4 contract and [coverage.report] thresholds`

- [ ] **Step 5: Full gate (main session — dev-VM rule)**

Run: `make coverage` from the session's main context (never inside a subagent — its background processes get reaped). Triage any failure via `reports/junit/coverage-python/coverage-python.xml` and `scripts/junit_failures.py`.
Expected: full suite green, coverage floor met. This is the plan's single full-suite run.

---

## Self-review notes (writing-time)

- **Spec coverage:** §6 host field → Task 3; §6 config block (thresholds + vocabulary) → Tasks 2+5; §6 ticket slot → Task 4; §11 settings additions → Task 1; §11 "Store v4" → Task 2; §4 threshold semantics → Task 5; §4 host-pill/per-host UX → data made available + derivability pinned (Task 3), UI itself is Plan C. CLI `--report`→`--dir` (§11) is Plan D scope (rides the renderer swap), deliberately not here.
- **Out of scope, deliberate:** capture format (stays v2); any SPA/renderer replacement; the validity follow-ups in `todo/coverage-validity-followups.md` (Chris ruled 2026-07-25: separate branch).
- **Known accepted gaps:** the CLI/suite glue in Task 5 step 5 (3-line threading in `cli/cov.py` + `suite/run.py`) is covered by typecheck + review + the Task 6 full gate rather than dedicated unit tests — monkeypatching `get_repos` for a 4-tuple unpack test buys little. The `--tier` escape-hatch branch intentionally renders with default thresholds.
- **Type consistency check:** `Thresholds` lives in `otto.coverage.store.model` (schema-owned), imported by `report_config.py`, `reporter.py`, `html_renderer.py`, tests — one definition. `host` naming consistent across `RunRecord`, `add_run`, `register_capture_run`, supersede docstring.
