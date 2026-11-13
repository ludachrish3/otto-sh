# Periodic review 2026-11-12 — breakage by kind, weak areas, practice, focus

**Window:** `6fc0b33d` (2026-09-29, the previous review's HEAD) →
`b29b5cbca` (2026-11-11). 551 commits (519 human, 32 dependabot), releases
v0.17.0, v0.18.0, v0.19.0 and v0.20.0 inside it. Asked by Chris: is API churn
winding down, are there signs of poor practice (test workarounds, coupled
changes, product shortcomings that users or docs work around), are the weak
areas of earlier reviews improving, and are new classes of issue appearing.
Mid-review steer from Chris: judge breakage by **kind and severity**, using
the project's own surface and behaviour mechanisms, not by counting `!`
commits. The count should trend down; the severity should trend down too.

**Method:** git census and issue-tracker census (`gh api search/issues`); the
project's own `scripts/interface_churn.py --unrecorded --detail` over the
release tags; every `!` commit's `BREAKING CHANGE:` footer or body read and
classified by what a user experiences; three parallel read-only sweeps (test
practice, interface stability, docs and product gaps) whose headline claims
were spot-checked against the tree. **No test, gate or build was run.** The
kind classification in §1 is this review's judgement of each commit's primary
kind; many commits carry several. Counts from the docs sweep cover the 95
passages it opened, not the whole docs tree.

Prior reviews this continues: `churn-and-design-review-2026-08-03.md`,
`test-infra-review-2026-08-06.md`, `periodic-review-2026-08-25.md`,
`periodic-review-2026-09-02.md`, `periodic-review-2026-09-29.md`,
`20261004-system-architecture-review.md`.

---

## 0. Verdict

Breaking changes are falling in number, but through 0.19.0 the mix was moving
toward the most severe kind: the same command runs and does something
different. 0.20.0 had none of those, but it is one five-day release and not
yet a trend. The surface tooling cannot tell either way, because it records
the mildest kind of break and none of the worst. Package structure, security
and test honesty improved. The hub files, the pytest wrapper and the backlog
did not. One new problem has appeared: the landing and gate machinery has
become the fastest-growing and most-edited part of the repo.

---

## 1. Breakage by kind

### 1.1 Kinds, most to least severe

| Kind | Meaning |
| --- | --- |
| silent | The same command or call still runs and does something different: other output, another exit code, other things deleted, installed or started, files written elsewhere |
| stored | Stored data, an env var or an on-disk layout is invalidated, with no alias and no reader for the old form |
| refusal | Input that was accepted is now refused, loudly, with a message |
| mechanical | A name or signature changed; the caller gets a TypeError or ImportError and the fix is mechanical |
| contract-only | A declared right was withdrawn; nothing changes at run time |
| maintainer | `make`, gates, release, lab: not user-facing |
| trivial | Message wording, a timing bound, a source-compatible reshaping |

### 1.2 Per release

| Release | silent | stored | refusal | mechanical | contract-only | maintainer | trivial | total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.17.0 (window part) | 10 | 5 | 7 | 8 | 0 | 7 | 2 | 39 |
| 0.18.0 | 12 | 4 | 5 | 2 | 0 | 0 | 5 | 28 |
| 0.19.0 | 7 | 1 | 1 | 3 | 0 | 0 | 0 | 12 |
| 0.20.0 | 0 | 1 | 2 | 4 | 3 | 2 | 0 | 12 |
| since 0.20.0 | 0 | 0 | 1 | 0 | 0 | 0 | 1 | 2 |

- **The count per release fell only because releases got shorter.** They went
  from about three weeks to five days; the weekly rate stayed at roughly 14 to
  17 throughout.
- **The two severe kinds grew as a share until 0.20.0.** Silent plus stored
  were half of the user-facing breaks in 0.17.0, about 70% in 0.18.0 and
  0.19.0, and one of ten in 0.20.0.
- **20 of the 29 silent breaks are `fix!`**: corrections found by an audit
  (thin-CLI parity, the security cluster, the naive-user study), not
  exploratory redesign. They are still what a user would have had to absorb.

### 1.3 What the mechanism sees

`scripts/interface_churn.py` over the tags:

| Window | marked | surface | broken | churn | flags (approx.) | env (approx.) |
| --- | --- | --- | --- | --- | --- | --- |
| v0.16.1..v0.17.0 | 41 | – | – | – | −8 +24 =147 | −2 +2 =23 |
| v0.17.0..v0.18.0 | 28 | 4,378 | 21 | 0.48% | −2 +8 =153 | −0 +3 =26 |
| v0.18.0..v0.19.0 | 12 | 4,464 | 15 | 0.34% | −0 +7 =160 | −0 +0 =26 |
| v0.19.0..v0.20.0 | 12 | 4,511 | 1,061 | 23.52% | −0 +0 =160 | −0 +0 =26 |
| v0.20.0..HEAD | 2 | 4,215 | 0 | 0.00% | −0 +0 =160 | −0 +0 =26 |

- **The report ranks the releases backwards by severity.** 0.18.0 and 0.19.0,
  which carried 19 silent breaks between them, score 0.48% and 0.34%. 0.20.0,
  which carried none, scores 23.5%: the keyword-only and rights sweep (1,443
  parameters narrowed to keyword-only, 365 rights withdrawn), loud, mechanical
  and one-time.
- **The 0.20.0 sweep reduced future severity.** Positional-slot shifts were 13
  of 74 distinct findings in 0.18.0 and 12 of 25 in 0.19.0; they are now
  impossible. Since 0.20.0 the recorded surface has zero broken sites.
- **Most breaks are invisible to the tooling.** Of 83 user-facing marks, 28
  carry a golden finding; 55 were marked on judgement (27 pure behaviour, 18 on
  surfaces with no golden: CLI 9, on-disk 4, lab 3, settings 1, env 1; 8 Python
  changes the dump does not record). CLI flags, env vars, exit codes and
  default paths are not in the golden. `docs/api/stability.md` says "A change
  of behaviour is caught by review" and "No area is stable yet".
- **No unmarked break on the recorded Python surface.** 15 unmarked commits
  touched the golden or manifest since the v2 cutover, with zero findings.

### 1.4 The breaks that would have hurt a user most

- `e5f5a8746` (0.19.0): `install` skips a product whose `is_installed` is true,
  with "no force mode". Rebuild and re-install keeps the old build.
- `764260584` (0.19.0): `otto run cleanup` and `ensure("clean")` delete
  coverage counters.
- `fe4e58d6a`, `76166406c` (0.18.0): `run()`/`exec()` return different text
  (blank lines and carriage returns kept), first on the shell route, then on
  local exec; `bc375284d` (0.19.0) fixed a third route.
- `93553702c` (0.19.0): any bool-returning verb or instruction exits 1 on
  False. `355b206f8` (0.18.0): a partial coverage fetch exits 1.
- `9e548cf97` (0.17.0): a library process that never imports `otto.bootstrap`
  silently attaches no products or providers; `8487f961e` made it a loud
  RuntimeError three days later.
- `4472f37b5` (0.17.0): the product-directories variable that `OTTOPATH` replaced is "no longer read at all: no alias
  and no warning", two days after `0d26d38da` changed its separator.
- `533d1f1fd` (0.18.0) then `4c55854a0` (0.19.0): run output moved under
  `otto-output/`, then out of it and under `$OTTO_HOME/otto-xdir`. The ignore
  lines `otto init` wrote in 0.18.0 protect nothing in 0.19.0.
- `560a9fe50` (0.20.0): `capture.json` schema 4 dropped an unused field;
  committed manual captures must be re-captured, three days after `782125ebb`
  (0.19.0) said they "keep loading".

### 1.5 Repeats and reversals

Docker took 15 marked commits, coverage 12 plus kmodcov 3, host verbs, options
and construction 16, run context and composition 12, monitor 6.

1. Run-output layout: reversed across consecutive releases (above). With
   `255487e2c` and `885c11991`, output and coverage directory shape took four
   breaks, after the three the 09-29 review counted.
2. Coverage files: store v8 → v9 in 0.19.0, capture v3 → v4 in 0.20.0.
3. Docker host flag: `--on` required (`cb23b63f1`, 10-01), added to new verbs
   (`73fcd18ae`, 10-03), renamed `--parent` (`50e5f464d`, 10-04).
4. The product-directories variable that `OTTOPATH` replaced: separator changed 10-16, variable deleted 10-18.
5. Repo vocabulary: 7 of the 14 names renamed on 10-18 were born in the window.
6. Reshaped, then made private: `CommandPowerController`, `NetBoxInventory`
   (`5b332cce1`), `GcdaFetcher.fetch_all` (`355b206f8`), all by `d1b75472d`.
7. `UnixHost.creds`: `da41798d0` said it "stays the second positional
   parameter"; `f47d617f6` made it keyword-only.
8. Docker auto-start: reshaped `1e311a944` (10-03), removed `69dc1f0e1`
   (10-13); staging paths changed in three commits within three days.
9. `Host` protocol: five commits changed it, none a pure widening for
   implementers (`764260584`, `615dbd9be`, `f47d617f6`, `30f90d07b`,
   `05e3a722d`).

Items 3, 4 and 5 sit inside one release. Items 1 and 2 crossed releases.

### 1.6 What raises the severity of what ships

- **No reader for old data.** 11 of 12 versioned formats read only the current
  version. There is no deprecation or rename mechanism in `src/otto`; removed
  settings keys are nine hand-written refusals.
- **Missing migration notes.** 23 of the 93 marked commits have no
  `BREAKING CHANGE:` footer, among them kebab-case host ids (`2f40bee66`), the
  uv dependency (`5c2e93022`) and the coverage run labels (`782125ebb`).
- **Inconsistent behaviour marking.** Four unmarked behaviour changes have a
  marked twin: `cd34a51bf`/`4bb4e37ac`, `a5b84c2ec`/`93553702c`,
  `889adab54`/`fe4e58d6a`, `2de808c73`/`f719dd572`. Found from five bodies read
  out of 41 candidate subjects.
- **Policy text.** `AGENTS.md:11` still says "Do not worry about backwards
  compatibility at this time", and 503 of 551 commits carry an Assisted-by
  trailer. Three `!` commits with no source file landed after the rule that
  maintainer tooling is never breaking (`6c0c4f2dc`, `9183cbea8`, `9d83d97e9`).

### 1.7 The plan already names the fix

#935 (behaviour contracts and conformance tests for host command output,
docker lifecycle ownership and the coverage layout) covers the three areas
that took the severe breaks. #937 covers the rename table and the deprecation
path. Neither has started. #520's CLI and settings half is open. Only #934,
which addressed the mildest kind, has landed.

