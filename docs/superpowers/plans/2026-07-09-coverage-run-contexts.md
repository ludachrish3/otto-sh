# Coverage Run Contexts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every coverage run (manual/e2e capture, or synthetic per-tier load) an identity — a "context" — preserved through the store merge, so each line in the HTML report can expand a right-hand "runs" drilldown listing which runs hit it.

**Architecture:** A `ContextRecord` run table on `CoverageStore` replaces `store.provenance`; per-line `context_hits`/`stale_contexts` are recorded by the existing loaders through the same remap/anchor chains as tier hits; the reporter allocates context ids at report time (nothing new persisted to the repo); the renderer adds a pure-CSS `<details>` drilldown column. Spec: `docs/superpowers/specs/2026-07-09-coverage-run-contexts-design.md`.

**Tech Stack:** Python 3.10+ dataclasses/pydantic v2, Jinja2 templates, plain CSS. No new dependencies.

## Global Constraints

- Work in the current worktree (`worktree-coverage-run-contexts`); self-commit per task with a conventional prefix and an `Assisted-by: Claude Fable 5` trailer.
- NEVER add `from __future__ import annotations` (breaks the nitpicky Sphinx gate). Use real 3.10+ annotations with module-top imports.
- Ruff runs with `select=ALL` minus the deny-list; after each task run `uv run ruff check src tests` and `uv run ruff format --check src tests` (implementers routinely miss `format`).
- `ty` only runs at the nox typecheck gate — after all src edits, budget one `make typecheck-python` round.
- All public classes/functions need docstrings (Sphinx `-W` docs gate).
- Tests: hits stored per tier under free-form tier names; every existing behavior must keep passing. Per-task check = the scoped pytest below each task; final gate = `make coverage-hostless` (do NOT run the VM-requiring `make coverage`).
- pytest emits coverage noise via `addopts`; do not pass `-p no:cov` (it errors on the `--cov` addopts).

---

### Task 1: Store model — ContextRecord, run table, per-line context data (+ merge double-count bugfix)

**Files:**
- Modify: `src/otto/coverage/store/model.py`
- Test: `tests/unit/cov/test_model.py`

