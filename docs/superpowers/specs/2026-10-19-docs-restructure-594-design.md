# Docs restructure for findability (#594) — design

**Issue:** #594. **Status:** reviewed by Astra and Fable (two rounds, cross-reviewed); Chris's decisions recorded in §11.

This design folds in the docs actions from the 2026-10-19 naive-user study, round 1: five setup agents (Sonnet) built projects from scratch, and five CLI-only agents (Haiku) drove those projects from a product description alone. Bugs and features found in the study are filed as #665–#687; this design depends on some of them (§10) and implements none.

## 1. Goal, evidence and acceptance

Users report that the docs sprawl, that specific things are hard to find, and that it is hard to know where to start. Round 1 confirmed all three:

- **Setup.** Every setup agent reached its goals. Each needed 4–6 workarounds, almost all in coverage, and each reported coverage prerequisites that no page states.
- **No entry point.** CLI-only agents learned mainly from `--help` and error messages, because no page is written for someone joining an existing project.
- **Maintainer pages.** The kernel-module and embedded pages are written for otto's maintainers: they cite `tests/`, `scripts/`, `make kmodcov` and issue numbers.
- **Links.** No broken links or anchors were found in about 2,800 checked. The gaps are missing links, not broken ones.

One round-1 finding is already false on `main`: a CLI agent reported that the `cli/index.md` Commands table has no links, but it has links. Round 2 must not re-raise it.

**Acceptance is round 2 of the study.** It reuses the same ten scenarios and the same briefs, plus a test that obtains each example from the rendered HTML (§3.2). Agents read the rendered HTML only, follow links, and log broken and missing links. The controller spot-checks every concrete claim against source and CLI before it counts. Round 2 passes when all of the following hold:

1. Every agent reaches every goal that is not blocked by a still-open issue listed in §10.
2. No agent hits a trip that round 1 found and this design fixes.
3. Each agent hits at most one dead end. A **dead end** is a page the agent opened from a link or toctree expecting to answer its current goal, and that did not answer it or link to the answer.
4. No agent finds a broken link.

Findings new in round 2 are triaged with Chris. Docs fixes are folded in, and bugs or features are filed as issues after Chris approves them.

**Principles kept:**
- One home per topic: link, never restate.
- Documentation is user-first.
- No URL backward compatibility: pages may move, and redirects remain #374.

**Out of scope:**
- **Embedded toolchain setup.** Users can already build for their target, and Zephyr is only the concrete example platform. The track still states its entry conditions (§3).
- **CLI reference contents**, beyond the fixes listed in §5.
- **Redirects.**
- **Any otto product change.**

## 2. Shape: navigation by reader, files mostly in place

Grouping is done by the top-level toctrees in `docs/index.rst`, each with a `:caption:`. Each page has one canonical toctree parent, and other hubs link to it with ordinary links. Tooling pins several paths, so these files stay where they are:

- `docs/cli/` and `docs/configuration/`.
- `docs/api/`, `docs/architecture/` and `docs/installation.md`.
- The generated pages `docs/cli/known-good.md` and `docs/cli/cov/instrumenting/kmodcov-matrix.md`.
- `docs/examples/getting-started/`.

```
Start here        overview · Installation (one home) · chooser
Set up a project  shared setup → tracks → combining products → coverage → monitoring
                  → writing tests / instructions / options classes
Use a project     Joining an existing project · task guides
Extend otto       Advanced usage, grouped by topic
Reference         CLI reference (cli/index) · configuration (configuration/index) · API (api/index)
Developing otto   architecture · contributing · release process · bed case study · maintainer matrices
```

- **Readers.** The project author uses Set up, the lab user who joined an existing project uses Use, and the extender uses Extend. Reference serves all three. Maintainers have the last section.
- **Start here.**
  - The chooser gives one line per reader, plus a maintainer line for Developing otto, each linking to its section.
  - Installation is merged into `docs/installation.md` as its one home. The Installation section inside Getting Started is removed and replaced by a link.
  - Shell completion keeps a discoverable home in installation.
- **The cookbook is dissolved, page by page.** Its pages move into Set up, Extend and Reference according to the disposition table (§2.1).
- **Getting Started is split by page.**
  - The bed tour (`defining-hosts/*`, `boards-of-interest`, `customizations`, `reservations`) becomes the bed case study under Developing otto.
  - Pages that work on the reader's own scaffold (`running-instructions`, `running-tests`, `coverage`, `customizing-project-instructions`) move under Set up a project.
  - Each page is classified individually in the disposition table.

