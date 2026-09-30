# Periodic review 2026-09-29 — sprint quality audit: churn, API stability, machinery, bugs

**Window:** 2026-09-07 → 2026-09-29 (`6fc0b33d`), ISO weeks 37–40. 282 commits,
releases v0.11.0 → v0.16.1 inside it. Asked by Chris: is otto's churn feature
growth or an interface-stability problem, is the bug level expected for its
maturity, how simple is the machinery, and is the thin-CLI layering series
(audit of 2026-09-28) the right workstream.

**Method:** git census (types, `!` commits, per-area numstat, new-vs-existing
file split, hub sizes), `gh` census (267 issues, 300 CI runs, 40 nightlies,
the auto-filed red issues' closing comments), the API golden's own history,
`tach.toml`'s measured cycle counts, and the three prior reviews
(`churn-and-design-review-2026-08-03.md`, `periodic-review-2026-08-25.md`,
`periodic-review-2026-09-02.md`) as the baseline. No test or gate was run.
Every number below was measured on `main` at `6fc0b33d`.

Issues filed from this review: see §5.

---

## 0. Verdicts

1. **Source churn is mostly additive feature growth.** Of 40.4k source lines
   added, 18.8k landed in 76 new files; the existing-file rewrite ratio
   (deleted ÷ added) is 0.50. Tests moved twice as much as source
   (+75.8k/−14.5k) and docs were September's largest commit category (43% of
   core commits). This is not "rewriting large amounts of otto every few
   weeks".
2. **The interface break rate is flat, not converging, and it repeats on one
   surface within weeks.** `!` commits per month: Jun 4, Jul 39, Aug 23,
   Sep 30; 21 in the sprint. Host identity took **seven** breaking commits
   between 09-01 and 09-22; the test suite was redesigned twice in four weeks
   (`7885b74c` 08-30 → `28375f4e` 09-28); coverage layout took three breaks in
   four days. `host` owns 26 of the 96 lifetime breaks. **Since the API
   golden landed (09-08) the *pinned* surface was stable**: 7 names removed,
   about a dozen added, and all four `Host` protocol changes were keyword
   widenings. The churn Chris feels lives on the surfaces the golden does not
   pin — settings keys, lab.json shape, CLI flag names, base classes — the
   same finding as 09-02 verdict 4.
3. **Structure moved the wrong way by the 08-03 review's own metrics.** The
   import cycle grew 16 → **20** modules (both flag-measured; `tach.toml`
   header); hubs doubled (§2.1); `cli/invoke.py`, created 08-04 as "one
   seam", is 778 → 2,286 lines; refactor share 3.2% against the 5% floor;
   0 of the 14 standing menu items closed for a third review.
