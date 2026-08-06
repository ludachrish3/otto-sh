# Remaining work after the churn review — agent handoff

**Written:** 2026-08-05
**Prerequisite:** the docs-alignment + quality-gates wave
(`docs/superpowers/specs/2026-08-05-docs-alignment-and-quality-gates-design.md`).
Start here once that has landed.
**Parents:** [churn-and-design-review-2026-08-03.md](churn-and-design-review-2026-08-03.md)
(the findings) and [churn-review-cheap-items-followups.md](churn-review-cheap-items-followups.md)
(the tail the first wave generated).

Every count and file:line below was **verified against the tree at `77af429d`**
on 2026-08-05, not carried forward from the review. Where the review's own
number has since moved, both are given. Re-verify before acting — several are
the kind that drift.

---

## 0. State of play, in one paragraph

The 44 commits from `56ffc7ab` to `77af429d` closed **Tier 0 in full** (8 of 8,
one partial), **4 of 10 Tier 1**, about **1.5 of 11 Tier 2**, and converted **4
of 7 policy items into gates**. Execution quality was high. Selection was
skewed: *every* completed Tier 1 item was S or S–M and *every* skipped one is M
or M–L, so the review's central structural thesis — §3/P1, "extractions stop at
the wrong altitude, so hubs keep every reason to be edited" — remains untested.
The four items that would test it (1.2, 1.8, 1.9, 1.10) are all still open.

---

## 1. Carried over from the quality-gates wave — plan coordinates in test trees

The wave gated **shipped source only**, by decision. Named here so it is a
bounded exclusion rather than an inferred sweep.

| Tree | Files | Comment blocks |
| --- | --- | --- |
| `web/src/**` tests (`__tests__/`, `*.test.ts(x)`) | 40 | ~109 |
| `tests/**` Python | 74 | ~195 |

Doing these is a **policy change**, not a cleanup: the Python rule has scoped to
`src/otto/**` since it was written. Decide the policy first; the sweep is
mechanical afterwards. Effort M once decided.

---

## 1b. Convert the 11 baselined tuple returns

`.ast-grep/rules/no-tuple-return.yml` landed 2026-08-05 with a **labelled,
shrink-only baseline** of 18 public functions whose outermost return type is a
bare tuple. Six are argued permanent exemptions; the other 11 keys (12 sites)
are debt and convert to frozen dataclasses.

The two gates pin each other, so this cannot rot: deleting a baseline entry
without fixing the code fails `make lint-arch`, and fixing the code without
deleting the entry fails `tests/unit/test_tuple_return_debt.py`. Every site
carries an inline `# DEBT(no-tuple-return): <reason>` naming what it crams.

| Site | Crams |
| --- | --- |
| `coverage/attribution.py` `attribute_tickets` | three unrelated maps — the worst in the tree |
| `config/completion_cache.py` `collect_current_commands` | two command lists |
| `host/binary_loader.py` `check_loaded` (×2, one ABC) | `(ok, detail)` — a `Result` in disguise |
| `host/connections.py` `credentials` | `(user, password)` |
| `host/login_proxy.py` `resolve_chain` | target credential + hop chain |
| `link/derive.py` `addressing_from_dict` | resolved id + addressing |
| `link/manage.py` `repair_all` | reports + skipped ids |
| `link/sentinel.py` `parse_impair_sentinel` | three parsed fields |
| `monitor/event_ops.py` `resolve_create` | `(start, end)` |
| `tunnel/discovery.py` `scan` | observations + error string |
| `tunnel/discovery.py` `discover_observations` | observations + error list |

Do them in small batches — each is independent, and `check_loaded`'s two
implementations must convert together. Effort S each, M in total.

---

## 1c. Close the plan-coordinate rules' five blind spots — as one change

`no-plan-coordinates`, `-ts` and `-tsx` share one regex and therefore share
one set of gaps. All three `note:` fields list them; this is the entry they
point at. Do the three rule files **together** — divergent regexes across the
three is the failure the split was already nearly caught by.

