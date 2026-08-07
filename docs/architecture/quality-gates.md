# Quality gates

otto is checked by a dozen small tools rather than one large one, split
across two languages that do not have the same tooling available. This page
is the inventory: what kind of check exists, which tool performs it on each
side, and — the half that keeps such a list from decaying into trivia —
what actually *runs* it.

It is deliberately about **gates**: checks a machine fails you for. otto's
recurring rules live in {doc}`principles`, its test taxonomy in
{doc}`testing`, and the "what do I type" table in {doc}`../contributing`.
This page is the seam between them, and its editorial rule is at the bottom.

## What checks what

Rows are kinds of check; the two columns are the two languages. A cell
reading `— none today` is a **gap**: the check is meaningful for that
language and nothing performs it. A bare `—` means the check does not apply
on that side.

| Kind of check | Python | TypeScript |
| --- | --- | --- |
| Lint rules | `ruff check` (`select = ["ALL"]`, minus a documented ignore list; `raises-extend-require-match-for` extends PT011 to pydantic's `ValidationError`, so a raises-check must name the field or constraint it means — with `extra='forbid'` models, a bare raises passes on *any* rejection) | `biome check --error-on-warnings` |
| Formatting | `ruff format --check` | Biome — the *same* command (rules + format + **assists**) |
| Type checking | `ty` (pinned `==0.0.64`) | `tsc --noEmit` via `scripts/typecheck_web.sh` (vendored Untitled UI diagnostics filtered) |
| Unused code / deps | ruff (`F401`, `ARG`, …) | `knip` — unused files, exports, dependencies |
| Module layering | `tach` against `tach.toml` | — none today |
| Scoped pattern rules | `ast-grep` against `.ast-grep/rules/` — fourteen Python rules | `ast-grep` — two rules (`no-plan-coordinates-ts`, `-tsx`), scoped to `web/src/**` |
| Import cost | `scripts/import_budget.py` — module-count caps, snapshots, denylist | — (knip covers dependencies only) |
| Tests | `pytest` (+ `xdist`, `repeat`, `hypothesis`) | `vitest` |
| Coverage floor | `coverage.py` / `pytest-cov` — 95 for the full local run, 90 for the hostless CI slice | `@vitest/coverage-v8` for the unit floor; the browser leg is folded in by `monocart-coverage-reports` and the merged report gated by `nyc` |
| Browser e2e | `pytest-playwright` — two suites (monitor dashboard, coverage report), three engines each (Chromium, Firefox, WebKit) | (same lane — `OTTO_TS_COVERAGE=1` arms CDP V8 collection under `make dashboard` only; the bundle-filter **drift guard** runs at configure time in every lane, armed or not — `tests/_fixtures/_ts_bundle_filter.py`) |
| Cross-language contract | `tests/_fixtures/covapp_contract.json`, asserted from both sides; `types.gen.ts` **and** `export.gen.ts` codegen + `git diff --exit-code` | (same two mechanisms) |
| Vendored source | — (nothing vendored) | `scripts/check_untitledui_hash.sh` — did *we* edit it; `scripts/check_untitledui_drift.sh` — did *upstream* |
| Built-bundle gates | — | `build_web_no_warnings.sh` (warnings are errors), `check_airgap.sh`, `check_brand_tokens.sh` |
| Docs | `sphinx-build -E -a -W` (clean rebuild), `doc8`, Sphinx doctest, `scripts/lint_markdown_doctests.py`, `--doctest-modules` over `src/otto` | — |

### The empty cell is the honest answer

TypeScript has **no module-layering gate**. `tach` validates otto's Python
module dependency contracts against `tach.toml`; `web/` has nothing that
does this. knip is the closest tool in the box and it is not the same
check — knip finds code nobody imports, `tach` finds code the wrong module
imports. Writing knip into that cell would produce a page that reads true
and is not, which is the failure mode this whole page exists against.

`tach.toml` is a hand-maintained **ratchet baseline**: every known-bad edge
is listed with a `DEBT:` comment explaining it, and the list may only
shrink. `tach sync` is forbidden as a fix — it rewrites the file and strips
every comment, converting a documented debt list into a blessed one.

### The ast-grep rules

`.ast-grep/rules/` holds sixteen rules, all `severity: error`. The scan roots
are `src/otto web/src tests` — tests/ joined in the test-infra remediation
(2026-08-06) so that *test-suite* pattern rules can exist — and the
discipline that keeps that widening safe is that every rule carries an
explicit `files:` scope; a rule scoped to `tests/**` must also ignore the
fixture SUT repos (`tests/repo1..repo3`, `repo_broken`, `repo_e2e`) and
`tests/firmware`, which are user-example input data:

| Rule | Scope |
| --- | --- |
| `coverage-git-through-gitio` | `src/otto/coverage/**`, `src/otto/cli/cov.py` |
| `error-render-through-helper` | `src/otto/**` — explicit since tests/ joined the scan roots (it always meant shipped renderers) |
| `models-no-module-scope-config-import` | `src/otto/models/**` |
| `no-bare-runtimeerror-in-libraries` | `src/otto/link/**`, `src/otto/tunnel/**`, `src/otto/docker/**`, `src/otto/host/transfer/**` — four packages signalled unreachable-host, command-failed and structural-refusal through one stdlib type, so no consumer could tell them apart; 37 sites converted to `otto.host.errors`' pair, package domain classes, or `ValueError` in the wave that landed the rule |
| `no-bare-status-return` | `src/otto/**` |
| `no-handrolled-deadline-poll` | `src/otto/**` + `tests/**` minus fixture repos — poll-until-deadline grew 21 copies in three incompatible shapes with divergent expiry behavior; `otto.utils.wait_for` / `wait_for_async` is the one spelling, and expiry always raises (silent expiry is the defect class); `src/otto/host/shell_liveness.py` is ignored as the sanctioned fused probe-response primitive |
| `no-parents-arithmetic-in-tests` | `tests/**` minus fixture repos — `Path(__file__).parents[N]` encodes the file's own depth, so moving the file silently re-anchors every path built from it; import `TESTS_ROOT`/`PROJECT_ROOT` from `tests/_fixtures/paths.py` (the one sanctioned derivation point) |
| `no-plan-coordinates` | `src/otto/**` |
| `no-plan-coordinates-ts` / `no-plan-coordinates-tsx` | `web/src/**` |
| `no-raw-started-poll` | `src/otto/**` + `tests/**` minus fixture repos — polling a started flag without a task-death guard turns a startup failure into an infinite hang; `MonitorServer.wait_started()` is the readiness API (its event is set on success AND failure, re-raising the recorded cause), and the wave retired every `noqa: ASYNC110` whose justification was "no event source available" |
| `no-retry-marker-in-otto-tests` | `tests/**` minus fixture repos — the first tests-scoped rule; fully armed (its one ratchet ignore died with the hop-transfer flake fix) |
| `no-tuple-return` | `src/otto/**` |
| `otto-subprocess-env-through-helper` | `tests/**` minus fixture repos — a local `COVERAGE_PROCESS_START` dict is a fork of the subprocess env dance, and every fork that ever existed dropped the `-p no:tach` #193 scar key first; `tests/e2e/_otto_subprocess.py` is the one home |
| `typer-exit-outside-cli` | `src/otto/**` (CLI exempt) |
| `typer-exit-raises-must-assert-code` | `tests/**` minus fixture repos — a bare `pytest.raises(typer.Exit)` passes on the default exit code 0 ("failed successfully"); the rule forces binding the excinfo, and the bound form's exit-code assert is what actually pins refusal |

Two carry a mechanism worth knowing about.

**`no-tuple-return` ships with a baseline that is not zero.** It bans a bare
`tuple[...]` in the *outermost* return position of a public function —
`-> tuple[A, B]` fires, `-> list[tuple[str, T]]` does not, because a tuple
inside a container is the `dict.items()` idiom and not a crammed return.
Eighteen suppression sites existed when it landed, each carrying an inline
`# ast-grep-ignore: no-tuple-return` beside a `# DEBT(no-tuple-return)` or
`# PERMANENT(no-tuple-return)` marker and a one-line reason — twelve debt,
six permanent. `tests/unit/test_tuple_return_debt.py` is the other half, and
it enumerates **seventeen** `(module, function)` keys, not eighteen: six
permanent and eleven debt. The two counts differ by one because
`host/binary_loader.py::check_loaded` is two implementations of the same ABC
method, so its two sites share a single key. Sites and entries are different
questions here, and the test asks the second. The two gates pin each other:
adding a site fails ast-grep, and fixing a site without deleting its entry
fails the test. That second half is what a count-based
ratchet cannot do — it makes the list keep *describing the tree*, rather
than merely bounding it.

The ast-grep half matches *text* on the annotation, which bounds it two
ways, and the asymmetry between them is the instructive part. It cannot see
through a type alias (`Pair = tuple[int, str]`; `-> Pair` is invisible) —
and that limit is hypothetical: no such alias exists in `src/otto`, and the
fix for one would be to inline it. It also could not, as first written, see
a *quoted* annotation — and that limit was live. otto bans
`from __future__ import annotations` (it trips Sphinx `-W`), so a quoted
string is the only forward-reference spelling available and 125 return
annotations under `src/otto` use it; the rule's own motivating defect,
`discover()`'s pre-refactor signature, was
`-> "tuple[OttoEnvSettings, list[Repo], list[BootstrapError]]"`. The regex
now admits one optional leading quote. The limit worth writing down was the
one nobody had thought to write down — which is why the pytest half matters:
its `_sites()` walks the real AST and strips quotes, so it was never blind
to the quoted form, and the composite gate held while one half did not.

**`no-plan-coordinates` is three files because one would not have worked.**
It bans references to SDD plan coordinates (`Task 12`, `Plan 5a`, `plan §`,
`review finding F3`) in shipped comments and docstrings — the plans are
purged at merge, so such a citation is a dangling reference the moment it
ships. The split is not stylistic: ast-grep's `language: tsx` matches
`.tsx` files **only**, and most of `web/`'s coordinates lived in `.ts`
(`data/*.ts`, `charts/options.ts`, `topo/layout.ts`). A single `tsx` rule
would have shipped as a gate that cannot fire over the majority of its own
subject matter.

That gate has **documented limits**, recorded in all three rules' `note:`
fields and owed follow-up work (`todo/churn-review-remaining-work-2026-08-05.md`
§1c). Five spellings it does not catch: plural `Tasks 4-6` (the `s` breaks
`Task\s+\d+`), letter `Plan C` (the alternation requires a digit),
lowercase `chaos plan 3` (the alternation is case-sensitive),
`task-12-report.md` or `pre-Task-11` (hyphenated, no whitespace to match, in
either case), and — the dangerous one — a
coordinate wrapped across two `//` lines, where each line is its own comment
node so no single-node regex can bridge it. The Python side fixed wrapping
with `\s+` because a docstring is one node spanning every wrapped line; in
TypeScript it is a node *boundary*, and needs a different mechanism entirely.

None of the five is gated; each was handled by a check that ran once — a
hand sweep for the first, second and fourth, a throwaway line-pair detector
for the fifth, and nothing at all for the third until it was found. The
honest thing to record is what that bought: three rounds, each finding what
the last one missed. One wrapped site survived the sweep and was caught in
review. Seven more occurrences across five files survived that review — `Plan
C` in `coverage/renderer/spa_data.py`, `chaos plan 3` in `lifecycle.py`,
`task-12b-report.md` in two `topo/` files, and `task-14` twice plus `task-10`
in `covapp/pages/TicketsPage.tsx`. One survived even that: `pre-Task-11` in
`covapp/format.ts`, a file the first sweep had already stripped eight
coordinates from. It lasted because the fourth blind spot had been written
down as *lowercase* `task-12-report.md`, so a capitalized `Task-11` was never
in the search space — the write-up was narrower than the thing it described,
which is the same decay this page is about, one level up.

All five spellings scan clean today, verified by a scan that joins wrapped
comment runs and matches both cases; nothing fails if one comes back. That
gap between *clean* and *guarded* is this page's own distinction turned on
this page's own subject. A gate claiming total coverage would be the exact
failure warned about below, so this one states its blind spots instead.

## What binds each gate

A tool nobody runs is a tool nobody runs. This table is the binding: the
`make` target you would type, the `nox` session that wraps it, and the CI
job that enforces it on push.

| Gate | `make` | `nox` | CI job |
| --- | --- | --- | --- |
| ruff (lint + format) | `lint-python` | `lint` | `lint-python` |
| tach + ast-grep | `lint-arch` | `lint` | `lint-python` |
| ty | `typecheck-python` | `typecheck` | `typecheck-python` |
| Biome + knip | `lint-ts` | — | `check-ts` |
| tsc | `typecheck-ts` | — | `check-ts` |
| vendored-source hash | `check-ts` | — | `check-ts` |
| vitest + unit coverage floor | `coverage-ts-unit` | — | `check-ts` |
| pytest (hostless matrix) | `coverage-hostless` | `tests_hostless` | `tests` (3.10–3.14) |
| test-isolation leak guard | `nox-unit-repeat` | `tests_unit_repeat` | `unit-repeat` |
| import budget | `profile` (adds hyperfine) | via `tests_hostless` | `tests` — enforced by `tests/unit/import_budget/` |
| build-lane invariants (addopts keeps `-p no:tach`) | via `coverage*` | via `tests_hostless` | `tests` — enforced by `tests/unit/test_lane_invariants.py` |
| probe-status honesty at the bed-probe helpers (`run_probe`/`probe_text`/`snapshot_host` raise on non-ok), plus AST scans over the chaos lanes banning the two consumer shapes found live: a factory unwrapping `.value` before the check can vet it, and a `pkill`/`pgrep -f` pattern not built by `argv_pattern`. Stated blind spots: factories imported from other modules; patterns assembled outside literals/f-strings | via `coverage*` | via `tests_hostless` | `tests` — enforced by `tests/unit/test_bed_oracle_honesty.py` |
| ambient-env hermeticity: the runtime strip pin, a per-tree collection pin (conftest chains must not write `OTTO_*` at import — caught integration's year-old `OTTO_SUT_DIRS` re-injection), and a module-scope env-write AST ban over all conftests/fixtures (aliases and one-hop helper calls resolved; the root conftest's colour+strip block is the one sanctioned exception) | via `coverage*` | via `tests_hostless` | `tests` — enforced by `tests/unit/test_env_hermeticity.py` + `tests/unit/test_conftest_env_writes.py` |
| air-gap + brand tokens + type drift | `web` | — | `dashboard`, `docs` (both run `make web`) |
| browser e2e | `dashboard` (Chromium; `dashboard-all` for all three) | `dashboard` (all three, serially) | `dashboard` (one parallel job per engine) |
| docs | `docs` | `docs` | `docs` |
| vendored-UI upstream drift | — | — | `untitledui-drift.yml` (its own workflow) |
| Python coverage floor (95) | `coverage`, `validate`, `all` | — | **not in CI** — local only; `release` runs a lower 92 floor via `nox -s tests_all` |
| chaos / stability lanes | `chaos`, `chaos-embedded`, `stability*` | `chaos`, `chaos_embedded` | **not in `ci.yml`** — nightly runs the no-VM legs only |

### The last two rows are the point

**`make coverage` is not in CI.** It is the per-task gate — the full pytest
run at the 95 floor, plus the merged TypeScript floor — and no workflow runs
it. CI gates the *hostless* slice at 90 (`coverage-hostless` / the `tests`
job), because the full run needs lab VMs. A contributor reading only
`.github/workflows/ci.yml` would conclude the 95 floor is enforced on push.

`make release` does not reach it either — and the near miss is worth being
precise about. Release runs `make nox`, whose `tests_all` session carries its
own `--cov-fail-under=92`: a real coverage gate, but a lower one, because
that session excludes the `browser` marker and so omits the dashboard
`--cov-append` fold-in that `coverage-python` gets from its `dashboard`
prerequisite. The 95 floor is reached only through `make coverage`, and
through `make validate` / `make all`, which invoke it via `COVERAGE_TARGET`
(default `coverage-python`; `make ci` overrides it to `coverage-hostless`).
So in practice the 95 floor is enforced by you, before you hand the work
over.

**The chaos and stability lanes are not in CI either.** They are bed-hostile
by construction — they SIGKILL sessions, reboot hosts, and hold a lease on
the shared testbed — so they are excluded from every default gate and from
`ci.yml` entirely. Nightly runs only the legs that need no lab: the
tier-2 real-signal chaos suite over a loopback sshd, a dockerized tier-3
slice, and `stability-unit` across the Python matrix. Everything that needs
a real host — `make chaos`, `stability-unix`, `stability-tunnel`,
`stability-embedded` — runs when a human decides to run it.

## Why this page exists

This repo has run the experiment on itself, and the result is in the
2026-08-03 design review (§3/P5). Gated lines held for five weeks with zero
raises: the import-budget caps (whose snapshots *shrank*), the vendored-UI
content hash, the wire-type diff gate, the warnings-as-errors web build, the
air-gap and brand-token gates. Rules that existed only as prose decayed in
the very same window: "models never imports config" was violated as
literally worded, "open the DB before spawning the task" was replicated as a
comment at four call sites after a five-issue flake wave, and fifty-nine
dangling plan coordinates accumulated in shipped source. The difference was
never how well the rule was written; it was whether anything failed when the
rule was broken.

So this page has one editorial rule: **a gate belongs here; a convention
that is only written down does not.** If a row appears here, something
fails. If a rule matters and cannot appear here, that is a finding — either
build the gate, or write the rule where conventions live
({doc}`principles`) and accept that it will decay.
