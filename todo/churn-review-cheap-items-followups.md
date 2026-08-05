# Follow-ups from the churn-review "cheap items" wave (2026-08-04)

Surfaced by the per-item opus reviews. Each is out of scope for the cheap item
that found it — recorded here rather than folded in, so the scope of each
squash stays one thing.

## STATUS

Addressed so far: the whole docker section; the whole covapp cross-language
inventory; 3 of the 4 completion items (`python_files`, the dot-dir walk, the
directory match); 2 of the 5 cli items (the `@cli_command` hole, the
sugar-vs-seam bypass); and the impairability half of the link section. New
items from the follow-up commits' own reviews are at the end.


## From `fix(completion): hash every test source the --tests scan can read` — 3 of 4 DONE

- **`Repo.iter_test_files` is a third, narrower reader of the same tests dirs.**
  `config/repo.py:716` still does a non-recursive `glob("test_*.py")`, so a
  `Test*` OttoSuite defined in `tests/unit/test_foo.py` or in `foo_test.py` is
  never registered in `SUITES`. Pre-existing, and NOT a glob fix: that reader
  *imports* what it returns, so widening it changes which user modules otto
  execs at bootstrap. Needs a decision, and a test that a nested suite becomes
  runnable, before anything moves.

- **A repo that overrides pytest's `python_files` still goes stale.** Neither
  `collect_test_names` nor `compute_fingerprint` knows about a `python_files`
  setting, and no pytest config file (`pytest.ini`, `pyproject.toml`,
  `tox.ini`, `setup.cfg`) is hashed. Verified live: with
  `python_files = check_*.py test_*.py`, pytest collects
  `check_alt.py::test_alt_pattern` and editing that file never moves the
  digest — the same bug this commit fixed, one config line away. Closing it
  means reading the repo's pytest config on the completion fast path.

