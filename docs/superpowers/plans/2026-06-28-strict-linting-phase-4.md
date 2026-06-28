# Strict-Linting Phase 4 — Naming & Bug-Class Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear the naming + bug-class ratchet debt — `F`, `ASYNC`, `BLE001`, `DTZ`, `N`+`A`, `S`, `SLF001`, `PLR2004` — adopting the rules that find real problems, fixing properly, and reserving deny/exempt for genuine false positives.

**Architecture:** Per Chris's standing call (keep rules enforced + fix properly), most codes are FIXED. Three exceptions are deliberate: (a) **deny ASYNC230/ASYNC240** — they premise trio/anyio structured concurrency; otto is asyncio and the flagged sites are brief/off-hot-path filesystem ops (low value, wrong remediation); (b) **exempt the S-family security codes in `tests/**`** — tests use fake creds, subprocess harnesses, and remote-host `/tmp` paths; they are not a security surface (extends the existing S101-tests exemption + Chris's S108 decision); (c) narrow per-site `# noqa` only where a rule is a genuine false positive or fights a deliberate pattern. Each task fixes/handles its sites then removes the now-clean codes from the `# ===== TEMP (ratchet) =====` block. STAGE-ONLY — never commit (Chris commits).

**Tech Stack:** ruff 0.15.x, ty 0.0.55 (`all = error`), pydantic v2, asyncio, pytest+xdist, Python 3.10 floor.

## Global Constraints

