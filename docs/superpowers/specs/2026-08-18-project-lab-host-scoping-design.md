# Per-Project Lab & Host Scoping (Fleet of Interest) — Design

**Date:** 2026-08-18
**Status:** Approved design, pre-implementation
**Predecessor:** `2026-08-16-first-party-default-instructions-design.md` (the
project layer this scopes)
**Companion (deferred, own spec):** multi-source lab data —
`todo/lab_flexibility.md`

## 1. Problem

A lab database can define every host an organization owns; a given project
targets a small fraction of them. Today the project layer walks every host in
the loaded lab: owner-scoping keeps the *product* walk correct (a host with no
owned products no-ops without contact), but nothing narrows the *host* walk,
and the host-global actions (debug logs, toolchain, cleanup, `status --full`)
sweep the entire loaded fleet by design. Monitoring likewise selects from the
whole lab.

A project needs to declare, as data:

- **which labs it applies to** — else it silently "applies" everywhere;
- **which hosts are of interest** — the universe within its labs. Any further
  runtime selection (a monitor sweep, a pattern on a walk) picks a **subset**
  of that universe, never a superset.

## 2. Decisions (all confirmed in brainstorm)

| # | Decision |
|---|----------|
| D1 | Declarations are **data in `.otto/settings.toml`** (`[project]` table); code consumes them. `ProjectActions` stays the home of *logic* only. |
| D2 | `lab_patterns` is **required for repos that register product/dev-tool providers**; bootstrap fails loud when missing. Product-less repos need no declaration. |
| D3 | Zero-match: the **current repo** matching no loaded lab (or an empty universe) is an **error at project-layer entry**. A **dependency** matching zero labs is **excluded loudly** — skipped by walks, one prominent log line, shown by `status` as not-applicable, not counted in the tri-state fold. |
| D4 | Multi-source lab data is a **separate workstream**; this design composes with it via a source-agnostic ingest stamp. |
| D5 | Walk shape (d): the universe is **ambient in the context**, not a per-call argument. `pattern=` remains the one explicit selection knob. |
| D6 | **`re.fullmatch` everywhere** — declarations *and* every runtime host/lab pattern, including user-facing `--hosts` flags. A runtime pattern fullmatching zero universe hosts **fails loud**. |
| D7 | Per-repo host regexes are **OR-ed** into that repo's universe; the fleet for host-global operations is the **union** of per-repo universes. |

## 3. Settings surface

```toml
[project]
lab_patterns  = ["tech-.*", "bench1"]     # required if the repo registers providers
host_patterns = ["sensor-.*", "gw-\\d+"]  # optional; default [".*"]
```

- New pydantic spec fields (settings changes require spec fields).
- Regexes compile at settings parse; an invalid one fails bootstrap naming the
  repo, the pattern, and the `re.error`.
- The required-if-products check runs at bootstrap **phase 2**, after init
  imports have registered providers: providers present and no `lab_patterns`
  → `BootstrapError` whose message contains the exact TOML to add, including
  that `lab_patterns = [".*"]` restores the old reach **explicitly** — the
  point of D2 is that match-all becomes a visible choice, never a default.
- Semantics: a repo applies to a lab when **any** `lab_patterns` entry
  fullmatches the lab's component name; a host is in the universe when its
  lab is applicable **and any** `host_patterns` entry fullmatches its id.
- **`otto init` template:** the scaffolded `settings.toml` gains a
  `[project]` block — **commented out** (`#[project]` / `#lab_patterns` /
  `#host_patterns`), matching the `#[coverage]` precedent, since a fresh
  scaffold registers no providers and the block is therefore optional for it.
  The prose above it states the D2 rule: the moment the repo registers a
  product/dev-tool provider, `lab_patterns` becomes required. The existing
  template drift guard (`tests/unit/cli/test_init_templates.py`, which holds
  the template to `SettingsModel` exactly) makes omitting this update a CI
  failure, not a review catch — extend its `_SECTION_SPECS` with the new
  spec class as part of the same task.

## 4. Lab attribution at ingest

- Every host gains loader-assigned **`source_lab: str`** — stamped by
  `load_lab(name)` on whatever the repository backend returns. Runtime
  attribution, **not** a `lab.json` field; source-agnostic so multi-source
  (D4) inherits it unchanged. The built-in `local` host and post-load
  containers are stamped with the lab they join.
- `Lab` records **`component_names: list[str]`** at load; `__add__`
  concatenates, so both per-host attribution and the component-name set
  survive `a+b` merges (which today collapse names and re-point `host._lab`).

## 5. One predicate, one resolver

```python
def repo_targets(scope: ProjectScopeConfig, lab_name: str, host_id: str) -> bool:
    """any lab_pattern fullmatches lab_name AND any host_pattern fullmatches host_id."""
```

