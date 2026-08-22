# otto CLI docs tree — the User Guide mirrors the command tree

**Date:** 2026-08-21
**Status:** Approved design, pending implementation plan

## Goal

The User Guide is currently a mix of CLI verbs and otto concepts, grouped by
functional area. A reader who has just run `otto <cmd> --help` cannot predict
where its page lives, and three sections are named for concepts rather than the
commands they document (`Project setup` = `otto init`, `Working with hosts` =
`otto host`, `Links & tunnels` = `otto link` + `otto tunnel`).

This restructure makes the User Guide's CLI section a literal mirror of otto's
command tree, and applies one triage rule to everything it touches:

> **Does the information serve the CLI user?** If yes, it stays in the CLI doc
> tree. If it serves an API user, it belongs in `library/` (or the docstring).
> If it is test methodology or design rationale, it belongs in `architecture/`.

The bulk of any topic is stated in exactly one place; every other mention is a
link.

This supersedes the User Guide half of
`2026-07-16-docs-toctree-restructure-design.md`. Its editorial rules (single
source of truth; behavior vs. mechanism; thin landing pages; docs capture the
current state) still apply unchanged. Its **extensibility placement** rule —
"users discover seams where they work" — is deliberately reversed: seam
authoring is Python work and moves to `library/`, with the CLI page carrying a
pointer.

## Decisions

1. **The User Guide has two children:** the CLI tree, and a configuration
   sibling. `lab.json` and `settings.toml` are read by *every* command, not
   just the one that scaffolds them, so filing them under `otto init` would be
   a category error.
2. **Strict mirror, plus topic subpages.** Every static subcommand gets its own
   page regardless of size. Verbs with no subcommands may still split by topic.
   The nav is a superset of the command tree, never a subset of it.
3. **`cli-reference.md` is dissolved.** Global options, completion and output
   directories become the CLI overview; each per-command section merges into
   that command's page. `otto <cmd> --help` is the flat lookup surface.
4. **Python authoring moves out.** Each command page documents what you need to
   *invoke* the command plus a pointer; the full authoring/extension treatment
   moves to `library/`.
5. **Full triage.** Big mixed pages are re-cut, not merely relocated —
   including `monitor` (1160 lines) and `coverage` (1291 lines).
6. **`otto --help` is reordered to match the docs.** Cosmetic; lands separately
   on main before the docs work begins (see Phase 0).

## The naming rule: verbs vs. topics

A tree that mixes `otto cov get` (a subcommand) with "Coverage tiers" (a topic)
is unreadable if both are rendered the same way. Two mechanisms carry the
distinction:

