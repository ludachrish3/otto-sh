# Repo-Level Default Protocol Options — Implementation Plan

## Context

Today, every host in `hosts.json` either specifies its own `ssh_options` / `telnet_options` / etc., or silently inherits the hardcoded defaults baked into the dataclasses in [src/otto/host/options.py](src/otto/host/options.py). There is no way for a repo (a SUT discovered via `OTTO_SUT_DIRS`, with a `.otto/settings.toml`) to declare *"every host I touch should default to telnet port 9023, ssh `connect_timeout=2.0`, etc."* — even though that pattern is common: a given product/test-bench tends to share connection conventions.

The TODO entry that prompted this ([todo/TODO.md:6](todo/TODO.md#L6)): *"Hosts should support per-project default values for protocol options. Ideally, these options apply to only hosts related to the relevant project. In cases of overlap, the last project in the list wins out."*

## Decisions (resolved with the user)

- **Approach: B — factory-time merge.** Apply repo defaults inside `create_host_from_dict()` so all current and future `LabRepository` backends (JSON today, DB/REST/etc. tomorrow) inherit the behavior for free.
- **Protocol signature:** `LabRepository.load_lab()` gains a `defaults: dict | None = None` parameter; each backend forwards it to the factory.
- **Shared-host semantics:** last repo in `OTTO_SUT_DIRS` wins globally.
- **TOML shape:** nested under `[host_defaults]` in `.otto/settings.toml`, mirroring `hosts.json` field names.

The remaining sections document the chosen implementation, then keep the alternatives explored as supporting context.

---

## Implementation Plan (Approach B)

### TOML schema

`.otto/settings.toml`:

```toml
[host_defaults.ssh_options]
port = 2222
connect_timeout = 5.0

[host_defaults.telnet_options]
cols = 200
echo_negotiation_timeout = 2.0
```

Any of the six protocol option tables (`ssh_options`, `telnet_options`, `sftp_options`, `scp_options`, `ftp_options`, `nc_options`) is permitted; all are optional. Unknown keys inside an options table should fail loudly so typos don't silently no-op.

### Merge precedence (lowest → highest)

1. Hardcoded dataclass defaults in [src/otto/host/options.py](src/otto/host/options.py).
2. Repo `[host_defaults]` tables, applied in `OTTO_SUT_DIRS` order (later repos overlay earlier ones, **per-key**).
3. Per-host `*_options` from `hosts.json` (or whatever backend), **per-key**.

Per-key merge means a host setting only `port` still inherits `connect_timeout` from the repo default.

### Code changes

1. **[src/otto/configmodule/repo.py](src/otto/configmodule/repo.py)** — extend `Repo.parseSettings()` ([line 319](src/otto/configmodule/repo.py#L319)) to extract `self.settings.get('host_defaults', {})` into a new `Repo.host_defaults: dict[str, dict[str, Any]]` attribute. Run it through the existing `_expandRecursive()` so `${sutDir}` etc. expand. Validate that every key is one of the six known `*_options` table names; raise on unknown.

2. **[src/otto/cli/main.py](src/otto/cli/main.py)** ([lines 253-303](src/otto/cli/main.py#L253-L303)) — after repos are loaded and before labs are loaded, reduce all repo `host_defaults` dicts in order:
   ```python
   merged_defaults: dict[str, dict[str, Any]] = {}
   for repo in repos:
       for opt_key, table in repo.host_defaults.items():
           merged_defaults.setdefault(opt_key, {}).update(table)
   ```
   Pass `merged_defaults` into each `LabRepository.load_lab()` call.

3. **[src/otto/storage/protocol.py](src/otto/storage/protocol.py)** — add `defaults: dict[str, dict[str, Any]] | None = None` to `LabRepository.load_lab()`'s signature.

4. **[src/otto/storage/json_repository.py](src/otto/storage/json_repository.py)** ([line 91](src/otto/storage/json_repository.py#L91)) — accept `defaults` and forward it to `create_host_from_dict(host_data, defaults=defaults)`. No merge logic here.

5. **[src/otto/storage/factory.py](src/otto/storage/factory.py)** ([line 87](src/otto/storage/factory.py#L87)) — add `defaults: dict[str, dict[str, Any]] | None = None` to `create_host_from_dict()`. In the `_OPTIONS_BUILDERS` loop ([lines 140-142](src/otto/storage/factory.py#L140-L142)), when constructing each option dataclass, merge per-key:
   ```python
   for opt_key, builder in _OPTIONS_BUILDERS.items():
       host_table = kwargs.get(opt_key) if isinstance(kwargs.get(opt_key), dict) else {}
       default_table = (defaults or {}).get(opt_key, {})
       if default_table or host_table:
           kwargs[opt_key] = builder({**default_table, **host_table})
   ```
   `defaults=None` reproduces today's behavior bit-for-bit.

6. **[src/otto/configmodule/completion_cache.py:515](src/otto/configmodule/completion_cache.py#L515)** — direct factory caller. If the cache should reflect repo-default-aware option values, plumb `defaults` here too; otherwise leave it (and document) since completions are mostly about host identity, not options.

### Tests

- **Factory unit tests** — exercise `create_host_from_dict()` with: `defaults=None` (today's behavior, must be unchanged); `defaults` only; per-host override only; both with overlap (host wins per-key); both without overlap (union); unknown key inside an options table (raises).
- **Repo settings parsing tests** — `[host_defaults.<bad_key>]` raises; `[host_defaults]` empty/absent yields empty dict.
- **CLI reduction test** — two repos, second overrides only one key; ensure the reduced dict has the right values per-key (last-wins).
- **End-to-end fixture** — add a fixture repo at `tests/repo_with_defaults/.otto/settings.toml` declaring an SSH default (e.g. `connect_timeout = 99`); a `hosts.json` host that overrides only `port`; assert the resulting `RemoteHost` has `ssh_options.connect_timeout == 99` and `ssh_options.port == <host's value>`.
- **Backward-compat sweep** — run the existing host-construction test suite unchanged; nothing should regress.

### Verification

- `uv run pytest tests/` — all existing tests pass; new tests added for the merge scenarios above.
- Smoke test against the Vagrant lab: declare a benign default (e.g. `ssh_options.keepalive_interval = 30`) in a fixture repo's `.otto/settings.toml`, run an existing test that connects via SSH; verify the connection still works and the option is observably applied (a debug log of the merged options dict is the cleanest assertion).
- Manual eyeball check: print the effective options for a known host before/after the change; confirm the deltas match the declared defaults.

### Non-goals (deferred)

- Context-sensitive resolution (a host seeing different defaults depending on which repo is "asking"). The chosen semantics are global; the TODO confirms this is acceptable.
- An `otto`-level diagnostic command for "show effective options per host." Worth filing as a follow-up TODO if defaults debugging gets painful.
- Backends other than JSON. The factory-level merge means new backends need only forward `defaults` through `load_lab()` to inherit the feature.

---

## Alternatives considered (kept for reference)

## Architectural facts (from exploration)

- A "repo" is a SUT directory with `.otto/settings.toml`, modeled by `Repo` at [src/otto/configmodule/repo.py:73-504](src/otto/configmodule/repo.py#L73-L504). `Repo.parseSettings()` ([repo.py:319](src/otto/configmodule/repo.py#L319)) parses the TOML and stashes the raw dict on `self.settings`.
- Multiple repos can be loaded in one CLI invocation; ordering is preserved from `OTTO_SUT_DIRS`. The TODO's "last project wins" rule applies to that order. **Confirmed semantics: last-repo-in-list wins globally.**
- `RemoteHost` ([src/otto/host/remoteHost.py:94-236](src/otto/host/remoteHost.py#L94-L236)) is a `slots=True` dataclass with one field per protocol's options, each defaulting via `field(default_factory=...)`.

### Host construction is pluggable across storage backends

Hosts can come from **multiple backends**, not just JSON. The codebase already defines a backend-agnostic abstraction:

- `LabRepository` Protocol ([src/otto/storage/protocol.py:11-74](src/otto/storage/protocol.py#L11-L74)) — `runtime_checkable` Protocol with `load_lab(name, search_paths) -> Lab`, `supports_location(path)`, and `list_labs(search_paths)`. **Explicitly described as "DB-agnostic" in its docstring.**
- Today's only implementation: `JsonFileLabRepository` ([src/otto/storage/json_repository.py:17-100](src/otto/storage/json_repository.py#L17-L100)).
- Other in-tree callers that build hosts directly via the factory: `completion_cache.py` at [src/otto/configmodule/completion_cache.py:515](src/otto/configmodule/completion_cache.py#L515).
- Future backends (e.g. SQL/NoSQL DB, REST API, Python module) are anticipated by the protocol design.

**The `create_host_from_dict()` factory at [src/otto/storage/factory.py:87-145](src/otto/storage/factory.py#L87-L145) is the universal chokepoint for host construction**, regardless of backend. It already has a `_OPTIONS_BUILDERS` dict that converts each `*_options` sub-dict into the corresponding dataclass — the natural place to apply defaults consistently across all backends.

- Wiring lives in [src/otto/cli/main.py:112-303](src/otto/cli/main.py#L112-L303): repos are loaded first, then labs, then both go into a `ConfigModule`.

### Why the storage abstraction matters for this design

The key design question becomes: **where in the pipeline do we merge defaults so that *every* current and future host source benefits, without each backend having to re-implement the merge?**

This shifts the relative attractiveness of the approaches: anything that puts merge logic *inside* a specific backend (Approach A's loader-level merge) requires every new backend to remember to do it. Approaches that merge at the universal chokepoint (factory) or after construction (resolver/property) avoid that hazard.

---

## Approach A — Merge at lab-load time (per-backend, before factory)

`Repo` parses a new `[host_defaults.ssh_options]` (etc.) block from `.otto/settings.toml`. `cli/main.py` reduces all repos' defaults in order into a single dict and threads it through the `LabRepository.load_lab()` Protocol. **Every backend implementation** (today: `JsonFileLabRepository`; tomorrow: a hypothetical SQL or REST backend) is responsible for merging that dict into each host record before calling `create_host_from_dict()`.

- **Merge happens:** at lab-load time, in dict-space, inside each backend.
- **Last-repo-wins:** by `cli/main.py` accumulator order.
- **Host override:** trivially correct via dict-spread.
- **Pros:**
  - Hosts remain self-contained; `RemoteHost` stays unchanged.
  - No new types, no new properties — easy for new contributors to read.
  - Effective config is auditable (you can dump the merged dict pre-factory).
- **Cons:**
  - **Every new `LabRepository` implementation has to remember to merge defaults**, and is free to do so inconsistently. This is the central drawback once you take the multi-backend reality seriously.
  - Threads a new param through `LabRepository.load_lab()` — a Protocol signature change.
  - Direct factory callers (e.g. [completion_cache.py:515](src/otto/configmodule/completion_cache.py#L515)) bypass the merge entirely unless updated separately.
  - Ad-hoc lab loading (no repo) silently gets no defaults — needs to be documented.
- **Cost:** **Small per backend, but linear in number of backends.**

---

## Approach B — Merge at factory time (one chokepoint for all backends)

Same TOML schema as A, but the merge moves *into* `create_host_from_dict()` via a new `defaults: dict | None = None` parameter. Each `_build_*_options` builder applies `{**defaults.get(opt_key, {}), **raw}` so host-level keys win. The `LabRepository.load_lab()` signature gains an optional `defaults` parameter that each backend is expected to forward (but not interpret); the factory does the actual work.

- **Merge happens:** at factory time, still in dict-space, but inside the factory module.
- **Last-repo-wins:** same accumulator in `cli/main.py`.
- **Host override:** dict-spread inside the builders.
- **Pros:**
  - **Works for every backend automatically** — JSON today, SQL/REST/Python-module tomorrow — as long as they go through the existing factory (which they should, and today they do).
  - Centralizes merge in one well-tested function — single point of correctness, single point of testing.
  - `defaults=None` reproduces today's behavior exactly — backward compatible at the factory boundary.
  - Direct factory callers like `completion_cache.py` can opt in by passing `defaults`; they aren't silently broken.
  - The factory becomes the natural place for any future cross-cutting host construction logic.
- **Cons:**
  - `LabRepository.load_lab()` still needs the `defaults` parameter so each backend can forward it (Protocol signature change, but a trivial pass-through).
  - Doesn't help shared-host context-sensitivity (uses global last-wins like A).
  - Doesn't help ad-hoc usage that builds `RemoteHost` directly without going through the factory.
- **Cost:** **Small.** Touches: [factory.py](src/otto/storage/factory.py) (the actual merge), [protocol.py](src/otto/storage/protocol.py) + [json_repository.py](src/otto/storage/json_repository.py) (signature + pass-through), [cli/main.py](src/otto/cli/main.py) (accumulate + pass), [repo.py](src/otto/configmodule/repo.py) (parse).

---

## Approach C — Lazy resolution (host stores raw + a defaults reference)

`RemoteHost` is restructured to store `_raw_ssh_options: dict` plus a reference to a small immutable `HostDefaults` object owned by the repo/lab binding. `host.ssh_options` becomes a `cached_property` that materializes the merged `SshOptions` on first read. `HostDefaults` is the result of the last-repo-wins reduction.

- **Merge happens:** at first access of `host.ssh_options`.
- **Last-repo-wins:** the `HostDefaults` object is computed once per (lab, repo-set) binding in `cli/main.py`.
- **Host override:** raw dict's keys take precedence inside the property merge.
- **Pros:**
  - Cleanly preserves "what the user wrote" vs "what the host effectively uses" — best diagnostics story.
  - A single host instance could be re-bound to a different defaults source for context-sensitive resolution.
  - No information loss across serialize/deserialize.
- **Cons:**
  - `RemoteHost` is `slots=True` and exposes `ssh_options` as a *field*; converting to a property is an invasive refactor that risks breaking every consumer that mutates it.
  - Mutation semantics change: `host.ssh_options.timeout = 5` no longer round-trips.
  - Adds a defaults-reference field that has to travel with serialization/pickling.
  - Larger blast radius than A or B; bigger cognitive load.
- **Cost:** **Medium-Large.** Touches: [remoteHost.py](src/otto/host/remoteHost.py) significantly, every site that constructs/mutates options on a host, plus the same loader/CLI plumbing.

---

## Approach D — ConfigModule-owned resolver (host stays raw; resolver at use time)

Hosts are built exactly as today. `ConfigModule` gains a `resolve_options(host, opt_kind)` method. Every protocol *consumer* (SSH transport, SCP runner, etc.) reads through the resolver instead of `host.ssh_options` directly. ConfigModule maintains `host_id → ordered list of repos that claim it` and applies last-repo-wins on each call (memoized).

- **Merge happens:** at *use* time, behind a resolver API.
- **Last-repo-wins:** the resolver knows the per-host repo list.
- **Host override:** resolver consults raw host options last (highest priority).
- **Pros:**
  - Zero changes to `RemoteHost` and `factory.py`.
  - Naturally handles a host shared between two repos with the correct semantics.
  - Same place would later host runtime overrides (env vars, CLI flags) for free.
- **Cons:**
  - Requires changing every transport/runner to go through the resolver — wide change surface.
  - Two ways to read host options is a footgun; new code can silently bypass repo defaults.
  - Tests that build a `RemoteHost` directly need ConfigModule scaffolding to see defaults.
  - Inverts the natural ownership (host owns its config).
- **Cost:** **Medium-Large.** Touches: [configmodule/](src/otto/configmodule/), every transport/runner site that reads `*_options`.

---

## Approach E — Post-load mutation pass

Hosts are built today's way. After `load_lab()` returns, `cli/main.py` walks each host and replaces each `*_options` instance with one whose missing-from-host fields are filled from defaults.

- **Merge happens:** end-of-load, in dataclass-space.
- **Last-repo-wins:** the post-pass walks repos in order.
- **Host override:** *Hard.* Once values are collapsed into a dataclass that has its own per-field defaults, you cannot tell "the user wrote `port=22`" apart from "the user wrote nothing and the dataclass defaulted to `port=22`." You'd have to retain the raw dicts anyway, at which point Approach A is strictly simpler.
- **Pros:** Final host object is fully self-describing.
- **Cons:** The "what did the user actually set?" problem makes this strictly worse than A; same plumbing cost without the benefit; mutation conflicts with `slots=True`'s immutable feel.
- **Cost:** **Medium.**
- **Verdict:** Not recommended.

---

## Comparison Summary

| Aspect                              | A (Loader merge)             | B (Factory merge)            | C (Lazy property)       | D (Resolver)            | E (Post-pass) |
| ----------------------------------- | ---------------------------- | ---------------------------- | ----------------------- | ----------------------- | ------------- |
| Cost                                | Small per backend            | **Small (one chokepoint)**   | Med-Large               | Med-Large               | Medium        |
| Works for all backends for free?    | **No (each must implement)** | **Yes (factory is shared)**  | Yes (post-construction) | Yes (post-construction) | Yes           |
| Touches `RemoteHost`?               | No                           | No                           | **Yes**                 | No                      | No            |
| Preserves user-intent vs effective? | No                           | No                           | **Yes**                 | **Yes**                 | No            |
| Per-host repo context?              | No (global order)            | No (global order)            | Possible                | **Yes**                 | No            |
| Ad-hoc usage still works?           | Yes (no defaults)            | Yes (no defaults)            | Yes (no defaults)       | Yes (no defaults)       | Yes           |
| Backward compatible?                | Yes                          | **Yes (param defaults None)**| No (field→property)     | Yes                     | Yes           |
| Best for future runtime overrides?  | No                           | Some                         | Some                    | **Yes**                 | No            |

**Recommendation if a single approach must be chosen:** **Approach B**, and the multi-backend reality strengthens the case decisively. It places the merge at the one place all current and future backends already converge (`create_host_from_dict()`), so a future SQL/REST backend gets repo-default support for free without re-implementing or re-testing it. Approach A would force every new backend to redo the same merge logic, with the risk of subtle drift. C and D are architecturally richer but only justified if richer requirements (per-context resolution, runtime overrides, deep diagnostics) are coming soon.

---

## Critical files for implementation (Approach B)

- [src/otto/configmodule/repo.py](src/otto/configmodule/repo.py) — parse `host_defaults` from TOML
- [src/otto/storage/factory.py](src/otto/storage/factory.py) — merge chokepoint
- [src/otto/storage/protocol.py](src/otto/storage/protocol.py) — add `defaults` to `load_lab()` signature
- [src/otto/storage/json_repository.py](src/otto/storage/json_repository.py) — accept + forward defaults
- [src/otto/cli/main.py](src/otto/cli/main.py) — reduce repos in order, thread defaults to lab loading
- [src/otto/configmodule/completion_cache.py](src/otto/configmodule/completion_cache.py) — direct factory caller (decide whether to plumb)
- [src/otto/host/options.py](src/otto/host/options.py) — option dataclasses (read-only reference)
- Tests: a new fixture under `tests/repo_with_defaults/.otto/settings.toml` plus reuse of `tests/lab_data/tech1/hosts.json`