### 2.1 Page disposition table (L1 input)

L1 starts with a committed manifest, a table in the plan that maps every non-API page under `docs/` to its destination: moved to a path, kept, or merged into a named page. It also lists every explicit anchor, generated page, example directory and path-dependent validator that the move touches. L1 is derived from that manifest mechanically. Placements that are already decided:

| Page | Destination |
| --- | --- |
| `cookbook/authoring/*` | Set up a project (writing tests, instructions, options classes) |
| `cookbook/test-recipes.md`, `cookbook/host-scopes.md` | beside test authoring (Set up) |
| `cookbook/dry-run-contract.md` | beside instruction and CLI extension (Extend) |
| `cookbook/python-library.md`, `connection-options.md`, `sessions.md`, `async-patterns.md`, `network-api.md` | Extend → Library use |
| `cookbook/extending/product-providers.md` (+ custom kinds) | Extend → Custom products (first) |
| `cookbook/extending/custom-host-classes.md`, `extending-embedded.md` | Extend → Hosts |
| `cookbook/extending/custom-parsers.md` | Extend → Monitoring |
| `cookbook/extending/extending-backends.md` | Extend → Backends (connection and transfer backends) |
| `cookbook/extending/{lab-source,reservation,inventory,creds}-backends.md`, `https-clients.md` | Extend → Backends |
| `cookbook/extending/extending-cli.md`, `cli-exposed-verbs.md` | Extend → CLI extension |
| `configuration/os-profiles.md` | stays in Reference; Extend links to its registration part |
| `configuration/declared-products-tools.md` | stays in Reference (built-in kinds); only custom-kind authoring goes to Extend |
| `docs/architecture/support-matrix.md`, `docs/cli/known-good.md` | the manifest records whether each is a user page or a maintainer page; the files stay where they are |

## 3. Set up a project: shared setup and tracks

**Shared setup page** (one home):
- Start with `otto init` in an existing directory.
- Turn the scaffolded placeholder inventory and credentials into one hand-written inline Linux host. The scaffold still writes the three-file form, so note that the leftover `[inventory]` and `[creds]` tables are optional.
- Confirm a first success with something observable: `otto --lab L --list-hosts`, then `otto --lab L host <id> exec 'uname -r'`.
- Show the required `[project]` declaration before any `[[products]]`.
- The first command block sets `OTTO_XDIR`, so output stays out of the project.
- Give the order of the first listing commands: `OTTOPATH` → `--list-labs` → `--lab L --list-hosts`.

**Tracks**, one per product kind: shell, kernel module, embedded, docker. Each track has the same spine:

1. **"You need"** — the track's entry conditions.
   - Kernel module: Kbuild and matching headers.
   - Embedded: an LLEXT-capable base image, the loader, exports, a patched runtime and a matching cross-`gcov`. Building these is out of scope.
   - Docker: a docker-capable parent host.
2. **Get the example** (§3.2), then `git init` and an initial commit.
3. Declare the product, then preview with `-n run install`, then install.
4. One e2e test, then one GoogleTest unit test.
5. Coverage, following the tested three-tier sequence: build → deploy → commit → `otto test --cov` → `otto cov get --tier manual` → commit the manual store → `otto cov report`. HEAD moves between captures; the page explains why each commit is required and links the rules in `cli/cov/tiers.md` and `cli/cov/report.md`.
6. Monitoring, with one custom metric (links `custom-parsers`).

Track-specific content:
- **Shell track.** The product is a CLI tool run once per test, which is the common case, so `GCOV_PREFIX` and `GCOV_PREFIX_STRIP` are set per invocation. A daemon variant is linked.
- **Kernel-module track.**
  - Explains teardown and unload against `ensure("installed")`, and shows `run install --ensure` afterwards.
  - Covers the pre-run clean, which drops `init()` coverage of a module loaded before the run.
  - Shows the freestanding-logic pattern for unit-testing module code with gtest.
- **Docker track.**
  - Shell products inside one compose image, plus a singleton image.
  - Image CMD, not `&`, for long-running processes.
  - It is the first-deploy home that #571 asked for.
- **Embedded track.** Zephyr is the example platform.

**Combining products** pages come after the tracks, and each builds on a track instead of repeating it:
- Shared code between shell products.
- Shell plus kernel module.