- **Title convention.** A page whose title is a literal command
  (`otto host run`) documents a command. A page whose title is prose ("Netcat
  transfers") documents a topic. No exceptions, in either direction.
- **Caption split.** Every index page splits its children into a
  `Subcommands` toctree and a `Topics` toctree. `sphinx_immaterial` renders
  toctree captions as nav group headers (`architecture/index.rst` already uses
  five), so the two kinds are visually separated in the sidebar.

A useful consequence: `otto monitor` renders with a `Topics` group and **no**
`Subcommands` group, so a reader can see at a glance that it takes none.

Two riders:

- `otto host` pages are titled `otto host run`, not `otto host <host_id> run`.
  The index explains that the host id sits between the group and the verb.
- Capability verbs (`power`, `reboot`, `ls`, …) are dynamic and scoped per host
  class, so they are grouped into seven prose-titled family pages rather than
  one page per verb. They are topics, and titled as such.

## Target tree

```text
User Guide                                          guide/index.rst
├─ The otto CLI                                     cli/index
│  │  Topics
│  │   └─ Dry runs                                  cli/dry-run
│  │  Commands
│  ├─ otto init                                     cli/init            (leaf)
│  ├─ otto host                                     cli/host/index
│  │   │ Subcommands
│  │   ├─ otto host run / put / get / login         cli/host/{run,put,get,login}
│  │   │ Topics
│  │   ├─ Connection control                        cli/host/connections
│  │   ├─ Netcat transfers                          cli/host/netcat
│  │   ├─ Embedded hosts                            cli/host/embedded
│  │   └─ Host capabilities                         cli/host/capabilities/index
│  │       │ Topics
│  │       └─ Power & reboot · Products & lifecycle · Dev tools & toolchain ·
│  │          Remote file operations · Kernel modules · Userland capabilities ·
│  │          Privilege elevation
│  ├─ otto run                                      cli/run/index
│  │   └─ Topics: Default lab actions               cli/run/defaults
│  ├─ otto test                                     cli/test/index
│  │   └─ Topics: Selection runs                    cli/test/selection
│  ├─ otto docker                                   cli/docker/index
│  │   ├─ Subcommands: build / up / down / ps       cli/docker/{build,up,down,ps}
│  │   └─ Topics: Image rebuild policy              cli/docker/rebuild-policy
│  ├─ otto link                                     cli/link/index
│  │   ├─ Subcommands: impair / repair / list       cli/link/{impair,repair,list}
│  │   └─ Topics: In-path impairment · Port-scoped impairments · Safety rules
│  ├─ otto tunnel                                   cli/tunnel/index
│  │   ├─ Subcommands: add / list / remove          cli/tunnel/{add,list,remove}
│  │   └─ Topics: Tunnel identity & discovery · Endpoints & host requirements ·
│  │              Portability & host-down behavior
│  ├─ otto monitor                                  cli/monitor/index
│  │   └─ Topics: Live mode · Reviewing a capture · Web dashboard ·
│  │              Serving the dashboard · Metrics & data sources ·
│  │              Monitoring during a test run
│  ├─ otto cov                                      cli/cov/index
│  │   ├─ Subcommands: get / report / clean         cli/cov/{get,report,clean}
│  │   └─ Topics: Coverage tiers · Per-ticket coverage · Exclusion markers ·
│  │              Report thresholds · Coverage during a test run ·
│  │              Instrumenting your product        cli/cov/instrumenting/index
│  │                 └─ Topics: GCC / Clang / Embedded (LLEXT) products
│  ├─ otto reservation                              cli/reservation/index
│  │   ├─ Subcommands: whoami / check               cli/reservation/{whoami,check}
│  │   └─ Topics: The JSON backend · Identity & overrides · Reservation windows ·
│  │              Skipping & disabling the check
│  └─ otto schema                                   cli/schema/index
│      ├─ Subcommands: export                       cli/schema/export
│      └─ Topics: Editor setup                      cli/schema/editors
│
└─ Project & lab configuration                      configuration/index
   ├─ Settings file (.otto/settings.toml)           configuration/settings
   ├─ Lab configuration (lab.json)                  configuration/lab-config
   ├─ Host options                                  configuration/host-options
   ├─ OS profiles                                   configuration/os-profiles
   └─ Host sources                                  configuration/host-sources
```

**Ordering.** The toctree uses a learning order — `init → host → run → test →
docker → link → tunnel → monitor → cov → reservation → schema`. `otto --help`
is reordered to match (Phase 0), so the two agree and neither has to explain
itself.

**Thin pages are accepted.** `otto docker build/up/down/ps` land at roughly
30–40 lines each and `otto schema export` has one sibling. That is the declared
cost of the strict mirror, not an oversight to be "fixed" during review.

**`cli/index` owns:** invocation shape; `--lab` and lab merging with `+`;
`--list-labs` / `--show-lab` / `--lab-depth` / `--list-hosts`; `--xdir` and the
output-directory layout; the logging flags; every `OTTO_*` environment
variable; shell completion including remote path completion; the exit-code
model; and a verb→page table. The links-vs-tunnels framing from the deleted
`network/index` becomes two sentences in that table.

## Disposition map

Every current guide page is accounted for. "→ leaves" means the content exits
the User Guide entirely.

| Current page | → CLI tree / configuration | → leaves the guide |
| --- | --- | --- |
| `guide/index.rst` | rewritten: two children | — |
| `cli-reference.md` | **dissolved.** global opts, completion, remote-path completion, output dirs → `cli/index`; `--dry-run --probe` → `cli/dry-run`; each `## otto <cmd>` → that verb's page | — |
| `dry-run.md` | default/stop/probe/three-states/per-verb answers → `cli/dry-run` | `Status.NotRun`, `.value` raises, unsupported return types, `dry_run_preview=True`, adapting run-parse-branch, sessions → `library/dry-run-contract` |
| `extending-cli.md` | — | whole page → `library/extending-cli` |
| `hosts/index.md` | `--help`, syntax, verb model, `--list-hosts`, completion, dry run, exit codes → `cli/host/index` | "From Python", put/get `mode` API, "Extending" → `library/` |
| `hosts/commands/index.md` | run / put / get / login → `cli/host/{run,put,get,login}` | `log` mode API note → `library/` |
| `hosts/commands/netcat.md` | hops, port/listener strategies, channel budget → `cli/host/netcat`; `nc_options` table → `configuration/host-options` | — |
| `hosts/connections.md` | whole → `cli/host/connections` | — |
| `hosts/capabilities.md` | power/reboot/reachability, lifecycle verbs, log retrieval, dev tools, file ops, kernel modules, userland, privilege, per-verb summary → `cli/host/capabilities/*`; toolchain declaration → `configuration/lab-config` | product providers, dev-tool registration, "Methods as CLI verbs" + authoring + parameter inference → `library/cli-exposed-verbs` |
| `hosts/embedded.md` | taxonomy, selecting, file transfer, example → `cli/host/embedded`; embedded-only lab fields → `configuration/lab-config` | custom frames, custom filesystems → `library/extending-embedded` |
| `hosts/configuration.md` | whole → `configuration/host-options` | — |
| `hosts/os-profiles.md` | data profiles → `configuration/os-profiles` | code profiles, custom host classes, composition → `library/custom-host-classes` |
| `hosts/extending-backends.md` | — | whole → `library/extending-backends` |
| `hosts/extending-embedded.md` | — | whole → `library/extending-embedded` |
| `hosts/busybox.md` | — | whole → `architecture/subsystems/busybox-bed.md` (new page; test-pinned, see Mechanics) |
| `run/index.md` | what `otto run` is, `--list-instructions`, invoking, completion, xdir layout, dry run → `cli/run/index` | `@instruction` authoring, async/blocking rules, `all_hosts`/`get_host`, fleet helpers, options sharing → `library/writing-instructions` |
| `run/defaults.md` | the six built-ins, fleet of interest, `pattern=`, `status`/`--full`, what `cleanup` removes, log locations → `cli/run/defaults` | override ladder (`ProjectActions`), converging fixtures, collision error → `library/writing-instructions` |
| `run/options.md` | — | whole → `library/options-classes` |
| `test.md` | `--help`, running suites, suiteless runs, `--tests` completion, parent options, markers → `cli/test/index` + `cli/test/selection` | defining a suite, registration, options classes, fixtures, suite features → `library/writing-suites` |
| `docker.md` | `--help`, constraints, CLI → `cli/docker/*`; rebuild policy, persistent shell state → `cli/docker/rebuild-policy`; `[docker]` schema → `configuration/settings` + `configuration/lab-config` | "Library API" → `library/` |
| `network/index.md` | **deleted** — framing → `cli/index` verb table | — |
| `network/link.md` | impair/repair/list → `cli/link/*`; in-path, port-scoped, safety, preview → topics | custom impairers, Python API → `library/` |
| `network/tunnel.md` | add/list/remove → `cli/tunnel/*`; identity, endpoints, requirements, host-down, old-OS, discovery, completion → topics | custom carriers, Library API → `library/` |
| `monitor.md` | live/hosts/interval/sessions/NFS → `live`; capture review → `review`; dashboard/topology/gestures/marking/status → `dashboard`; access key/TLS/certs → `serving`; built-in + log-sourced + SNMP metrics → `metrics`; during a test run → `during-tests`; `snmp` block + parser intervals → `configuration/` | custom parsers, per-host/project parsers, parser health, custom SNMP descriptors, monitoring from suites → `library/custom-parsers`; frontend dev → `contributing` |
| `coverage.md` | setup/prereqs → `cli/cov/index`; get/report/clean → subcommand pages; tiers, tickets, exclusions, thresholds, colors/output/CI hosting, `otto test --cov` → topics; `[coverage]` schema → `configuration/settings` | honesty model, "git log walk, not git blame", `tickets.json` compatibility policy → `architecture/subsystems/coverage/` |
| `coverage-{gcc,clang,embedded}.md` | → `cli/cov/instrumenting/{gcc,clang,embedded}` | — |
| `reservations.md` | `--help`, what's checked, JSON backend, identity override + completion, `-R`, disabling, inspecting state, windows, fail-closed, troubleshooting → `cli/reservation/*` | writing a custom backend, contract rules, verify, "library in your own CLI" → `library/` |
| `setup/index.md` | **deleted** — replaced by `configuration/index` | — |
| `setup/repo-setup.md` | settings file, path resolution, field reference, startup, multiple repos, repo dependencies, lab files, team checklist → `configuration/settings`; `otto init --help` → `cli/init` | "Defining shared options" → `library/options-classes` |
| `setup/lab-config.md` | whole → `configuration/lab-config`; `--list-labs`/`--show-lab`/lab merging → `cli/index`; project scope → `configuration/settings` | — |
| `setup/host-database.md` | declaring sources, json paths, `_`-annotations, combining/ordering/layering, credentials & login proxies, troubleshooting → `configuration/host-sources` | the interface, writing a custom backend, error contract, verify → `library/lab-source-backends` |
| `setup/editor-schemas.md` | `otto schema --help` → `cli/schema/index`; generate → `cli/schema/export`; VS Code / Neovim / drift → `cli/schema/editors` | — |

### `library/` target pages

The existing `library/` section has `index`, `async-patterns`,
`connection-options`, `sessions` and `suite-recipes`. This restructure adds:

| Page | Receives |
| --- | --- |
| `library/writing-instructions` | `@instruction` authoring, async/blocking rules, host selectors, fleet helpers, the `ProjectActions` override ladder, converging fixtures, the collision error |
| `library/writing-suites` | defining a suite, registration, fixtures, suite features |
| `library/options-classes` | whole of `run/options.md`, plus "Defining shared options" from `repo-setup` |
| `library/extending-cli` | whole of `extending-cli.md` |
| `library/cli-exposed-verbs` | "Methods as CLI verbs", authoring, parameter inference, product and dev-tool provider registration |
| `library/extending-backends` | whole of `hosts/extending-backends.md` |
| `library/extending-embedded` | whole of `hosts/extending-embedded.md`, plus custom frames and filesystems from `hosts/embedded.md` |
| `library/custom-host-classes` | code profiles, custom host classes, composition |
| `library/lab-source-backends` | the source interface, writing a custom backend, error contract, verify |
| `library/custom-parsers` | custom monitor parsers, per-host/project parsers, parser health, custom SNMP descriptors, monitoring from suites |
| `library/dry-run-contract` | `Status.NotRun`, `.value` raises, unsupported return types, `dry_run_preview=True`, adapting run-parse-branch code, sessions |
| `library/network-api` | link custom impairers + Python API; tunnel custom carriers + Library API |
| `library/reservation-backends` | writing a custom backend, contract rules, verify, "using the reservation library in your own CLI" |
| `library/suite-recipes` (existing) | docker's "Library API" section |

Net: 34 guide pages become roughly 60 CLI pages plus 5 configuration pages,
with about 11 new or expanded `library/` pages and 2–3 architecture additions.

## Mechanics

### Code and test sites that cite guide paths

These are docstring prose and test literals. The docs build cannot see them, so
they must be swept deliberately.

| Site | What it cites |
| --- | --- |
| `tests/unit/host/test_busybox_artifacts.py` :64, :914, :959, :1005, :1012, :1022 | reads `guide/hosts/busybox.md` by path; asserts a `## Trust:` heading; asserts the page is listed in **the hosts toctree**; asserts the recovery text names a real doc |
| `tests/_fixtures/busybox.py` :253, :321 | runtime error strings naming the page |
| `src/otto/cli/init_templates.py` (7 sites) | scaffolded settings/lab comments; `:237` already cites the **stale** `docs/guide/options.md` — fix as a drive-by |
| `src/otto/cli/init.py` :8, :201 | prose + scaffold hint |
| `src/otto/cli/cov.py` :16 | two `:doc:` refs |
| `src/otto/cli/run.py` :240 | error message citing `run/defaults.md` |
| `src/otto/cli/invoke.py` :966 | cites `extending-cli.md` (moving to `library/`) |
| `src/otto/tunnel/discovery.py` :43 | cites `cli-reference.md` — **being deleted** |
| `src/otto/host/embedded_filesystem.py` :21 | `:doc:/guide/hosts/extending-embedded` (moving to `library/`) |
| `src/otto/reservations/__init__.py` :4 | prose path |
| `src/otto/examples/reservations_cli.py` :6 | prose path |
| `src/otto/coverage/collect.py` :361 | prose path |
| `scripts/capture_docs_media.py` :21 | cites `guide/monitor.md` |
| `src/otto/host/userland.py` `GAP_DOCS_PAGE` | **unaffected** — already `architecture/subsystems/busybox-support.md` |

`docs/architecture/subsystems/busybox-support.md` :16 also `{doc}`-links the
moving busybox page; that one the build will catch.

### The busybox toctree guard

`test_the_busybox_doc_is_reachable_from_the_hosts_toctree` asserts the page
appears in `docs/guide/hosts/index.md`'s toctree — a file this restructure
deletes. The guard's *intent* (an orphaned page is a `-W` build failure, and
`make docs` is not the per-task gate) stays valid; only its target moves.
Retarget it to `docs/architecture/index.rst`. Do not delete it.

