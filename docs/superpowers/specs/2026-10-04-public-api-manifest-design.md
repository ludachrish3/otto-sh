# Public surface declaration — design (spec 1 of the #590 contract-first series)

**Status:** v7, for owner approval. v6 was approved and P0 built against it. **Date:**
2026-10-04, amended 2026-10-05; spec 4 amended §7 and the appendix on 2026-10-06.
- v1 tried to freeze otto's contracts. Codex rejected it, and the owner then said a freeze is not
  the goal (§1).
- v2 declared the surface, but its transition dropped protection for paths the docs teach today.
  Codex rejected it.
- v3 kept those paths protected until a marked retirement. Codex found its legacy list drawn from
  the old golden only, and its validator inputs and conversion rule loose.
- v4 adopts the owner's **hard cutover** (§8 P-1): no interim legacy paths, no interim
  membership lists, no aliases for D1. Everything public changes once, in one marked commit
  (P1). It also defines the teaching boundary and the validator's binding scope, and converts the
  golden record by record.
- v5 answers Codex's v4 review, which accepted the hard-cutover design and asked only for
  enforcement detail:
  - complete validator inputs;
  - pages that rely on Sphinx's doctest setup;
  - where the footer's contents come from;
  - merge commits;
  - bare-module records;
  - the P0 activation condition and the D1 migration inventory.
- v6 adds the owner's sequencing decision (§8 P-3). The window for hard cutovers is closing, so
  the whole #590 series makes **one** public cutover. P0 lands first; specs 2–4 are designed
  next; then a single P1 carries every public-path change in the series. Spec 5 moves code
  behind paths that no longer change.
- v7 adopts the **API dump** (`2026-10-05-api-dump-design.md`, "the dump spec"). The golden
  records each public binding's shape: calls, inputs, members, ancestry, obligations, enums. Each
  commit's dump is regenerated from that commit and diffed by explicit compatibility rules. It
  replaces the `Host` lines and Q1: a gated member is a public member, and P1 gives the
  underscore hooks public names.

**Series:** #590 (untangle the import cycle) is split into specs, each approved before code
moves: **1. public surface declaration (this)**, 2. run-state contracts (variant/policy lifetime,
loop-scope ownership, host resolution), 3. spec validation and the registry catalog, 4. repo and
scope inputs, 5. placement and phases. Spec 1 comes first because spec 5 moves modules, and a
move is only safe if we know which import paths must survive it.

**Owner decisions it builds on** (all 2026-10-04):
- **D0.** Two tiers: package-root facades for ordinary use, plus an explicit list of extension
  namespaces.
- **D1.** `otto.config` holds settings and `Repo` only. A new public `otto.lab` is the home of the
  lab and fleet API (`Lab`, `load_lab`, `get_lab`, `get_host`, `all_hosts`, ...).
- **§8.** Q1–Q5, plus two policy decisions:
  - P-1: a hard cutover. No code depends on otto yet, so undeclared taught paths are retired at
    once rather than carried.
  - P-2: a mark is for breaking changes only.

**Appendix (generated):** `2026-10-04-public-api-manifest-appendix.md`.
- A: the namespaces.
- B: a home for every user-taught name with no public path today.
- C: the per-facade evidence (Q4).
- D: the internal packages.
- F: the first `__all__` for every declared module that has none.
- G: the taught paths P1 retires, each with the declared path that replaces it.

## 1. Goal: stable paths and visible changes, not a frozen API

otto is pre-1.0 and several areas are still moving (docker, reservations, host construction,
the thin CLI). The owner's goal for "long-term public-API stability" is:

1. **Public import paths survive reorganisation.** #590 moves a lot of code. A taught
   `from otto.x import Y` keeps working through it, unless retiring it is itself an approved,
   marked decision. Every retirement in the #590 series happens in one marked commit, P1:
   spec 1's (D1 and appendix G) and whatever specs 2–4 decide. From then on, the declared surface is what paths are kept against.
2. **Every breaking change to the public API is visible.** Either a `!` in the subject or a
   `BREAKING CHANGE:` footer marks it; the checker accepts either, as it does today. Additions
   are not breaking, and appear as ordinary `feat:` entries in the generated changelog. Three
   kinds of addition are breaking, because they break existing callers or implementers: a new
   required parameter, a new required input, and a new obligation (abstract or protocol member) on an existing class (dump
   spec §4.2).
3. **Contracts stay free to evolve.** Changing a signature or behaviour is allowed when it is
   visible (point 2). It is not prevented.

So this spec **declares** the public surface: which names are public, and at which path. It does
**not** freeze signatures or behaviour. Shape changes are detected and must be marked (the dump
spec), but they are allowed. Every public name starts **provisional**. An area is promoted to
**stable** on its own schedule (§5), which is when its behavioural contract is designed.

## 2. What exists today