**Interfaces:**
- Produces: `ContextRecord` dataclass (fields: `id, tier, label, board, labs, captured_at, tester, ticket, note, pin, dirty_remap, aging`); `CoverageStore.contexts: list[ContextRecord]`; `CoverageStore.add_context(*, tier, label=None, board="", labs=None, captured_at="", tester=None, ticket=None, note=None, pin="", dirty_remap=False) -> int`; `LineRecord.context_hits: dict[int, int]`; `LineRecord.stale_contexts: list[int]`. `store.provenance` stays for now (removed in Task 6).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cov/test_model.py` (module already imports `json`, `Path`, `CoverageStore`, `FileRecord`, `LineHits`, `LineRecord`; add `ContextRecord` to the `otto.coverage.store.model` import line):

```python
class TestContexts:
    def test_add_context_allocates_sequential_ids(self):
        store = CoverageStore()
        a = store.add_context(tier="manual", label="rack2-slot4", board="rack2-slot4-id")
        b = store.add_context(tier="system", board="gw-a")
        assert (a, b) == (0, 1)
        assert store.contexts[a].label == "rack2-slot4"
        assert store.contexts[b].label == "gw-a"  # falls back to board

    def test_add_context_label_falls_back_to_tier(self):
        store = CoverageStore()
        cid = store.add_context(tier="unit")
        rec = store.contexts[cid]
        assert rec.label == "unit"
        assert rec.board == ""
        assert rec.pin == ""
        assert rec.aging is False

    def test_line_merge_adds_context_hits_and_unions_stale(self):
        a = LineRecord(line_number=1)
        a.context_hits = {0: 2}
        a.stale_contexts = [1]
        b = LineRecord(line_number=1)
        b.context_hits = {0: 3, 2: 1}
        b.stale_contexts = [1, 3]
        a.merge(b)
        assert a.context_hits == {0: 5, 2: 1}
        assert a.stale_contexts == [1, 3]

    def test_contexts_roundtrip_through_store_json(self, tmp_path):
        store = CoverageStore(tier_order=["manual"])
        cid = store.add_context(
            tier="manual",
            label="slot4",
            board="slot4-id",
            labs=["lab1"],
            captured_at="2026-07-01T00:00:00Z",
            tester={"name": "Alice"},
            ticket="T-1",
            note="n",
            pin="deadbeef",
            dirty_remap=True,
        )
        store.contexts[cid].aging = True
        fr = store.get_or_create_file(Path("/a.c"))
        lr = fr.get_or_create_line(5)
        lr.hits.add("manual", 4)
        lr.context_hits[cid] = 4
        fr.get_or_create_line(6).stale_contexts.append(cid)

        path = tmp_path / "store.json"
        store.save(path)
        raw = json.loads(path.read_text())
        line5 = raw["files"][0]["lines"]["5"]
        assert line5["ctx"] == {"0": 4}
        assert "stale_ctx" not in line5  # omitted when empty
        assert raw["files"][0]["lines"]["6"]["stale_ctx"] == [0]

        loaded = CoverageStore.load(path)
        (lrec,) = [c for c in loaded.contexts]
        assert (lrec.id, lrec.label, lrec.ticket, lrec.aging) == (0, "slot4", "T-1", True)
        assert lrec.dirty_remap is True
        (frec,) = list(loaded.files())
        assert frec.lines[5].context_hits == {0: 4}
        assert frec.lines[6].stale_contexts == [0]

    def test_load_defaults_contexts_for_legacy_file(self, tmp_path):
        legacy = {
            "tier_order": ["system"],
            "files": [{"path": "/a.c", "lines": {"1": {"hits": {"system": 1}, "branches": []}}}],
        }
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps(legacy))
        loaded = CoverageStore.load(path)
        assert loaded.contexts == []
        assert next(iter(loaded.files())).lines[1].context_hits == {}

    def test_file_merge_clone_path_does_not_double_hits(self):
        # Regression: FileRecord.merge's else-branch seeded the clone with
        # copied counts AND then merged them again, doubling every hit.
        a = FileRecord(path=Path("/x.c"))
        b = FileRecord(path=Path("/x.c"))
        lb = b.get_or_create_line(1)
        lb.hits.add("system", 5)
        lb.context_hits = {0: 5}
        a.merge(b)
        assert a.lines[1].hits.for_tier("system") == 5
        assert a.lines[1].context_hits == {0: 5}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m pytest tests/unit/cov/test_model.py -q 2>&1 | tail -5`
Expected: FAIL — `ImportError: cannot import name 'ContextRecord'`.

- [ ] **Step 3: Implement the model changes**

In `src/otto/coverage/store/model.py`:

(a) After the `BranchHits` class, add:

```python
@dataclass
class ContextRecord:
    """One coverage run in the report — a capture or a synthetic per-tier load.

    The run table (``CoverageStore.contexts``) is derived fresh at report
    time from the capture inputs; ``id`` is the record's index into that
    list.  ``label`` is what the drilldown chip shows: the host display
    name when the capture carries one, else the board (host id), else the
    tier name (synthetic contexts pass neither).
    """

    id: int
    tier: str
    label: str
    board: str = ""
    labs: list[str] = field(default_factory=list)
    captured_at: str = ""
    tester: dict[str, str] | None = None
    ticket: str | None = None
    note: str | None = None
    pin: str = ""
    dirty_remap: bool = False
    aging: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation of this run."""
        return {
            "id": self.id,
            "tier": self.tier,
            "label": self.label,
            "board": self.board,
            "labs": list(self.labs),
            "captured_at": self.captured_at,
            "tester": self.tester,
            "ticket": self.ticket,
            "note": self.note,
            "pin": self.pin,
            "dirty_remap": self.dirty_remap,
            "aging": self.aging,
        }
```

(b) `LineRecord`: add two fields after `state`:

```python
    # Per-run traceability (run-contexts spec §5): hits keyed by context id
    # (index into CoverageStore.contexts), and the ids of runs whose
    # evidence for this line was revoked by the manual-validity pass.
    context_hits: dict[int, int] = field(default_factory=dict)
    stale_contexts: list[int] = field(default_factory=list)
```

(c) `LineRecord.merge`: after `self.hits.merge(other.hits)` add:

```python
        for ctx_id, count in other.context_hits.items():
            self.context_hits[ctx_id] = self.context_hits.get(ctx_id, 0) + count
        for ctx_id in other.stale_contexts:
            if ctx_id not in self.stale_contexts:
                self.stale_contexts.append(ctx_id)
```

(d) `FileRecord.merge` else-branch bugfix — replace:

```python
            else:
                clone = LineRecord(
                    line_number=lineno,
                    hits=LineHits(counts=dict(other_line.hits.counts)),
                )
                clone.merge(other_line)  # picks up branches cleanly
                self.lines[lineno] = clone
```

with:

```python
            else:
                # Start empty and let merge copy hits/branches/contexts —
                # pre-seeding the clone with copied counts and then merging
                # doubled every hit.
                clone = LineRecord(line_number=lineno)
                clone.merge(other_line)
                self.lines[lineno] = clone
```

(e) `FileRecord.to_dict`: build each line dict conditionally — replace the `"lines": {...}` comprehension with:

```python
            "lines": {str(lineno): self._line_to_dict(rec) for lineno, rec in self.lines.items()},
```

and add the helper to `FileRecord`:

```python
    @staticmethod
    def _line_to_dict(rec: "LineRecord") -> dict[str, Any]:
        d: dict[str, Any] = {
            "hits": rec.hits.to_dict(),
            "branches": [b.to_dict() for b in rec.branches],
            "state": rec.state,
        }
        if rec.context_hits:
            d["ctx"] = {str(cid): n for cid, n in rec.context_hits.items()}
        if rec.stale_contexts:
            d["stale_ctx"] = list(rec.stale_contexts)
        return d
```

(f) `CoverageStore.__init__`: add `self.contexts: list[ContextRecord] = []` next to `self.provenance`.

(g) Add the allocator method to `CoverageStore`:

```python
    def add_context(
        self,
        *,
        tier: str,
        label: str | None = None,
        board: str = "",
        labs: list[str] | None = None,
        captured_at: str = "",
        tester: dict[str, str] | None = None,
        ticket: str | None = None,
        note: str | None = None,
        pin: str = "",
        dirty_remap: bool = False,
    ) -> int:
        """Register one run and return its context id (index into ``contexts``).

        ``label`` falls back to ``board``, then to ``tier`` — the synthetic
        per-tier contexts pass neither.  Also registers *tier* so the run
        table can never reference an unknown tier.
        """
        self.register_tier(tier)
        ctx_id = len(self.contexts)
        self.contexts.append(
            ContextRecord(
                id=ctx_id,
                tier=tier,
                label=label or board or tier,
                board=board,
                labs=list(labs or []),
                captured_at=captured_at,
                tester=tester,
                ticket=ticket,
                note=note,
                pin=pin,
                dirty_remap=dirty_remap,
            )
        )
        return ctx_id
```

(h) `CoverageStore.save`: add `"contexts": [c.to_dict() for c in self.contexts],` to the `data` dict (keep `"provenance"` for now).

(i) `CoverageStore.load`: in the dict-envelope branch add `contexts_data = data.get("contexts") or []`; in the legacy branch `contexts_data = []`. After `store.tier_colors = ...`:

```python
        for cd in contexts_data:
            store.contexts.append(
                ContextRecord(
                    id=cd["id"],
                    tier=cd["tier"],
                    label=cd["label"],
                    board=cd.get("board", ""),
                    labs=list(cd.get("labs") or []),
                    captured_at=cd.get("captured_at", ""),
                    tester=cd.get("tester"),
                    ticket=cd.get("ticket"),
                    note=cd.get("note"),
                    pin=cd.get("pin", ""),
                    dirty_remap=cd.get("dirty_remap", False),
                    aging=cd.get("aging", False),
                )
            )
```

and in the per-line reconstruction, after `state=ld.get("state")` construction of `lr`, add:

```python
                lr.context_hits = {int(k): v for k, v in (ld.get("ctx") or {}).items()}
                lr.stale_contexts = list(ld.get("stale_ctx") or [])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync python -m pytest tests/unit/cov/test_model.py -q 2>&1 | tail -3`
Expected: all PASS.

- [ ] **Step 5: Run the whole cov unit suite + lint, then commit**

Run: `uv run --no-sync python -m pytest tests/unit/cov -q 2>&1 | tail -3` (expected: all pass) and `uv run ruff check src/otto/coverage tests/unit/cov && uv run ruff format --check src/otto/coverage tests/unit/cov`.

```bash
git add src/otto/coverage/store/model.py tests/unit/cov/test_model.py
git commit -m "feat(cov): ContextRecord run table + per-line context hits in the store model

Also fixes a latent FileRecord.merge clone-path bug that doubled every
hit when merging a line absent from the target record.

Assisted-by: Claude Fable 5"
```

---

### Task 2: Capture `display_name` + produce plumbing + CLI stamping for all tiers

**Files:**
- Modify: `src/otto/coverage/capture/model.py`, `src/otto/coverage/capture/produce.py`, `src/otto/cli/cov.py`
- Test: `tests/unit/cov/test_capture_model.py`, `tests/unit/cov/test_produce.py`, `tests/unit/cli/test_cov.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Capture.display_name: str | None`; `build_capture(..., display_name: str | None = None)`; `produce_captures(..., display_names: dict[str, str] | None = None)` (board-dir name → host display name); CLI helper `_capture_stamps(kind: str, ticket: str | None, note: str | None, tester_name: str | None, tester_email: str | None) -> tuple[dict[str, str] | None, str | None, str | None]` in `src/otto/cli/cov.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cov/test_capture_model.py`:

```python
def test_build_capture_stamps_display_name_and_roundtrips(repo: Path, tmp_path: Path) -> None:
    info = _write_info(tmp_path, repo / "f.c")
    cap = build_capture(
        info_path=info,
        tier="system",
        repo_root=repo,
        board="rack2-slot4-id",
        labs=["lab1"],
        display_name="Rack 2 Slot 4",
    )
    assert cap.display_name == "Rack 2 Slot 4"
    out = tmp_path / "cap.json"
    cap.save(out)
    assert Capture.load(out).display_name == "Rack 2 Slot 4"


def test_capture_display_name_defaults_none_for_old_files(repo: Path, tmp_path: Path) -> None:
    # A capture serialized before the field existed must load with None.
    info = _write_info(tmp_path, repo / "f.c")
    cap = build_capture(info_path=info, tier="system", repo_root=repo, board="b", labs=["lab1"])
    out = tmp_path / "cap.json"
    cap.save(out)
    raw = json.loads(out.read_text())
    raw.pop("display_name")
    out.write_text(json.dumps(raw))
    assert Capture.load(out).display_name is None
```

(`test_capture_model.py` already imports `build_capture`, `Capture`, `_write_info`, `repo`; add `import json` to its imports if absent.)

Append to `tests/unit/cov/test_produce.py` (reuses the `repo` fixture and `fake_capture` pattern shown in `test_produce_writes_per_board_captures`):

```python
@pytest.mark.asyncio
async def test_produce_stamps_display_names_by_board(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cov_dir = tmp_path / "out" / "cov"
    for board in ("board1", "board2"):
        (cov_dir / board).mkdir(parents=True)
        (cov_dir / board / "x.gcda").write_bytes(b"")
    (cov_dir / ".otto_cov_meta.json").write_text(
        f'{{"repo_name": "r", "sut_dir": "{repo}", "toolchains": {{}}, "source_roots": {{}}}}'
    )

    async def fake_capture(self, gcda_dir, gcno_dir, output, toolchain=None):
        output.write_text(f"TN:\nSF:{repo / 'f.c'}\nDA:1,3\nend_of_record\n")
        return output

    monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

    written = await produce_captures(
        cov_dir,
        tier="system",
        repo_root=repo,
        labs=["lab1"],
        display_names={"board1": "Rack 2 Slot 4"},
    )

    by_board = {p.parent.name: Capture.load(p) for p in written}
    assert by_board["board1"].display_name == "Rack 2 Slot 4"
    assert by_board["board2"].display_name is None  # no entry -> not stamped
```

Append to `tests/unit/cli/test_cov.py`:

```python
class TestCaptureStamps:
    """ticket/note stamp every tier kind; tester attribution stays manual-only."""

    def test_e2e_kind_keeps_ticket_and_note_but_no_tester(self):
        from otto.cli.cov import _capture_stamps

        tester, ticket, note = _capture_stamps("e2e", "CI-77", "nightly run", "Al", "al@x")
        assert tester is None
        assert (ticket, note) == ("CI-77", "nightly run")

    def test_manual_kind_resolves_tester(self, monkeypatch):
        import otto.cli.cov as cov_mod

        monkeypatch.setattr(cov_mod, "_resolve_tester", lambda n, e: {"name": n, "email": e})
        tester, ticket, note = cov_mod._capture_stamps("manual", "T-1", None, "Al", "al@x")
        assert tester == {"name": "Al", "email": "al@x"}
        assert (ticket, note) == ("T-1", None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m pytest tests/unit/cov/test_capture_model.py tests/unit/cov/test_produce.py tests/unit/cli/test_cov.py -q 2>&1 | tail -5`
Expected: FAIL — `build_capture` has no `display_name` parameter; `_capture_stamps` does not exist.

- [ ] **Step 3: Implement**

(a) `src/otto/coverage/capture/model.py` — `Capture`: add `display_name: str | None = None` directly after `board: str = ""`. `build_capture`: add keyword param `display_name: str | None = None` (documented: "Host display name to stamp; ``board`` stays the staging-dir/host-id name") and pass `display_name=display_name` in the `Capture(...)` construction.

(b) `src/otto/coverage/capture/produce.py` — `produce_captures`: add keyword param `display_names: dict[str, str] | None = None` (docstring: "Board-dir name (host id) → host display name; boards without an entry are stamped ``None``"), and in the loop pass `display_name=(display_names or {}).get(board)` to `build_capture`.

(c) `src/otto/cli/cov.py` — add near `_resolve_tester`:

```python
def _capture_stamps(
    kind: str,
    ticket: str | None,
    note: str | None,
    tester_name: str | None,
    tester_email: str | None,
) -> tuple[dict[str, str] | None, str | None, str | None]:
    """Resolve the (tester, ticket, note) stamps for a capture run.

    Ticket and note annotate every tier kind (run-contexts spec §4);
    tester attribution stays manual-only — an automated run has no human
    session to attribute.
    """
    tester = _resolve_tester(tester_name, tester_email) if kind == "manual" else None
    return tester, ticket, note
```

In `_do_get`, replace the block

```python
    tester: dict[str, str] | None = None
    produce_ticket: str | None = None
    produce_note: str | None = None
    if resolved_tier.kind == "manual":
        tester = _resolve_tester(tester_name, tester_email)
        produce_ticket = ticket
        produce_note = note
```

with

```python
    tester, produce_ticket, produce_note = _capture_stamps(
        resolved_tier.kind, ticket, note, tester_name, tester_email
    )
```

and in the `produce_captures(...)` call add `display_names={h.id: h.name for h in cov_hosts},`. Also update the comment above (it currently says ticket/note are manual-only) and the `--ticket`/`--note` help text in the module docstring (lines ~65-69) to say they annotate every tier's captures, with `--ticket` still required for manual-kind.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync python -m pytest tests/unit/cov/test_capture_model.py tests/unit/cov/test_produce.py tests/unit/cli/test_cov.py -q 2>&1 | tail -3`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

Run `uv run ruff check src tests && uv run ruff format --check src tests`, then:

```bash
git add src/otto/coverage/capture/model.py src/otto/coverage/capture/produce.py src/otto/cli/cov.py tests/unit/cov/test_capture_model.py tests/unit/cov/test_produce.py tests/unit/cli/test_cov.py
git commit -m "feat(cov): capture display_name + ticket/note stamped for all tier kinds

Assisted-by: Claude Fable 5"
```

---

### Task 3: Loaders and validity — thread ctx ids through every insert path

**Files:**
- Modify: `src/otto/coverage/validity.py`, `src/otto/coverage/correlator/lcov_loader.py`
- Test: `tests/unit/cov/test_validity.py`, `tests/unit/cov/test_lcov_loader.py`

**Interfaces:**
- Consumes: `CoverageStore.add_context`, `LineRecord.context_hits/stale_contexts` (Task 1); `Capture.display_name` (Task 2).
- Produces: `register_capture_context(store: CoverageStore, capture: Capture) -> int` in `validity.py`; `ctx_id: int | None = None` keyword on `_insert_lines`, `load_capture_into_store`, `load_dirty_capture_into_store`, `apply_manual_capture`, and `LCOVLoader.load`. Context credit only for `count > 0` lines. `apply_manual_capture` with a ctx id records stale lines in `stale_contexts` and sets `store.contexts[ctx_id].aging` (provenance-append still present; removed in Task 4).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cov/test_validity.py` (reuses its `repo`, `_capture`, `_find`, `_commit_edit` helpers; add `register_capture_context` to the `otto.coverage.validity` import and `load_capture_into_store` if not imported):

```python
def test_register_capture_context_prefers_display_name(repo: Path) -> None:
    cap = _capture(repo)
    cap.display_name = "Rack 2 Slot 4"
    store = CoverageStore(tier_order=["manual"])
    cid = register_capture_context(store, cap)
    rec = store.contexts[cid]
    assert rec.label == "Rack 2 Slot 4"
    assert rec.board == "b"
    assert rec.ticket == "T-1"
    assert rec.pin == cap.pin


def test_register_capture_context_falls_back_to_board(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    cid = register_capture_context(store, _capture(repo))
    assert store.contexts[cid].label == "b"


def test_apply_manual_capture_credits_context_hits(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    cap = _capture(repo)  # lines {1: 2, 3: 1}
    cid = register_capture_context(store, cap)
    apply_manual_capture(store, cap, repo, max_age_days=None, ctx_id=cid)
    assert _find(store, repo, 1).context_hits == {cid: 2}
    assert _find(store, repo, 3).context_hits == {cid: 1}


def test_stale_line_records_revoked_context(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    cap = _capture(repo)
    cid = register_capture_context(store, cap)
    _commit_edit(repo, "int a;\nint b;\nint CHANGED;\n")  # stales line 3
    apply_manual_capture(store, cap, repo, max_age_days=None, ctx_id=cid)
    line3 = _find(store, repo, 3)
    assert line3.state == "stale"
    assert line3.stale_contexts == [cid]
    assert line3.context_hits == {}  # no credit for the revoked run


def test_aging_capture_flags_its_context_record(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    cap = _capture(repo, "2025-01-01T00:00:00Z")
    cid = register_capture_context(store, cap)
    apply_manual_capture(
        store,
        cap,
        repo,
        max_age_days=180,
        today=datetime(2026, 7, 2, tzinfo=timezone.utc),
        ctx_id=cid,
    )
    assert store.contexts[cid].aging is True
    assert _find(store, repo, 1).context_hits == {cid: 2}


def test_e2e_capture_load_credits_context(repo: Path) -> None:
    store = CoverageStore(tier_order=["system"])
    cap = _capture(repo)
    cid = register_capture_context(store, cap)
    load_capture_into_store(store, cap, repo, ctx_id=cid)
    assert _find(store, repo, 1).context_hits == {cid: 2}
    # zero-count DA lines never credit a context (line 3 has count 1; craft a 0):
    cap0 = _capture(repo)
    cap0.files["f.c"].lines = {2: 0}
    cid0 = register_capture_context(store, cap0)
    load_capture_into_store(store, cap0, repo, ctx_id=cid0)
    assert _find(store, repo, 2).context_hits == {}
```

Append to `tests/unit/cov/test_lcov_loader.py` (it has an existing pattern of building an `.info` file, a `PathCorrelator([])`, and calling `loader.load` — mirror the first test's fixture style):

```python
def test_load_credits_context_id_for_hit_lines(tmp_path):
    from otto.coverage.correlator.lcov_loader import LCOVLoader
    from otto.coverage.correlator.paths import PathCorrelator
    from otto.coverage.store.model import CoverageStore

    info = tmp_path / "x.info"
    info.write_text(f"TN:\nSF:{tmp_path / 'f.c'}\nDA:1,3\nDA:2,0\nend_of_record\n")
    store = CoverageStore()
    ctx = store.add_context(tier="unit")
    loader = LCOVLoader(store, PathCorrelator([]))
    loader.load(info, "unit", ctx_id=ctx)

    (fr,) = list(store.files())
    assert fr.lines[1].context_hits == {ctx: 3}
    assert fr.lines[2].context_hits == {}  # zero-count line: no context credit
    assert fr.lines[1].hits.for_tier("unit") == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m pytest tests/unit/cov/test_validity.py tests/unit/cov/test_lcov_loader.py -q 2>&1 | tail -5`
Expected: FAIL — `register_capture_context` not importable; unexpected `ctx_id` keyword.

- [ ] **Step 3: Implement**

In `src/otto/coverage/validity.py`:

(a) Add after `_insert_branch_triples`:

```python
def register_capture_context(store: CoverageStore, capture: Capture) -> int:
    """Register one capture as a run context; returns its context id.

    The chip label prefers the host display name, then the board (host
    id) — ``add_context`` itself falls back to the tier name, which only
    synthetic contexts use.
    """
    return store.add_context(
        tier=capture.tier,
        label=capture.display_name or capture.board or None,
        board=capture.board,
        labs=capture.labs,
        captured_at=capture.captured_at,
        tester=capture.tester,
        ticket=capture.ticket,
        note=capture.note,
        pin=capture.pin,
        dirty_remap=capture.dirty_remap,
    )
```

(b) `_insert_lines`: signature becomes
`def _insert_lines(file_rec, tier, lines, branches, ctx_id: int | None = None) -> None:` and the line loop becomes:

```python
    for lineno, count in lines.items():
        line_rec = file_rec.get_or_create_line(lineno)
        line_rec.hits.add(tier, count)
        if ctx_id is not None and count > 0:
            line_rec.context_hits[ctx_id] = line_rec.context_hits.get(ctx_id, 0) + count
```

(docstring: note that only executed lines (count > 0) credit the context — a run that instrumented but never hit a line is not listed for it).

(c) `load_capture_into_store` and `load_dirty_capture_into_store`: add `ctx_id: int | None = None` as the last keyword param; pass `ctx_id=ctx_id` to their `_insert_lines` calls.

(d) `apply_manual_capture`: add `ctx_id: int | None = None` keyword param. Changes inside:
- after computing `aging`, add: `if aging and ctx_id is not None: store.contexts[ctx_id].aging = True`
- pass `ctx_id=ctx_id` to `_insert_lines`.
- in the final stale loop, extend:

```python
        for lineno in stale_linenos:
            lr = file_rec.get_or_create_line(lineno)
            if not lr.hits.is_hit():
                lr.state = "stale"
            if ctx_id is not None and ctx_id not in lr.stale_contexts:
                lr.stale_contexts.append(ctx_id)
```

- in the unverifiable branch (diff is None), also record the revoked run on each marked line:

```python
            for lineno in fc.lines:
                lr = file_rec.get_or_create_line(lineno)
                if lr.state is None and not lr.hits.is_hit():
                    lr.state = "stale"
                if ctx_id is not None and ctx_id not in lr.stale_contexts:
                    lr.stale_contexts.append(ctx_id)
```

In `src/otto/coverage/correlator/lcov_loader.py` — `load`: signature `def load(self, info_path: Path | str, tier: str, ctx_id: int | None = None) -> int:` (docstring: optional run-context id credited for every DA line with a nonzero count); in the `DA:` branch after `lr.hits.add(tier, count)`:

```python
                    if ctx_id is not None and count > 0:
                        lr.context_hits[ctx_id] = lr.context_hits.get(ctx_id, 0) + count
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync python -m pytest tests/unit/cov/test_validity.py tests/unit/cov/test_lcov_loader.py -q 2>&1 | tail -3`
Expected: all PASS (old tests too — `ctx_id` defaults keep existing callers working).

- [ ] **Step 5: Lint + commit**

Run `uv run ruff check src tests && uv run ruff format --check src tests`, then:

```bash
git add src/otto/coverage/validity.py src/otto/coverage/correlator/lcov_loader.py tests/unit/cov/test_validity.py tests/unit/cov/test_lcov_loader.py
git commit -m "feat(cov): thread run-context ids through capture loaders and LCOVLoader

Assisted-by: Claude Fable 5"
```

---

### Task 4: Reporter — register contexts at every load site, dedupe captures, drop the validity provenance-append

**Files:**
- Modify: `src/otto/coverage/reporter.py`, `src/otto/coverage/validity.py`
- Test: `tests/unit/cov/test_pipeline.py`, `tests/unit/cov/test_validity.py`

**Interfaces:**
- Consumes: `register_capture_context`, `ctx_id` loader params (Task 3), `store.add_context` (Task 1).
- Produces: `CoverageReporter` behavior — every load registers a context; manual store loads BEFORE e2e captures; both consult a shared dedupe set keyed `(tier, pin, board, captured_at)`; `apply_manual_capture` no longer appends to `store.provenance`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cov/test_pipeline.py` (it already builds `CoverageReporter`/`run_coverage_report` scenarios with tmp git repos and stubbed mergers — mirror its existing fixture idioms; ensure `from pathlib import Path` and `pytest` are imported at its top, which they are today; the tests below use only the capture-path plumbing):

```python
@pytest.mark.asyncio
async def test_duplicate_capture_across_sources_registers_one_context(tmp_path):
    """The same capture in a cov dir AND the manual store folds in once.

    Dedupe key: (tier, pin, board, captured_at). The manual-store copy
    (validity-aware anchor chain) wins because manual loads first.
    """
    import subprocess

    from otto.coverage.capture.gitio import blob_sha, head_commit
    from otto.coverage.capture.model import Capture, CaptureFileCov
    from otto.coverage.capture.store_dir import write_manual_capture
    from otto.coverage.reporter import CollectionInputs, CoverageReporter
    from otto.coverage.tiers import load_tiers

    repo = tmp_path / "sut"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env, capture_output=True)
    (repo / "f.c").write_text("int a;\n")
    subprocess.run(["git", "add", "f.c"], cwd=repo, check=True, env=env, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True, env=env, capture_output=True)

    cap = Capture(
        tier="manual",
        pin=head_commit(repo),
        captured_at="2026-07-01T00:00:00Z",
        ticket="T-1",
        labs=["lab1"],
        board="b1",
        files={"f.c": CaptureFileCov(blob=blob_sha(repo, Path("f.c")), lines={1: 2})},
    )
    write_manual_capture(cap, repo)          # manual-store copy
    dup = tmp_path / "cov" / "b1" / "capture.json"
    dup.parent.mkdir(parents=True)
    cap.save(dup)                            # cov-dir copy of the SAME run

    cov = {"tiers": {"manual": {"kind": "manual", "precedence": 1}}}
    reporter = CoverageReporter(
        [],
        repo,
        tmp_path / "report",
        collection=CollectionInputs(
            repo_root=repo,
            tier_configs=load_tiers(cov),
            capture_paths=[dup],
        ),
    )
    store = await reporter.run()

    assert len(store.contexts) == 1          # deduped to one run
    (fr,) = [f for f in store.files() if f.path.name == "f.c"]
    assert fr.lines[1].hits.for_tier("manual") == 2      # folded once, not twice
    assert fr.lines[1].context_hits == {0: 2}


@pytest.mark.asyncio
async def test_explicit_info_tier_gets_synthetic_context(tmp_path):
    from otto.coverage.reporter import CoverageReporter

    src = tmp_path / "src"
    src.mkdir()
    (src / "f.c").write_text("int a;\n")
    info = tmp_path / "u.info"
    info.write_text(f"TN:\nSF:{src / 'f.c'}\nDA:1,7\nend_of_record\n")

    reporter = CoverageReporter([], src, tmp_path / "report", tiers=[("unit", info)])
    store = await reporter.run()

    (rec,) = store.contexts
    assert (rec.tier, rec.label, rec.pin) == ("unit", "unit", "")
    (fr,) = [f for f in store.files() if f.path.name == "f.c"]
    assert fr.lines[1].context_hits == {rec.id: 7}
```

Also in `tests/unit/cov/test_validity.py`, update the two tests that assert the old provenance-append (`test_unchanged_file_all_valid` asserts `store.provenance[0]["ticket"] == "T-1"`): replace that assertion with

```python
    assert store.provenance == []  # registration is reporter-side now (run contexts)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m pytest tests/unit/cov/test_pipeline.py tests/unit/cov/test_validity.py -q 2>&1 | tail -5`
Expected: new pipeline tests FAIL (`store.contexts` empty / double counts); validity test FAILS (provenance still appended).

- [ ] **Step 3: Implement**

In `src/otto/coverage/validity.py` — `apply_manual_capture`: delete the entire trailing `store.provenance.append({...})` block.

In `src/otto/coverage/reporter.py`:

(a) `run()` — step-3 tier loop gains synthetic contexts:

```python
            for tier_name, tier_path in self.tiers:
                if tier_path is None:
                    if system_info is not None:
                        ctx = store.add_context(tier=tier_name)
                        loader.load(system_info, tier_name, ctx_id=ctx)
                    else:
                        store.register_tier(tier_name)
                else:
                    if not tier_path.exists():
                        logger.warning(
                            "Tier %r .info file not found: %s — skipping",
                            tier_name,
                            tier_path,
                        )
                        store.register_tier(tier_name)
                        continue
                    ctx = store.add_context(tier=tier_name)
                    loader.load(tier_path, tier_name, ctx_id=ctx)
```

(b) `run()` — swap the collection-step order and share a dedupe set (manual first, so the validity-aware anchor-chain path wins over a verbatim cov-dir copy of the same run):

```python
            seen_runs: set[tuple[str, str, str, str]] = set()
            self._load_manual_store(store, seen_runs)
            self._load_captures(store, seen_runs)
            await self._harvest_unit_tiers(localhost, work_dir, loader)
            self._fill_tier_colors(store)
```

(c) Add a small helper near `_load_captures`:

```python
    @staticmethod
    def _run_key(capture: "Capture") -> tuple[str, str, str, str]:
        """Dedupe key: one context per distinct run across all capture sources."""
        return (capture.tier, capture.pin, capture.board, capture.captured_at)
```

(keep the annotation quoted as shown — the reporter already uses quoted `"Toolchain"` annotations with lazy imports, and `"Capture"` follows the same pattern; do NOT add a module-top import.)

(d) `_load_captures(self, store, seen_runs)`: for each loaded capture, before the pin guard's load calls:

```python
            key = self._run_key(capture)
            if key in seen_runs:
                logger.info("Skipping duplicate capture %s (already folded)", cap_path)
                continue
            seen_runs.add(key)
            ctx = register_capture_context(store, capture)
```

(import `register_capture_context` in the function's existing `from .validity import ...` line) and pass `ctx_id=ctx` to both `load_dirty_capture_into_store` and `load_capture_into_store`. NOTE: keep the pin-guard error AFTER the dedupe check so a stale manual duplicate in a cov dir is skipped rather than fatal.

(e) `_harvest_unit_tiers`: allocate one context per tier lazily on first successful load. Two changes only: add `tier_ctx: int | None = None` as the first line inside `for tier in unit_tiers:`, and replace the final two lines of the inner loop

```python
                await merger.capture(hdir, hdir, info_out)
                loader.load(info_out, tier.name)
```

with

```python
                if tier_ctx is None:
                    tier_ctx = loader.store.add_context(tier=tier.name)
                await merger.capture(hdir, hdir, info_out)
                loader.load(info_out, tier.name, ctx_id=tier_ctx)
```

(all the existing dir/gcda checks with their `continue`s stay untouched above; `LCOVLoader` exposes `self.store`.)

(f) `_load_manual_store(self, store, seen_runs)`: inside the loop:

```python
        for capture in load_manual_captures(self.repo_root):
            key = self._run_key(capture)
            if key in seen_runs:
                logger.info("Skipping duplicate manual capture %s (already folded)", capture.ticket)
                continue
            seen_runs.add(key)
            ctx = register_capture_context(store, capture)
            apply_manual_capture(
                store,
                capture,
                self.repo_root,
                max_age_days=max_age_by_tier.get(capture.tier),
                ctx_id=ctx,
            )
```

(add `register_capture_context` to its `from .validity import ...`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync python -m pytest tests/unit/cov/test_pipeline.py tests/unit/cov/test_validity.py -q 2>&1 | tail -3`
Expected: all PASS.

- [ ] **Step 5: Run the whole cov suite (unit + integration) + lint, then commit**

Run: `uv run --no-sync python -m pytest tests/unit/cov tests/integration/cov -q 2>&1 | tail -3`. The integration test still asserts `store2.provenance` — it now FAILS. Update `tests/integration/cov/test_capture_report_cycle.py` lines 81-82 to:

```python
    assert store2.contexts
    assert store2.contexts[0].ticket == "T-9"
```

Re-run; expected: all PASS. Then `uv run ruff check src tests && uv run ruff format --check src tests`, and:

```bash
git add src/otto/coverage/reporter.py src/otto/coverage/validity.py tests/unit/cov/test_pipeline.py tests/unit/cov/test_validity.py tests/integration/cov/test_capture_report_cycle.py
git commit -m "feat(cov): reporter registers run contexts at every load site with cross-source dedupe

Manual store now folds before e2e captures so the validity-aware copy of
a run wins; a cov-dir duplicate of a committed manual capture is skipped
instead of double-counting (or tripping the pin guard).

Assisted-by: Claude Fable 5"
```

---

### Task 5: Renderer — runs drilldown column + run table on the index

**Files:**
- Modify: `src/otto/coverage/renderer/html_renderer.py`, `src/otto/coverage/renderer/templates/file.html`, `src/otto/coverage/renderer/templates/index.html`, `src/otto/coverage/renderer/static/report.css`
- Test: `tests/unit/cov/test_renderer.py`

**Interfaces:**
- Consumes: `store.contexts` (`ContextRecord`), `LineRecord.context_hits/stale_contexts`.
- Produces: file pages with a right-most `runs` column (`<details class="ctx-details">`); index `Captures` section renders from `contexts` (new `Run` label column). Template line dicts gain key `"contexts"`: list of `{"label", "count", "tier_index", "stale", "aging", "tooltip"}`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/cov/test_renderer.py`, add `ContextRecord` to the model import if needed and append:

```python
class TestRunsDrilldown:
    def _store(self, tmp_path):
        src = _write(tmp_path, "f.c", "int a;\nint b;\nint c;\n")
        store = CoverageStore(tier_order=["system", "manual"])
        manual_ctx = store.add_context(
            tier="manual",
            label="Rack 2 Slot 4",
            ticket="T-42",
            captured_at="2026-07-01T00:00:00Z",
        )
        stale_ctx = store.add_context(tier="manual", label="oldrun", ticket="T-9")
        fr = store.get_or_create_file(src)
        lr = fr.get_or_create_line(1)
        lr.hits.add("manual", 5)
        lr.context_hits[manual_ctx] = 5
        lr2 = fr.get_or_create_line(2)
        lr2.state = "stale"
        lr2.stale_contexts.append(stale_ctx)
        fr.get_or_create_line(3)  # uncovered, no contexts
        return store, fr

    def _render(self, tmp_path):
        store, fr = self._store(tmp_path)
        out_dir = tmp_path / "report"
        HtmlRenderer(out_dir).render(store)
        return (out_dir / HtmlRenderer._file_link(fr)).read_text()

    def test_covered_line_lists_run_chip_with_count_and_tooltip(self, tmp_path):
        html = self._render(tmp_path)
        assert "Rack 2 Slot 4" in html
        assert "× 5" in html
        assert "ticket T-42" in html  # tooltip carries the ticket

    def test_stale_line_lists_revoked_run_chip(self, tmp_path):
        html = self._render(tmp_path)
        assert "ctx-stale" in html
        assert "oldrun" in html

    def test_context_free_line_renders_no_details_element(self, tmp_path):
        html = self._render(tmp_path)
        # 3 source rows, only 2 carry a drilldown
        assert html.count("<details") == 2

    def test_runs_column_header_present(self, tmp_path):
        html = self._render(tmp_path)
        assert '<th class="ctx">runs</th>' in html

    def test_index_run_table_shows_labels(self, tmp_path):
        store, _ = self._store(tmp_path)
        out_dir = tmp_path / "report"
        HtmlRenderer(out_dir).render(store)
        index_html = (out_dir / "index.html").read_text()
        assert "Rack 2 Slot 4" in index_html
        assert "T-42" in index_html
```

Also update `TestIndexProvenanceAndLegend.test_provenance_ticket_and_legend_tier_name_appear`: replace the `store.provenance.append({...})` block with

```python
        store.add_context(
            tier="manual",
            label="b1",
            board="b1",
            labs=["lab1"],
            captured_at="2026-07-01T00:00:00Z",
            tester={"name": "Alice"},
            ticket="T-42",
            note="note text",
            dirty_remap=True,
            pin="f" * 40,
        )
```

(assertions unchanged — ticket, legend, and the ✎ glyph must still appear).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m pytest tests/unit/cov/test_renderer.py -q 2>&1 | tail -5`
Expected: new tests FAIL (no runs column/chips; index still reads `store.provenance`).

- [ ] **Step 3: Implement the renderer changes**

In `src/otto/coverage/renderer/html_renderer.py`:

(a) Import `ContextRecord` in the existing `from ..store.model import ...` line.

(b) `render()`: after `tier_colors = ...` add `tier_index = {t: i for i, t in enumerate(tier_order)}`, and pass `store.contexts` + `tier_index` into `_render_file` (extend its signature: `contexts: list[ContextRecord], tier_index: dict[str, int]`).

(c) `_render_index`: replace `provenance=store.provenance,` with `contexts=store.contexts,`.

(d) `_render_file`: pass the two new args through to `_build_line_row`, i.e. the comprehension becomes:

```python
        annotated_lines = [
            self._build_line_row(
                i, text, record.lines.get(i), tier_order, i in excluded_linenos, contexts, tier_index
            )
            for i, text in enumerate(source_lines, start=1)
        ]
```

(e) `_build_line_row`: extend the signature with `contexts: list[ContextRecord], tier_index: dict[str, int]` and add `"contexts": self._build_ctx_entries(lr, contexts, tier_index),` to the returned dict. Add the two helpers:

```python
    @staticmethod
    def _ctx_tooltip(c: ContextRecord) -> str:
        """Tier + ticket/note/date/pin — tells same-host runs apart on hover."""
        parts = [c.tier]
        if c.ticket:
            parts.append(f"ticket {c.ticket}")
        if c.note:
            parts.append(c.note)
        if c.captured_at:
            parts.append(c.captured_at)
        if c.pin:
            parts.append(f"pin {c.pin[:12]}")
        return " · ".join(parts)

    @classmethod
    def _build_ctx_entries(
        cls,
        lr: LineRecord | None,
        contexts: list[ContextRecord],
        tier_index: dict[str, int],
    ) -> list[dict[str, Any]]:
        """Drilldown entries for one line: valid runs first, then revoked ones."""
        if lr is None or not contexts:
            return []
        entries: list[dict[str, Any]] = []
        for ctx_id in sorted(lr.context_hits):
            c = contexts[ctx_id]
            entries.append(
                {
                    "label": c.label,
                    "count": lr.context_hits[ctx_id],
                    "tier_index": tier_index.get(c.tier, 0),
                    "stale": False,
                    "aging": c.aging,
                    "tooltip": cls._ctx_tooltip(c),
                }
            )
        for ctx_id in lr.stale_contexts:
            c = contexts[ctx_id]
            entries.append(
                {
                    "label": c.label,
                    "count": 0,
                    "tier_index": tier_index.get(c.tier, 0),
                    "stale": True,
                    "aging": c.aging,
                    "tooltip": cls._ctx_tooltip(c),
                }
            )
        return entries
```

In `templates/file.html`:

(f) Header row: after `<th class="source">source</th>` add `<th class="ctx">runs</th>`.

(g) Body row: after the `<td class="source">…</td>` cell add:

```html
          <td class="ctx">
            {% if line.contexts %}
            <details class="ctx-details">
              <summary>▾ {{ line.contexts|length }}</summary>
              <div class="ctx-panel">
                {% for c in line.contexts %}
                <span class="ctx-chip{% if c.stale %} ctx-stale{% endif %}{% if c.aging %} ctx-aging{% endif %}" title="{{ c.tooltip }}">
                  <span class="ctx-dot" style="background: var(--tier-{{ c.tier_index }})"></span>{{ c.label }}{% if c.stale %} — STALE{% else %} × {{ c.count }}{% endif %}
                </span>
                {% endfor %}
              </div>
            </details>
            {% endif %}
          </td>
```

In `templates/index.html`:

(h) Replace `{% if provenance %}` with `{% if contexts %}`, `{% for cap in provenance %}` with `{% for cap in contexts %}`, add `<th>Run</th>` as the first header column and `<td>{{ cap.label }}</td>` as the first body cell (the remaining columns keep working: `ContextRecord` fields match the old dict keys except `date` — change `{{ cap.date }}` to `{{ cap.captured_at }}`).

In `static/report.css`, append:

```css
/* Run-context drilldown (run-contexts spec §7) */
.source-table th.ctx, .source-table td.ctx { text-align: right; white-space: nowrap; }
.source-table td.ctx { position: relative; }
details.ctx-details summary {
  cursor: pointer;
  list-style: none;
  color: #777;
  font-size: 0.85em;
}
details.ctx-details summary::-webkit-details-marker { display: none; }
.ctx-panel {
  position: absolute;
  right: 0;
  z-index: 10;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 220px;
  padding: 4px 8px;
  text-align: left;
  background: #fff;
  border: 1px solid #ccc;
  border-radius: 4px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
}
.ctx-chip { font-size: 0.85em; white-space: nowrap; }
.ctx-dot {
  display: inline-block;
  width: 0.7em;
  height: 0.7em;
  margin-right: 0.4em;
  border-radius: 50%;
}
.ctx-chip.ctx-stale { color: var(--state-stale); text-decoration: line-through; }
.ctx-chip.ctx-aging { color: var(--state-aging); }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync python -m pytest tests/unit/cov/test_renderer.py tests/unit/cov/test_html_renderer_prefix.py tests/unit/cov/test_report_fixture.py -q 2>&1 | tail -3`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

Run `uv run ruff check src tests && uv run ruff format --check src tests`, then:

```bash
git add src/otto/coverage/renderer tests/unit/cov/test_renderer.py
git commit -m "feat(cov): per-line runs drilldown column + run table on the report index

Assisted-by: Claude Fable 5"
```

---

### Task 6: Remove `store.provenance` (hard cutover completion)

**Files:**
- Modify: `src/otto/coverage/store/model.py`
- Test: `tests/unit/cov/test_model.py`, `tests/unit/cov/test_validity.py`

**Interfaces:**
- Consumes: everything already migrated (Tasks 4-5). After this task `store.provenance` no longer exists; `store.json` has no `"provenance"` key.

- [ ] **Step 1: Write the failing test**

In `tests/unit/cov/test_model.py`, delete `test_provenance_and_tier_colors_roundtrip` (superseded by `test_contexts_roundtrip_through_store_json`) and rewrite `test_load_defaults_state_provenance_tier_colors_for_legacy_file` — rename to `test_load_defaults_state_contexts_tier_colors_for_legacy_file`, replace `assert loaded.provenance == []` with `assert loaded.contexts == []`. Add:

```python
    def test_store_has_no_provenance_attribute(self, tmp_path):
        store = CoverageStore()
        assert not hasattr(store, "provenance")
        store.add_context(tier="manual", ticket="T-1")
        path = tmp_path / "store.json"
        store.save(path)
        assert "provenance" not in json.loads(path.read_text())
```

- [ ] **Step 2: Run the tests to verify the new one fails**

Run: `uv run --no-sync python -m pytest tests/unit/cov/test_model.py -q 2>&1 | tail -3`
Expected: `test_store_has_no_provenance_attribute` FAILS (attribute exists).

- [ ] **Step 3: Implement**

In `src/otto/coverage/store/model.py`: delete `self.provenance: list[dict[str, Any]] = []` from `__init__`, the `"provenance": list(self.provenance),` line from `save`, and the `provenance = data.get(...)` / `store.provenance = list(provenance)` lines from `load` (both branches). Update the `CoverageStore` docstring sentence about provenance to describe `contexts` instead.

In `tests/unit/cov/test_validity.py`: remove the `assert store.provenance == []` line added in Task 4 (the attribute is gone) — the test keeps its hit/state assertions.

- [ ] **Step 4: Run the full cov suites to verify nothing else references it**

Run: `uv run --no-sync python -m pytest tests/unit/cov tests/integration/cov -q 2>&1 | tail -3` and `grep -rn "provenance" src/otto/coverage/ tests/unit/cov/ tests/integration/cov/` (expected: no hits, or comment-only).
Expected: all PASS, no stray references.

- [ ] **Step 5: Lint + commit**

```bash
git add src/otto/coverage/store/model.py tests/unit/cov/test_model.py tests/unit/cov/test_validity.py
git commit -m "feat(cov)!: store.contexts replaces store.provenance (hard cutover)

Assisted-by: Claude Fable 5"
```

---

### Task 7: Integration scenario, guide docs, and full gates

**Files:**
- Modify: `tests/integration/cov/test_capture_report_cycle.py`, `docs/guide/coverage.md`
- Test: the integration file itself + full gates

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing integration test**

Append to `tests/integration/cov/test_capture_report_cycle.py`:

```python
@pytest.mark.asyncio
async def test_run_contexts_traceable_end_to_end(tmp_path: Path) -> None:
    """Two manual runs on one file: drilldown credits each valid run per line,
    a staled line names the revoked run, and store.json round-trips it all."""
    from otto.coverage.store.model import CoverageStore

    repo = tmp_path / "sut"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True,
            env={**ENV, "HOME": str(tmp_path)},
        )

    git("init", "-q")
    (repo / "f.c").write_text("int a;\nint b;\nint c;\n")
    git("add", "f.c")
    git("commit", "-qm", "init")

    def cap(ticket: str, lines: dict[int, int], display_name: str | None) -> Capture:
        return Capture(
            tier="manual",
            pin=head_commit(repo),
            captured_at=f"2026-07-0{len(ticket)}T00:00:00Z",
            ticket=ticket,
            labs=["lab1"],
            board="b1",
            display_name=display_name,
            files={"f.c": CaptureFileCov(blob=blob_sha(repo, Path("f.c")), lines=lines)},
        )

    write_manual_capture(cap("T-1", {1: 2, 2: 1}, "Rack 2 Slot 4"), repo)
    write_manual_capture(cap("T-22", {2: 3}, None), repo)

    # Edit line 1 → T-1's evidence for it is revoked.
    (repo / "f.c").write_text("int EDITED;\nint b;\nint c;\n")
    git("commit", "-aqm", "edit line 1")

    report = tmp_path / "r"
    store = await run_coverage_report([], report, repo_root=repo, tier_configs=load_tiers(COV))

    by_ticket = {c.ticket: c for c in store.contexts}
    assert by_ticket["T-1"].label == "Rack 2 Slot 4"
    assert by_ticket["T-22"].label == "b1"

    (fr,) = [f for f in store.files() if f.path.name == "f.c"]
    t1, t22 = by_ticket["T-1"].id, by_ticket["T-22"].id
    assert fr.lines[2].context_hits == {t1: 1, t22: 3}      # both runs credited
    assert fr.lines[1].stale_contexts == [t1]               # revoked run named
    assert fr.lines[1].context_hits == {}

    # store.json round-trip preserves the run table + line context data.
    reloaded = CoverageStore.load(report / "store.json")
    (fr2,) = [f for f in reloaded.files() if f.path.name == "f.c"]
    assert fr2.lines[2].context_hits == {t1: 1, t22: 3}
    assert reloaded.contexts[t1].label == "Rack 2 Slot 4"

    # The rendered page carries the drilldown.
    page = next((report / "files").glob("*.html")).read_text()
    assert "Rack 2 Slot 4" in page
    assert "ctx-stale" in page
```

- [ ] **Step 2: Run it to verify current state**

Run: `uv run --no-sync python -m pytest tests/integration/cov -q 2>&1 | tail -3`
Expected: PASS already if Tasks 1-6 are complete (this is the end-to-end proof; if anything fails, fix the responsible task before proceeding).

- [ ] **Step 3: Update the guide**

In `docs/guide/coverage.md`: (a) in the `### Staleness and aging` section, after the whitespace-insensitivity paragraph, add:

```markdown
(coverage-run-contexts)=
### Run contexts: which run covered this line?

Every coverage input becomes a **run context** at report time: each manual
or e2e capture is one run (labelled by the host's display name; hover for
tier, ticket, note, date, and pin), and each unit-tier harvest or legacy
`.info` load gets a synthetic per-tier run.  On a file's annotated page,
the right-hand **runs** column expands per line to list every run that hit
it, colored by tier, with per-run hit counts.  A stale line lists the
revoked run struck through — the ticket to re-verify.  The index's
Captures table is the full run table, and `store.json` carries it
(`contexts` plus per-line `ctx`/`stale_ctx`) for downstream consumers.

`--ticket` and `--note` on `otto cov get` annotate captures of **every**
tier kind (`--ticket` remains required for manual-kind tiers; tester
attribution stays manual-only).
```

(b) Search the guide for other `provenance` mentions (`grep -n provenance docs/guide/coverage.md`) and reword them to the run table / `contexts`.

- [ ] **Step 4: Full gates**

Run, in order, from the worktree root:

1. `uv run ruff check src tests && uv run ruff format --check src tests` — expected clean.
2. `make typecheck-python` — expected clean (budgeted `ty` round; fix any findings).
3. `timeout 1500 make coverage-hostless` — expected: all pass, coverage ≥ 90%.
4. `make docs-lint docs-html` — expected: clean (new guide section, no Sphinx warnings).

- [ ] **Step 5: Commit**

```bash
git add tests/integration/cov/test_capture_report_cycle.py docs/guide/coverage.md
git commit -m "test(cov)+docs: end-to-end run-context traceability scenario + guide section

Assisted-by: Claude Fable 5"
```