| # | Spelling the regex misses | Why | Fixable by widening? |
| --- | --- | --- | --- |
| 1 | plural `Tasks 4-6`, `Tasks 5/6` | `Task\s+\d+` cannot match the `s` | yes |
| 2 | letter `Plan C` | the alternation requires a digit | yes |
| 3 | lowercase `chaos plan 3` | the alternation is case-sensitive | yes |
| 4 | `task-12-report.md`, `pre-Task-11` | hyphenated, no whitespace to match, **either case** | yes |
| 5 | a coordinate wrapped across two `//` lines | **no** — see below | **never** |

**Class 5 is the one to design for, and no regex reaches it.** ast-grep's
`kind: comment` makes each `//` line its own AST node, so `…(Task` and
`// 13's…` are two nodes and no single-node pattern can bridge them. Do not
start from `b2ba1a4f`'s fix: that worked in `src/otto` only because a Python
docstring is ONE node spanning every wrapped line, which is why `\s+` sufficed
there. In TypeScript it is a node *boundary*. A real fix needs a different
mechanism — join adjacent line comments before matching, or a separate
line-based check run alongside ast-grep — and whatever it is has to run in
`make lint-arch` or it is not a gate.

Two pieces of evidence for why this is worth a gate rather than another sweep:

- The hand sweep leaked three times, each round finding what the last missed.
  Round 1: one class-5 site in `pages/SubjectPage.tsx`, caught in review.
  Round 2: seven occurrences across five files survived that review — `Plan C`
  in `coverage/renderer/spa_data.py`, `chaos plan 3` in `lifecycle.py` (the
  purged `2026-07-31-chaos-plan3-real-signal-integration.md`),
  `task-12b-report.md` in `topo/routing.ts` and `topo/LinkEdge.tsx` (a
  filename that exists nowhere, including in the `plans-archive-2026-08-03`
  tag), and `task-14` twice plus `task-10` in `covapp/pages/TicketsPage.tsx`.
  Round 3: `pre-Task-11` in `covapp/format.ts` — the file round 1 had already
  stripped eight coordinates from. It survived because class 4 was written up
  as *lowercase* `task-12-report.md`, so the capitalized hyphenated form was
  never searched for. **The enumeration of the blind spots had its own blind
  spot**, which is the strongest argument here for a gate over another sweep:
  a rule that runs on every commit does not care what the write-up imagined.
  All nine are fixed; nothing fails if they return.
- The sibling `no-tuple-return` had the same shape of bug and it was closed on
  2026-08-05: its `^(tuple|Tuple)\[` could not see a *quoted* annotation, so
  `discover()`'s own pre-refactor signature —
  `-> "tuple[OttoEnvSettings, list[Repo], list[BootstrapError]]"` — was
  invisible to the rule written because of it. The regex now admits one
  optional leading quote. Same lesson: these rules match TEXT, and text has
  more spellings than the first draft imagines.

Effort S for classes 1-4 (regex + a re-scan of both trees), M for class 5.
Whichever ships, prove it RED against a planted site of each class first —
the notes are YAML folded scalars, where a syntax slip yields a rule that
silently always passes.

---

## 1d. Two carry-overs from the docs-alignment wave

**The dependency pass is in neither architecture page.** `bootstrap()` runs
`resolve_dependencies(repos)` (`config/dependencies.py`) *between* its two
documented phases: it orders repos before any `[init]` module is imported, and
it is the sole producer of `BootstrapResult.warnings` — the one field
`DiscoveryResult` deliberately does not carry. `architecture/subsystems/bootstrap.md`
describes discovery and registration and never names it;
`architecture/overview.md` does not either. A reader currently cannot learn
from the architecture docs why repos are imported in the order they are, or
where a bootstrap *warning* comes from. Effort S.

**Ragged comments after the coordinate sweep — done, not debt.** Excising a
phrase mid-sentence left ~30 shipped `web/src` comment lines wrapped at half
width. Biome does not reflow comments, so nothing caught it. These were
reflowed in the final-review round: whitespace and line-breaking only, verified
by comparing the comment word-sequence of every touched file before and after.
Listed here so a future reader knows the reflow was mechanical and deliberate,
not a rewrite.