Pure, single module, imported by every consumer — ingest gating, fleet
iteration, and the resolver — so agreement is structural, not disciplinary.

The **resolver** runs at context creation (cheap, pure, no I/O): per repo it
computes applicable lab names, the universe, and an `excluded` flag; it also
records the union. Zero-match consequences (D3) are **stored, not raised** at
context creation: the error fires at project-layer entry (default
instructions, `ensure_*` fixtures, monitor fleet build) so a scoping typo
never bricks explicit `otto host <id> <verb>` targeting. Resolution output is
display data (`status --full`) and the abort/skip decision; fleet iteration
itself re-evaluates the live predicate (§6), so hosts that join after context
creation (docker containers) are scoped correctly rather than frozen out.

## 6. Ambient universe (shape d)

`all_hosts()` / `do_for_all_hosts()` change their **base iteration set**:

- Through a **repo-scoped context view** (§7): that repo's universe.
- Through the plain context: the **union** of applicable repos' universes.
- Fallback: when **no** repo in the resolved set declares `[project]`, the
  whole loaded lab — today's behavior, so product-less projects and existing
  tests are untouched.

`pattern=` is applied **within** that base set, by `re.fullmatch` (D6). A
pattern that fullmatches zero hosts of the base set raises a loud error
naming the pattern, the universe size, and a `.*`-suffix hint — a silently
empty selection is the one failure mode worse than a crash. `include_local`
and `include_containers` keep their existing meanings, applied after scoping.

Deliberately **unscoped** surfaces (explicit targeting beats scoping):
`get_host("id")`, `otto host <id> <verb>`, and host-id shell completion for
`otto host`. An `include_unscoped=True` escape hatch for admin sweeps is
**deferred** until a concrete need appears.

Observability: context creation logs one line —
`fleet of interest: 6 of 214 lab hosts (2 repos, 1 excluded)` — and
`status --full` lists each repo's applicable labs and universe.

## 7. Repo-scoped context view

`ctx.for_repo(repo)` returns a thin facade over the same context (no copy):

- fleet iteration bounded to that repo's universe (§6);
- `owner=repo.name` **auto-supplied** to owner-accepting host verbs
  dispatched through its walks — host-set scoping and product-set scoping
  come from *which object* the call goes through, never from arguments a
  call site must remember. Owner-less verbs (e.g. `host.cleanup`) remain
  dispatchable through the view untouched; the injection mechanics are the
  implementation plan's concern, with that constraint stated here.

`actions_for(repo)` constructs each `ProjectActions` with this view, so a
repo's `install()` body is `self.ctx.do_for_all_hosts(_dispatch_install)` —
no `owner=`, no pattern plumbing. Everything else on the view delegates
unchanged (`get_host`, links, options).

The orchestrator's two-level structure is unchanged: level 1 iterates every
applicable repo in dependency order (reverse for uninstall/cleanup) exactly
as today, skipping excluded repos loudly; level 2 — each repo's fleet walk —
is where the universe binds.

## 8. Consumers

- **Provider gating:** `apply_product_providers` invokes a provider only when
  `repo_targets(...)` passes for that host — a product can never attach
  outside its repo's universe, so owner-scoping and universe-scoping agree by
  construction.
- **Host-global walks** (debug logs, toolchain install/remove, cleanup's
  netem `repair_all` + tunnel reap, `is_clean`, `status --full` cleanliness):
  the **union**, via the plain context. `is_clean` checks exactly the set
  `cleanup` sweeps — mutator/predicate agreement is non-negotiable. Accepted
  boundary: a declared link whose endpoints straddle the union gets its
  in-union side repaired only.
- **Monitor:** fleet = ambient union ∩ `--hosts` fullmatch. Subset semantics
  fall out of §6 with no monitor-specific code beyond the fullmatch migration.
- **Fixtures** (`ensure_installed` / `ensure_uninstalled` / `ensure_clean`):
  unchanged code path — they call the orchestrator, which is scoped above.

## 9. Fullmatch migration (user-facing, deliberate break)

`monitor --hosts` moves from `search` to `fullmatch`.
`sensor` no longer matches `sensor-1`; write `sensor.*`. Made safe by the
zero-match guard (§6): the first stale invocation fails loudly with the hint
rather than silently operating on nothing. Release-note line required. All
host/lab regex surfaces added by this design are fullmatch from birth; an
audit task enumerates any remaining `search`-semantics host selectors and
migrates them in the same commit.

**Correction (Task 12, post-implementation).** This section originally paired
`tunnel add --hosts` with `monitor --hosts` as a second `search`→`fullmatch`
migration. That was wrong when it was written: `tunnel add --hosts` is an
ordered comma-separated list of explicit `host[@iface]` entries resolved by
dict lookup (`_parse_hosts` → `lab.hosts.get()`), and has never been a regex on
either semantics. There was nothing to migrate and nothing migrated; the tunnel
CLI is untouched by this design. The audit that closed D6 verdicted every
`.search(`/`.match(` in `src/otto` and found the only runtime host selector to
be `OttoContext.all_hosts` (fullmatch since Task 6).