Today otto has three overlapping ideas of "public":
- **`__all__` of lazy packages.** It is pinned equal to the public eager bindings plus the public
  lazy entries (`test_lazy_packages.py:360-392`). Its 542 distinct names
  include everything one otto package borrows from another, so it means "shared inside otto",
  not "promised to users" (§3).
- **The golden** (`tests/unit/api_snapshot/public_api.txt`): `otto.__all__` (30 names), the deep
  imports docs teach, and 36 `Host` protocol lines. Removal rules:
  - a root line or a `Host` line needs a mark;
  - a docs-taught line needs one only if its path **stops importing**
    (`check_breaking_marks.py:511-518`).
- **What docs and examples actually teach.** Some of it has no public path at all, for example
  `otto.config.lab:Lab` and `otto.host.capability_grid:HostCapabilities`.

There is no single declaration, so a module move cannot be checked against one.

## 3. Evidence

The ledger (scratchpad `ledger.py`) is static and AST-based. It follows every reference through
`_LAZY_*` tables and re-exports to the **defining site**.

**Evidence kinds:**
- `root`: `otto.__all__`.
- `import`. Its sources:
  - docs code;
  - every import line of `docs/examples/**/*.py` and `src/otto/examples/*.py`, docstrings
    included, and imports between example modules.
- `attr`: dotted references, including `otto.docker.deploy(...)` after `import otto.docker`.
- `role`: Sphinx/MyST cross-references.
- `setting`: `module:Name` strings.
- `testing`: imported by `otto.testing`.
- `autodoc`: listed by an `automodule` directive.

**Rules:**
- `attr`, `role` and `setting` count as *deliberate* only on user-facing pages: cookbook, cli,
  configuration, getting-started, examples, installation, overview and README.
- **The teaching boundary.** Teaching lives in docs pages, `docs/examples`, and the
  `otto.examples` sources, docstrings included. A doctest on a function elsewhere in `src/otto`
  is a test of that function, not teaching. So `human_readable` (`monitor/parsers.py:121`) and
  `split_on` (`utils.py:135`) gain nothing from their doctests, and neither do the
  `config.scope` doctest imports.
- `doctest_global_setup` (`docs/conf.py:609`) preloads otto names for those function doctests,
  and some docs pages lean on it too. For example, `async-patterns.md:10-27` and
  `sessions.md:10-23` call `LocalHost` without importing it. Such a page teaches a name without
  showing the reader its import, so P1 adds the explicit imports (§6).

**Results:**
- **542** facade sites, read from each facade's literal `__all__`, module-valued exports
  included:
  - **191** have deliberate evidence;
  - **328** are reference-only, almost all autodoc (262 `automodule` directives);
  - **23** have none.
- **All 351 facade names without public evidence are used inside otto.** 290 are used by other
  modules. 49 are used only inside their own module, plus tests. 12 are used only inside their own
  module, as return or field types (`env_status() -> EnvStatus`). None is dead code: the
  lazy-package pattern exports whatever siblings import.
- **123 taught names have no public path today.** Appendix B gives each a home, for example
  `Lab`, `HostCapabilities`, `CredSpec`, `ParseContext`, `parse_one`, `SESSION_SETUPS`,
  `OPTIONS`/`options_key`/`verbs_for` and `register_dev_tool_kind`.
- 11 references are unresolved. All are filenames or stale text (appendix E).

**Positive evidence is decisive; absent evidence is not.** No name is made internal because it
lacks evidence (§4, K).

**Teaching vs. explaining.**
- Docs that *teach* a name are code a reader copies: an import, a call, a subclass, or a
  registration or config string. Taught names must be public.
- Docs that *explain* behaviour may link any documented name. For example,
  `extending-cli.md:153` links `otto.cli.main.entry` to say when bootstrap runs.
- `dry-run-contract.md:13-20` registers `"otto.cli.link:link_app"` in a code block. That is
  teaching, of an internal target. P1 rewrites the example to target a reader's own module, so
  the CLI implementation stays internal.

## 4. The declaration

**Namespaces** (appendix A):
- **Tier 1, package facades:** `otto`, `otto.config`, `otto.lab` (new, D1), `otto.host`,
  `otto.labs`, `otto.models`, `otto.link`, `otto.tunnel`, `otto.docker`, `otto.reservations`,
  `otto.inventory`, `otto.creds`, `otto.monitor`, `otto.coverage`, `otto.suite`, `otto.project`,
  `otto.session`, `otto.logger`, `otto.testing`, and `otto.init` (taught as a library at
  `docs/cli/init.md:222`).
- **Tier 1, single-file modules:** `otto.bootstrap`, `otto.context`, `otto.instructions`,
  `otto.result`, `otto.errors`, `otto.utils`, `otto.registry`, `otto.params`, `otto.tls`.