The page becomes a **new** `architecture/subsystems/busybox-bed.md`, listed
under "Design by area". It is deliberately *not* merged into the existing
`busybox-support.md`: that page's table, section and path structure is parsed
and pinned in both directions by `tests/unit/test_docs_gap_sync.py`, and
appending unrelated prose to it risks the parser for no dedup gain. The two
pages link across — gap registry on one, bed and artifact tier on the other.

Only line 74's test in that file carries `@pytest.mark.busybox`. All three
docs-pinning guards are unmarked, so they run in the hostless lane and need no
bed to verify.

### Inbound links

116 link sites across 29 files outside `guide/`: `README.md`, `overview.md`,
`getting-started.md`, `contributing.md`, `index.rst`, 12 architecture pages,
4 library pages, and 4 `api/*.rst`.

### The silent-failure class

Sphinx runs `-E -a -W` with `nitpicky = True`, so every broken `{doc}`/`{ref}`
fails the build, and "document isn't included in any toctree" is a warning and
therefore an error — orphans cannot hide.

What the build does **not** catch is raw markdown fragment links —
`guide/run/index.md#sharing-repo-wide-options…`,
`cli-reference.md#output-directories`. These resolve at the file level and then
silently point at a heading that now lives on a different page.
`myst_heading_anchors = 3` means many exist.