**Coverage build requirement.** It is stated once, in its home `cli/cov/instrumenting/gcc.md`: *build with absolute source and object paths; otto relies on them to map coverage to source automatically.* Each track carries one sentence and a link. `GCOV_PREFIX_STRIP` for CMake trees is explained there too, once. The requirement is provisional until the examples prove it (§3.1, §10).

### 3.1 Example projects

- **One real project per track** under `docs/examples/quickstart-<track>/`: sources, CMake with a pinned FetchContent gtest, `.otto/` config and tests. The offline-install path is consistent with `installation.md`. Pages show the files only via `literalinclude`.
- **The shell and docker examples are deliberately hierarchical**, because that is how real product repos look and it tests #665:
  - a product is stitched from a subsystem library in its own object directory plus an executable;
  - two products share a `main.cpp` basename;
  - the gtest tree sits under the project root.
- **Hostless check**, gated on every docs change. A script copies each example to a temp dir and runs the real validation path: `otto --list-products`, `otto -n run install`, and the cmake build plus a run of the gtest binaries. It is wired into `docs-lint` and `nox -s docs`. (`otto init --check` does not exist and is not used.)
- **Bed captures**, a gate-tip lane. The existing `scripts/refresh_docs_captures.py` runner and its manifest (moved up to `docs/examples/captures.toml` once there are several example projects) gain one `labless = false` entry set per track. Each set runs real install → `test --cov` → `cov get --tier manual` → `cov report`.
  - **Assertions.** The runner gains an assertion field. Each entry asserts per-product and per-tier source identity and the expected hit counts from `store.json`, including duplicate basenames and the shared library. A text diff of captured output alone is not proof, and aggregate percentages are never asserted.
  - **What pages quote.** Pages quote captured output, never hand-typed numbers.
  - **Coordination.** Bed use follows the runner's existing machine-wide scratch lock (`/tmp/otto-gs`, shared with `make docs`) and per-capture project copies. Track runs serialize.
  - The runner also gains a step type for running a gtest binary.
- **Gate oracle.** `scripts/gate_scope.py` gains rows for `docs/examples/quickstart-*/**`, the captures manifest and the runner. Example or harness changes select the hostless docs lane, plus the bed-captures lane at the tip. It covers `tests/unit/scripts/test_gate_scope.py` tests. Without these rows, any change under `docs/examples/*` or `docs/conf.py` falls through to the full gate.
- **Rebuilds.** The Makefile's `SPHINX_SRCS` gains the example trees, so that editing a `literalinclude` input rebuilds the HTML.

### 3.2 Getting an example from the rendered HTML

`conf.py` builds one archive per track from `docs/examples/quickstart-<track>/` at docs-build time. Each archive is gitignored generated media, `-W` fails if one is missing, and the archive contains no `.git`. Each track links its archive with `:download:`. Nothing is committed twice. The page text keeps the extract, `OTTOPATH`, `git init` and commit steps.

## 4. Use a project