---

## 2. Earlier weak areas

### 2.1 Improved

- **Import cycle: gone.** 20 modules on 09-29, 23 on 10-04; `tach.toml` now
  sets `forbid_circular_dependencies = true`, with ten role layers and exact
  edges (#522, #590).
- **Security cluster** (#487, #547–#552): all closed in 0.18.0.
- **10-04 P0s:** the stale-cache false green (#592, `55d373350`) and the
  pytest-cov dependency (#593, `9231cf82a`) are fixed, with an installed-wheel
  smoke test in CI and release (`scripts/wheel_smoke.py`).
- **Standing menu items closed:** `OttoError` root, one registry engine
  (`5b332cce1`), `HostOverrides` (`fe411684c`), the coverage verbs out of the
  CLI (`cli/cov.py` 1,127 → 847), eleven CLI-versus-library differential test
  files (#525's substance).
- **Main reds:** 17 auto-filed "CI failed on main" issues in October, 4 so far
  in November, after landings moved to pull requests on 11-01. Nightlies are
  still red regularly (5 in November's first 12 days, two open).
- **User feedback of 10-02:** about 15 of 21 items fixed, 3 partly (thin docker
  wrappers, code-block headers, termynal width 100 not 120), 3 open (fuzzy
  completion, netem on a container, `@cli_exposed` beyond host verbs).
- **Fable feature gaps:** `otto init` and power control done; secrets
  indirection and JSON result export partly; host groups open.

### 2.2 Not moved

| Hub | 09-29 | 11-11 |
| --- | --- | --- |
| host/session.py | 3,462 | 3,953 |
| host/host.py | 3,004 | 3,871 |
| host/userland.py | 3,184 | 3,207 |
| config/completion_cache.py | 2,614 | 3,039 |
| cli/invoke.py | 2,286 | 2,486 |
| host/transfer/nc.py | 2,052 | 2,066 |
| host/unix_host.py | 1,611 | 1,690 |
| cli/main.py | 1,175 | 1,443 |
| cli/cov.py | 1,127 | 847 |

- `src/otto`: 122.6k lines / 339 files → 159.0k / 409. Files of 1,500 lines or
  more: 11 → 16. `host.py` took 36 commits, 22 of them breaking. `cli/main.py`
  has two 300-line functions (`main`, `entry`).
- Lock-downs of 09-29: #522 and #523 closed; #520, #521, #524, #526, #527,
  #528, #529 open. #526–#528 (the hub splits) are untouched for a fourth
  review.
- **Pytest wrapper (#469):** `addopts` still cleared, warnings config still
  replaced, no passthrough, `_pytest.runner`, `_request`/`funcargs`, the
  private pytest-asyncio runners and the SIGALRM timeout all remain.
- **Still open:** `otto env` failing on the layout the install docs recommend
  (#554, since 10-01); no pyproject extras (since the July review); unbounded
  fleet fan-out and a commit per metric point (10-04 finding 10); docker
  staging still tars the whole context before `.dockerignore` applies; the
  parity bugs #501, #509, #510, #511 from the 09-29 audit.
- **Completion-cache shadow schema** (08-03 P4): schema 23 → 30 in the window;
  `cli/main.py ↔ completion_cache.py` is still a top co-change pair.

---

## 3. Development practice

### 3.1 Tests

Mostly honest. Zero unconditional skips, zero "until issue N" skips, no rerun
plugin, `filterwarnings = error` unchanged, the full coverage floor raised
95.5 → 96, and eight commits explicitly refuse to soften a test (`b984a66b8`,
`5299bca86`, `a7539dd0c` among them). Debits:

- **Wall-clock assertions are regrowing:** 50 → 78, and 17 → 44 in files with
  no `serial_timing` marker. `b68016bb4` added 0.05–0.1 s real-clock bounds
  eleven days after the `no-short-deadline-in-tests` rule, which matches
  literals only.
- **Product defaults moved for the lab's sake:** the readiness timeout went
  3 → 10 s (`f0c63f3b0`) after "128-149 lab tests failed nightly", and e2e
  children get a 60 s completion deadline against the 2 s product default
  (`tests/e2e/_otto_subprocess.py:35`).
- **One new non-strict `pytest.xfail`** at
  `tests/integration/host/test_console_release_bed.py:215`.
- **Skip sites 65 → 135**, all capability, tool-presence or root probes; no
  guard counts how many actually run in CI.

### 3.2 One decision fixed at several entry points

- Command output: `889adab54`, `fe4e58d6a`, `76166406c`, `bc375284d`.
- Monitor library and archive writes missing a check the HTTP path has:
  `0c25d4771`, `cd34a51bf`, `4bb4e37ac`.
- Stdout leaking into completion: `6668ffe59`, `77484527a`, `5a35bd647`,
  `e4477894e`.
- Stopped or absent container: `204093d5b`, `d13f8b548`, `26f08ae4f`
  ("replaces the three copies"), `b488cd0ac`.
- User text eaten by Rich markup: `40df51982`, `78f5ff84f`, `c03bdca29`,
  `a64146a5c`.

This is the class the ONE DECISION, ONE PLACE rule targets; it is still
arriving copy by copy.

### 3.3 Docs that work around the product

Of 89 user-doc passages classified: 12 domain physics, 25 deliberate
trade-offs, **28 product gaps the docs paper over, 24 inconsistencies to
memorise**. "silently" appears 45 times in user docs. Examples:

- `docs/cli/cov/exclusions.md:194`: a rule that matches nothing gives no
  warning.
- `docs/cli/docker/use-cases.md:204`: a later fragment's value "silently
  replaces an earlier one's. There is no refusal and no warning."
- `docs/cli/link/list.md:44`: a `--from` impairment can be left "live,
  invisible, and clearable only by hand with `tc`".
- `docs/cli/cov/instrumenting/containers.md:162`: a non-root container fails
  to write counters "silently, with no error anywhere otto surfaces".
- `docs/cli/host/embedded.md:137` says `tftp` is not supported; the generated
  `docs/cli/host/families.md:48` lists it as a transfer.
- Three list separators: `,` for `-E`, the path separator for `OTTOPATH`, `+`
  for labs.

### 3.4 Coupling

- 97 of 216 source commits also edited a guard or baseline file (the golden
  55, `tach.toml` 42, `test_error_base.py` 23, `test_dynamic_import_audit.py`
  19).
- The cache toggle (#764) landed as 30 commits in one day (10-29), 13 of them
  fixes, one reversing the fix before it (`e16aee196` → `edaa53bd3`).
- Human commits: median 5 files, mean 20.7, p90 46. The tail is deliberate
  tree-wide sweeps (`f47d617f6` 797 files, `4472f37b5` 721).

---

## 4. New classes of issue

- **Process tooling is now a product.** `scripts/*.py` 12.9k → 35.0k lines
  (31 → 74 files); tests of it 292 → 1,945 functions (5.8k → 33.2k lines);
  Makefile 1,643 → 2,220 lines and 97 → 124 targets; `docs/contributing.md`
  999 → 2,520 lines and the most-edited file in the repo (90 commits); the
  gate table 69 → 113 rows; 9 of the window's 44 specs are about landing and
  gating. The gate design changed about ten times in six weeks.
- **Suite cost is steering what runs.** `unit-repeat` left the PR gate (56–63
  minutes, 19 at 09-02); the `interpreter_agnostic` family is deselected on
  four of five Pythons; chaos left the release; the local tip gate dropped
  quick, docs and coverage. Each has a stated reason; together they are a
  trend. Tests 289k → 441k lines (2.4× → 2.8× source); unit test functions
  11,220 → 17,865. Harness self-tests are the slow tail of `tests/unit`: 22
  carry 960 s budgets.
- **Audits outrun fixes.** 387 issues opened and 229 closed in the window;
  open issues 169 (10-04) → 299, 63 of them bugs; 123 open issues date from
  September. Fix share of commits: 12% in September, 28% in October, 41% in
  November; feature commits 57 → 50 → 16. The right phase for the project, and
  the source of §1's silent column.

---

## 5. Maturity

Process discipline and package structure are well past alpha. The contract of
the core verbs is not: what `install`, `cleanup`, `exec` and the output layout
do was still being decided in November, there are zero stable areas, and
there is no deprecation path. Otto is not ready for a user who cannot read the
changelog at every release.

---

## 6. Recommendations

1. **Do #935 next**, and add CLI flags, exit codes and default paths to the
   golden, so severity becomes measurable.
2. **Make the footer mandatory on every `!` and have it name the kind**, so
   `interface_churn.py` can report silent and stored breaks per release.
3. **Stop hard cutovers on stored data:** read the previous format version,
   and land the rename table (#937).
4. **Replace `AGENTS.md:11`** with a rule that a silent behaviour change needs
   a recorded decision.
5. **Freeze gate tooling for a release cycle** and spend it on `host.py`,
   `session.py` and the bug backlog.

---

## 7. Baselines for the next review

Monthly census (`git log --since/--until`): Sep 365 commits / 32 `!`, Oct 386 /
63, Nov 1–11 123 / 26. Marking eras do not compare: "user-facing behaviour
change is breaking" entered `docs/contributing.md` on 10-19 (`fix!` 7 before,
37 after).

Window type census: fix 119, docs 118, build 54, test 49, fix! 44, feat 43,
chore 37, feat! 34, refactor 20, ci 10, refactor! 7, build! 6, perf 5.

Suppressions, src (09-29 → HEAD): `ty: ignore` 67 → 104, `# noqa` 447 → 549,
`type: ignore` 21 → 19, `Any` annotations 310 → 416, TODO markers 14 → 14.

Harness: conftests 6,642 → 7,698 lines; `tests/_fixtures` 8,075 → 13,425; CI
YAML 1,600 → 2,235; ast-grep rules 29 → 38; `mark.timeout(` 80 → 142;
`mark.xdist_group(` 86 → 115.

Kind classification of the 93 marked commits (primary kind):

- **silent (29):** `255487e2c` `56c08b3f5` `493b7cf6f` `ddf16d6f2` `1e311a944`
  `73fcd18ae` `55d373350` `9cde256d4` `9e548cf97` `1dfced7bc` `40df51982`
  `a5a397c99` `fe4e58d6a` `ece92df42` `39931b882` `f6ae8a377` `690419640`
  `8bfa81b01` `693d9086a` `355b206f8` `be8266114` `76166406c` `93553702c`
  `764260584` `e5f5a8746` `bcb5bdace` `885c11991` `f15bffa03` `4c55854a0`
- **stored (11):** `c61c7baa0` `da60230b5` `2f40bee66` `0d26d38da` `4472f37b5`
  `19efcce6f` `4f1c20d75` `533d1f1fd` `c200faf6a` `782125ebb` `560a9fe50`
- **refusal (16):** `bf375c894` `35c289c92` `6022fcf82` `da41798d0` `69dc1f0e1`
  `2f0942f0a` `8487f961e` `881e206a2` `e7f3eece8` `60c4d7dac` `9b31a3c91`
  `440b04ea7` `2b2260bb0` `f719dd572` `6d1ca1a30` `4bb4e37ac`
- **mechanical (17):** `cb23b63f1` `844e905c3` `9a57a301c` `a36f0eea8`
  `50e5f464d` `14caec5b6` `96671a511` `5b332cce1` `a45548cc0` `7023c7cb0`
  `5c2e93022` `b68016bb4` `bc375284d` `8b7ba619d` `f47d617f6` `30f90d07b`
  `05e3a722d`
- **contract-only (3):** `6c0c4f2dc` `615dbd9be` `d1b75472d`
- **maintainer (9):** `0f067d673` `733bceb1f` `1b940a8d3` `cfb1c05cd`
  `1b55fcd2b` `53f416d1b` `19fd77163` `9183cbea8` `9d83d97e9`
- **trivial (8):** `718a98aa1` `743177957` `fe411684c` `819abad45` `43b0eb6b8`
  `2c366bed7` `bb91c0464` `cf2f15c4b`

## 8. Caveats

- The kind classification is a judgement from commit footers and bodies; a
  commit with several breaks is counted once, under its most severe kind.
- Unmarked behaviour breaks were sampled, not swept (5 bodies of 41 candidate
  subjects).
- The docs classification covers 95 opened passages; the keyword counts are
  raw greps.
- CI run history was not read; the main-red figures are the auto-filed issues.
- The 10-04 architecture review's line 244 ("items 1–3 and 5–10 are still
  open") is stale: findings 1, 2 and 7 are fixed, 5 and 6 partly.
