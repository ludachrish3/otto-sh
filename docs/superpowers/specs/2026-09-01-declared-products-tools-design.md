# Declared products & dev tools — settings.toml entries over a generic kind registry

**Date:** 2026-09-01
**Status:** Approved design, pre-implementation
**Scope:** `[[products]]` and `[[dev_tools]]` in `.otto/settings.toml`, backed by a
seam-neutral matcher + kind registry documented as the convention for future
declarative seams (login-proxy selection, post-login commands, …).

## 1. Motivation and decisions

Products and dev tools are today **code-only** seams: a repo registers a
provider callback (`register_product_provider` / `register_dev_tool_provider`)
from a `.otto` init module, and the callback inspects each ingested host to
decide what it carries. That is maximally flexible and stays as the fallback,
but the common cases — "this artifact goes to hosts matching X" — deserve a
declarative, user-friendly surface in repo config.

Decisions taken during brainstorming (Chris, 2026-09-01):

1. **Behavior = named kinds + built-ins.** A TOML entry binds `kind = "…"` to a
   code-registered factory; otto ships built-in kinds so simple cases are
   zero-code. One mechanism, generalizable ("data items that map to arbitrary
   code").
2. **Selection = typed match table.** No expression language; a `match` table
   with type-driven value semantics, AND across keys.
3. **Competition = first match wins**, in declaration order, per product/tool
   name — literally the existing provider dedup semantics ("a product whose
   name already appears on the host is skipped"). Specific entries first,
   generic fallback last.
4. **Scope = generic core, two clients.** The matcher and registry live in a
   seam-neutral module and are documented as the convention; products and dev
   tools are the only clients this pass. Products and dev tools are handled
   **identically** throughout — same schema, same semantics, two registries.
5. **Code providers remain and become the fallback**: declared entries build
   before providers run, so the existing name-dedup gives data precedence.

Not lab data: these entries live in `.otto/settings.toml` — committed,
per-repo **project config**. The standing principle "lab data never names a
product" survives unchanged; the docstring prose "customized in code, not lab
data" is updated to "customized in repo config or code, not lab data" (§10).

## 2. TOML surface

Two new top-level arrays with an identical schema:

```toml
[[products]]
name = "firmware"            # required — logical identity (Product.name role)
kind = "file"                # required — key into the seam's kind registry
match = { "metadata.hw_version" = "rev2" }   # optional; omitted = every targeted host
artifact = "build/fw-rev2.bin"               # every remaining key = kind param

[[products]]                 # fallback: declared last, wins only when rev2 didn't
name = "firmware"
kind = "file"
artifact = "build/fw.bin"

[[dev_tools]]                # byte-for-byte the same schema, other registry
name = "trace-probe"
kind = "file"
artifact = "tools/probe.sh"
match = { id = "bb.*", os_version = ">=3.7" }
```

- **Reserved keys:** `name`, `kind`, `match`. All remaining keys are the
  kind's params, passed through to the factory untouched (the `Cred.params`
  philosophy).
- **Pydantic boundary:** a `DeclaredEntrySpec` in `otto/models/settings.py`
  with `extra="allow"`; extra keys are collected from `model_extra` into
  `params`. The top-level `SettingsModel` gains `products` / `dev_tools`
  list fields (it is `extra="forbid"`, so the new sections must be declared
  there).

## 3. Matching semantics

`match` is a table; a host matches when **every** key matches (AND).

**Key resolution.** A key names a host attribute from a defined allowlist —
the tooling-agnostic surface the provider docstrings already promise
(`id`, `element`, `element_id`, `os_type`, `os_name`, `os_version`, `ip`,
`source_lab`) — or a dotted path under `metadata.` / `element_metadata.`.
An unknown key is an **ingest error** naming the valid keys: a typo (or an
attribute that only exists in some labs' metadata, e.g. `is_virtual`) fails
loudly instead of silently never matching.

**Value typing** (type-driven, no expression parser):

| TOML value                                     | Semantics                                        |
| ---------------------------------------------- | ------------------------------------------------ |
| string starting `>=` `<=` `<` `>` `==` `~=` `!=` | `packaging` `SpecifierSet` against the attribute parsed as a version |
| any other string                               | `re.fullmatch` against `str(attribute)`          |
| bool / number                                  | equality                                         |
| list                                           | any-of; each element typed by the same rules     |

**Version edge.** packaging ≥26 makes `SpecifierSet.contains` return `False`
on an unparseable version rather than raising — a host whose attribute is not
PEP-440-parseable is a **no-match with a one-time warning** (per entry+host),
never a crash. Malformed *entry-side* patterns (bad regex, bad specifier) are
ingest errors (§8).

## 4. Paths

Slashes in TOML must not leak OS-specific separator assumptions. Two distinct
path domains, both already solved in the codebase:

- **Local paths** (e.g. the `file` kind's `artifact`): written with forward
  slashes; declared as `RepoPath` (`models/settings.py`) so pydantic coerces
  the string to `pathlib.Path` — which accepts `/` on every OS — then
  `~`-expands and anchors still-relative paths to the repo root via
  `anchor_path` (CWD-relative in committed config is always a bug; symlinks
  deliberately not resolved). Kinds declare which params are local paths by
  typing them `RepoPath` in their param spec; the generic core does not guess.
- **Remote paths** (e.g. `dest_dir`): the *host's* path domain, exactly as
  `FileProduct.dest_dir` today — handed to `host.put`, which resolves it
  against the host's `default_dest_dir` under the host's own conventions.
  Written with forward slashes; never interpreted by the local OS.

## 5. Generic core — `otto/declared.py`

Seam-neutral, ~100 lines plus docs; the documented convention for future
seams:

- **`DeclaredEntry`** — frozen dataclass: `name`, `kind`,
  `match: dict[str, MatchValue]`, `params: dict[str, Any]`, `owner`
  (declaring repo, stamped at parse), `base_dir` (repo root, for kinds that
  anchor further paths lazily).
- **`host_matches(match, host) -> bool`** — §3's rules, one implementation,
  shared by every seam.
- **`KindRegistry[T]`** — named factories for one seam,
  `factory(entry, host) -> T`, built on the same registry primitive as
  `LOGIN_PROXIES` so unknown-kind errors carry the standard
  "register via `register_<seam>_kind()`" hint.
  `build(entries, host) -> list[T]` walks declaration order, first match per
  `name` wins, instances get `owner` stamped (unless the instance already
  names one — same deliberate carve-out as the provider loops).

Placed at `otto/declared.py`, not under `otto/config/`: the config package's
`__init__` boots the app on import, and the seam modules must import this
core at module top (implementation finding, 2026-09-01).

## 6. Seam adapters and built-in kinds

- `register_product_kind(name, factory)` in `host/product.py` and
  `register_dev_tool_kind(name, factory)` in `host/dev_tool.py` — public API
  mirroring `register_login_proxy`. **Two registries on purpose**, same
  reasoning as the two provider lists: a kind can never attach to the other
  seam's lifecycle.
- **Built-in `file` kind**, registered in both seams. Params:
  - `artifact` (required, local `RepoPath`) — staged via `host.put`.
  - `dest_dir` (optional, host-domain path).
  - `install`, `uninstall`, `check` (optional command strings, run on the
    host).
  Defaults when a command is omitted: `install` → no-op success (staging
  placed the artifact); `uninstall` → no-op success; `check` absent →
  `is_installed` returns `False` (documented: without a check otto assumes
  not installed and re-stages/installs — safe and idempotent for the simple
  cases this kind serves). `host.exists()` does not exist yet
  (`product.py` notes the remote file-ops phase); when it lands, the
  `check` default upgrades to an artifact-existence test.
- Anything richer than `file` + command strings is a repo-registered kind —
  that is the mechanism working as intended, not a limitation.

Declared command strings run under the host's default command timeout;
longer operations belong to a repo-registered kind (a `timeout` param is a
compatible later extension).

## 7. Ingest wiring

At the existing chokepoint (`host/factory.py` — `create_host_from_dict`,
currently `apply_product_providers(host)` / `apply_dev_tool_providers(host)`):

1. **Declared entries build first** (`PRODUCT_KINDS.build(...)`,
   `DEV_TOOL_KINDS.build(...)`), then the code providers run. The existing
   name-dedup then makes code the fallback: a provider product whose name a
   declared entry already claimed is skipped.
2. Declared entries are gated by the **same §5 `[project]` targeting rule**
   as providers — a repo's entries are skipped for hosts outside
   `(host.source_lab, host.id)` targeting — enforced independently per seam
   for the same reason the provider loops gate independently.
3. Parsing slots into `config/repo.py` beside `model.docker` →
   `self.declared_products` / `self.declared_dev_tools`
   (lists of `DeclaredEntry`).
4. A repo whose `declared_products` or `declared_dev_tools` is non-empty is
   "providing" for bootstrap's D2 scope-required check exactly like a repo
   that registered a code provider — it must declare `[project]` or bootstrap
   refuses it the same way.
5. Entry collection (`declared_for_host`) never forces bootstrap: it probes
   whether bootstrap has already run and returns no entries when it has not,
   so an un-bootstrapped process collects nothing rather than paying
   discovery's cost or running repo init imports just to answer.
6. A repo the dependency pass skipped contributes **no** declared entries
   (Chris, 2026-09-02): its settings were parsed in phase 1, but its init
   modules never ran, so neither half of it — declared or code — applies;
   collection filters against the dependency pass's survivors while keeping
   discovery order for cross-repo first-match precedence. Consequently the
   D2 scope-required check (point 4) judges declared entries over the same
   survivor list as providers — a skipped repo's missing scope is vacuous,
   and its missing dependency has already surfaced as its own finding.

## 8. Failure modes

Entry-side defects fail **ingest, loudly** (the "misconfigured provider fails
ingest" precedent): unknown `kind` (with register hint), unknown match key
(naming valid keys), malformed regex or version specifier, missing/invalid
required kind param (kind factories validate their params and raise with the
entry's `name` and seam in the message). Host-side oddities at match time
(unparseable host version) are no-match + one-time warning (§3).

## 9. Testing

- Table-driven unit tests for `host_matches`: every value type ×
  match / no-match / error, dotted metadata paths, unknown keys, the
  packaging-≥26 unparseable-version row.
- `KindRegistry.build`: ordering, first-match-wins dedup, owner stamping,
  unknown kind — written once, run against **both** seams parametrically, so
  "products and tools are handled the same way" is enforced structurally.
- Ingest integration: declared-before-provider dedup, §5 gate per seam,
  `file` kind end-to-end against the unit-test host doubles.
- Every new test proven able to fail (mutate-and-observe-red) per the usual
  discipline.

## 10. Documentation updates

- `product.py` / `dev_tool.py` module docstrings: "customized in code, not
  lab data" → "customized in repo config (`[[products]]` / `[[dev_tools]]`)
  or code, not lab data"; providers documented as the code fallback and the
  escape hatch for what the match table cannot express.
- New guide page for declared products/tools beside
  `guide/cli/host/capabilities/dev-tools.md`; settings reference gains the
  two arrays and the match-table semantics (one home, linked from both seams'
  pages — single-source-of-truth rule).
- `otto/config/declared.py` module docstring is the normative home of the
  entry-schema convention (reserved keys, match semantics, ordering) for
  future seams.

## 11. Future seams (explicitly out of scope this pass)

The convention is designed so a later seam pays only: one
`KindRegistry[ItsType]`, one `register_<seam>_kind()` wrapper, one `build()`
call at its chokepoint, one `[[<seam>]]` array in `SettingsModel`. Candidates:

- **Login-proxy selection** — the registry half already exists
  (`register_login_proxy` + `Cred.proxy`/`params` in lab data); a
  `[[login_proxy_defaults]]` seam would add declarative *selection* only
  (hosts matching X default to proxy Y).
- **Post-login commands** — not a seam today; would arrive as
  `[[post_login_commands]]` with a built-in `command` kind.

## 12. Out of scope

- Removing or deprecating the provider callbacks (they are the fallback,
  permanently, per decision 5).
- Declaring products/tools in lab data (standing deliberate exclusion).
- An expression language for matching (`when = "…"`) — revisit only if the
  match table demonstrably cannot serve real repos.
- Cross-repo priority competition between declared entries (dedup order
  across repos remains registration/ingest order; a `priority` field is a
  compatible later extension if a real need appears).