- **Tier 2, extension namespaces:**
  - host: `otto.host.transfer`, `otto.host.options`, `otto.host.command_frame`,
    `otto.host.login_proxy`, `otto.host.session_setup`, `otto.host.app_shell`,
    `otto.host.embedded_filesystem`, `otto.host.product`, `otto.host.dev_tool`;
  - monitor: `otto.monitor.parsers`, `otto.monitor.snmp`, `otto.monitor.log_sourced`;
  - CLI: `otto.cli.registry`;
  - each `otto.examples.*` module.
- **Internal packages** (appendix D). They back one CLI verb or are plumbing. Their `__all__`
  stays, because the lazy guard needs it, but it is not a promise.
  - `otto.cli` (except `otto.cli.registry`), `otto.check`, `otto.env`, `otto.host.survey`,
    `otto.kmodcov`, `otto._webassets`;
  - `otto.coverage.fetcher`, `.store` and `.merge`. The two public names among them,
    `GcdaFetcher` and `CoverageStore`, are already exported by `otto.coverage`.
  - `otto.lifecycle`, `otto.layout`, `otto.declared`.
- Every other module path is internal.

**Membership authority for namespace bindings: `__all__`, and nothing else.** A name is public
if and only if it is in the `__all__` of a declared namespace. Every package facade has one
today (`otto.host.transfer` included), and so does `otto.tls`. P1 gives every other declared
module its first `__all__` (appendix F).

**Members of a declared class** follow D-5 instead (dump spec §7.1): a member is public, and
gated, if and only if its name has no leading underscore, or it is one of the supported dunders
(dump spec §3.4).

`api/public.toml` (repo root, read only by tooling) lists namespaces, never names. For each one
it records the tier, the stability (`provisional` throughout at first) and a free-text *pending*
note. It also declares otto's versioned formats, each pointing at the constants that hold the
versions otto reads and writes (dump spec §13).

A name reachable from two namespaces (`otto.host:TransferProgressHandler` and
`otto.host.transfer:TransferProgressHandler`) is public at both. A test asserts that both resolve
to the **same object**.

**Dispositions:**
- **K. Keep.** Every current export of a tier-1/2 namespace stays public-provisional at its
  current path. Nothing is narrowed (Q4). Two sets move instead: D1's fleet names go to
  `otto.lab`, and spec 4's five repo accessors go from `otto.config` to `otto.bootstrap`
  (`2026-10-06-repo-and-scope-inputs-design.md` §2).
- **A. Add.** Each taught name without a public path gets one (appendix B).
  - The default is the deepest declared namespace above its defining module: `HostCapabilities`
    → `otto.host`, `CredSpec` → `otto.models`, `prepare_run` → `otto.suite`.
  - A name already taught at a tier-2 path keeps it (`otto.host.options:SshOptions`).
  - Exceptions:
    - `Lab` → `otto.lab` (D1).
    - `MonitorTarget` → `otto.monitor`. The collector owns the type and imports the parsers, so
      a re-export from parsers would be a reverse edge.
    - `DeclaredEntry` → `otto.host.product`. It becomes a runtime binding: `product.py` already
      imports `otto.declared` at runtime, so this adds no dependency.
  - Scope helpers (`resolve_scopes`, `scoped_ids`): spec 4 (§2) keeps them internal. The one
    page that imported them teaches `otto.lab:fleet_of_interest` instead, and
    `EmptySelectionError` is declared at `otto.lab`.
- **R. Retire.** Appendix G lists 26 taught paths that are not declared: every v1 golden deep
  line plus every import a reader copies (docs code, `docs/examples`, `otto.examples`). The
  ledger keeps each import's **original** path, not just its defining site. Examples:
  `otto.host.element:Element`, `otto.suite.run:prepare_run`, and
  `otto.host.factory:host_identity` (imported by the copyable `examples/lab_repository.py:43`).
  - P1 re-points every docs page and example to the declared path, and the old path stops being
    public. The footer lists each one.
  - The module itself is untouched, so the old path still imports for now. It just has no
    golden line any more, and spec 5 may move or delete it freely.
- **S. First `__all__`.** In P1, each declared module without `__all__` gets the list in
  appendix F. That narrows what `from m import *` returns, and the footer says so.
- **D1, in P1, no aliases.** `otto.lab` is created as a package facade over the existing
  implementation (`otto.config.lab`, `otto.config.fleet`), which spec 5 relocates later. It
  exports `Lab`, `load_lab`, `get_lab`, `get_host`, `all_hosts`, `do_for_all_hosts` and
  `run_on_all_hosts`.
  - `otto.config` stops exporting them.
  - The 15 internal import sites, plus tests and docs, switch at the same time. Internal callers
    import from the owning module (§9), not from `otto.lab`.
  - The root `otto.*` names are unchanged.
- **Specs 2–4.** Their public-path decisions land in the same P1 (§7). Spec 5 makes no public
  change.