---

## 2. Live user-facing bugs — highest value per hour

From [churn-review-cheap-items-followups.md](churn-review-cheap-items-followups.md);
promoted here because they affect users today.

| # | Bug | Effort |
| --- | --- | --- |
| 2.1 | **★ `pytest.importorskip` escapes bootstrap containment.** `bootstrap.py:126` catches `Exception`, but `Skipped`'s MRO is `(Skipped, OutcomeException, BaseException)`. One optional-dependency test file makes **every** otto command traceback. The module docstring promises the opposite. Widening the `except` must not swallow `KeyboardInterrupt` or `SyncPhaseInterrupt`. | S–M |
| 2.2 | **`otto test --tests <name>` panics when `tach` is in the venv.** `collect_tests` clears `sys.modules`; the next session re-imports `tach.extension`, whose Rust init re-registers a Ctrl-C handler → `PanicException: MultipleHandlers`. Self-inflicted by the gate wave. CI is safe (test envs exclude the `lint` group); a lint-synced dev venv is not. Workaround `PYTEST_ADDOPTS="-p no:tach"`. | S |
| 2.3 | **`otto test <Suite>` reports 3× the true pass count.** 1-method suite prints `3 passed`; junit records 1. Reporting only, but user-facing. Root cause not established. | S–M |
| 2.4 | **`otto docker up` has no `any_failed` accumulator.** `_build`/`_down` sweep every repo and report at the end; `_up` raises out of the first failing repo. `decbec97` added three new raises to `compose_up`, making this far more reachable than when it was filed. Give `_up` its siblings' shape. | S |
| 2.5 | **`repair_link` can never repair a one-sided link it just impaired.** `impair --from <interfaced end>` succeeds; `repair_link` asks for both directions and hits the refusal. Fix is for repair to repair whatever directions are *placeable*. | S |
| 2.6 | **`import_test_file` keys its module name on the file STEM alone** (`_otto_suite_{stem}`, early return if present). Two repos with `tests/test_device.py` silently register only the first. The early return is load-bearing for idempotence — make the name path-derived, don't remove the check. | S |

---

## 3. Tier 1 remaining — the structural core

All six are M or M–L. This is the part the first wave did not touch, and §3/P1
is the reason it matters.

| # | Item | Verified state at `77af429d` |
| --- | --- | --- |
| 1.2 | **Split `session.py`** along its visible seams (transports ~290 ln, exec pool ~150 ln, recovery, elevation) | **1922 lines**, up 3 from the review's 1919. Received **zero** commits in the whole 44-commit wave — untouched, not fixed. |
| 1.3 | **Delete `Repo.settings` raw-dict API**; route `[coverage]`/`[lab]`/`[reservations]` through the specs that already validate them | Unchanged. `config/repo.py:752` still reads `self.settings = tomli.loads(...)` with the comment `# raw — coverage/reservation read it`; `coverage/config.py:39` still does `repo.settings["coverage"]`. |
| 1.7 | **Collapse the 7-way option-kwarg duplication** into one `HostOverrides` carrier; drop `timeout` from `run_on_all_hosts` | No `HostOverrides` exists. `context.py:310` and `config/fleet.py:266` both still carry the wide signature (each `# noqa: PLR0913`). `tach.toml` still records the DEBT edge that survives on `fleet.py`'s `DEFAULT_COMMAND_TIMEOUT`, annotated "(Tier 1.7)". |
| 1.8 | **Finish the cov extraction** — move `_do_get`/`_do_clean`/`_resolve_cov_settings`/`_connect_cov_hosts` (~400 ln) into `otto.coverage` | `cli/cov.py` **961 lines, byte-identical** to the review. Zero progress. |
| 1.9 | **`ACQUISITION_BACKENDS` registry** for monitor; collapse the 5-site branch | No registry. Still 5 sites: `monitor/factory.py:54`, `collector.py:189,358,456,694`. |
| 1.10 | **De-façade the collector** — move event CRUD to store/event_ops, meta derivation to models | `collector.py` **grew 853 → 899**. The Phase-1 façade is intact. |

