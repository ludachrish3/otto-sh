# First-party project actions & default instructions

**Status:** approved by Chris 2026-08-16 (brainstorm in session; decisions recorded here).
**Absorbs:** `todo/first-party-default-instructions.md` (the open questions there are
answered below; the todo file is retired by this spec).

## The design, one sentence

Every repo gets working `install` / `uninstall` / `cleanup` / `status` / `get-logs` /
`install-tools` behavior for free — as library functions, as `otto run` instructions,
and as `ensure_*` test fixtures — all four surfaces dispatching through one per-repo
override point (`ProjectActions`), composed across repos in dependency order.

## Why (recorded so the rationale outlives the transcript)

These actions are the instructions basically every project needs, and the default must
be cheap: an otto user with products registered and zero extra effort should get a
working `otto run install`. But the same behavior is wanted in four places —
instructions, scripts, suites, and fixtures — so the override point MUST live at the
library level. If a repo could override the `install` *instruction*, then
`otto run install` and the `ensure_installed` fixture would run different code: the
exact mirrored-default split-brain this codebase has been bitten by before. Instructions
and fixtures are therefore never override points; they are thin wrappers by construction.

The second governing concern (Chris): composition, not shadowing. When repo B depends on
repo A, A's install actions must run *before* B's — including when A is an **optional**
dependency, which rules out cross-repo subclassing as the composition mechanism (you
cannot subclass a class that may be absent). Composition is therefore by *iteration over
resolved repos in dependency order*, which bootstrap already computes.

## 1. Layer map

```text
Product / DevTool     per-repo per-host behavior contracts, owner-tagged
        ↓ attached at lab ingest (providers)
Host methods          iterate own attachments: install, uninstall, get_logs,
                      install_tools, cleanup, is_clean, …   (all @cli_exposed)
        ↓ scoped per repo by
ProjectActions        one per repo: registered subclass, else otto's default
        ↓ composed in dependency order by
otto.project library  install / uninstall / cleanup / status / get_logs /
                      install_tools / is_clean  +  ensure_installed /
                      ensure_uninstalled / ensure_clean
        ↓ thin wrappers (never override points)
default instructions  ·  ensure_* fixtures  ·  user scripts and suites
```

Override points are exactly two: per-host behavior (subclass a host class, a `Product`,
a `DevTool`) and per-repo behavior (subclass `ProjectActions`). Naming decision: the
library module is **`otto.project`** and the class **`ProjectActions`** — "project"
matches the todo doc's framing, and it avoids the `otto.labs` (lab-repository backends)
collision that `otto.lab` would have invited.

## 2. Owner attribution

Bootstrap's init-import loop (`bootstrap.py`, phase 2) already imports each repo's init
modules inside a per-repo loop, in topological dependency order. It gains a module-level
"currently registering repo" marker set around each repo's imports.
`register_product_provider`, the new `register_dev_tool_provider`, and
`register_project_actions` capture it at call time. `apply_product_providers` (and the
dev-tool twin) stamp each attached instance with `owner: str | None` — the owning
repo's name; attachments made outside any repo's init import keep `owner = None`.
There is no sentinel string: `None` **is** the unowned value, and the filters read it
as "belongs to no repo's actions" rather than matching it against a reserved name.
Stamping is conditional — an instance that already names an owner keeps it, so one repo
can hand an attachment to another's ownership deliberately.

Consequence: default `ProjectActions` methods filter each host's attachments by
`owner == self.repo.name`, so one repo's actions never install, uninstall, or log-gather
another repo's products. `Host.install()` et al. keep their current all-products
semantics for direct host-level use; they gain an optional owner/products filter that the
defaults use rather than duplicating the iteration logic.

## 3. New host surface

All new methods are `@cli_exposed`, so `otto host <id> get-logs`, `otto host <id>
cleanup`, etc. come free via the existing `HostGroup` synthesis.

### 3.1 Log retrieval