**Pending seams.** Each is settled by the spec named here, **before P1**. Any path change it
decides lands in P1, and its `api/public.toml` entry names that spec:
- host construction (`HostSpec`, `to_host`, custom host fields): spec 3b;
- run state (`otto.context`): spec 2, **settled** (`2026-10-06-run-state-contracts-design.md`).
  `otto.context` stays the facade; `RunPolicy`, `HostResolver` and `ContextBinding` join it after
  P1, implemented in a leaf `otto.invocation` with no public path;
- repo and scope inputs (`otto.config.scope`, `get_ordered_repos`, `is_bootstrapped`): spec 4,
  **settled** (`2026-10-06-repo-and-scope-inputs-design.md`). The repo accessors move to
  `otto.bootstrap`; `fleet_of_interest` and `EmptySelectionError` are declared at `otto.lab`;
  the scope helpers stay internal;
- registry objects (`Registry`, `Ref`, `*_BACKENDS`, `FRAME_CLASSES`, `LOGIN_PROXIES`,
  `SESSION_SETUPS`, `OPTIONS`): spec 3a, **settled** (`2026-10-06-registry-catalog-design.md`).
  Every registry object keeps its declared path. P1 adds `otto.registry`'s `__all__` (appendix F)
  and retires three facade names (appendix G addendum); the new registration contract lands after
  P1 as marked commits;
- the term-backend seam (`ConnectionManager`, `TermContext`, `register_term_backend`): spec 3a
  settles its construction contract (`TermContext`, `build_term_backend(name, ctx)`); the ABC
  stays with #600;
- inventory constants imported by conformance (`INVENTORY_KEY_FIELDS`, ...): whether users
  should import them is decided when the inventory area is promoted.

The last two keep their paths through P1. Their later work changes contracts, not paths; any
path change after P1 would go through the deprecation policy the owner has yet to define (§8
P-3). The registration-semantics spec runs before P1 only if it would hide or move a registry
object. Otherwise those objects stay public at their declared paths.

## 5. Stability: provisional by default, stable by promotion

| Class | Remove, rename or move a public name | Breaking signature/behaviour change | Mechanically detected |
|---|---|---|---|
| **provisional** (default) | mark required | mark required | the dump's records (dump spec §4); the namespace list |
| **stable** (by promotion) | mark required | mark required | the same, plus the area's conformance tests |
| internal | free | free | — |

- **What provisional means.** The gate catches a removed, moved or renamed name, and every shape
  break the dump records (dump spec §4.2). A behaviour break, and any break the dump does not
  cover (dump spec §1), relies on review and the commit convention. This is the deliberate cost
  of not freezing.
- **Promotion** of an area is a small spec of its own, and must provide:
  1. a contract page: hooks with their direction (who calls whom), required/optional/conditional
     members and constructor obligations. Every contract member is already public (D-5);
  2. named conformance tests for its behaviour;
  3. owner sign-off.
- **Transitions, read from `api/public.toml`:**
  - internal → public: free;
  - provisional → stable: free;
  - stable → provisional: needs a mark;
  - removing a namespace: needs a mark.

## 6. Machinery

- **Producer** (`scripts/api_snapshot.py`). The golden becomes v2: the API dump. Its format,
  records and generation are the dump spec's §§2–3 and §5. Every member of every declared
  namespace's `__all__` gets a `name` record, plus the records its kind carries. The `Host`
  lines are replaced by `otto.host:Host`'s records.

  Docs stop producing lines; they are validated instead.
- **Docs validator,** replacing the docs scan.
  - **Inputs:** exactly the teaching boundary of §3.
    - **Docs pages and README.** Every code block counts, including MyST/RST fences, `eval-rst`,
      doctest blocks, and `{testsetup}`/`{testcleanup}` blocks.
    - **The whole source of every `docs/examples/**/*.py` and `src/otto/examples/*.py`:**
      ordinary imports (relative ones resolved), plus every code block in their docstrings,
      whether doctest or literal (`::`).
    - Doctests on functions elsewhere in `src/otto` are **not** inputs (§3).
  - **Binding scope.**
    - A docs page is one scope. Its code blocks, setup blocks included, are read in order, and a
      name bound in one block stays bound in later ones (`python-library.md:100-128` imports
      `otto` in one block and uses it in another).
    - A `.py` file is one scope.
    - A `{literalinclude}` is validated as its source file, a whole-file scope that covers any
      slice the page shows. It contributes no bindings to the page.
    - `import x as y` and later rebinding are tracked.
  - **Sphinx's doctest setup.** `doctest_global_setup` may keep supplying non-otto
    infrastructure (`asyncio`, `run`, `GS_EXAMPLE`); those are not API. An **otto** object a page
    uses must be bound by an import on that page. P1 adds the missing imports to the pages that
    rely on the setup today. `docs/contributing.md:821-823` changes to say so.
  - **Checks:**
    - every `from otto… import N` resolves to a declared name;
    - every `import otto.x [as y]` is a declared namespace;
    - every later `otto.x.N` / `y.N` access in the same block or file is a declared name of `x`;
    - every `"pkg.mod:Name"` string that names an otto object is declared;
    - no star import;
    - no taught use of an otto underscore member (`taught-private-member`, dump spec §7.3).
  - Unsupported syntax fails loudly. There is no exception list.
  - Explanatory roles are not restricted (§3).