## 10. Failure modes

| Condition | When | Behavior |
|---|---|---|
| Invalid regex in `[project]` | bootstrap | `BootstrapError`: repo, pattern, `re.error` |
| Providers registered, no `lab_patterns` | bootstrap phase 2 | `BootstrapError` with exact TOML to add (D2) |
| Current repo: zero applicable labs, or empty universe | project-layer entry | error listing loaded component labs vs patterns (D3) |
| Dependency: zero applicable labs | resolution | excluded; loud log; `status` row "not applicable"; walks skip (D3) |
| Runtime `pattern=` matches zero universe hosts | the call | loud error: pattern, universe size, `.*` hint (D6) |
| Effective fleet empty at a fleet surface (all contributing repos excluded) | the call | same loud error as current-repo empty universe (D3) |
| No repo declares `[project]` | resolution | whole-lab fallback, logged once (§6) |

**Correction (final review, post-implementation).** The last row's "logged
once" did not ship, deliberately. The observability line is emitted only when
some repo actually declared a `[project]` table (`OttoContext._resolve_scopes`,
pinned by `test_undeclared_run_says_nothing`): the whole-lab fallback is the
behavior every pre-`[project]` project already had, so a line announcing it on
every undeclared run is a line nobody reads on the run that mattered. The
fallback itself is unchanged — only its announcement was dropped. Left in the
table and corrected here rather than edited away, so a reader who finds the
claim knows it was retracted.

**Correction (final review, post-implementation).** The dependency row above
names only "zero applicable labs". The skip shipped keyed on the WHOLE D3
condition — declared, and either no loaded lab applies OR no host in the
applicable ones matches `host_patterns` — because the second shape has an empty
fleet exactly as the first does: its owner-bound walk is refused by
`require_nonempty_fleet`, so admitting it raises out of the dependency's own hop
and takes the driving project's verb down with it (including `cleanup`, whose
every-step-runs contract then strands the tunnel reap). Skipping only the
`excluded` shape was the letter of this row and defeated its purpose, which is
D3's asymmetry: one project's declaration must not veto another's run. The
`status` row and the skip warning split per condition, since "load a different
lab" and "widen `host_patterns`" are different fixes.

## 11. Testing

- **Predicate:** pure-function tests — fullmatch (not search: `bench` must
  not match `bench-overflow`), OR-ing, invalid-regex compile failure.
- **Attribution:** `source_lab` and `component_names` survive `a+b` merges;
  post-load containers stamped.
- **Resolver matrix:** current-abort / dep-exclude / fallback, each proven by
  mutating the condition and observing the named error or skip.
- **Ambient walks:** fakes that *record contact*; assert the contacted set
  **equals** the universe — counting contacts, not inspecting results, so a
  wrongly-widened walk reds even when out-of-universe hosts would succeed.
- **View:** `for_repo` auto-owner proven by a host-class fake that records
  the `owner=` it receives; plain-context walk = union; fallback = whole lab.
- **Provider gate:** a provider returning a product for an out-of-universe
  host attaches nothing — asserted on the host's product list, not the
  provider's return.
- **Zero-match guard:** a pattern matching nothing raises; the message names
  the pattern (mutation: guard deleted → silent empty walk must red a test).
- **Agreement:** `is_clean` and `cleanup` walk the same set — one test drives
  both against a fake fleet and diffs the contacted sets.
- **Migration audit:** every `--hosts` surface fullmatches (`sensor` selects
  nothing where `sensor-1` exists; `sensor.*` selects it).
- Every guard mutation-proven red per house discipline.

## 12. Documentation

`guide/run/defaults.md` (universe concept, scoped walks),
`guide/hosts/capabilities.md` (provider gating), `guide/setup/lab-config.md`
(`[project]` table, migration note incl. the explicit `[".*"]` escape),
monitor/tunnel CLI reference (fullmatch + zero-match guard), settings
reference (new spec fields), `otto init` scaffold + its template drift guard
(§3), release notes (D2 requirement + fullmatch break).

## 13. Deferred

- **Multi-source lab data** (`todo/lab_flexibility.md`) — own brainstorm/spec;
  composes via the source-agnostic `source_lab` stamp and post-ingest
  resolution.
- **Attribute-based patterns** (element/os_type/role) — regexes are id-only;
  attribute logic already has the provider seam.
- **`include_unscoped=True`** admin escape hatch — until a concrete need.
- **Per-verb winnowing parameters** on project actions (install to a subset
  of the universe) — `pattern=` on the walk already exists for library users;
  CLI exposure deferred.