- `get_logs(product=True, debug=True, require_product_logs=False)` — conditional
  dispatcher over the two methods below. Retrieving **zero logs is success** by
  default; `require_product_logs=True` makes an empty product-log haul a failure
  (there is deliberately no `require_debug_logs` — no strong case, and symmetry alone
  doesn't buy a flag).
- `get_product_logs()` — iterates the host's products calling a new **non-abstract**
  `Product.get_logs(host, dest) -> Result` hook (default: retrieves nothing). The
  product knows where its logs live; retrieval logic need not run on the host itself
  (external retrieval mechanisms are fine) — the hook receives the host and a
  destination directory and does whatever it needs. Per-board retrieval is the
  **default shape**, not an assumption: a product that aggregates its logs in one
  location (a single host, an external collector) doesn't decompose per board, and
  that is precisely what the per-repo `ProjectActions.get_logs` override is for —
  align retrieval with how the product actually collects. Overrides should still
  land files under the documented destination tree wherever a host attribution
  exists.
- `get_debug_logs()` — default implementation fetches files matching the host's
  **`debug_log_globs`** declaration: a list of remote glob patterns settable on host
  classes, OS profiles, and per host in `lab.json`; default empty. Host classes may
  override the method wholesale (e.g. a journald capture). Like product logs,
  debug-log retrieval need not run on the host itself — an override may use an
  external retrieval mechanism.
- **Destination tree** (documented and contract-tested, mirroring the coverage
  layout's host-id keying):

  ```text
  <output-dir>/logs/<host-id>/product/…
  <output-dir>/logs/<host-id>/debug/…
  ```

  `<output-dir>` defaults to the active command's output directory; callers may pass
  an explicit `dest`.

### 3.2 Glob support (decision from brainstorm)

No transfer backend supports globbing today — `get_files` takes concrete paths in all
seven backends. Decision: **transfer backends stay glob-free forever**; expansion
happens above the transfer layer.

- `PosixFileOps.glob(pattern)` — remote expansion via one POSIX-shell round-trip
  (`sh -c 'for p in <pattern>; do [ -e "$p" ] && printf "%s\n" "$p"; done'` in
  spirit), BusyBox-safe, returning concrete paths; an unmatched pattern returns an
  empty list, consistent with zero-logs-is-success.
- Hosts without a POSIX shell (embedded) are **deferred** (scoping decision): until
  the follow-up lands a client-side `fnmatch`-over-directory-listing fallback, an
  embedded host declares concrete paths in `debug_log_globs` or overrides
  `get_debug_logs`. Concrete paths always work — expansion only engages when a
  pattern contains glob metacharacters.
- Local-side expansion, wherever it is ever needed, is plain `pathlib` globbing.

Rationale: teaching seven backends glob semantics means seven quoting/expansion
dialects that can disagree; expanding once above the seam means transfer only ever
sees concrete paths, identically over `nc`, `shell`, `scp`, and the rest.

### 3.3 Tools

- **`DevTool`** — new ABC mirroring `Product` exactly (`name`, `stage`, `install`,
  `uninstall`, `is_installed`), attached via `register_dev_tool_provider` from init
  modules, landing on `host.dev_tools`, owner-stamped like products. Dev tools are
  repo-defined internal tooling per host, deliberately parallel to products so the
  authoring experience transfers.
- **`ToolchainTool`** — declaration added to the existing `Toolchain` dataclass
  (`tools: list[ToolchainTool]`), each with `name`, local `source`, remote `dest`,
  owning `user`, and `mode`. Declared per host in `lab.json [toolchain]` like the
  existing gcov/lcov fields.
- `install_tools(dev=True, toolchain=False)` — dispatcher. Toolchain is off by
  default (large transfers, rarely needed); dev tools on by default (small, common).
  - `install_dev_tools()` — iterates `host.dev_tools` (stage + install).
  - `install_toolchain_tools()` — default implementation puts each declared tool and
    applies ownership/mode through the existing privilege layer (these installs
    commonly need root-owned destinations). Overrideable per host class — this is
    the method most likely to need project surgery, and that is fine.

There is no standalone `uninstall_tools` surface in v1; tool removal is `cleanup()`'s
job, using each tool's `uninstall`.

### 3.4 Uninstall and cleanup

- `uninstall(get_product_logs=True, get_debug_logs=True)` — the existing method gains
  the two log kwargs. Order: **product logs first** (a lost log set is the
  frustration this design is told to prevent), then uninstall products (best-effort
  across products, as today), then **debug logs last** — teardown-time activity is
  typically exactly what debug logs exist to capture.
- `cleanup(get_product_logs=True, get_debug_logs=True)` — strictly more than
  uninstall: calls `uninstall(...)` (which logs), then uninstalls dev tools and
  toolchain tools via their own `uninstall` contracts. Removal of any further
  project-specific remnants belongs in an override.
- `is_clean()` — `is_uninstalled()` AND no dev tools installed AND no toolchain tools
  present; overrideable for project-specific remnant checks.

## 4. `ProjectActions` (per repo)

```python
class ProjectActions:
    repo: Repo          # provided at construction
    ctx: OttoContext    # provided at construction, per command

    async def install(self) -> Result
    async def uninstall(self, get_product_logs=True) -> Result
    async def cleanup(self, get_product_logs=True) -> Result
    async def get_logs(self, product=True, require_product_logs=False) -> Result
    async def install_tools(self, dev=True, toolchain=False) -> Result

    @property
    def owns_products(self) -> bool             # what the counted-repo rule reads
    async def status(self) -> InstallState      # INSTALLED | PARTIAL | UNINSTALLED
    async def is_clean(self) -> bool
```

**Two host-global concerns are deliberately absent from these signatures** (§5 owns
both), which is why the shapes here differ from the host verbs of §3:

- **No debug half.** `get_logs` has no `debug` parameter and `uninstall` hard-wires
  `get_debug_logs=False` down to the host verb, rather than exposing a flag a repo
  could turn on. N repos each sweeping the same host's debug logs means N transfers,
  each overwriting the last.
- **No toolchain half.** `install_tools`'s `toolchain` is accepted and is a *declared
  no-op* at this layer — kept in the signature so an override has somewhere to hang
  toolchain work of its own and so `super().install_tools(...)` takes the caller's
  flags unchanged. Symmetrically, this `cleanup` uninstalls the repo's products and
  then its dev tools (`uninstall_dev_tools(owner=…)`) and stops there; it does **not**
  remove toolchain tools the way `Host.cleanup` (§3.4) does, because one host's
  toolchain is shared by every owner on it.

- Defaults iterate the lab's fleet hosts (`ctx.all_hosts()` membership — `local` and
  Docker containers excluded, as everywhere else) and drive each host's methods
  scoped to `owner == self.repo.name`. Within a repo, hosts proceed in parallel
  (`do_for_all_hosts`); within a host, products install in declaration order — which
  is already repo-dependency order, because bootstrap imports init modules
  topologically and providers append in registration order.
- `register_project_actions(MyActions)` from the repo's init module. A second
  registration **from the same repo** fails loud at bootstrap. Different repos each
  registering their own class is the intended composition, not a collision.
- Overriders keep `super()` access to every default, and may freely mix custom
  sequencing with per-host defaults (`await host.install()` for chosen hosts).
- A repo that registers nothing gets `ProjectActions` itself, constructed with its
  `Repo` — the zero-effort default.

## 5. `otto.project` orchestrator library

Module-level async functions using the ambient context (the `otto.config.all_hosts`
idiom — zero-argument from instructions, suites, and fixtures alike):

- `install()`, `install_tools(...)` — walk resolved repos in **topological order**
  (dependencies first; optional dependencies that are present are simply in the walk,
  absent ones simply are not), awaiting each repo's actions. **Fail-fast**: the first
  repo whose install fails stops the walk.
- `uninstall(...)`, `cleanup(...)` — walk in **reverse** topological order,
  **best-effort**: every repo's action runs; failures are collected into the result.
- `get_logs(...)` — walks all repos (order immaterial), best-effort.
- **Debug logs are host-level, gathered once.** Product logs are owner-scoped and so
  belong to each repo's actions; host debug logs do not belong to any repo. The
  orchestrator therefore performs **one** host-level debug sweep per operation and
  passes `get_debug_logs=False` down the per-repo walk. For `uninstall`/`cleanup`
  the sweep runs **after teardown completes** — teardown-time activity is typically
  exactly what debug logs exist to capture (Chris), and a per-repo gather would both
  duplicate transfers and have each interleaved sweep overwrite the last. Standalone
  host-level calls (`otto host <id> uninstall`) keep `get_debug_logs=True` — no
  orchestrator is present to own the sweep, and the host's own ordering (product
  logs → uninstall → debug logs) preserves the same principle.