- **STAGE-ONLY.** `git add` only; never `git commit` (otto's prepare-commit-msg hook needs /dev/tty). Chris commits.
- **`ruff check .` is authoritative** (covers `scripts/`+`docs/`); the scoped `--select X src tests` form misses them. Stage `.ruff.toml` + every touched file.
- **`make typecheck` in every task's verification** (ty `all=error`; `ruff format`/autofix is NOT type-checker-neutral). Behavior-touching tasks also run `make coverage-unit` (85% floor, no-VM).
- **Never blanket `--fix --unsafe-fixes`.** Apply per-site with judgment, matching surrounding code.
- **Python 3.10 floor + Sphinx-nitpicky (`-W`) docs gate are HARD.** No `from __future__ import annotations`, no `TYPE_CHECKING`-hidden imports, no unquoted forward refs. For tz: use `timezone.utc` (NOT `datetime.UTC`, which is 3.11+).
- **Ratchet rule:** a code leaves the TEMP block ONLY when all its sites (src+test+scripts) are resolved. Verify `uv run ruff check . --select <CODE> --config 'lint.ignore=[]'` reflects the intended end-state before deleting from TEMP.
- **Per-site `# noqa` format:** `# noqa: CODE — <reason>`. Justified, specific reason; never bare.

**Phase-4 codes leaving TEMP (~30):** `F403 F821 F841 ASYNC110 ASYNC221 ASYNC230 ASYNC240 BLE001 DTZ001 DTZ005 DTZ901 S101 S104 S105 S106 S108 S110 S310 S314 S603 S607 N801 N806 N812 N814 A002 A004 SLF001 PLR2004`. **Deferred to a later "Phase 4b/cleanup" pass (NOT this phase):** `INP001 E402 E501 E731 E741 ERA001 EXE001 T201 PT* PTH* PLW* PLC0414 PLR0133 PLR0913 RET504 RSE102 PYI034 TRY* C901 TD004 PGH003`, plus the `D*` (Phase D) and `ANN*` (Phase A) families.

---

## Task 1: Config — deny ASYNC230/240, exempt S-family in tests

**Why:** Pure `.ruff.toml` policy. ASYNC230/240 premise trio/anyio (otto is asyncio); the S-family in tests is harness/fixture noise, not a security surface.

**Files:** Modify `.ruff.toml` only.

- [ ] **Step 1: Deny ASYNC230 + ASYNC240.** In the PERMANENT deny-list (Group 2 — otto deliberate patterns), add:
  ```toml
  "ASYNC230", "ASYNC240",  # blocking file I/O in async: rule premises trio/anyio; otto is asyncio, flagged sites are brief/off-hot-path FS ops
  ```

- [ ] **Step 2: Exempt the S-family in tests.** In `[lint.per-file-ignores]`, extend the existing `"tests/**"` list with: `"S104", "S105", "S106", "S108", "S110", "S310", "S603", "S607"` (S101 already present). Add a trailing comment: `# S*: tests use fake creds / subprocess harnesses / remote-host paths — not a security surface`.

- [ ] **Step 3: Remove from the TEMP block the codes now fully covered by Steps 1-2** (i.e. denied, or only-occurring-in-tests-and-now-exempt): `ASYNC230`, `ASYNC240`, `S105`, `S106`, `S310`, `S603`, `S607`. (Leave `S101 S104 S108 S110 S314` in TEMP — they have src/scripts sites handled in Task 4.)

- [ ] **Step 4: Record the policy changes in the spec.** In `docs/superpowers/specs/2026-06-27-strict-linting-design.md`, add to the deny-list rationale: `ASYNC230/ASYNC240` (Group 2 — trio/anyio premise, otto is asyncio), and a one-line note that the `tests/**` per-file-ignore is broadened to the S-family (tests are not a security surface).

- [ ] **Step 5: Verify.**
  ```bash
  uv run ruff check . --select ASYNC230,ASYNC240,S105,S106,S310,S603,S607 --config 'lint.ignore=[]'  # sites still print (ignore bypassed) — expected
  uv run ruff check .            # expect green (deny + per-file-ignore take effect under real config)
  make typecheck
  ```
  The real check is `uv run ruff check .` staying green. Confirm the permanent deny-list and the new per-file-ignore entries are syntactically correct (ruff doesn't error on load).

- [ ] **Step 6: Stage** `git add .ruff.toml docs/superpowers/specs/2026-06-27-strict-linting-design.md`

---

## Task 2: F-family — F403 / F821 / F841 (pyflakes real bugs)

**Why:** Pyflakes catches real bugs. F821 (undefined name) and F403 (star-import) are in sample fixture repos; F841 (unused variable) is genuine dead code in unit tests.

**Files (exact sites):**
- `F403` (star-import in fixture-repo `__init__.py`): `tests/repo1/pylib/repo1_instructions/__init__.py:1,2`; `tests/repo2/pylib/repo2_instructions/__init__.py:1`
- `F821` (undefined `Path`): `tests/repo1/pylib/utils.py:1`; `tests/repo2/pylib/utils.py:1`
- `F841` (unused variable, 10 sites — get exact lines live): all under `tests/`
- Modify `.ruff.toml` (remove `F403 F821 F841` from TEMP)

- [ ] **Step 1: F821** — `utils.py:1` uses `Path` in an annotation without importing it. Add `from pathlib import Path` at the top of each `utils.py`. These are sample-product helper modules; keep them importable.
- [ ] **Step 2: F403** — these fixture-package `__init__.py` re-export submodules via `from .x import *`. Replace each star-import with explicit re-exports (`from .install import install` etc. — read the submodule's public names) OR, if the sample's intent is to re-export everything, add an `__all__` to the submodule and keep the star-import with `# noqa: F403` is NOT allowed (F403 stays enforced). Prefer explicit imports. If the submodule has many public names, add `__all__` to it and convert to explicit names.
- [ ] **Step 3: F841** — run `uv run ruff check tests --select F841 --config 'lint.ignore=[]' --output-format concise` for the exact 10 sites. At each, either delete the unused assignment, or if the call has a needed side effect, drop the binding (`func()` not `x = func()`), or rename to `_` if it documents a tuple position. Confirm genuinely unused.
- [ ] **Step 4: Remove `F403 F821 F841` from TEMP.**
- [ ] **Step 5: Verify.**
  ```bash
  uv run ruff check . --select F403,F821,F841 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  make typecheck
  uv run pytest tests/repo1 tests/repo2 -q   # fixtures still import; plus run any test module whose F841 you touched
  ```
- [ ] **Step 6: Stage** the touched fixture/test files + `.ruff.toml`.

---

## Task 3: DTZ — timezone-aware datetimes everywhere (src + tests)

**Why (HIGHEST behavior risk in this phase):** `datetime.now()` / `datetime.max` without tzinfo are a real correctness bug-class. Chris's call: make everything tz-aware. **src and tests MUST move together** — once src produces aware datetimes, test fixtures comparing against them must also be aware (comparing naive vs aware raises `TypeError`).

**Files (exact sites):**
- src DTZ005 (`datetime.now()` → `datetime.now(tz=timezone.utc)`): `host/interact.py:152,153,167,168`; `host/repeat.py:60,67,80,108`; `logger/management.py:152,221`; `monitor/collector.py:398,405,422,428,524`; `monitor/server.py:133`
- src DTZ901 (`datetime.max`/`datetime.min` → make aware): `host/host.py:734`; `host/repeat.py:62,125`
- tests DTZ001 (`datetime(...)` naive constructors → add `tzinfo=timezone.utc`): `tests/unit/cli/test_monitor.py` (1); `tests/unit/models/test_monitor.py` (10); `tests/unit/monitor/test_collector_db.py` (19); `tests/unit/monitor/test_monitor_import_export.py` (6)
- Modify `.ruff.toml` (remove `DTZ001 DTZ005 DTZ901` from TEMP)

- [ ] **Step 1: src DTZ005/DTZ901.** Add `from datetime import timezone` (3.10-safe; do NOT use `datetime.UTC`). Convert `datetime.now()` → `datetime.now(tz=timezone.utc)`. For `datetime.max`/`datetime.min` sentinels, use `datetime.max.replace(tzinfo=timezone.utc)` (and likewise `.min`). **Critical:** in `host/repeat.py`, the `datetime.max` deadline sentinel is compared against `datetime.now()` — both must be aware after this change, or the comparison raises `TypeError`. Audit each file so every datetime that meets another in a comparison/subtraction is aware.
- [ ] **Step 2: tests DTZ001.** At each naive `datetime(Y, M, D, ...)` constructor, add `tzinfo=timezone.utc` (import `timezone` in the test file). These fixtures feed monitor records compared against src output — keeping them aware matches Step 1.
- [ ] **Step 3: Remove `DTZ001 DTZ005 DTZ901` from TEMP.**
- [ ] **Step 4: Verify (run the affected suites — monitor + repeat + logger are the blast radius).**
  ```bash
  uv run ruff check . --select DTZ001,DTZ005,DTZ901 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  make typecheck
  uv run pytest tests/unit/monitor tests/unit/models/test_monitor.py tests/unit/cli/test_monitor.py tests/unit/host/test_repeat.py -q
  make coverage-unit   # full no-VM suite — DTZ touches serialized monitor records; confirm nothing regressed
  ```
  Watch for `TypeError: can't compare offset-naive and offset-aware datetimes` — that means a comparison site was missed.
- [ ] **Step 5: Stage** the touched src + test files + `.ruff.toml`.

---

## Task 4: S-family src — asserts, bind-all, temp paths, XML

**Why:** The real security surface (src + scripts). Per-site judgment; deliberate cases get a narrow `# noqa` with rationale.

**Files (exact sites):**
- `S101` (29 src asserts — per-site: load-bearing/user-facing invariant → raise a real exception; internal sanity check → `# noqa: S101 — internal invariant`): `host/session.py` (12); `host/transfer/nc.py` (4); `coverage/store/model.py` (3); `host/connections.py` (2); `host/transfer/{console,embedded_base,ftp,progress,scp,sftp}.py` (1 each); `monitor/collector.py` (1); `reservations/check.py` (1)
- `S104` (bind-all, deliberate servers → `# noqa: S104 — server intentionally binds all interfaces`): `monitor/server.py:218,231,239`; `host/transfer/nc.py:624`
- `S108` (hardcoded staging/temp paths, deliberate defaults → `# noqa: S108 — deliberate staging path` OR `tempfile` if it's a truly local scratch dir; judge each): `cli/test.py:673`; `docker/staging.py:31`; `host/docker_host.py:395`; `host/unix_host.py:727`
- `S110` (`try/except/pass` → add a debug log OR `# noqa: S110 — best-effort, failure is non-fatal`): `host/interact.py:522`; `host/telnet.py:82`
- `S314` (`xml.etree` on OUR junit output → `# noqa: S314 — parses our own trusted JUnit output, not untrusted input`): `scripts/junit_failures.py:25`
- `S603` (subprocess on trusted/controlled args → `# noqa: S603 — trusted args` OR confirm args are controlled): `scripts/lab_health.py:118`; `scripts/stability_campaign.py:186`
- `S607` (partial executable path, relies on PATH → use a full path OR `# noqa: S607 — resolved via PATH by design`): `src/otto/monitor/server.py:189`
- Modify `.ruff.toml` (remove `S101 S104 S108 S110 S314 S603 S607` from TEMP)

- [ ] **Step 1: S101** — at each src `assert`, judge: is it validating external/user input or a contract another module relies on? → replace with an explicit `raise <Error>(...)`. Is it an internal sanity check the author placed for development? → `# noqa: S101 — internal invariant`. (session.py's 12 are most likely internal state invariants; verify each.)
- [ ] **Step 2: S104** — these bind `0.0.0.0` deliberately (monitor server reachable from test hosts; nc listener). Add `# noqa: S104 — intentional all-interface bind`.
- [ ] **Step 3: S108** — judge each: a docker/remote staging default (`/tmp/otto-docker*`) is deliberate → `# noqa: S108 — deliberate staging path`. A genuinely-local scratch file → switch to `tempfile.mkdtemp()`/`NamedTemporaryFile`. Do NOT break remote/docker path expectations.
- [ ] **Step 4: S110** — prefer adding a `logger.debug(...)` in the `except` (it's `try/except/pass`); if the swallow is genuinely intentional and logging would be noise, `# noqa: S110 — best-effort cleanup`.
- [ ] **Step 5: S314** — `# noqa: S314` with the trusted-input rationale (don't add a `defusedxml` dependency for parsing our own JUnit).
- [ ] **Step 6: Remove `S101 S104 S108 S110 S314` from TEMP.**
- [ ] **Step 7: Verify.**
  ```bash
  uv run ruff check . --select S101,S104,S105,S106,S108,S110,S310,S314,S603,S607 --config 'lint.ignore=[]'  # expect 0
  uv run ruff check .
  make typecheck
  make coverage-unit   # S101→raise changes are behavior; confirm no regression
  ```
- [ ] **Step 8: Stage** the touched src + scripts files + `.ruff.toml`.

---

## Task 5: BLE001 + ASYNC110/ASYNC221 (exception/async correctness)

**Why:** BLE001 (blind `except Exception`) — re-apply the handling intent stripped by RUF100 in Phase 1a. ASYNC110 (busy-wait) / ASYNC221 (sync subprocess in async) — fix where a clean swap exists, else justify.

**Files (exact sites):**
- `BLE001` (32: 29 src + 3 tests — at each, either narrow the caught type, add real handling/logging, or `# noqa: BLE001 — best-effort, intentionally catches all`): src across `host/{interact,telnet,unix_host,remote_host,local_host,embedded_host}.py`, `host/transfer/{ftp,nc,console}.py`, `suite/{suite,plugin}.py`, `monitor/snmp.py`, `docker/compose.py`, `context.py` (BaseException), `configmodule/completion_cache.py`, `cli/expose.py`, `testing/conformance.py` (4); 3 tests
- `ASYNC110` (busy-wait `while ...: await asyncio.sleep()`): `monitor/server.py` (1), `suite/suite.py` (1), `tests/unit/monitor/test_server.py` (4)
- `ASYNC221` (sync subprocess in async): `tests/integration/host/test_session_stability_integration.py` (1)
- Modify `.ruff.toml` (remove `BLE001 ASYNC110 ASYNC221` from TEMP)

- [ ] **Step 1: BLE001** — get exact lines live (`uv run ruff check . --select BLE001 --config 'lint.ignore=[]' --output-format concise`). At each: if a specific exception type is what's really expected, narrow it. If it's a best-effort cleanup/teardown/iteration-resilience that must swallow everything, add `# noqa: BLE001 — best-effort, intentionally catches all` (and ensure the body at least logs at debug, matching nearby code). Do NOT silently broaden behavior. `context.py` catches `BaseException` deliberately (collect-results) — noqa that one.
- [ ] **Step 2: ASYNC110** — if the `while: await sleep()` polls internal state that a producer could signal, switch to `asyncio.Event`. If it polls external/remote state (no event source — the common case for monitor/host polling), `# noqa: ASYNC110 — polling external state, no event source`. Judge each (the 4 test_server.py sites are likely test polling → noqa).
- [ ] **Step 3: ASYNC221** — if a clean `asyncio.create_subprocess_exec` swap exists, use it; else `# noqa: ASYNC221 — test harness, blocking subprocess acceptable`.
- [ ] **Step 4: Remove `BLE001 ASYNC110 ASYNC221` from TEMP.**
- [ ] **Step 5: Verify.**
  ```bash
  uv run ruff check . --select BLE001,ASYNC110,ASYNC221 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  make typecheck
  make coverage-unit
  ```
- [ ] **Step 6: Stage** touched files + `.ruff.toml`.

---

## Task 6: Naming — N801 / N806 / N812 / N814 / A002 / A004

**Why:** Naming consistency (the ws2-naming-sweep left these). Mixed: rename genuine camelCase; keep legitimately-capitalized names (class objects, math `N`/`M`) via `# noqa`.

**Files (exact sites — get live lists per code):**
- `N806` (29: camelCase locals): RENAME genuine camelCase to snake_case (`allValues`→`all_values`, `settingsText`→`settings_text`, `ottoSettingsPath`→`otto_settings_path`, `commandStatus`→`command_status`, `pprintDepth`→`pprint_depth`, `originalMsg`→`original_msg`, `formattedLine(s)`→`formatted_line(s)`, `sutDir1/2`→`sut_dir1/2`, `repoSettingsFile`→`repo_settings_file`, etc.). KEEP via `# noqa: N806` where the name is a class object bound to a local (`MockTelnetSession`, `MockSshSession`, `MockTelnet` — CapWords is correct) or a conventional single-letter math dimension (`N`, `M`).
- `N801` (2: `class status`): in `tests/unit/cli/test_dynamic_host_commands.py` — rename the fake class to `Status`/`_Status` (update references), or `# noqa: N801` if lowercase is mimicking an external API shape.
- `N812` (2: `get_repos` imported as `_getRepos`): fix the alias to `_get_repos` in `host/docker_host.py` + `configmodule/__init__.py` (update local uses).
- `N814` (1: `Exclude` imported as `_E2`): `tests/unit/test_utils_cli_markers.py` — rename alias to a non-constant style (`_Excl`) or `# noqa: N814` if `_E2` is a deliberate short test alias.
- `A002` (5: arg shadows builtin): `all`/`type`/`help`. RENAME internal-only params (`utils.py` `type`→`type_`, `help`→`help_`). KEEP via `# noqa: A002 — CLI-exposed param name` where the param maps to a CLI flag (`host/file_ops.py` `all`, `host/embedded_host.py` `all` — renaming would change `--all`).
- `A004` (1: `compile` import shadows builtin): `configmodule/version.py` — rename the import alias (`import re; re.compile` or alias `compile_re`).
- Modify `.ruff.toml` (remove `N801 N806 N812 N814 A002 A004` from TEMP)

- [ ] **Step 1: N812/N814/A004** (alias fixes — mechanical): correct each import alias to snake_case / non-shadowing and update local references.
- [ ] **Step 2: N806** — rename genuine camelCase locals to snake_case (update all in-scope references); `# noqa: N806` the class-object and math-dimension cases. Verify each renamed variable's every use is updated (tests must still pass).
- [ ] **Step 3: N801** — rename the fake `status` class (update refs) or noqa with rationale.
- [ ] **Step 4: A002** — rename internal params; noqa the CLI-exposed `all` params (renaming would change the CLI surface — confirm via `@cli_exposed`/signature whether the param becomes a flag).
- [ ] **Step 5: Remove `N801 N806 N812 N814 A002 A004` from TEMP.**
- [ ] **Step 6: Verify.**
  ```bash
  uv run ruff check . --select N801,N806,N812,N814,A002,A004 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  make typecheck
  make coverage-unit   # renames touch real call sites
  ```
- [ ] **Step 7: Stage** touched files + `.ruff.toml`.

---

## Task 7: SLF001 + PLR2004 (private access + magic values)

**Why:** SLF001 (private-member access) — mostly deliberate intra-package "friend" access in the host/session/transfer layer. PLR2004 (magic-value comparison) — extract named constants where it clarifies; noqa protocol/byte-offset literals that are clearer inline.

**Files (exact sites — get live lists per code):**
- `SLF001` (34 src, all in `host/**` + `suite/**`): these access `_private` members of sibling/related objects (`remote_host._parent`, `session._on_output`, `connections._kwargs`, `privilege._set_current_user`, etc.) — deliberate intra-package collaboration. Default: `# noqa: SLF001 — intra-package access to sibling internals`. Where a public accessor already exists, prefer it; do NOT add new public API just to dodge the lint.
- `PLR2004` (17 src + 1 scripts): `monitor/parsers.py` (5), `host/session.py` (3), `host/transfer/nc.py` (2), `coverage/renderer/html_renderer.py` (2), `filesystem.py` (2), `cli/expose.py` (1), `cli/param_synth.py` (1), `host/command_frame.py` (1), `scripts/lint_markdown_doctests.py` (1). Extract a named module-level constant where the magic number has clear meaning (thresholds, sizes); `# noqa: PLR2004 — <protocol/offset> literal` for protocol byte values / status codes that are clearer inline.
- Modify `.ruff.toml` (remove `SLF001 PLR2004` from TEMP)

- [ ] **Step 1: SLF001** — get live lines; at each, noqa with the intra-package rationale (or use an existing public accessor if one exists). Keep behavior identical.
- [ ] **Step 2: PLR2004** — get live lines; extract named constants where meaningful, else noqa with a specific reason. Behavior identical.
- [ ] **Step 3: Remove `SLF001 PLR2004` from TEMP.**
- [ ] **Step 4: Verify.**
  ```bash
  uv run ruff check . --select SLF001,PLR2004 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  make typecheck
  make coverage-unit
  ```
- [ ] **Step 5: Stage** touched files + `.ruff.toml`.

---

## End-of-Phase verification (after all 7 tasks, before handoff)

1. **TEMP is ~30 codes lighter:** none of the Phase-4 codes remain in the TEMP block; the deferred families (INP001/E*/PT*/PTH*/PLW*/TRY*/D*/ANN*/PGH003/...) are untouched.
2. **Whole-tree clean:** `uv run ruff check .` → 0; `uv run ruff format --check .` → clean.
3. **Types:** `make typecheck` → clean.
4. **Behavior:** `make coverage` (full bed, 92% floor) — DTZ + S101→raise + naming renames touch src runtime. Offer `make nox` (py3.10–3.14); the changes are mostly version-agnostic except `timezone.utc` (3.10-safe) — `make coverage` + typecheck is the standard bar.
5. **Opus final whole-branch review** before handoff — special attention to DTZ comparison-consistency, S101→raise correctness, and that every `# noqa` is justified.
6. **PAUSE** — give Chris a single paste-able commit message; he commits + pushes.

## Self-review notes

- Roadmap Phase 4 = "Naming/bug-class: N, DTZ, BLE, SLF001/PLR2004, S + F/ASYNC early." All covered. ✅
- Chris's 4 decisions encoded: S108→tests-exempt (extended to the S-family); ASYNC240(+230)→deny; DTZ→tz-aware everywhere; S101→per-site invariant/sanity. ✅
- Deny-list additions (ASYNC230/240) + the broadened tests/** S-exemption are the only policy changes — record them in the spec's deny-list/per-file-ignore section during Task 1 (add a one-line note) so the roadmap stays source-of-truth.
- Deferred families explicitly listed so the next phase has a clear worklist; end goal remains TEMP empty.
