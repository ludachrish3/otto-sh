# Coverage Tier & Collection Model — Design

**Date:** 2026-07-02
**Status:** Revised after second review; awaiting re-review
**Scope:** First of four sub-projects from `todo/coverage_roadmap.md` ("Updates" section). Successors, in dependency order: per-ticket reporting, gcno+DWARF boolean-clause linkage, React/Vite/TS report frontend.

## 1. Context and problem

`otto cov` today ships a mature `.gcda` → lcov → custom-HTML pipeline (`src/otto/coverage/`, ~2,100 lines + ~3,400 lines of tests) covering Unix and Zephyr-embedded targets with gcc and clang (gcov-compatible mode). But:

- Only the `system` tier (e2e via `otto test --cov`) is collected *by otto*. Unit-test and manual tiers require the user to hand-produce `.info` files; tiers exist only as report-time CLI flags (`--tier NAME[=PATH]`) with no durable definition.
- Manual coverage cannot accumulate: raw gcov counters are only mergeable when the entire source is identical, so stored manual data goes stale on the first commit.
- Manual testing frequently happens on **locally modified** builds (printf-and-recompile, GDB-driven). These sessions still run instrumented products, so counters *do* exist — but their line numbers refer to the modified tree and need offset correction to map onto committed code.
- Roadmap-file corrections established during design: git-blame annotation was **never implemented** (only a stub comment at `store/model.py:146`; no blame code exists in git history) and `--cov-fail-under` does not exist. gcc **and** clang support already exists (`host/toolchain_discovery.py`), contrary to the roadmap's top-priority framing; the genuine gap there is clang *source-based* coverage (`-fprofile-instr-generate`), which is out of scope here.

Goal per the roadmap: make 100%-coverage tracking meaningful for **all** testing styles — on-target e2e, build-host unit tests, and manual sessions — with per-ticket reporting as a fast follow.

## 2. Decisions log (from brainstorming Q&A and reviews)

| # | Question | Decision |
| --- | --- | --- |
| 1 | First sub-project | Tier/collection model (this spec) |
| 2 | Tier definition | Declarative in `.otto/settings.toml`; multiple tiers may share a `kind` (e.g. two manual tiers), differentiated by name and color |
| 3 | Manual-data staleness | Pinned line validity: a line's manual coverage holds iff unchanged since the capture's pin |
| 4 | Manual-capture storage | Committed in-repo (`.otto/coverage/manual/`) |
| 5 | Unit-tier collection | Harvest-only: sweep configured build dirs in the **current build tree** at report time; no run discipline imposed |
| 6 | Manual aging | Configurable `max_age`, **flag-only** (never silently drops), off by default |
| 7 | Manual evidence | Always measured counters — GDB and printf-recompile sessions still run instrumented builds. "Attestation" is metadata (required ticket, note, tester) attached to a manual retrieval, not a separate counter-less path |
| 8 | Locally-modified builds | Auto-remap hits through the working-tree diff at retrieval time; hits on added/changed lines are dropped; **no** embedded diff in the capture file |
| 9 | Architecture | Evolve the existing store (Approach 1; ledger rewrite and `.info`-sidecar bolt-on rejected) |
| 10 | Exclusions | Honor `LCOV_EXCL_LINE` / `LCOV_EXCL_START`/`STOP` (+ `_BR_` variants); renderer-aware; custom markers configurable |
| 11 | Pinning scope | **Only manual captures are pinned and committed to the repo.** On-target/e2e data comes from output dirs of previous otto suites; unit data from the current build tree |
| 12 | CLI shape | No `otto cov capture` / `otto cov attest`. A single `otto cov get` retrieves counters; by default it records an e2e capture; selecting a manual-kind tier switches it to a manual capture |
| 13 | Capture layout | `<output_dir>/cov/<board_id>/capture.json` — one file per board. JSON `hosts` key renamed to `labs`; `ticket` required on manual captures |
| 14 | Remap transparency | The `dirty_remap` boolean is sufficient; per-file dropped-hit counts removed |
| 15 | Tester identity | `name` defaults to the OS username running otto; `email` defaults to `git config user.email` when set; both overridable via CLI options |
| 16 | Pin durability | Per-file **blob SHA** recorded alongside the pin; validity prefers blob comparison (rebase-tolerant), falls back to the pin commit, degrades to stale + loud warning when both are unusable — a commit hash is never assumed valid forever |
| 17 | Tier colors | Optional `color` per tier (CSS color name or `#RRGGBB`); defaults e2e = green, unit = yellow, manual = orange; state colors: uncovered = light red, excluded = grey, stale = violet, aging = tan; legend rendered in the report |
| 18 | Debug artifacts | Retrieved `.gcda` and toolchain-produced `.gcov`/`.info` intermediates are kept next to `capture.json` in the output dir (never in the repo store) |