4. **The bug profile is harness-dominated, and the product bugs that matter
   are one design defect repeated.** Of 175 issues since 09-07: 21 auto-filed
   CI/nightly reds (all closed, median < 6 h), ~14 harness flakes, **19
   CLI-vs-library parity bugs** filed in one audit day (#493–#511), the rest
   follow-ups. Sprint fixes: 15 touch product source, 19 harness only. 12 of
   79 completed push-to-main runs were red; **4 of those landed on docs-only
   or version-bump commits**, and every red with a recorded root cause was
   harness-side (§3). Product fixes cluster in lifecycle signals, console
   teardown, nc/tunnel ports, monitor shutdown — the hard asyncio edges,
   the expected shape for this domain.
5. **The thin-CLI series is the right workstream and an incomplete
   stabilization program.** It targets exactly the parity-bug class; item 1's
   boundary is clean and gated. It grows the `invoke.py` hub, schedules the
   cycle-cutting item (7) last, cannot gate "no typer outside cli" until
   #513, and never touches the `Host` protocol or the cycle (§4).

**The 08-03 thesis holds a fourth time: every line held by automation held;
every line held only by prose decayed.** Paradigms Chris wants to lock will
lock only as gates.

---

## 1. Churn

### 1.1 Sprint census

| Type | Sprint (09-07→09-29) | Prior 3 weeks (08-17→09-06) |
| --- | --- | --- |
| commits | 282 | 219 |
| docs | 96 | 50 |
| build (dependabot) | 52 | 53 |
| fix | 34 | 30 |
| feat / feat! | 20 / 15 | 29 / 15 |
| test | 22 | 20 |
| refactor / refactor! | 3 / 3 | 4 / 1 |
| perf / perf! | 1 / 2 | 2 / 0 |
| breaking total | **21** | 16 |

### 1.2 Where the lines went (added / deleted / files)

| Area | Sprint |
| --- | --- |
| tests/unit | 75,818 / 14,459 / 507 |
| docs | 26,475 / 7,591 / 431 |
| src/otto/host | 12,172 / 2,572 / 65 |
| tests/e2e | 5,675 / 851 / 57 |
| web | 4,450 / 1,300 / 52 |
| src/otto/link | 3,707 / 407 / 12 |
| src/otto/cli | 3,348 / 1,522 / 25 |
| src/otto/tunnel | 3,279 / 208 / 13 |
| src/otto (root) | 3,230 / 591 / 16 |
| src/otto/suite | 2,796 / 1,463 / 12 |
| src/otto/config | 2,572 / 1,842 / 14 |

`src/otto` total: +40,408 / −11,656. New files: 76 (+18,839 / −771). Existing
files: 188 touched, +21,569 / −10,885, **rewrite ratio 0.50**. Two source
files deleted. `suite` and `config` are the areas where deletion approached
addition — those were redesigns, not growth.

### 1.3 Breaking-change history

| Month | commits | `!` commits |
| --- | --- | --- |
| 2026-06 | 179 | 4 |
| 2026-07 | 458 | 39 |
| 2026-08 | 340 | 23 |
| 2026-09 | 359 | 30 |

Per scope, lifetime: host 26, monitor 11, cov 7, cli 6, unscoped 6, tunnel 4.

Repeated breaks on one surface inside the window:

- **Host identity (7 in three weeks):** `6b3e35dd` uniform `user=` (09-01),
  `d7426e75` unix `user=` parity (09-01), `2a6ea7f9` `su -` default (09-05),
  `acbe8d23` per-family identity declaration (09-08), `9c0542a1`
  `--as-user`→`--holder` (09-09), `fd4d1b21` per-protocol cred scope (09-14),
  `828cf5ad` `run`→`exec` verb (09-22).
- **Test suite (2 in four weeks):** `7885b74c` suites go pytest-native
  (08-30); `28375f4e` tests are plain pytest, `OttoSuite` deleted (09-28).
- **Coverage layout (3 in four days):** `096c00d5`, `48017f27` (09-17),
  `6af85c02` (09-20).

### 1.4 The declared surface held

`tests/unit/api_snapshot/public_api.txt` (209 lines: 31 `otto:` exports, 36
`Host` protocol signatures, the rest docs-taught deep imports). Since its
first commit `1b6f8074` (09-08): 7 real removals (`otto.cli.run:instruction`,
`otto.host:FileProduct`, `otto.suite.run:run_selection`, `otto.suite:OttoSuite`,
`otto.suite:find_suite`, `otto.utils:Status`, `otto:run_suite`), about a
dozen additions, and every `Host` change a trailing keyword widening
(`exec` +`expects`,`sudo`; `put`/`get` +`recursive`,`concurrent`; `login`
+`force`; `logout` new). `scripts/check_breaking_marks.py` already treats
widenings as non-breaking. **The pinning works where it points; it points at
a third of the surface users touch.**

### 1.5 Blast radius of a break

For the 21 sprint `!` commits, test lines changed ÷ source lines changed
ranges 0.7–2.6 (median ≈1.4); `28375f4e` touched 62 source and 161 test
files, `53d025cb` 103 and 169. A break costs roughly 1.5–2.5× its source
size in test repair, which is the number a user-facing break would also cost
a downstream repo.

---

## 2. Machinery

### 2.1 Hubs since the 08-03 review

| File | 08-03 | 09-29 |
| --- | --- | --- |
| host/session.py | 1,919 | **3,462** |
| host/host.py | 1,242 | **3,004** |
| host/userland.py | — | 3,184 |
| config/completion_cache.py | 1,405 | **2,614** |
| cli/invoke.py (created 08-04 at 778) | — | **2,286** (60 functions) |
| host/transfer/nc.py | 1,055 | 2,052 |
| host/unix_host.py | 941 | 1,611 |
| cli/main.py | 698 | 1,175 |
| cli/cov.py | 961 | 1,127 |
| config/repo.py | 845 | 626 (the one shrink) |

`src/otto`: 122.6k lines / 339 files; 3,874 functions, 673 classes; 23
functions over McCabe 15, none over 25. The 08-03 Tier-1 hub items (1.2
session split, 1.8 cov extraction, 1.9 acquisition registry, 1.10 collector
de-façade) have zero movement across three reviews; the menu's checks re-run
today all still fail (no `HostOverrides`; 5 raw `settings` dict readers; no
acquisition registry; no pyproject extras; `forbid_circular_dependencies`
unset). `cli/invoke.py` is the instance of the 08-03 P1 pattern ("extractions
that keep every reason to edit the hub"): the seam absorbed preamble, lab
context, reservation gate, dependency refusal, bootstrap-error rendering,
dry-run, probe, help banner, result rendering and lifecycle wrapping, and 25
commits since creation.

### 2.2 The import cycle

`tach.toml` header, both figures produced by `forbid_circular_dependencies =
true` runs: **16 modules on 2026-08-17, 20 on 2026-09-29** (bootstrap, check,
cli, config, context, coverage, creds, docker, host, instructions, inventory,
labs, lifecycle, link, models, monitor, project, reservations, suite, tunnel).
Thin-CLI item 1 retired one loop and added one (instructions↔project). Nobody
decided the component should grow; nothing refuses it.

### 2.3 The harness is a product

Makefile 1,643 lines / 97 targets; `scripts/` 12,933 lines in 31 files;
conftests 6,642 lines + `tests/_fixtures` 8,075; CI YAML 1,600 lines; 29
ast-grep rules; a 69-row gate inventory (`docs/architecture/quality-gates.md`);
16 repo-wide guard tests under `tests/unit`. Tests: 289k lines vs 123k source
(2.4×); 11,220 unit test functions (+29% since 09-02's 8.7k). This is where 19
of the sprint's 34 fixes and every recorded main red went (§3).

### 2.4 What is genuinely good

The registry idiom, the `Result` family and `OttoError` root, the PEP 562
edge and the shim, one `asyncio.run` with the two-stage interrupt policy, the
API golden + `check-breaking` marking gate (09-08, the sprint's best
structural addition), doctested docs under nitpicky `-W`, 3.10–3.14 lanes,
and a median sub-six-hour close on 100 auto-filed reds with zero retry
automation. The process is mature for a five-month codebase; the structure is
not.

---

## 3. Bugs

### 3.1 Issue census since 09-07 (175 issues)

| Class | Count | Notes |
| --- | --- | --- |
| auto-filed "CI failed on main" / "Nightly run failed" | 21 | all closed; median 5.8 h, p75 13.4 h over all 100 ever filed |
| harness flakes filed by hand | ~14 | #319 #321 #330 #357 #381 #382 #383 #401 #402 #421 #438 #454 #483 #514 |
| CLI-vs-library parity bugs (2026-09-28 audit) | 19 | #493–#511, filed in one batch on 09-29 |
| kmodcov / product-kind / console follow-ups | ~25 | #362–#371, #403–#416, #430–#453 |
| covapp (web) nits | ~15 | #335–#348 |
| enhancements / follow-ups filed at landing | rest | |

### 3.2 Main reds in the sprint (12 of 79 completed push runs)

| Date | Commit | Touched | Recorded cause |
| --- | --- | --- | --- |
| 09-11 | `78f607bd` docs(spec) | docs only | — |
| 09-16 | `2b18bf7a` feat(cov) | src | import-budget warm-repeat listdir 59 vs 60 (#343) |
| 09-17 | `cf5f1bc6` test(import-budget) | tests | (#344) |
| 09-18 | `0185f62b` ci(canary) | ci | import-budget listdir ±1 / FileFinder refill (#360/#361) |
| 09-19 | `33944c92` feat(kmodcov) | src | toolchain fake patched the parse, not the exec seam (#405) |
| 09-19 | `e69a64c6` docs(spec) | docs only | empty `FORCE_COLOR` still set (#387) |
| 09-20 | `e0d43f0b` fix(test) | tests | tick-count-in-a-window timing (#407) |
| 09-21 | `97c88056` chore(release) | bump only | pytest-repeat `--count` multiplies before deselection (#428/#429) |
| 09-25 | `1e813e08` feat(link) | src | (#439) |
| 09-26 | `e122e9b7` docs(spec) | docs only | import-budget cold bytecode cache on first measurement (#474) |
| 09-27 | `53d025cb` perf! | src | API golden vs a new docs example (#484) |
| 09-27 | `4e0347d5` fix(host) | src | closed green on next commit (#488) |

Not one recorded cause is a product defect. Four reds landed on commits that
could not have changed product behaviour. Nightly: 8 reds in the window,
dominated by the 3.15 canary (explicitly monitoring, #422) and the
stability-matrix fake (#359).

### 3.3 Product fixes in the sprint (15)

lifecycle signal handling ×3 (`59328c67`, `e4a383f4`, `fd55be39`); console /
session teardown ×3 (`c9979b1a`, `868baf35`, `4e0347d5`); nc / tunnel ports
×4 (`89cbee11`, `d665d84a`, `a7a46b58`, `266e3f5d`); monitor shutdown ×2
(`d5db8da2`, `ddfb19b2`); dry-run arm (`9988b1e2`); shim marker
(`38d797ea`); lcov BRDA (`25115d13`).

### 3.4 Is this expected?

For the product: yes. The fix clusters are the hard edges of an asyncio lab
orchestrator, and the auto-filed-red turnaround is better than most mature
projects. The abnormal part is the harness red rate (~15% of push runs),
self-inflicted by guards strict enough to fire on runner noise (syscall
counts, wall-clock windows, `filterwarnings=error`). The **parity bugs are the
signal**: with no users, the audit was the first "second user", and it found
19 places where a rule exists twice and diverged. That is a design defect
class, and it is the one the thin-CLI series removes structurally.

---

## 4. The thin-CLI series

**Not misguided.** It targets the bug class the audit surfaced (#501, #502,
#505–#508, #511 are all "two copies of one rule"), and it is the first series
in three reviews to attack a medium-sized structural item rather than the
S-sized ones that kept displacing them (08-05 §0).

Item 1 (`1ff9effe`) boundary: clean and gated — `no-cli-import-outside-cli`
ast-grep rule, `project → cli` tach edge removed, `otto.instructions`
typer-free, `@instruction` returns the function. Item 2's principle (rules in
the library entry point; I/O preflights in `prepare_*`; the CLI parses,
completes, constructs, calls, translates) is right and stated once.

Cautions:

- It adds translation logic (`usage_error_from(flags=)`) to `cli/invoke.py`,
  already the hub (§2.1, 08-25 N6).
- Item 7 (completion-cache typer half, `open_context` divergence #508) is
  where the cycle-cutting lives and is scheduled last.
- #513 deferred ⇒ "no `import typer` outside `otto.cli`" cannot be a gate
  yet, and only gates hold here.
- The series does not touch the `Host` protocol (26 of 96 breaks) or the
  20-module cycle.

---

## 5. Lock-downs → issues

Filed 2026-09-29 as #520–#529 (board: Backlog, P0/P1, sized). Priority per Chris ("all high or
medium", triage delegated).

| # | Lock-down | Effort | Priority |
| --- | --- | --- | --- |
| L1 #520 | Extend the API golden to settings keys, the lab.json schema and CLI flag names | M | P0 |
| L2 #521 | `Host` protocol frozen to keyword widenings by rule + gate | S | P0 |
| L3 #522 | Pin the import-cycle member list as a shrink-only ratchet | S | P0 |
| L4 #523 | Move syscall-count and timing guards off the push gate; budget the main red rate | M | P0 |
| L5 #524 | Cooling-off rule: no second `!` on a surface within N weeks unless the first was a bug | S | P1 |
| L6 #525 | One CLI-vs-library differential test per verb, added as each thin-CLI item lands | M | P1 |
| L7 #526 | De-hub `cli/invoke.py` before thin-CLI item 7 | M | P1 |
| L8 #527 | `host.py`: one declaration per field/method (08-25 N1) | M | P1 |
| L9 #528 | Split `session.py` along its seams (08-03 1.2) | L | P1 |
| L10 #529 | Refactor-share floor (≥5%) and hub sizes in the periodic-review script | S | P1 |

### Recommended execution order, interleaved with the thin-CLI series

1. **Now, in parallel with item 2** (no tree overlap: `scripts/`, `tests/unit`,
   CI YAML): L3, L4, L1.
2. **When item 2 lands:** L2 and L5 (policy + `check_breaking_marks.py`
   extension), because item 2 is the last planned `Host`-adjacent break.
3. **Items 3–4 (docker, cov get/clean):** L6 rides along — each item adds its
   verb's differential test as its last task.
4. **Before item 7:** L7. Item 7 moves the completion cache's typer half and
   `open_context`; doing that into a 2.3k-line hub deepens it.
5. **Items 5–6 gap:** L8, then L9 (the only L-sized item; one release).
6. **Any time:** L10.

---

## 6. Corrections and caveats

- Weekly issue counts (W38: 85, W39: 50) are inflated by the practice of
  filing every landing's follow-ups as issues; they are not a bug rate.
- The 09-02 review's "~55 additions" style counts in an earlier draft of this
  review were wrong; the golden gained about a dozen real names plus the 36
  `Host` lines that pinned an existing surface.
- Root causes for #306, #344, #439 were not recorded in their closing
  comments; they are counted as unrecorded, not as harness.
- Nothing here was run against the bed; the integration conftest reaps the
  lab.