- **The walk descends into dot-directories.** `rglob` yields `.tox/`,
  `.venv/`, `.git/` contents where pytest's `norecursedirs` would not.
  Harmless today (otto's own tests/ has none) but it is the pathological cost
  case: a venv tree measured 83 ms warm, on a path that runs twice per TAB.
  Excluding them means a manual walk instead of `rglob`.

- **`_test_sources` can yield a directory.** A directory literally named
  `test_x.py` is matched, and `_hash_file` folds its mtime in, so unrelated
  writes inside it move the digest. No crash on either side (the scan's
  `read_text` raises `IsADirectoryError` ⊂ `OSError`, already caught). Cosmetic
  — costs a `stat` per candidate to filter.

## From `fix(cli)!: an instruction must be async def` — 2 of 5 DONE

- **DONE** — **`@cli_command` has the same hole and is not gated.** A sync
  `@cli_command` that calls `ctx.all_hosts()` registers every host into a
  scope that is never entered, so nothing sweeps them — the identical silent
  failure `@instruction` now rejects, and the guide's own canonical example
  (`docs/guide/extending-cli.md`) is a lab-touching `ping`. The line that
  actually carries the weight is not "instruction vs cli_command" but
  `lab_free`: `@cli_command(lab_free=True)` is a defensible sync exemption,
  a lab-bound one is not. Documented as a caveat for now; gating it needs a
  sweep of in-tree sync leaves first.

- **DONE** — **The invariant is enforced at the sugar, not the seam.**
  `INSTRUCTIONS.register(InstructionEntry(...))` and `@run_app.command()` both
  reach `otto run` without passing the check. Airtight enforcement would live
  in `InstructionEntry.__post_init__` or `make_registry_group.get_command`.

- **DONE** — **`raise TypeError` vs the OttoError convention.** Resolved the
  prose way, not the rule way: the real count is **330** bare stdlib raise
  sites, not ~40 (179 `ValueError`, 63 `RuntimeError`, 42
  `NotImplementedError`, 16 `TypeError`, …), so a raise-site rule is a
  workstream and a count ratchet would be a metric target. `errors.py`,
  `docs/library/index.md`, `docs/architecture/principles.md` and
  `docs/architecture/utilities/results.md` now say DEFINES and spell out
  exactly what each catch clause does and does not reach — 284/330 sites and
  15/24 named classes for `except (ValueError, RuntimeError)`, all but five
  raises for `except Exception`. The sweep also grew three missing halves
  (declaration completeness, aliased bases, vacuous `Exception` rows).

- **DONE** — **A `!` in a conventional-commit type is inert in the changelog.**
  Taught cliff the marker (both spellings — `type(scope)!:` and a
  `BREAKING CHANGE:` footer), plus the scope, which was also being dropped.
  The footer's TEXT is deliberately not rendered — see the section below.
  `CHANGELOG.md` regenerated so the reformat lands in a reviewed commit
  instead of inside a release. Pinned by
  `tests/unit/test_changelog_rendering.py`, which drives the real renderer
  over a synthetic repo, and now also pins every type→section mapping and
  every dropped type.

- **DONE** — **`SupportsHostSummaries` conformance checks ids, not completeness.**
  `testing/conformance.py`'s `_expect_host_summaries_conform` only asserts the
  summarized ids are a subset of `load_lab`'s. Any field a completer starts
  depending on (the `hop` idea explored and dropped in the link-completion
  work would have been the first) can be silently absent from a third-party
  backend with the conformance suite still green.

- **DONE (the timeout; the fallback cost stands)** — **`repo_host_summaries` has no timeout.** It catches every exception, so a
  custom backend that FAILS is contained — but one that HANGS hangs the TAB.
  Measured fallback cost for a non-`SupportsHostSummaries` backend is
  O(labs × hosts) host constructions (~18 ms for 200 hosts in one lab), since
  the fallback loads every lab.

## From `fix(completion): scope link completion to the lab` — 1 of 2 DONE

- ~~**Implicit links are unimpairable, and nothing says so where it would be
  read.**~~ DONE — `impairment_refusal` in `link/placement.py`, carried on
  `LinkState.refusal` and printed by `otto link list`.

- **A declared link between two interface-less hosts is offered but not
  impairable.** CONFIRMED reachable, not just theoretical: `_resolve_endpoint`
  leaves `interface=None` when a host declares no `interfaces` map, and such a
  link loads, resolves through `find_link`, is offered by `otto link impair
  <TAB>`, and can never be impaired. `collect_link_ids`' `provenance !=
  IMPLICIT` filter is a proxy for impairability with exactly this hole.

  I attempted the obvious fix inside the F7 squash — resolve each raw entry
  through the real loader (`resolve_declared_links` + `addressing_from_dict`)
  and filter on `impairment_refusal` — and BACKED IT OUT. It works, and its
  review proved four regressions that make it its own item, not a rider:

  1. **Completion goes dark for the whole repo** when an `os_profile` is
     registered by an `[init]` module. `addressing_from_dict` resolves through
     the profile registry, and the completion path deliberately runs WITHOUT
     `bootstrap()` (`completion_cache.py:1170` says so). Every host record
     then raises, is suppressed, and every link is skipped — silently.
     `otto host <TAB>` survives this because host ids are cached; link ids
     have no cache entry and are computed live on every TAB.
  2. **Cross-REPO links are dropped.** `cli/invoke.py` aggregates every
     repo's `labs` into ONE `JsonFileLabRepository`, so a link declared in
     repo A between a host in A and a host in B resolves. Per-repo addressing
     cannot see that.
  3. **One-sided links are dropped.** `endpoint_placements` refuses per
     direction, so a link between an interfaced host and a bare one is dead
     for the default but alive under `impair --from <interfaced end>`.
     (`impairment_refusal` now takes `directions` for exactly this; a filter
     would have to ask about each direction separately, not about the link.)
  4. **Duplicate host id across lab files resolves the wrong way.**
     `json_repository` keeps the FIRST record and warns; a dict built by
     iteration keeps the LAST. That re-opens this very hole in the
     over-offering direction.

  So the real fix is the repository seam for links that the original note
  called for — one place that resolves links the way the loader does, usable
  by both dispatch and completion — not a second resolver in the completer.
  Until then the completer must stay profile-independent.

## From `test(cov): gate the data-format version both languages hand-mirror` — DONE

An inventory of what WAS still hand-mirrored between `src/otto/coverage/**`
and `web/src/covapp/**`, ranked. All of it is now pinned by the shared
contract, along with four more its own review found (`Tester`, `BranchJson`,
`Stats.flags`, and `LineJson.state`'s value domain).

1. **`CoverageState`** — `types.ts`'s `"uncovered"|"excluded"|"stale"|"aging"`
   vs `STATE_COLORS`'s keys in `coverage/colors.py`. A CLOSED set, mirrored by
   hand, and `types.ts` says so in a comment. `test_colors.py` only validates
   the colour VALUES. Adding a fifth state Python-side leaves the TS
   `Record<CoverageState, string>` silently short. Best candidate for the next
   contract row.
2. **`IndexPayload`'s 18 top-level keys** — only the ticket sub-payloads are
   pinned, so adding or renaming a top-level key is undetected.
3. **`FileChunk` / `LineJson` keys** — unpinned, and `LineJson.state` is a
   second copy of the state vocabulary from (1).
4. **`RunJson` / `OverrideJson` / `RunContrib` / `Stats` / `DirNode` /
   `FileNode`** — unpinned.
5. **The `cov_data/` path layout** — `covapp.html` and `data.ts` hard-code
   `./cov_data/index.js` / `files/` / `tickets/` against `spa_data.py`'s
   emitter. Caught only by the browser lane driving a real report.

Also: the TS half asserts `chunk_callbacks.file` and `.ticket` are installed
on `window` but never `.index` (`__OTTO_COV__`), which `data.ts` reads as a
hard-coded property. Pre-existing.

Not mirrored, correctly out of scope: `STORE_FORMAT_VERSION`,
`CAPTURE_FORMAT_VERSION`, `TICKET_EXPORT_FORMAT` (no TS references at all).
The monitor side is covered by a stronger mechanism — `types.gen.ts` codegen
plus a `git diff --exit-code` drift gate — except `stream.ts`'s `ARRAY_FIELDS`
string literals.

## From `fix(docker): a cached image whose :latest cannot be re-pointed` — DONE

The same silent-failure family, elsewhere in `otto/docker/**`. All
pre-existing; none is deliberate unless noted.

- **`compose.py`'s `docker compose config --services` is guarded by
  `if live.is_ok:` with no else and no log.** A repo declaring only
  `path` + `default_host` has no `services`, so on failure
  `declared_services or sorted(live_services)` is `[]`, the registration loop
  never runs, and `compose_up` returns `{}`. `otto docker up` prints
  "0 container(s) registered" and exits 0 — a failed stack reported as
  success. The worst of the set.
- **`_stack_already_up` folds a failed `docker ps` into `False`.** A transient
  blip during `composed()` makes `was_up` False, so the `finally` tears down a
  stack an outer fixture is holding — the exact thing the "don't yank the
  stack from peers" comment promises not to do.
- **`staging.py`'s `rm -rf … && mkdir -p …` results are discarded** (build
  tree and compose tree). Build staging is keyed on `repo.name` while compose
  staging is deliberately keyed on the suffix-bearing project name, so two
  users on one parent DO collide on the build path. A partial `rm -rf`
  short-circuits the `mkdir`, and the later `tar -xf` overlays rather than
  replaces — `docker build` then sees a context still holding a file the user
  deleted, producing a wrong image under a hash that says it is right.
- **`compose_ps` returns `[]` on a failed `docker ps`** — a host whose daemon
  is down renders identically to one with no containers.
- `cleanup_project`'s discard IS deliberate and documented, and the chaos
  lane's hygiene bracket catches residue independently.
- `_image_exists` folding any failure into "not cached" is the fail-safe
  direction (rebuild is correct-but-slow) but is undocumented.

## Cross-cutting

- **`typer.main.get_command_name` does not strip leading dashes.** A function
  named `_foo` derives the command name `-foo`, not `foo`. Harmless for real
  instructions; it silently makes some name-derivation assertions in
  `tests/unit/cli/test_run.py` (~199, ~211) vacuous.

## From `fix(docker): stop absorbing exec failures into empty successes`

- **`otto docker up` has no `any_failed` accumulator.** `_build` and `_down`
  in `cli/docker.py` sweep every selected repo and report at the end; `_up`
  raises out of the first failing repo, so repos 2..n are neither brought up
  nor reported. Three new raises in `compose_up` make that reachable far more
  often than before. Give `_up` the same shape as its siblings.

- **`host/docker_host.py` runs the same absorbed queries, one module over.**
  It issues the same `docker ps -q --filter label=...` that `compose.py` now
  reports honestly, and still folds a failure into `""` — so `is_running()`
  returns False for a host whose daemon merely hiccuped, and tunnel discovery
  silently skips it. Its `_ensure_running` recovers loudly, so the damage is
  confined to the read-only paths. It also discards two staging `rm -rf`
  results, the same shape fixed in `docker/staging.py`.

## From `fix(completion): honour a repo's pytest python_files`

- **`python_classes` / `python_functions` are still hardcoded.** The AST scan
  matches `Test*` classes and `test*` functions literally, so a repo that
  overrides either collects names the completer cannot see — the same shape
  as the `python_files` gap just closed, one config key over. Harder than
  `python_files` was: pytest matches those two by prefix OR glob depending on
  whether the pattern contains glob characters.

- **otto does not replicate pytest's rootdir/inifile discovery.** Only the SUT
  root is consulted, so a config living elsewhere (a monorepo whose pytest.ini
  sits above the SUT) falls back to the defaults. That IS the safe direction,
  but it is a divergence worth knowing about.

- **pytest FOLLOWS symlinked directories; both completion readers do not.** A
  tests tree assembled by symlink is neither offered nor hashed, so its names
  go stale for a full TTL. Pre-existing (`rglob` did not follow them either),
  but the walk now claims to walk like pytest, so it is a named divergence
  rather than an accident.

- **A repo that NARROWS `norecursedirs` is pruned too aggressively.** Setting
  `norecursedirs = .*` makes pytest collect from `tests/build/`, which these
  readers now skip — completer blind and digest blind, the same shape as the
  `python_files` gap. One config key over, same fix shape.

## From `fix(cli): a lab-bound command must be async, at the decorator AND the seam`

A sweep of the whole dispatch tree (21 leaves) while scoping that change found
the sync lab-bound leaves otto has ON PURPOSE, which is why the rule is scoped
to `otto run` and `@cli_command` rather than applied to every leaf:

- **`otto test <Suite>` leaves are sync**, because `pytest.main` is. The suite
  run therefore does NOT go through `run_command` — that is Tier 0.4 of the
  churn review ("the longest, most host-holding phase has no two-stage
  interrupt policy"), still open, and the reason a blanket seam rule would
  have had to be lied to.
- **`otto cov report/get/clean` are sync and self-bridge**, each calling
  `run_command` in its own body. Converting them to `async def` and letting
  the leaf wrapper drive them would finish the lifecycle migration for the
  last three first-party leaves; it is a real behaviour change to three
  commands, so it wants its own item.

- **`register_cli_command(name, sync_func)` is still ungated**, and so is any
  sync leaf on a `typer.Typer` app loader — the symmetric hole to
  `INSTRUCTIONS.register`, which IS now covered. Closing it means the same
  `async_leaves` flag on more registrations, but first it needs the same
  in-tree sweep: `otto test` and `otto cov` are exactly those shapes.

- **`lab_free` is not the axis it reads as.** It means "otto will not load a
  lab, open a session, or run the gate for you" — not "touches no hosts".
  `otto monitor` is `lab_free=True`, sync, and calls `all_hosts()` itself. So
  the `@cli_command` exemption really means "I drive the lifecycle myself",
  and a third-party command that declares `lab_free=True` without doing so is
  waved through. A real host-touching axis would need the context to record
  whether a scope was ever entered.

## From `fix(cli): render every user-facing error through one escaping helper`

- **The gate is red-only.** `[green]`, `[yellow]` and `[dim]` renders are
  untouched, and today they carry otto's own literals — but a `[yellow]`
  warning built from a host id has the identical hazard and nothing catches
  it. Widening means deciding whether every coloured f-string in the tree
  must escape, which is a bigger call than this commit's.

- **Rich Table CELLS parse markup too.** `cli/init.py`'s validation table puts
  `"\n".join(problems)` — foreign text — straight into `add_row`. Same class,
  invisible to a rule that matches on a red f-string.

- **`otto.console.CONSOLE` would silently disable the gate.** The rule binds
  to the f-string, so it survives a `CONSOLE.print` switch — but a message
  assembled into a variable first, or built with `+`, escapes it. The rule
  catches the shapes that exist, not every shape possible.

- **Ten more inline `escape()` calls already existed** in `coverage/` and
  `suite/run.py`, reached independently. They are correct but they are a
  second mechanism; folding them onto `print_error` (or a non-CLI sibling)
  would leave one.

- **`typer.echo` sites are a separate, safe dialect.** `cli/monitor.py` prints
  a pydantic ValidationError through `typer.echo` (no markup, so the
  `[type=missing, ...]` detail survives). That is correct today but it is a
  second way of saying "this failed", with different colouring and a different
  stream — the error-dialect unification (review Tier 2.3) still has work
  left after this commit.

## From `fix(labs): a host summary must agree with the host it summarizes`

- **The non-`SupportsHostSummaries` fallback is still O(labs × hosts) host
  CONSTRUCTIONS.** `host_summaries` loads every lab when a backend does not
  implement the capability (~18 ms for 200 hosts in one lab, multiplied by
  lab count). The new deadline bounds the damage but does not remove it; the
  fix is to make the capability easier to implement than to skip, which the
  strengthened conformance rules now at least make honest about what it costs.

- **The deadline is per-repo, not per-invocation.** Five repos each with a
  stalled backend cost 5 × the deadline before completion gives up. Fine at
  the current 2s for realistic workspaces; worth revisiting if the value grows.

## From `feat(link): say why a link cannot be impaired`

- **`repair_link` can never repair a one-sided link it just impaired.**
  `impair --from <interfaced end>` succeeds on a link whose other endpoint has
  no named interface, but `repair_link` asks `_directions(link, None)` = both
  and so hits the refusal; `repair_all` skips it via `except ValueError`. The
  impairment is then only clearable by hand. Pre-existing, surfaced by making
  the direction rule explicit. Fix is probably for `repair` to repair whatever
  directions are actually placeable rather than demanding both.

- **`inpath_placements` has a structural refusal the predicate cannot see.**
  `_facing_netdev` rejects an endpoint with an empty `ip`, and that IS
  reachable: `host_identity` validates the PROFILE-MERGED dict while
  `addressing_from_dict` reads `host_data.get("ip", "")` from the RAW one, so
  a host whose `ip` comes from its `os_profile` defaults validates fine and
  yields `HostAddressing(ip="")` → `LinkEndpoint(ip="")`. Contrived (the
  profile would have to give every host the same ip) but not impossible, and
  `impairment_refusal` would call such an in-path link impairable while the
  scan refuses it. The raw-vs-merged split is the interesting half.

## From `docs(errors): the OttoError convention says what it means`

- **The RAISES convention is gated at class-definition level only.** A public
  function raising a bare `ValueError` is still ordinary, so `except OttoError`
  covers otto's named failures and nothing else. Making it cover every raise
  means naming 330 sites, most of which are argument validation where a bare
  `ValueError` is the right answer. If it is ever wanted, the tractable
  subset is the ~63 `RuntimeError` sites (live/operational failures, where a
  caller most plausibly wants "was that otto?") rather than the 179
  `ValueError` ones.

## From `fix(changelog): a breaking change reaches the changelog`

- **★ The `BREAKING CHANGE:` footer text still does not reach any reader.**
  I rendered it and backed it out: git-cliff's `commit.breaking_description`
  is not the footer. It stops at the first continuation line shaped like
  `Token: value` and ignores every footer after the first. On otto's own
  history that truncated `e4b18336` mid-sentence (`...is now lab.json with
  the shape`, next line `{"hosts": [...]}`) and dropped two of `ad0edab3`'s
  three migration notes — `repo_dir keyword` and `load_tiers` appear nowhere
  in the rendered file. It also OVER-includes in the other direction, since
  the description runs to the next footer token and swallowed a
  test-methodology paragraph into `0ef6c0d5`'s bullet.

  A correct version has to parse `commit.body` itself: take everything from
  the first `BREAKING CHANGE:` to the end, keep the paragraph breaks. That
  needs `trim = false` plus explicit `{%-`/`-%}` control throughout the
  template (with `trim = true`, an indented continuation renders at column 0).
  Worth doing — the footer is the only place a migration instruction is
  written — but it is a template rewrite, not a clause.

  Note `.github/workflows/release.yml` generates the GitHub Release notes
  with the same `cliff.toml` via `git-cliff-action`, so whatever lands here
  lands there too.

- **The footer template fails outright on a repo with no tags.** It reads
  `releases[0].previous.version` to build the Unreleased compare link, and
  git-cliff aborts the whole render — not just the footer — when nothing is
  tagged. Harmless for otto (it has tags) but it is why the rendering test has
  to seed a `v0.1.0`, and it would bite anyone lifting `cliff.toml` as a
  template. A `{% if %}` guard would cost one line.

- **DONE** — **prose predating the generated changelog.** Both stale sites
  fixed: `docs/contributing.md` (PR checklist asking for an `## [Unreleased]`
  entry that `make changelog` erases, and crediting bump-my-version with
  promoting it) and `.github/pull_request_template.md`, which carried the same
  instruction and is the copy that auto-populates every PR. Swept the rest —
  `docs/release_process.md`, `pyproject.toml` and the spec files were already
  correct.

## Observed flake (not caused by this wave)

- **`tests/unit/test_lifecycle_sync_phase.py::test_second_signal_forces_immediately`**
  failed once during `make coverage` on 2026-08-05 with
  `assert 'FORCE-HOOK' in 'PHASE-EXITED\n'`, and passed on a re-run plus five
  targeted runs. It spawns a child and races two SIGINTs: the second has to
  land while teardown is still running, and under a loaded `make coverage`
  (xdist + the browser lane) the teardown can finish first. Verified unrelated
  to the commit it appeared under — the child imports only `otto.lifecycle`,
  and the change touched `completion_cache` / `conformance` — but the test is
  timing-dependent by construction and will recur. A deterministic fix would
  have the child block teardown on a marker the parent releases, rather than
  relying on the second signal winning a race.