---

## 4. Tier 2 remaining

`2.3` is half done (`render_leaf_value` seam landed in `607a948b`; the dialect
retirement is partial — `typer.Exit(1)` went **43 → 32**, `raise typer.Exit`
**71 → 57**). Everything below is open.

| # | Item | Note |
| --- | --- | --- |
| 2.1 | Lazy rich in `host/transfer/__init__.py`; `import otto.host` budget surface with rich/pydantic denylisted | Eager re-exports still at `transfer/__init__.py:21-39`. |
| 2.2 | **pyproject extras** (`[monitor]`, `[cli]`…) | No `[project.optional-dependencies]` at all. A ~15-line edit that the review flagged as getting much harder after first release — and 0.8.3 has shipped. **Cheapest item on this page.** |
| 2.4 | Split `utils.py`: `Status` → `result.py`; `cli_exposed`/`Arg`/`Opt` → `cli/` | `Status` still at `utils.py:193`. `d333087f` retired the `utils → lifecycle` DEBT edge (the adjacency), not the split. |
| 2.5 | Split `config/repo.py` (data / Rich panels / pytest collection / git client) | **Worse.** 845 → **1023** lines; `314eebbf` added a whole pytest ini-file discovery layer (`PYTEST_CONFIG_NAMES`, `pytest_config_paths`, `_load_pytest_config`, `configured_python_files`, +139 ln) — an 8th concern to a file already flagged for having 7. |
| 2.6 | Coverage: move `read_cov_*` meta readers down to `capture/`; split `reporter.py` | `reporter.py` unchanged at 1075. Coverage still has **zero** registries (§3/P3). |
| 2.7 | `/api/mode` → pydantic model through the existing codegen gate | Still hand-written both sides (`monitor/server.py:314`). |
| 2.8 | Web: shared `AppBarShell`; one theme owner (4→1); TreeView/CodeView placement; monitor testUtils; delete dead `types.gen.ts`; knip entry for `main.tsx`; shared CSS preamble | Untouched — the wave's 9 web files were the classicScript guard and contract tests. |
| 2.9 | Declared `rebuild_connections` protocol (3 `getattr` sites silently no-op on embedded); `EmbeddedHost` through `build_term_backend` | Explicitly deferred by `d72ca272`, which moved the sibling methods but left this. |
| 2.10 | Completion cache: registries expose `snapshot()`; cache becomes serialization, not re-derivation | **Worse.** 1405 → **1764** lines (+26%), and it was the wave's **top churn file** (8 of 44 commits). §3/P4's worst shadow copy grew while its structural fix stayed open. |
| 2.11 | Dead/stale sweep | Partially done. **Still open:** `suite/plugin.py:7`'s `_test_lifecycle` phantom (survives verbatim through two full workstreams); `remote_host.py:450` `_element_id_str` and `:458` `_slot_str`, both with **zero call sites**. **Done:** `BootstrapError` is now constructed (3 sites, `5475ff27`); plan coordinates in `src/otto` are handled by the quality-gates wave. |

---

## 5. Policy items still ungated

| § | Item | State |
| --- | --- | --- |
| 5.2 | **A registry or a recorded exemption.** New extension decision = registry entry, or a comment citing a recorded decision not to. | No gate, no checklist line. Coverage still has zero registries; product providers are still a bare list. |
| 5.5 | **Conventions that must hold at N call sites become functions.** | Half done — the DB-init rule got its seam (`15299154`). The other instance survives: the "usage-error exits are deliberately kept OUT of the try/except" comment is duplicated at `cli/tunnel.py:244` and `cli/link.py:216`. One extraction away. |
| 5.7 | **Refactor share as a health metric.** | Not instrumented. Currently **healthy** — the post-review window ran 18% refactor vs July's 2% — which is exactly when it is cheap to start measuring. |

Also still open from the review's §2 scorecard, none of it addressed:

- **Server-safety traps (PERSISTS +2).** `logger/management.py:67`'s `_state`
  module global; the two import-time uvicorn logger mutations at
  `monitor/server.py:87` and `:124`; 2 of 4 monitor registries still raise on
  re-register.