- `status() -> ProjectStatus` — per-repo `InstallState` plus the lab-level aggregate:
  INSTALLED when every counted repo is INSTALLED, UNINSTALLED when every counted repo
  is UNINSTALLED, else PARTIAL. A repo whose actions are the default AND which owns no
  products anywhere in the lab is **not counted** (a docs-only repo must not drag the
  aggregate to PARTIAL); a repo with a registered `ProjectActions` subclass is always
  counted — it opted into having an opinion. Zero counted repos aggregate to
  UNINSTALLED, mirroring `Host.is_installed`'s empty-products rule — nothing that
  could be installed is not "installed".
- `is_clean() -> bool` — AND across **every** repo, not only the counted ones (a
  tooling repo that owns no products still owns dev tools, and `owns_products` cannot
  see those), plus one host-global `toolchain_tools_absent()` sweep. A host that could
  not answer raises rather than counting as unclean.

The converge layer (used by fixtures, callable from anywhere, also reachable from the
CLI via `install --ensure`):

- `ensure_installed(recover_partial=True)` — status INSTALLED: no-op. UNINSTALLED:
  install. PARTIAL: uninstall (best-effort) then fresh install when `recover_partial`
  is True (the default, per Chris); with `recover_partial=False`, PARTIAL proceeds
  straight to install.
- `ensure_uninstalled()` — uninstall unless already fully UNINSTALLED.
- `ensure_clean()` — cleanup unless `is_clean()`.