**A green docs build is therefore not the exit criterion. The grep sweep is.**

Named anchors that must survive: `team-setup-checklist` (referenced from
`getting-started.md`), `coverage-gcc-stamp-guard`,
`coverage-embedded-stamp-guard`, `coverage-clang-stale-deploys`.

### Termynal captures

About 17 pages carry `{raw} html :file: ../../_static/generated/termynal/…`,
read by docutils at parse time. Every one changes relative depth when its page
moves.

`COMMANDS` in `scripts/capture_docs_termynal.py` is missing `link` and
`tunnel`; its comment claims "the nine first-party commands" when there are
eleven. Add both and reorder to match the docs.

**`--help` capture blocks stay at verb level only.** Capturing all ~20
subcommand helps would add 20 subprocess runs to every docs build for pages
whose flags already appear in a table. Subcommand pages get flag tables.

The generated directory is gitignored and its stamp covers all of `src/otto`,
so captures regenerate on the next docs build. Nothing committed needs a manual
refresh.

## Phase 0 — reorder `otto --help`

Lands on main **before** the docs worktree branches, as its own commit.

`otto --help` currently lists `run test monitor cov host docker reservation
schema tunnel link init`, which is registration order and nothing else. Reorder
to the docs order: `init host run test docker link tunnel monitor cov
reservation schema`.