- **Shared record format.** `scripts/api_records.py` holds the one v2 renderer/parser that the
  producer, the comparator and `check_breaking_marks.py` all import (dump spec §2).
  `scripts/api_lines.py` keeps the v1 format and the header check.
- **`check_breaking_marks.py`.** It already walks commits one by one, and picks each commit's
  policy from its **parent's** schema:
  - **Parent v1:** today's rules, unchanged. A removed root or `Host` line needs a mark. A removed
    deep line needs one if its path no longer imports, including bare-module lines.
  - **Parent v2:**
    - the commit's dump must equal the one regenerated from that commit (dump spec §5). A stale
      or re-sorted dump is refused, marked or not;
    - records are compared by the dump spec's §4 rules. A breaking finding (§4.2), such as a
      removed `name` record, needs a mark;
    - a namespace removed from `api/public.toml`, or downgraded from stable to provisional,
      needs a mark (the checker diffs the parent and current TOML).
  - **Merge commits.** Before P0 the walk skipped them. P0 includes them in the v2 era
    (`evaluate_commit` in `check_breaking_marks.py`). Under v2, a merge is compared with its
    **first parent**:
    - one that changes the golden or the TOML in a breaking way needs a mark in its message;
    - a harmless merge passes;
    - a merge with any v2 parent is in the v2 era: if a merged parent carries a v2 golden and
      the merge does not, it is refused, marked or not (otherwise `git merge -s ours` from a
      v1 branch would leave v2).

    P0 replaced the old "merges are skipped" test with merge-inclusion tests for the v2
    era (`test_check_breaking_marks.py`): a harmless merge passes, and a merge-only removal
    fails. A merge whose parents are all v1 is still skipped, as v1 always did: the
    v1 → v1 path is unchanged.
  - **The v1 → v2 commit** (P1) is converted record by record:
    - a v1 root line (`otto:N`) that does not reappear as a `name otto:N` record needs a mark,
      even if `N`
      still imports (today's rule, pinned in `tests/unit/scripts/test_check_breaking_marks.py`);
    - `Host` lines convert to `otto.host:Host` member records, by the dump spec's §8;
    - a bare v1 module line (`otto.docker:`, `otto.coverage:`, `otto:`) converts to that
      namespace's entry in `api/public.toml`. Without one it needs a mark, whether or not the
      module still imports;
    - a v1 deep line that does not reappear as a `name` record needs a mark, **whether or not it
      still imports**. The v1 "still imports" exemption ends at the conversion. P1 is marked
      anyway, and its footer lists every such line.
    Nothing is inferred that v1 did not record.
  - **Activation.** The v2 rules apply exactly when the parent or the commit carries the v2
    header. A v1 → v1 commit needs no `api/public.toml`. P0 is therefore checked by the v1 rules
    alone, and its new tests run on fixtures, not the live tree.
  - **Schema edge cases:**
    - a commit whose parent is v2 and which writes v1 (rollback) **or deletes the golden** is
      refused, marked or not: once the golden is v2, its only next state is v2. A marked deletion
      would otherwise let the next commit add a v1 golden under the v1 rules, switching the
      manifest and the name diff off in two passing commits. Retiring or moving the golden is a
      change to this spec and to the checker, not a marked commit;
    - a producer-schema decrease, along any parent edge, is refused (dump spec §5.4);
    - a missing or malformed `api/public.toml`, or an unknown stability value, fails the check;
    - the TOML diff runs on every commit, including commits that change no golden line. Before
      P0 the checker skipped those; P0 does not.
- **Agreement tests:**
  - Every namespace in `api/public.toml` exists and has a literal `__all__`.
  - Every `__all__` name resolves.
  - Every two-path name is the same object. Two names are aliases only if the ledger gives
    them one defining site, never because they are spelled alike (`otto.link:DryRunPlan` and
    `otto.tunnel:DryRunPlan` are different classes).
  - These checks run against **runtime** bindings, in a fresh interpreter. A name bound only
    under `TYPE_CHECKING` fails. Today's guarantee that every golden line resolves
    (`test_every_golden_line_resolves` in `test_public_api_snapshot.py`) carries over to `name`
    records.
  - No docs import targets an internal package.
- **Guards:**
  - Package facades keep the existing lazy-package guard: `__all__` equals the public eager
    bindings plus the public lazy entries (`test_lazy_packages.py:360-392`). That covers
    `otto.config`'s `_LAZY_EXPORTS`, `otto.logger`'s module-valued exports and
    `otto.host.transfer`'s mix.
  - Implementation modules that carry `__all__` (single-file and tier-2, from P1) get a separate
    check: `__all__` is a literal list of unique strings, each resolving to a binding in the
    module. Completeness is not required, so `inspect` or `Path` never have to be listed.
- **API reference:** the pages split into *Public API* (declared namespaces) and *Internals* (no
  promise). The *Internals* pages use `:ignore-module-all:`, so explanatory links keep resolving.

## 7. Delivery: one hard cutover for the whole series

1. **P0, tooling, dormant.** It lands as soon as this spec is approved, unmarked, because it
   changes no surface:
   - producer v2 (the API dump), the validator, the per-parent checker and the agreement tests
     are built and tested **on fixtures**;
   - against today's tree they run report-only;
   - no `api/public.toml`, no v2 golden and no `otto.lab` exist yet, so nothing live is enforced.
2. **Specs 2–4 are designed and approved.** Each records its public-path changes as a delta to
   this declaration: additions, moves, retirements. Their internal work may land before P1 if it
   changes no declared path. Their contract changes land **after** P1, each as its own marked
   commit, so the live dump judges each one (owner decision S-1, recorded in spec 4,
   `2026-10-06-repo-and-scope-inputs-design.md` §0).
3. **P1, the cutover. One marked commit** (`feat(api)!: ...`):
   - **Declaration:** `api/public.toml` becomes live, and the golden switches to v2.
   - **Specs 2–4:** their approved path deltas (spec 4: `2026-10-06-repo-and-scope-inputs-design.md` §2).
   - **Additions:** the appendix-B names join their namespaces, and `DeclaredEntry` becomes a
     runtime binding.
   - **First `__all__`:** every declared module in appendix F gets one.
   - **Versioned formats (dump spec §13):** the formats in its inventory are declared. Their
     acceptance paths, the browser's monitor-export reader included, switch to the declared
     constants. Each declared `reads` version gets a frozen, populated conformance sample.
   - **Hook renames (D-5):** every underscore member that is an extension contract gets a
     public name, with every implementation, override, caller, docs page and example (dump spec
     §7.4).
   - **D1:** `otto.lab` is created and `otto.config` loses the fleet names. Everything that used
     the old names switches in the same commit:
     - the root lazy entries and their `TYPE_CHECKING` imports (`otto/__init__.py:40,81-85`);
     - internal callers (e.g. `cli/remote_completion.py:275`, `session/lab.py:57`);
     - the export tests (`tests/unit/config/test_lazy_exports.py:274-290`);
     - the scaffold text that tells users which path to import (`init/templates.py:480-491`);
     - the docs.
   - **API reference:** the split into *Public API* and *Internals* pages goes live.
   - **Retirements:** the 26 appendix-G paths stop being public, and every docs page and example
     moves to the declared path. The `link_app` example is rewritten.
   - **Footer.** Generated from six inventories frozen from the **pre-cutover** tree:
     1. the retirement inventory (appendix G). It comes from the ledger's original taught paths,
        so it includes paths v1 never recorded, such as `otto.host.factory:host_identity`;
     2. the D1 export delta;
     3. the star-import narrowing: for each module in appendix F, its old non-underscore globals
        minus its new `__all__`;
     4. the path deltas of specs 2–4;
     5. the v1 → v2 golden and TOML diff;
     6. the hook rename inventory (dump spec §7.4). v1 never recorded underscore members, so
        these renames do not appear in item 5.

     A test asserts that the footer names every item in each inventory.
   - **Import checks:** additions go through each facade's lazy table and its matching
     `TYPE_CHECKING` block. The lazy guard, the import-budget and import-contract tests, and `tach
     check --exact` stay green. New names are imported in fresh interpreters, in both import
     orders. P1 also accounts for its file operations, as described in *File-operation accounting*
     below.
   - **Squash timing.** CI checks a PR commit by commit (`.github/workflows/ci.yml:425-434`).
     So the P1 branch is squashed to its single marked commit **before** it is gated and pushed,
     not at landing.
4. **Spec 5** moves implementations behind the declared facades, so no public path changes.
   - The v2 checker proves it: a spec-5 commit that removes a `name` record fails unless marked,
     and a mark would contradict this plan.
   - Changes to declared names after P1 are additions, or contract changes marked case by case.
   - A path retirement after P1 would need the owner's future deprecation policy, not another
     cutover.

### File-operation accounting (P1, specs 2–4, spec 5)

The import budget (`scripts/import_budget.py`, `docs/architecture/startup-performance.md`) caps
each gated command's file operations at its baseline plus 10%, or plus 5 for small counters. That
catches an unneeded heavy dependency. It does not account for a refactor:
- **The slack is large.** 10% of `dispatch_local_warm` (about 2,600 operations) is about 260. On
  Python 3.10 a module costs about 3 operations, so roughly 85 extra modules fit unnoticed.
- **A saving is not kept.** The ceiling stays at the old baseline, and the saving only earns an
  advisory note.
- **Not every command is gated.** The 25 gated surfaces leave out `init`, `env`, `cache`,
  `docker`, `link`, `tunnel`, `monitor`, `cov`, `reservation`, `inventory`, `schema`, `host
  probe` and `host power`. These 13 are measured at `--help` and never enforced.

#590 changes what otto imports, so a module split can add operations and breaking a cycle can
remove them. For example, today a warm TAB handover loads `otto.host` (167 operations). So every
phase that changes otto's imports accounts for them: P1, any internal work from specs 2–4 that
lands earlier, and spec 5.

1. **Before and after table.**
   - The phase runs `scripts/import_budget.py --report-json` on its base and on its tip, under
     Python 3.10, the highest-measuring gated interpreter.
   - It records the `file_ops` and `workspace` delta for every surface, tracked ones included. The
     table goes in the plan's hand-back and in the commit body.
   - A surface that grew lists the packages that grew, from the report's breakdown.
2. **Budget.**
   - **No growth** on the latency-critical surfaces: `import_otto`, `version_repo`,
     `completion_repo_warm`, `completion_repo_handover` and `help_repo_warm`. TAB is the most
     NFS-sensitive path otto has.
   - **Growth anywhere else** is allowed only when the phase's spec names the surface and says why.
     Staying within the ceiling is not a reason.
3. **Savings are locked in.** A phase that shrinks a gated surface regenerates the ceilings in the
   same commit (`make import-snapshot`), on every gated interpreter (3.10–3.14). The next
   regression is then measured from the new baseline.
4. **Internal imports skip the facades.** Otto's own code imports from the module that defines a
   name, never through a public facade, so a facade never joins a command's import path just
   because it exists. This is §9's spec-5 rule, applied in P1 to every import site P1 adds or
   switches. D1 is the first case: internal callers of the fleet names import the implementing
   module, not `otto.lab`.

## 8. Decisions (owner-approved 2026-10-04, recorded for review)

- **Q1. Underscore hooks** (`_run_put`, `_run_get`, `_dispatch_per_file`, `_apply_mode`,
  `ZephyrFrame._region_before_end`, the `BaseHost` family hooks, `_connection_factory`).
  *Replaced 2026-10-05 by D-5 (dump spec §7).* A gated member is a public member. P1 gives every
  contract underscore member a public name; from then on the producer refuses a hidden
  obligation, and the validator refuses taught underscore use.
- **Q2. Host extension base.**
  - Registered hosts subclass `RemoteHost`/`UnixHost`/`EmbeddedHost`; `register_host_class`
    already requires it (`os_profile.py:78-101`).
  - `BaseHost` stays public for `isinstance` and annotations. `LocalHost` derives from it.
  - "Or BaseHost directly" in the docs is a defect, to triage.
- **Q3. Registry objects.**
  - Direction: extend through `register_*`.
  - The objects stay public-provisional until a registration-semantics spec defines the
    replacement operations: membership (`.names()`, `gs_example/__init__.py:41,53`), lookup, lazy
    `Ref` registration, attribution and duplicates.
  - **Settled by spec 3a** (`2026-10-06-registry-catalog-design.md`): the objects stay public;
    the operations are defined there.
- **Q4. Narrowing.**
  - No name-level narrowing here. Narrowing happens per area at promotion, using appendix C.
  - There are two exceptions:
    - the package-level internal list (§4): 65 facade names with no taught use, which back a
      CLI verb;
    - the three names spec 3a retires (appendix G addendum).
  - Easiest later candidates: the 49 names used only by their own module and tests.
- **Q5. Initial stable set:** empty.
  - First promotion candidates: the `Host` protocol, transfer backends, `Result`/`Status` and
    monitor parsers.
  - Binary loaders have no contract yet.
  - The term-backend seam's construction contract is in spec 3a; its ABC is #600.
- **P-1. Hard cutover.** No code depends on otto yet, so nothing is carried. P1 does every
  public change at once:
  - undeclared taught paths are retired;
  - first `__all__` lists are written;
  - D1 moves without aliases.

  It is one marked commit whose footer names everything retired. There are no legacy lines, no
  interim TOML membership lists, and no deprecation aliases.
- **P-2. Marking.** A mark (`!` subject or `BREAKING CHANGE:` footer, either accepted) is for
  breaking changes only. Additions are `feat:`.
- **P-3. One cutover for the series.** Hard cutovers are close to no longer being allowed, and
  this is one of the last. So:
  - the #590 series makes exactly one: P1, after specs 2–4;
  - P0 lands first;
  - no name is planned to move twice;
  - spec 5 changes no public path.

**API dump decisions (2026-10-05):** D-1 to D-6 are recorded in the dump spec's §0:
- per-commit freshness;
- gated obligations;
- version-invariant records;
- the `Host` keyword-order and external-member relaxations;
- gated ⟺ public ⟺ no underscore;
- keyword interception not breaking.

## 9. Out of scope, recorded

- **Spec 5:** internal code imports from the owning module, never through a re-export alias, even
  when the owning module is public. P1 already follows this rule for the import sites it touches
  (§7, *File-operation accounting*, item 4).
- **A registration-semantics spec** (Q3): duplicate handling, the constructor contract for a
  replaced built-in, and `options_key`'s `module:qualname` identity. Settled by spec 3a
  (`2026-10-06-registry-catalog-design.md`).
- **Docs defects** from the extension audit (26, e.g. `Arg(type=)` raises `TypeError` in
  `cli-exposed-verbs.md:96`; `otto.host.transfer.unix` at `extending-backends.md:47` names no
  module). These are reported to the owner for triage.
- **Settings and lab-file key stability:** the schema-diff gate already planned at
  `docs/contributing.md:349`. It also covers the other retained but unversioned inputs listed in
  dump spec §13.4.
- **Version bumps.** A cache schema bump needs no mark. A versioned format's dropped version
  does (dump spec §13).

## 10. Verification

- **The dump's proofs** are the dump spec's §11 table, which is authoritative for records,
  comparisons, generation, conversion of `Host` lines and D-5. The lists below keep spec 1's own
  proofs.
- **Red proofs, each a planted change the gate must refuse:**
  - **Docs validator:**
    - a docs import of an undeclared path;
    - `import otto.docker` followed by `otto.docker.undeclared()`;
    - an undeclared `"otto.x:Name"` string;
    - a star import in a docs fence;
    - a new internal import planted in an ordinary (non-doctest) line of `src/otto/examples`;
    - a page using an otto name that only `doctest_global_setup` binds.
  - **Checker:**
    - a removed `name` record;
    - a moved name;
    - a namespace removed from the TOML;
    - a stable → provisional edit;
    - an **unmarked** v1 → v2 commit that drops a v1 deep line, even though its path still
      imports;
    - a deletion of a declared name after the conversion;
    - a merge whose only change removes a declared name, or downgrades a namespace;
    - a bare v1 module line with no TOML namespace in the conversion.
  - **Agreement tests and guards:**
    - two paths bound to different objects;
    - a declared namespace without a literal `__all__`;
    - a non-literal or unresolvable `__all__` in an implementation module.
- **Must-not-flag proofs:**
  - a re-sorted golden produces no compatibility finding (freshness refuses it, dump spec §2.2);
  - a v1 → v1 commit dropping a docs line whose path still imports (today's exemption, kept for
    history);
  - a harmless merge;
  - checker matrix: v1→v1, strict v1→v2, v2→v2, rejected v2→v1, a TOML-only downgrade;
  - P0 as a whole;
  - additions that the dump spec's §4.3 classifies as not breaking. §4.2 takes precedence, so a
    new required parameter or input, or a new obligation, is not covered by this.
- **Preserved behaviour:** every existing `test_public_api_snapshot.py` and
  `test_check_breaking_marks.py` case is kept, or replaced by a stated equivalent. The
  subject-only and footer-only marker cases stay valid (P-2).
- **Gates:** `make check-api-snapshot`, `make check-breaking`, the lazy-package guard, `make
  docs`, the full chain, and `make gate-fresh`.
- **File operations (P1, and every later phase that changes imports):** a 3.10 before and after
  table from `import_budget.py --report-json` covering every surface, gated and tracked:
  - zero growth on the five latency-critical surfaces;
  - every other growth named and justified in the spec;
  - shrunk surfaces re-baselined on 3.10–3.14 in the same commit.

  P0 changes nothing under `src/`, so its table is all zeros.

## 11. Left to the P1 implementation plan

These are implementation choices inside the rules above, not open policy:
- **API-reference rendering.** The public pages follow `__all__`, which autodoc honours. The
  *Internals* pages use `:ignore-module-all:`.
- **Parsers.** The validator parses whole statements, so a multi-line
  `from otto.x import (\n  a,\n  b)` is read like a one-line import. Today's scanner already
  handles that (`scripts/api_snapshot.py:152-158`), and so does the rebuilt ledger. Nested
  RST/MyST constructs are covered by fixtures.
- **Where the scanners live.** The docs validator and the ledger-derived retirement inventory are
  scripts with their own tests. The ledger rules in §3 are their specification.
- **Hook names.** The rename inventory is derived mechanically (dump spec §7.4); the public name
  for each hook is chosen per seam in P1's review.
- **Splitting P1.** P1 may be developed as several commits on its branch, but it lands as **one**
  commit, squashed, so the checker sees a single v1 → v2 step.