The orchestrator itself is **not** overrideable in v1 (recorded decision): a repo
customizes by overriding its own actions; cross-repo sequencing beyond topological
order is deferred until a real project needs it.

## 6. Default instructions

Otto pre-registers `install`, `uninstall`, `cleanup`, `get-logs`, `install-tools`, and
`status` as ordinary `InstructionEntry`s — same registry, listed by
`otto run --list-instructions` in a first-party panel section (attributed to otto, not
to any repo). Each is a thin wrapper over §5 with minimal options:

- `install`: `--ensure/--no-ensure` (default off), `--recover-partial/--no-recover-partial`
  (default on, meaningful with `--ensure`)
- `uninstall`, `cleanup`: `--product-logs/--no-product-logs`, `--debug-logs/--no-debug-logs`
  (both default on)
- `get-logs`: the same two flags plus `--require-product-logs`
- `install-tools`: `--dev/--no-dev` (default on), `--toolchain/--no-toolchain` (default off)
- `status`: prints the per-repo table; exit code 0 iff the aggregate is INSTALLED,
  1 for UNINSTALLED, 2 for PARTIAL (documented; scripts may branch on it)

**Collision policy (recorded decision): fail loud at bootstrap.** A repo instruction
named like a first-party default aborts startup with an error naming the repo and the
instruction and pointing at `ProjectActions` as the sanctioned override path (or a
rename, if the instruction is genuinely unrelated). This guarantees `otto run install`
and `ensure_installed` can never diverge. Existing repos with same-named instructions
migrate at upgrade time; the error message is the migration note.

**Non-goal (deferred, per the todo doc and confirmed in brainstorm):** per-host /
per-board option patchwork. Default instruction options are lab-wide. A project whose
hosts need per-host options overrides its `ProjectActions`; a first-class option-merge
scheme is revisited separately once the basics have mileage.

## 7. Test fixtures

`ensure_installed`, `ensure_uninstalled`, `ensure_clean` ship from the suite plugin
alongside the existing `ctx` / `suite_options` fixtures (available under `otto test`,
like everything the plugin provides). Each is **function-scoped** (recorded decision:
the per-test-case start-state guarantee is the point; the cost when state already
holds is one `status()` sweep) and is a one-line wrapper over §5's converge functions
with their defaults (`recover_partial=True`).

Convergence failure raises a host-named error — never a skip (house rule: a down host
fails loudly with its name, it does not skip tests).

## 8. Testing

- **Orchestrator**: unit tests against fake hosts/products (existing fake-host
  idioms): topological walk order forward and reverse, optional-dep
  presence/absence, fail-fast install vs best-effort uninstall, the
  counted/not-counted status aggregation, tri-state transitions, and
  `ensure_installed`'s PARTIAL → uninstall → install path with and without
  `recover_partial`.
- **Owner attribution**: a two-repo fixture (dependency between them) asserting
  products/dev-tools are owner-stamped and that repo A's default actions never touch
  repo B's products.
- **Log tree**: a contract test pinning the `<output>/logs/<host-id>/{product,debug}/`
  layout — the directory shape is API.
- **Glob**: `PosixFileOps.glob` against real shells in the existing conformance
  matrix (BusyBox included); unmatched pattern → empty list, not an error; a
  metacharacter-free pattern passes through as a concrete path.
- **Collision policy**: bootstrap test that a repo instruction named `install` aborts
  with the migration message.
- **Fixtures**: suite-level tests that each `ensure_*` fixture converges from each
  starting state and errors (not skips) when convergence fails.
- Every new host method is `@cli_exposed`; the existing exposure conformance tests
  pick them up.

## 9. Documentation and scaffolding

- New guide page `docs/guide/run/defaults.md`: the four surfaces, the override
  ladder (Product/DevTool → host class → ProjectActions), the log tree, and worked
  single-repo and two-repo examples.
- `otto init` templates mention the defaults (the sample instructions module gains a
  comment pointing at `ProjectActions` instead of hand-rolling an `install`).
- `todo/first-party-default-instructions.md` is deleted in the implementing branch;
  its content lives here.

## Deferred (recorded so they aren't re-litigated silently)

- Per-host/per-board option patchwork for default instructions (§6).
- Orchestrator override / conductor hook (§5).
- Recursive (`**`) remote globbing (§3.2).
- Embedded glob fallback — client-side `fnmatch` over a directory listing (§3.2);
  embedded hosts use concrete paths or a `get_debug_logs` override until then.
- `require_debug_logs` (§3.1) — add only when a real case appears.
- Richer OS-specific debug-log defaults (journald capture, dmesg) — host classes can
  override `get_debug_logs` today; first-party capture recipes come later.