The change is the order of the `register_cli_command(...)` calls in
`src/otto/cli/builtin_commands.py`. Help order comes from `Registry.names()`
("registered names in registration order"), surfaced through
`OttoGroup.list_commands`.

Verified cosmetic:

- No test pins the order. The two candidates iterate for membership, not
  sequence: `tests/e2e/cli/test_schema_run_help_e2e.py` :17 and
  `tests/unit/cli/test_root_group.py` :19.
- `docs/_static/generated/termynal/` is gitignored and stamped on `src/otto`,
  so `help-otto.html` regenerates itself.

Rider: the idempotence sentinel `if "run" in CLI_COMMANDS: return` is
membership-based and stays correct under any order, but retarget it to whatever
ends up first so the invariant reads obviously.

## Gate plan

Another agent holds the bed. Every gate below is bed-free. Check `ps` before
starting and do not run two of them concurrently — the dev VM is shared, and a
neighbouring gate run has previously presented as spurious SIGKILLs and
Error-124 timeouts.

**Run:**

1. `make docs` — the authoritative gate: `doc8`, the markdown-doctest,
   version-literal and dependency-table linters, `sphinx-build -E -a -W`
   (clean rebuild, warnings are errors), the Sphinx doctest builder, and
   `pytest --doctest-modules src/otto`.
2. Grep sweeps — the real exit criteria:
   - `grep -rn 'guide/' src/ scripts/ tests/ README.md docs/` → zero hits on
     retired paths.
   - every `](…#fragment)` in `docs/` resolves to a heading on the page it
     names.