**Joining an existing project** is a new page and the lab user's start. It covers:
- Installing otto and the project's environment and dependencies, with links to `installation.md` and `cli/env`.
- `OTTOPATH`, then `--list-labs`, then `--lab L` for `--list-hosts` and `--list-products`.
- Where global options go.
- `run status` and `run install --ensure`, including that a coverage run's kernel-module teardown leaves the lab partial and `--ensure` restores it.
- Listing tests and running the whole suite. The how-to lives on `cli/test/index.md` "Running tests", and Joining links to it.
- Where output goes (under the invocation's cwd unless `--xdir` or `OTTO_XDIR` is set) and where `cov report` writes.
- Watching monitoring headless.
- What to do when a reservation refuses access.

The page opens with a short command block whose commands link to their CLI-reference homes.

## 5. Reference fixes

**CLI reference.** The tree is unchanged. Each fact below goes in its one home:
- **`cli/test/index.md`:** the "run everything" form (today's catch-all marker workaround, replaced when #679 lands).
- **`cli/index.md`:**
  - `OTTO_XDIR` and the output location;
  - the `--lab` requirement for host listings;
  - the built-in `local` host id, versus `localhost` in `--show-lab`;
  - the role of hop hosts;
  - `--list-products` truncation at narrow widths;
  - the `--show-lab` comment, which wrongly says `-v`.
- **`cli/run/defaults.md`:** "Registering products" retargeted to declared products.
- **`cli/cov/*`:** `cov report` examples gain `--lab`.
- **`cli/host/exec`:** quoting and `--`.
- **`cli/monitor/*`:**
  - `metrics` links to project-defined metrics in `custom-parsers`;
  - `live` notes the access key in the URL and the bind address;
  - `monitor/index` links project scope.
- **`cli/cov/report.md`:** dirty-remap counting untracked files (one sentence).

**Configuration:**
- **`configuration/index.md`:** gains a hub paragraph on `settings.toml`, `lab.json` and the inventory, each linked.
- **`lab-config.md` split** into an index page that opens with a minimal one-host example, then one page per area. The plan holds the full content map; nothing is dropped. Areas:
  - hosts and fields
  - lab membership
  - elements
  - host identity
  - inventory references
  - power
  - network interfaces
  - docker
  - hops and embedded
  - links
  - monitoring / snmp
  - coverage toolchains
  - product host preferences
  - project scope
  - toolchain tools

  `tests/unit/docs/test_lab_config_field_coverage.py` is updated to search the split pages.
- **`settings.md`:** the `[coverage]` entry's "see cli/cov/index for the schema" is replaced by per-key links (`hosts`, tiers → `cli/cov/tiers.md`, exclusions → `cli/cov/exclusions.md`). The empty-`[coverage]` contradiction between `getting-started/coverage.md` and `cli/cov/index.md` is resolved in one home, citing otto's actual rule.
- **`declared-products-tools.md`:**
  - `[project] host_patterns` also scopes monitoring and coverage;
  - `stage_dir` must be an existing writable directory until #673 lands, then is created when missing;
  - example uninstalls remove only the artifact, never the staging directory.

## 6. Extend otto ("Advanced usage")

Grouped by topic, with custom products first: Custom products · Hosts · Monitoring · Backends · CLI extension · Library use (see §2.1). The extension-author gaps listed in #594's body are closed on their target pages:
- `TransferContext.sftp_options` → `extending-backends.md`
- `HostSummary` conformance fields → `lab-source-backends.md`
- `CommandFrame` extra overrides → `extending-embedded.md`
- `SnmpMetric.kind` / `meta_of` → `custom-parsers.md`
- `register_cli_command` metadata → `extending-cli.md`
- provider instances must accept attribute assignment → `product-providers.md`, `cli-exposed-verbs.md`

The per-host versus project parser precedence note goes on `custom-parsers.md`.

## 7. Coverage pages

**Kernel modules.** `cli/cov/instrumenting/kernel-modules.md` keeps the user recipe:
- your first instrumented module: Kbuild, Makefile, sentinels, teardown plus ensure;
- the `otto host exec --sudo` link;
- a sentence on vermagic.

Its "another kernel, ISA or compiler" provisioning content moves to a maintainer page under Developing otto, next to the generated `kmodcov-matrix.md`. The generated page stays in place, with its renderer, `conf.py`, `.gitignore` and tests untouched. The `tests/project5/kmod` literalincludes move to `docs/examples/quickstart-kmod/`. The page's claim that `otto init --kmodcov` appends a `[[dev_tools]]` stub is false until #675 is fixed, so it is corrected or removed.

**Embedded.** `embedded.md` keeps the user recipe and moves its `tests/repo3` references to the maintainer page.

**Coverage rules, each in one home:**
- commit order, and the tested three-tier sequence (§3);
- system-header exclusion, with an absolute-path pattern example;
- unit-tier rules.

These are linked from the tracks. #687 (coverage on uncommitted changes) will revise the commit-order text when it lands. `getting-started/coverage.md` and `cli/cov/instrumenting/gcc.md` gain the run-per-test CLI case next to the daemon case. The containers page gains the stop-to-flush idiom.

## 8. Developing otto

Contains architecture, contributing, the release process, the bed case study and the maintainer pages (kmod provisioning, embedded). `docs/contributing.md` "Documentation layout" is rewritten in L1 to match the new shape. Its rule that Python-author material goes in `cookbook/` goes away.

**Citation lint.** Criterion: a page outside Developing otto must not require otto's maintainer checkout or infrastructure. It is enforced as a lint that is part of the docs-lint target and of `nox -s docs`, with tests under `tests/unit/scripts/`:
- **Banned:** `literalinclude` targets and cited paths that resolve outside `docs/examples/` and shipped `src/otto/` sources.
- **Banned:** maintainer make targets, from an explicit list (for example `make kmodcov`). User words such as `make install` are allowed.
- **Banned:** issue references, in the `(#NNN` and `issue #NNN` forms only. This avoids TOML colour literals.
- **Allowed:** user-project paths such as `tests/test_device.py` in prose.
- **Exempt:** generated pages, recognized by their `GENERATED` header.

It lands last (L5), after the kernel-module and embedded literalincludes have moved.

## 9. Landings

Each landing is a squash onto `main`, gated by the oracle (`make gate-scope` / `make gate-tip`). Example, `conf.py`, Makefile and nox changes may escalate, so each landing budgets for the lanes the oracle names, plus `make gate-fresh`.

1. **L1: manifest and skeleton.**
   - The disposition manifest.
   - New `docs/index.rst` groups and toctrees, cookbook and Getting Started pages moved, every internal link fixed, and `make docs` warning-free (no page in two toctrees).
   - The path-pinned tests and scripts updated: `test_getting_started_includes.py`, `test_extending_backends_example.py`, `test_host_id_invariant.py`, `test_gate_scope.py`, `relativize_docs_links.py`, and wherever `installation.md` defaults are read.
   - The `contributing.md` layout rewritten.
2. **L2: entry paths and the small CLI-page fixes.**
   - Start here and the chooser.
   - The single Installation page.
   - The shared setup page.
   - Joining an existing project.
   - All the §5 CLI-reference fixes.
   - The configuration hub paragraph.
3. **L3: reference, coverage and Extend content, plus example infrastructure.**
   - The `lab-config` split, the `settings` and declared-products fixes, the §7 coverage pages and the §6 Extend gaps.
   - The hostless check, the captures-runner extensions (assertion field, gtest step), the `gate_scope` rows and `SPHINX_SRCS`.
   - The example-archive build.
4. **L4: tracks, one landing per track,** in this order:
   1. shell, after #665 and #680 are fixed;
   2. kernel module, after #665, #670 and #680;
   3. embedded;
   4. combining-products pages;
   5. docker last, after #667, #676, #678 and #683.

   Each brings its example, its capture entries and its page.
5. **L5: citation lint.**

Round 2 of the study runs after L4. It may run earlier, against the tracks that have landed, for the pages they cover.

## 10. Dependencies and risks

**Track dependencies (Chris: fix the coverage bugs first):**
- **Shell:** #665 (.gcda→.gcno pairing) and #680 (gtest sources in the unit harvest). #686 (output in the project root) is addressed in the docs by `OTTO_XDIR` until it lands.
- **Kernel module:** #665, #670 (reload overwrite) and #680. #675 only affects page text.
- **Docker:** #667, #676, #678 and #683.
- **Embedded:** #665 for its coverage step.

**#665 and the absolute-path contract.** If absolute paths make the hierarchical examples map correctly, absolute paths become the documented contract and #665 narrows. If they do not, #665 is updated with the exact reason, and the tracks wait for the fix. The docs never document a workaround for a limitation Chris has rejected.

**Risks:**
- **Bed contention.** The bed-captures lane shares test1–3 and zephyr with other sessions; the runner's machine-wide lock serializes otto's own runs, and other sessions follow the existing bed-priority practice.
- **Broken inbound links.** Inbound links from issues and bookmarks break (accepted; #374 later).
- **L4 delay.** L4 is gated on product fixes. L1–L3 and L5 are not, except that L5 needs the literalincludes moved in L3/L4.

## 11. Decisions (Chris, 2026-10-19)

- Organize by three readers, plus a separate Developing otto section.
- Several small quickstart tracks; quickstarts live under Set up a project only.
- Real example projects, checked hostless and run on the lab bed.
- Break URLs now; #374 later.
- Staged landings.
- Absolute source and object paths are the build requirement otto may rely on for coverage mapping.
- The tracks wait for the coverage bug fixes (#665, #680).
- The docker track lands last, gated on #667/#676/#678/#683; it is #571's first-deploy home.
- Hostless checks gate every docs change; the bed captures are a gate-tip lane when the examples or their harness change.
- Embedded toolchain setup is out of scope.
- Settled between reviewers by the controller:
  - the citation check is an allow-list lint implementing the "requires maintainer checkout" criterion;
  - example archives are built at docs-build time, never committed;
  - the small CLI-page fixes land in L2, before any track.