- **Host field multiplicity.** `1.1` removed the duplicated *methods*; the
  *fields* remain — **38 shared fields, 214 byte-identical lines** between
  `unix_host.py` and `embedded_host.py` (the review measured 204). Blast radius
  is smaller now but the mechanism is intact.
- **Terminology (§2 #10).** Untouched: `term` entrenched in a 6-symbol registry
  vocabulary, coverage "context" in 5 senses, "stamp" in 3.

---

## 6. Do NOT naively retry these — prior attempts were backed out with evidence

An agent picking this up will find these look like easy wins. They are not; each
was implemented, reviewed, and reverted for reasons that still hold.

1. **`collect_link_ids` resolving through the real loader** (backed out of
   `595b6c28`). Four proven regressions: completion goes dark for a whole repo
   when an `os_profile` is registered by an `[init]` module (the completer runs
   *without* `bootstrap()`, and link ids have no cache entry); cross-repo links
   dropped; one-sided links dropped; duplicate-host-id resolution inverted
   against the loader's first-wins. **The real fix is a link repository seam**,
   not a second resolver in the completer. Full detail in the follow-ups doc.
2. **Rendering the `BREAKING CHANGE:` footer text** (backed out of `56e89e01`).
   git-cliff's `commit.breaking_description` is *not* the footer — it stops at
   the first continuation line shaped like `Token: value` and ignores every
   footer after the first. On otto's own history it truncated `e4b18336`
   mid-sentence and dropped two of `ad0edab3`'s three migration notes. A correct
   version parses `commit.body` and needs `trim = false` plus explicit
   whitespace control throughout the template. `.github/workflows/release.yml`
   uses the same `cliff.toml`, so whatever lands here lands there too.
3. **Widening `iter_test_files` to recurse** (decided against in `77af429d`).
   The narrowness is now a documented contract: it is the only one of the three
   tests-dir readers that *executes* what it returns, at bootstrap, on every
   command. Note the recorded correction — the original latency argument was
   measured **backwards** and is not the reason; blast radius is.

## 7. Recorded decisions — not debt

Do not "fix" these; they are deliberate and documented.

- **`element`/`board`/`slot` vocabulary** — locked by recorded decision.
- **Spec/runtime field collapse** — deferred to
  [collapse-pure-data-spec-runtime-types.md](collapse-pure-data-spec-runtime-types.md),
  revisit at freeze.
- **Choke-point default resolution** — waiting on a second fan-out.
- **`cleanup_project`'s discarded result** and **`_image_exists` folding
  failures into "not cached"** — both fail-safe and documented.
- **`cli/test.py`'s exit passthrough** — runs inside a group callback whose
  return value click discards; a `return result` there would silently make
  failing `--tests` selections exit 0. Promoting it to a real leaf is its own
  refactor.

---

## 8. Suggested order

1. **§2** — six live bugs, all S or S–M, highest user value per hour. 2.1 and
   2.2 first: one breaks every command for a whole class of repo, the other
   breaks `otto test` in any lint-synced dev venv.
2. **2.2 (pyproject extras)** — the single cheapest structural item, and it is
   past its "gets harder after release" threshold already.
3. **One Tier 1 hub item, end to end** — recommend **1.8** (`cli/cov.py`). It is
   the smallest of the six, its sibling `cli/test.py` is the existence proof
   that the shape works, and it is the cleanest test of whether §3/P1's
   prescription actually reduces churn. Measure the file before and after.
4. **1.3 and 2.10 together** — both are P2/P4 ("validation gate ≠
   representation", "shadow copies amplify"), both touch `config/`, and 2.10's
   file grew 26% during a wave that never meant to touch it.
5. Everything else by the review's own ranking.

**A note on sequencing that the first wave learned the hard way:** each fix
commit's review generated 2–5 new follow-ups, and ten follow-up commits produced
roughly thirty new items. That tail is real work and worth doing, but it
outran the Tier 1 backlog once already. Budget for it explicitly rather than
letting it displace the structural items again.