3. `make coverage-hostless` — the no-testbed CI gate slice.
4. `nox -s tests_hostless-3.14` — the canary interpreter, per the standing
   blind-spot rule.
5. `make lint` and `make typecheck`.

**Do not run** while the bed is held: `make coverage` (defaults to
`coverage-python`, which includes the unix and embedded legs), `coverage-unix`,
`coverage-integration`, `coverage-embedded`, any `nox`/`nox-full`/`nox-unix`/
`nox-embedded` lane, `stability*`, `chaos*`, or `make busybox` (five qemu
guests on test1 plus a live busybox.net fetch).

A fresh worktree needs `uv sync`, `npm ci` and `make web` before any docs
build: the dashboard and covapp dists are load-bearing prerequisites, because
`docs/conf.py` boots a real `MonitorServer` and photographs the frontend
through headless Chromium.

## Coordination: busybox bed phase B

Phase B **is landed**, as the squash `ce47b011` ("feat(bed)!: busybox guests
become first-party; retire the contrived tier"). It carries all four docs files
this restructure also touches:

| File | Phase B | This restructure |
| --- | --- | --- |
| `docs/guide/hosts/busybox.md` | 228 lines of body rewritten | moves the whole page to `architecture/subsystems/busybox-bed.md` |
| `docs/architecture/subsystems/busybox-support.md` | 103 lines changed | not moved; gains a sibling that links to it |
| `docs/guide/hosts/extending-backends.md` | 8 lines changed | moves the whole page to `library/extending-backends` |

So there is no ordering constraint. The work may proceed through Task 8 directly.

### The check that got this wrong, and the one that gets it right

I first reported phase B as *unlanded*, on the strength of

```bash
git diff --stat main...worktree-busybox-bed-phase-b -- docs/   # 266 insertions
git log main..worktree-busybox-bed-phase-b --oneline           # 20 commits
```

Both are wrong, for the same reason. A **squash** merge replays the branch's
content as one new commit, so the branch's own commits never become ancestors of
main. Every ancestry-based check therefore reports the branch as unlanded even
though its content is fully present — `git log A..B`, `git branch --merged`, and
the three-dot `git diff A...B`, which diffs from the *merge base* the squash left
behind.

**Compare trees, not ancestry:**

```bash
git diff --stat main worktree-busybox-bed-phase-b -- docs/   # empty == landed
```

Empty output is the only trustworthy answer. This matters beyond busybox: one
squash per item is the house rule, so every branch in this repo is squash-merged
and every ancestry check lies about all of them.

### What was verified against phase B's content

- `busybox.md`'s heading structure is byte-identical to main's, so the
  `## Trust:` guard and the whole-page disposition are unaffected.
- All three docs-pinning guards in `test_busybox_artifacts.py` survive at the
  same line numbers (:64, :919, :1005, :1012, :1022).
- `tests/_fixtures/busybox.py` :253 and :321 still cite the page.
- `GAP_DOCS_PAGE` still resolves to `busybox-support.md`.
- `docs/guide/hosts/index.md` — the toctree this restructure deletes — is
  untouched by phase B.

The page's own intro describes it as "how to run both on your machine … the
trust note that comes with executing someone else's binary", and a note
explicitly disclaims the user-facing question in favour of the architecture
page. That is harness documentation by its own account, which is why it leaves
the User Guide.

## Sequencing

One worktree, one squash. Nine internal commits so review is tractable. The
order below groups the work so each commit is reviewable on its own. Phase B is
already landed, so commit 8 carries no rebase risk:

1. `cli/index` + `configuration/` scaffold; dissolve `cli-reference`
2. run / test
3. docker / link / tunnel
4. monitor
5. cov
6. reservation / schema / init
7. `library/` receiving pages (**except** `extending-backends`)
8. host tree; `extending-backends` → `library/`; `busybox` → `architecture/`
   — the entire collision surface, in one commit
9. `src/` docstring paths, `scripts/` paths, test guard updates, global sweep

Until commit 8 the hosts toctree still exists and still lists `busybox`, so the
toctree guard stays green throughout commits 1–7.

**Each commit fixes the links it breaks.** Deferring to a single sweep at the
end leaves eight of nine commits red, which makes bisection worthless and turns
the last commit into a big bang.

## Out of scope

- API docs (`api/`) — unchanged.
- URL redirects — nothing external deep-links the published docs.
- Prose rewriting beyond what the moves and the triage rule require. This is a
  restructure, not a rewrite.
- Any change to what the commands actually do. Phase 0 changes help *order*
  only.