## 3. The capture file

`otto cov get` produces one `capture.json` per board: retrieve all `.gcda` from the target, parse them with the board's toolchain gcov (via the existing lcov wrapping and toolchain discovery), apply the dirty-repo offset correction when the working tree is modified, and write line/branch data in **committed-code line numbers**.

Manual capture (pinned, committed to the repo):

```json
{
  "schema": 1,
  "tier": "manual",
  "pin": "<commit sha>",
  "dirty_remap": true,
  "captured_at": "2026-07-02T18:40:00Z",
  "tester": {"name": "chris", "email": "chriscoll93@gmail.com"},
  "ticket": "PROJ-123",
  "note": "verified failover via GDB",
  "labs": ["lab1"],
  "board": "mps2_an385",
  "files": {
    "src/foo.c": {
      "blob": "<git blob sha of src/foo.c at pin>",
      "lines": {"12": 3, "13": 1},
      "branches": {"12": [[0, 0, 2], [0, 1, 0]]}
    }
  }
}
```

Field semantics:

- `pin`: the commit whose coordinates the line numbers mean (after remap). **Validity is computed at report time, never stored** — aging/staleness policy changes apply retroactively because captures are immutable facts.
- `blob` (per file): the git blob SHA of that file at the pin. This is the primary validity anchor. Blob SHAs are content-addressed, so they survive rebases (a rewritten commit keeps identical blobs for files the rebase didn't change) — the design never depends on the pin commit hash remaining reachable.
- `tester`: `name` = OS username running otto; `email` = `git config user.email` when configured; both overridable via CLI options.
- `ticket`: **required** on manual captures (the roadmap's assumption — all sut code has a ticket — is enforced where human effort is recorded). E2E captures omit it, along with `tester`/`note`.
- `labs` / `board`: provenance — which lab(s) the counters came from and which board produced them.
- `dirty_remap`: true when the working tree was modified at retrieval and the offset correction was applied. The diff itself is not stored, and no per-file drop counts are kept (decisions 8, 14).

E2E variant: same shape with `tier` from config (default `system`), `pin` recorded as a **merge guard** (see §7), no `tester`/`ticket`/`note`.

### Storage by tier

- **Manual**: `capture.json` committed to the sut repo at `.otto/coverage/manual/<utc-stamp>-<slug>.json`. Proof of testing becomes versioned, PR-reviewable, and travels with the code. Only the JSON goes in the repo — it is proof enough and carries all metadata.
- **On-target / e2e**: `<output_dir>/cov/<board_id>/capture.json` inside each otto suite's output dir — not committed; same artifact lifecycle as today's run outputs.
- **Debug artifacts (both of the above)**: the retrieved `.gcda` and the `.gcov`/`.info` files the toolchain gcov produced stay in `<output_dir>/cov/<board_id>/` next to `capture.json`, for debugging coverage-count and merge issues in the field. Never copied into the repo store.
- **Unit**: no capture file. `otto cov report` harvests `.gcda` from the configured `harvest_dirs` in the current build tree at report time.

## 4. Configuration (typed)

`SettingsModel.coverage` changes from `dict[str, Any]` to a typed Pydantic model (feeds `otto schema export` / `make schema`; drift-guard tests updated). Existing keys (`hosts`, `gcda_remote_dir`, `[coverage.embedded]`, `[coverage.embedded.builds."<ver>"]`) keep their shapes, now typed. New:

```toml
[coverage.tiers.system]
kind = "e2e"                 # collected by `otto test --cov` / `otto cov get`
precedence = 1               # lower number = wins winner-take-all coloring
color = "green"              # CSS color name or "#RRGGBB"; per-kind default if omitted

[coverage.tiers.unit]
kind = "unit"
precedence = 2
harvest_dirs = ["build"]     # repo-relative roots swept for .gcda at report time
color = "yellow"

[coverage.tiers.manual]
kind = "manual"
precedence = 3
max_age = "180d"             # optional; flag-only aging
color = "orange"

[coverage.exclusions]
markers = ["MYPROJ_NO_COV"]  # optional additions to the LCOV_EXCL_* set
```

- Multiple tiers may share a `kind` (e.g. `manual_qa` and `manual_dev`, both `kind = "manual"`); name, precedence, and color differentiate them.
- `color` validation at settings load: `#RRGGBB` by regex; names checked against the renderer's known-color list (CSS named colors). Validating names against what the *future frontend* can render is deferred to a later phase (§12).
- Backward compatibility: no `[coverage.tiers]` section → implicit `system` tier only; behavior identical to today. Tier names remain free-form strings; `kind ∈ {e2e, unit, manual}` selects collection machinery.

## 5. CLI surface

| Command | Status | Behavior |
| --- | --- | --- |
| `otto cov get` | **new** | The single retrieval command. Pulls all `.gcda` from the target lab(s) (Unix hosts via the existing `GcdaFetcher`, embedded boards via the console-dump collector), parses them with the discovered toolchain, applies the dirty-tree remap when `git status` is non-empty, and writes `cov/<board_id>/capture.json` per board — plus the raw `.gcda` and `.gcov`/`.info` debug artifacts — into the command's output dir (otto's standard per-command output-dir handling). `--clean` pre-zeroes remote counters. `--tier NAME` selects the target tier: default is the sole `kind = "e2e"` tier (ambiguity → error). Selecting a manual-kind tier requires `--ticket` (plus optional `--note`) and additionally writes the pinned capture into `.otto/coverage/manual/`. Tester identity defaults per decision 15; `--tester-name` / `--tester-email` override. |
| `otto test --cov` | changed | Post-run collection becomes a call to the `otto cov get` machinery — same per-board `capture.json` + debug-artifact layout in the run's output dir. |
| `otto cov report` | changed | Reads: manual captures from the in-repo store (automatic), e2e captures from the output dirs passed, and unit `.gcda` harvested from the current build tree's `harvest_dirs`. Runs the validity pass (§7). `--tier NAME=PATH` remains as an escape hatch for foreign `.info` files (works without git). |
| `otto cov clean` | **new** | Zeroes `.gcda` counters on the lab's Unix coverage hosts (same host selection as `get`, via the existing `clean_remote` machinery). Embedded boards are deferred: counter reset needs a product-side `cov_reset` LLEXT function (later phase); the command says so when the lab has embedded coverage hosts. |

## 6. Hunk remap engine (one implementation, two uses)

New module `coverage/capture/remap.py`: maps line coordinates across a git diff's hunks.

Line classes under a diff:

- **Unchanged** — exists on both sides, shifted by cumulative hunk offset. Hits map exactly (correct even when lines move).
- **Added** — no counterpart on the far side. Hits dropped (the added printfs themselves).
- **Changed** — text differs; crediting the far side would claim untested code. Dropped conservatively.

Use 1 — **retrieval time** (dirty tree): diff *working tree vs HEAD*. gcov/lcov run against the modified tree (where gcov stamps are coherent); hits remap into committed coordinates; `dirty_remap` set true. Auto-engaged when `git status --porcelain` is non-empty. This is exact, not heuristic: the actual diff is in hand.

Use 2 — **report time, manual tier**: diff *capture state vs HEAD* per file, cached. Maps each manual capture's pinned lines onto current source; changed/deleted lines become **stale**. A capture taken at HEAD diffs empty → zero-cost fast path.

## 7. Report-time validity pass

New module `coverage/validity.py`.

**Manual tier** — per file, an anchor lookup chain that never assumes the pin commit still exists (decision 16):

1. Current file's blob SHA == recorded `blob` → file unchanged; **all lines valid** (fast path; rebase-proof).
2. Recorded blob present in the object DB → `git diff <blob> <working file>` → remap; unchanged lines **valid**, changed/deleted **stale**. Still works after rebases, since unmodified files keep their blob objects.
3. Blob gone but pin commit resolvable → diff `pin:<file>` vs HEAD (equivalent result).
4. Neither usable → capture **unverifiable**: treated as stale with a loud per-capture warning naming the remedy (re-capture).

Line states produced:

- **valid** — coverage counts normally.
- **stale** — line changed since capture; coverage does not count; rendered as "needs re-verification."
- **aging** — *valid* line whose capture exceeds the tier's `max_age`; still counts toward covered-by-any-tier (flag-only), rendered distinctly and tallied separately.

Stale vs aging, precisely: **stale = the code changed** out from under the evidence (coverage revoked); **aging = the code is unchanged but the evidence is old** (coverage retained, flagged for re-verification because surrounding behavior may have drifted).

**E2E tier** (output-dir captures): the recorded `pin` acts as a **merge guard**, mirroring today's stamp-mismatch semantics — a capture whose pin differs from HEAD fails the report with a clean error (the parsed line numbers describe a different tree). Remapping stale e2e captures with the same engine is a possible later opt-in (§12).

**Unit tier**: harvested fresh from the current build tree; stamp-mismatch detection (existing) is the guard; no validity states.

`CoverageStore` gains capture-level provenance (contributing captures: tier, pin, tester, date, ticket, note, labs, board, dirty_remap) and per-line validity state, populated only by this pass. Existing per-tier hit dicts, tri-state branches, and precedence logic unchanged. New `PinnedCaptureLoader` alongside `LCOVLoader`; the dead `commit_*` stub fields on `LineRecord` are removed in favor of this mechanism.

## 8. Exclusions

- **Measured path:** lcov's geninfo natively honors `LCOV_EXCL_LINE`, `LCOV_EXCL_START`/`LCOV_EXCL_STOP` (blocks), and `LCOV_EXCL_BR_*` — excluded lines never enter the parsed data, hence never enter denominators. No new pipeline code.
- **Renderer:** cheap regex scan of rendered sources marks excluded lines/blocks visually (grey + per-file excluded count) instead of leaving them indistinguishable from comments.
- **Custom markers:** `[coverage.exclusions] markers` extends the recognized set (wired to lcov rc overrides and the renderer scan).

## 9. Rendering (current Jinja2 stack)

**Line coloring**: each line takes the color of the tier that covers it, resolved winner-take-all by precedence among tiers with *valid* evidence. State colors apply when no valid tier covers the line, in this order: **excluded** (grey, always wins) → valid tier color (e2e green / unit yellow / manual orange by default, or the tier's configured `color`) → **aging** (tan — the winning evidence is valid manual data past `max_age`, i.e. faded orange) → **stale** (violet — the only evidence was manual and the code changed; distinct from plain uncovered so "was verified, needs re-verification" is visible at a glance) → **uncovered** (light red).

**Legend**: with free-form tier names, multiple tiers per kind, and configurable colors, the report cannot rely on convention — the index and every file page render a legend mapping each tier name and state to its color.

The index gains per-file stale/aging/excluded counts and a **provenance table** — every contributing capture with its tier, board, labs, date, and (for manual) tester, ticket, note, and dirty_remap. `store.json` continues to be written, now including validity states, colors, and provenance — it is the explicit data contract for the future React/Vite/TS frontend, so that rewrite touches no pipeline code.

## 10. Error handling

All new failures follow the `CoverageDataMismatchError` convention — one clean line, cause + remedy, no traceback:

- `otto cov get` finds zero counters → error naming the host/board and searched locations.
- `otto cov get` with a manual-kind tier and no `--ticket` → refuse (ticket is required on manual captures).
- `otto cov get --tier` ambiguity (no flag, multiple e2e-kind tiers) → error listing the candidates.
- Invalid `color` value (bad hex, unknown name) → settings-validation error at load.
- Unit harvest with `.gcda` older than `.gcno` → warn + proceed (harvest-only contract), flag in the report.
- E2E capture whose pin ≠ HEAD at report time → error with remedy (re-run or re-report from the matching commit), mirroring stamp-mismatch semantics.
- Retrieval outside a git repo → refuse (remap and pins require git); git-less flows keep the `--tier NAME=PATH` escape hatch.
- Stamp mismatch during gcov/lcov parse → existing detection/exception reused.
- Unverifiable manual capture at report time → warn, treat as stale (§7).

## 11. Testing

- **Unit** (`tests/unit/cov/`): remap math (inserts/deletes/edits/moves, hunk-boundary overlaps); capture JSON round-trip incl. required-ticket validation; validity anchor chain (blob fast path, blob-diff after simulated rebase, pin fallback, unverifiable degradation) against scripted `tmp_path` git repos; aging thresholds; e2e merge-guard behavior; exclusion scanning; color validation; typed settings model + schema-export drift guard.
- **Integration**: full get → modify → report cycle on a synthetic git repo asserting the valid/stale split, legend/colors, and provenance output.
- **E2E**: repo1 (Unix product) gains dirty-tree manual `cov get` and unit-harvest flows; repo3 exercises per-board capture through the embedded console-decode path.
- **Docs**: `guide/coverage.md` tiers + cookbook rewritten around declarative tiers and `otto cov get`; `architecture/monitoring-and-coverage.md` updated; new API pages. Budget for the `-W` nitpicky gate.
- No new Python dependencies (git via subprocess, matching existing style) — `docs/getting-started.md` dependency table unaffected.

## 12. Non-goals (deferred, deliberately unblocked)

- **Per-ticket rollups** — `ticket` is required on every manual capture, so the data exists from day one; reporting is the next sub-project.
- **Counter-less declared coverage** — dropped: GDB and recompile sessions produce real counters. Revisit only if a genuinely unmeasurable verification style appears.
- **Remapping stale e2e captures** — the engine could map old output dirs onto HEAD like manual captures; deferred until wanted, guard-error for now.
- **Embedded counter reset for `otto cov clean`** — requires a product-side `cov_reset` LLEXT function mirroring `cov_dump`; Unix hosts only until then.
- **Frontend color-name capability validation** — checking that a configured color *name* renders correctly in the future frontend; a later phase per review.
- **gcno+DWARF boolean-clause linkage** — independent R&D; its output will extend `store.json`.
- **React/Vite/TS frontend** — consumes the `store.json` contract established here.
- **`--cov-fail-under` + console summary table** — small usability items, orthogonal to this model; schedule separately.
- **clang source-based coverage** (`-fprofile-instr-generate`) — the actual gap behind the roadmap's "gcc and clang" line; gcov-compatible clang already works.
- **libgit2 / batched-diff optimization** — subprocess git with per-file caching first; optimize on evidence.
