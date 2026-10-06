# Registries that cannot diverge — design (spec 3a of the #590 contract-first series)

**Status:** v6, **approved by the owner 2026-10-06**. He pre-approved it on the condition that no
open question remained after Astra and Fable. Astra was READY on v5 and Fable READY on v6, and
neither left a question for the owner. **Date:** 2026-10-06.

**Terms used below.**
- **Registry:** a named table of extension entries, built on the `otto.registry` engine.
- **Wrapper:** the public `register_*` function that adds to one registry.
- **Record:** the frozen value a registry stores for one name.
- **Capability:** one of the closed set of sanctioned deviations from the default behaviour.
- **Backend seam:** a registry whose entries are built from config or a runtime context through
  `factory(ctx)`.
- **Eager field / `Ref` field:** a record field holding a real value, or holding a `Ref` (a lazy
  reference to a module attribute) that is imported on first read.
- **Walk shape:** an instruction's settings that decide how it runs across hosts and repos (`walk`,
  `continue_on_failure`, `require_dependencies`, ...; `instructions.py:258-262`).
- **Paths.** `src/otto/` is left off source citations. Some short names, in full:
  - `completion_cache.py` and `cache_sections.py` are under `config/`;
  - `snmp.py` and `parsers.py` are under `monitor/`;
  - `transfer/registry.py` is `host/transfer/registry.py`;
  - `os-profiles.md` is under `docs/configuration/`;
  - `registries.md`, `extension-points.md`, `execution.md` and `data-boundary.md` are under
    `docs/architecture/subsystems/`;
  - the other `.md` pages are under `docs/cookbook/`.

**Where this comes from.**
- Spec 3 of #590 ("spec validation and the registry catalog") was split by the owner into:
  - **3a** (registries: how registration works and how backends are built);
  - **3b** (host construction and spec validation).
- #590's 2026-10-05 comment added the registry work to spec 3:
  - one duplicate rule;
  - one construction contract;
  - complete payloads on direct registration.
- On 2026-10-06 the owner asked for registries whose rules are **enforced by structure, not by
  policy**, with this goal: "registries that no longer diverge, while still allowing slight
  adjustments based on specific needs. But the default behavior should be used in almost all
  cases with deviations justified in comments."
- Astra (Codex) and Fable consulted on the approach. Both then reviewed each design section,
  cross-reviewed each other's findings, and settled them. The owner decided what they left open
  (§0).
- The Opus review of v1 (two blockers, seven majors) is folded into v2. The rulings it needed are
  marked **(v2 ruling)**.
- Astra's review of v2 (six majors) is folded into v3, and its re-check (three majors) into v4.
  Their rulings are marked **(v3 ruling)** and **(v4 ruling)**.
- Astra's re-check of v4 (one major, deferred to #606) is folded into v5, and Fable's final review
  (three majors, no owner question) into v6, marked **(v6 ruling)**.

**Evidence.**
- Read-only surveys and reviews of src at `d0839893`; src is unchanged since.
- Counts state the command that produced them.

**Amends** spec 1 (`2026-10-04-public-api-manifest-design.md`) and its appendix. It settles the
pending seam that spec 1 §7 assigns to "a registration-semantics spec (Q3)". §12 lists every
amendment.

**Series.** Spec 3a follows S-1, like specs 2 and 4:
- public-path changes land in P1;
- contract changes land after P1 as marked commits, judged by the live API dump.

## 0. Owner decisions

| # | Decision | Date |
|---|---|---|
| G-1 | **A taken name raises unless `overwrite=True` is passed.** This applies to every wrapper, including `@instruction` and CLI commands, and to a direct `.register()`. A registration that writes more than one structure checks every collision before it writes anything. Ordered lists with no names stay ordered subscriptions. | 2026-10-05 |
| G-2 | **A direct `.register()` carries a complete payload.** | 2026-10-05 |
| G-3 | **One construction contract:** `factory(ctx)` with a typed context per seam. **Scope:** only the seams that take config or context: term, transfer, lab sources, inventory, creds, reservations, power controllers. **Also:** config parsing moves with the registration; the `"json"`/`"none"` name branches go; inventory's cache exemption becomes declared metadata. No-argument classes and function-valued seams keep their call shapes. | 2026-10-05, scoped 2026-10-06 |
| G-4 | **Scope.** Spec 3 splits into 3a and 3b. #601 (embedded hosts bypass the term registry) is fixed in 3a, test first, as its first product commit. #600 (the term-backend ABC) stays separate. | 2026-10-06 |
| G-5 | **Structural, not policy.** The rules live in the engine and in typed records. Wrappers only build records. | 2026-10-06 |
| G-6 | **`Ref` lives only inside a record.** A record is always eager; any of its implementation fields may be a `Ref`. A bare `Ref` passed to `.register()` is refused. | 2026-10-06 |
| G-7 | **A deviation is justified where it is declared:** `Justified(capability, reason=...)` in code, plus an entry in a shrink-only approved list in the tests. | 2026-10-06 |
| G-8 | **`INSTRUCTIONS` and `PROJECT_INSTRUCTIONS` become read-only views.** Their writable sources are `PROJECT_ACTIONS` entries that carry their bodies, plus a new `STANDALONE_INSTRUCTIONS`. | 2026-10-06 |
| G-9 | **Lab-source backend config is validated after init registration.** Settings parse validates only the envelope. | 2026-10-06 |
| G-10 | **OS-profile precedence:** an explicit code profile wins, then the selected repos' `[os_profiles]` data, then the host class's own profile. | 2026-10-06 |
| G-11 | **Same `[os_profiles]` name in two repos: the later repo in `OTTO_SUT_DIRS` order wins**, as today. #606 (backlog, P2) tracks refusing unequal tables. | 2026-10-06 |
| G-12 | **The cross-repo options-clash check for project instructions stays a post-loop bootstrap step.** It is not part of the per-registration constraint. | 2026-10-06 |
| G-13 | **No first-declarer rule for removal.** The views recompute from their sources. | 2026-10-06 |

**Rulings the owner saw and did not change:**
- every registry has a revision counter, and derived caches key on it;
- CLI commands keep `overwrite`;
- force-exit hooks are out of 3a, because they are a runtime lifecycle API;
- there is no separate catalog module: `otto.registry.instances()` plus a source scan is the
  catalog;
- the reservation half-ported and stale-method diagnostics are removed;
- the docs teach the `register_*` wrappers;
- wrappers mark themselves as transparent, and the engine credits the first frame outside that
  marked chain (§2.4);
- the three public retirements in §8.1 land in P1.

## 1. What exists today

**The engine.** `otto.registry` (`Registry[T]`, `Ref`, `RegistrationRefused`, `registering_repo`)
is a leaf: tach gives it `depends_on = ["otto.errors"]`.
- `register` runs the test-load refusal, then the collision check, then `validate`, then the write
  (`registry.py:205-237`).
- A `Ref` names a module attribute; `get` imports it on first read (`:61-93, 256-268`).
- `Ref.resolve` turns the test-load refusal off while it imports, on purpose (`:75-81`).
- The test-support restore skips refusal, collision and `validate`, also on purpose (`:328-342`).

**Wrappers diverge.** The engine has one duplicate rule; the wrappers do not. Counts are from an
AST scan of every `.register(` call in `src/otto` outside the engine: 49 calls.
- **16** pass `overwrite=overwrite` and raise on a duplicate.
- **4** always overwrite:
  - host class (`os_profile.py:255`);
  - OS profile (`:258`, `:435-446`);
  - SNMP metric (`monitor/snmp.py:141`);
  - exact-id host parsers (`monitor/parsers.py:631`).
- These raise with no way to overwrite:
  - CLI commands, project actions, compose adapters, options, project parsers, pattern parsers;
  - `@instruction`.
- **Misleading messages.** Two of those still say "Pass overwrite=True" (`PROJECT_PARSERS`,
  `HOST_PATTERN_PARSERS`), and so does `@instruction`'s.
- **Caller-supplied origins.** **36** calls pass `origin=`; 11 of them pass a module other than the
  caller's.
- **Collision hints.** **3** registries pass a `collision_hint`: `cli/registry.py:82`,
  `project/actions.py:614`, `docker/adapter.py:40`.

**Two registrations write several structures with no rollback.**
- `register_host_class` writes `HOST_CLASSES`, then the plain dict `_HOST_SPECS`, then
  `OS_PROFILES` (`os_profile.py:251-260`, "no rollback").
- `register_project_actions` writes its class before checking its bodies
  (`project/actions.py:708-709`).

**Settings write profiles too.** `Repo._register_os_profiles` registers data profiles on every
parse and relies on last-writer-wins (`config/repo.py:688-704`). Its `check_os_profile` call needs
the profile's base class already registered, so a data profile over a custom host class fails at
parse today.

**Backend construction branches on the name.** There are 12 `"json"`/`"none"` comparisons
(`grep -rnE '(backend|backend_name|name)\s*[!=]=\s*"(json|none)"' src/otto`). They drive
validation, caching and construction:
- `labs/sources.py:90,140`;
- `inventory/config.py:105,159,218`;
- `creds/config.py:56,95`;
- `reservations/factory.py:168,170`;
- `completion_cache.py:484`, `cli/cache.py:333`, `init/areas.py:81`.

A thirteenth site, `reservations/factory.py:148`, assigns `"none"` as a default.

**Each seam wraps construction errors its own way.**

**#601.** Embedded hosts call `ConnectionManager(...)` directly (`embedded_host.py:266`). A
registered term replacement never reaches them.

**Test isolation scans `sys.modules`** for `Registry` objects (`tests/conftest.py:2135-2163`).
That is why:
- `KindRegistry` subclasses `Registry` (`declared.py:277-281`);
- `_HOST_SPECS` and the provider lists need hand-written snapshots.

## 2. The engine (`otto.registry`)

The engine stays a leaf: it imports nothing but `otto.errors`. It never imports pydantic; where it
must handle a pydantic model it duck-types the model (§2.3).

### 2.1 Surface

```python
@final
class Registry(Generic[E]):
    def __init__(self, kind: str, *, entry: type[E], register_hint: str,
                 validate: EntryCheck[E] | None = None,
                 check_resolved: ResolvedCheck[E] | None = None,
                 capabilities: Sequence[Justified[Capability]] = ()) -> None: ...
    def register(self, name: str, entry: E, *, overwrite: bool = False) -> None: ...
    def register_many(self, entries: Sequence[tuple[str, E]], *, overwrite: bool = False) -> None: ...
    def unregister(self, name: str) -> None: ...
    def get(self, name: str) -> E: ...           # resolves Ref fields once, checks, caches
    def find(self, name: str) -> E | None: ...   # None only for an absent name
    def peek(self, name: str) -> E: ...          # the stored record, never resolved
    def raw_items(self) -> list[tuple[str, E]]: ...   # never resolves
    def items(self) -> list[tuple[str, E]]: ...  # resolves
    def names(self) -> list[str]: ...; def __contains__; def __len__
    def origin(self, name: str) -> str: ...; def repo(self, name: str) -> str | None: ...
    @property
    def revision(self) -> int: ...

EntryCheck = Callable[[str, E, "Proposed[E]"], None]   # eager; Ref fields still unresolved
ResolvedCheck = Callable[[str, E], None]               # deferred; runs on the resolved record

def instances() -> list[Registry | BackendRegistry | Subscription | RegistryView]: ...  # test support
```

The module function `instances()` replaces today's `Registry.instances()` classmethod
(`registry.py:201`), which is removed.

**`Proposed[E]`** is a read-only mapping. It holds the stored entries with the batch applied: the
batch's names added, and the names it overwrites replaced. `validate` reads it to check rules that
span entries. §3.3's instruction constraint is one such rule.

**Other engine types:**
- **`Subscription[T]`** is an ordered collection with `subscribe(value) -> Token`,
  `Token.cancel()` and `items()`. It records the origin and repo of each subscription and applies
  the test-load refusal. Repeated values stay repeated occurrences. Its users are the product
  providers and the dev-tool providers.
- **`RegistryView[E]`** is a read-only table derived from source registries. It offers `names`,
  `get`, `find`, `items`, `origin`, `repo`, `__contains__`, `__len__` and `revision`.
  - Its `revision` is a token over its sources' revisions, and its cache is keyed by that token.
  - Its only users are `INSTRUCTIONS` and `PROJECT_INSTRUCTIONS` (§3.3).
- **`BackendRegistry`** is owned by the engine and contains a `Registry`; it does not subclass one
  (§4.1).
- **`FrozenMap[K, V]`** is an immutable `Mapping`, hashable when its values are, and built from
  any mapping (§2.3).
- **Errors:**
  - `RegistrationRefused` is unchanged;
  - `DuplicateRegistration(OttoError, ValueError)` and `IncompleteRegistration(OttoError,
    ValueError)` are new, so code that catches `ValueError` today keeps working.

**Constraints on the types:**
- `Registry`, `BackendRegistry`, `Subscription` and `RegistryView` are `@final`, so no domain
  module can subclass them. Being `@final`, their methods need no separate `@final`.
- `instances()` lists every engine-built table **at the top level**, in one engine-wide list. A
  `BackendRegistry`'s inner `Registry` is not listed separately: it is owned, and its
  `BackendRegistry` reports it.

### 2.2 One private executor, one step matrix

Every change to stored state goes through one private `_commit`. Each operation runs the steps
marked for it, in the order shown. **(v2 ruling)**, adopting the Opus review's matrix:

| Step | register / register_many | unregister | test restore | publish (from `get`) | `subscribe` | `Token.cancel` |
|---|---|---|---|---|---|---|
| 1. attribution (§2.4) | captured | — | the snapshot's | the entry's, kept | captured | — |
| 2. test-load refusal | yes | no | no | no | yes | no |
| 3. nested-write guard | yes | yes | no | exempt | yes | yes |
| 4. capabilities (§2.5) | yes | no | no | no | no | no |
| 5. record type and freeze (§2.3) | yes | — | no | resolved fields only | no | — |
| 6. collisions | yes | — | no | no | no (repeats allowed) | — |
| 7. checks | `validate` | no | no | `check_resolved` | no | no |
| 8. write; revision bump | once | once | once | **no** | once | once; a second cancel of one token is a no-op with no bump |
| 9. entry generation bump | yes | yes | yes | **no** | — | — |

Restore covers subscriptions too. It reinstalls the snapshot's occurrences, origins, repos and
order exactly. A token issued after the snapshot cancels nothing once the snapshot is restored.

**What the steps mean:**
- **Step 2 runs first among the checks**, before `validate` or any capability. No registry can
  disable it.
- **The nested-write guard (step 3)** is engine-wide. While any registry's `validate` or
  `check_resolved` runs, a register, register_many, unregister, subscribe or cancel on *any*
  table is refused.
  - Reads stay allowed, including a `get` that resolves and publishes. A publish replaces a `Ref`
    with the value it names, so it changes no observable entry.
- **Collisions (step 6).** A name already stored, or repeated within one batch, raises
  `DuplicateRegistration` naming both origins.
  - `overwrite=True` covers a stored name.
  - It never covers a repeat within one batch.
- **Revision and generation (steps 8–9).** A publish bumps neither.
  - The **revision** counts changes to what a registry holds. A derived cache keys on it.
  - The **entry generation** identifies one registration of one name. A `Prepared` (§4.1) records
    it.
  - A publish is a read made faster, so it invalidates no cache and no `Prepared`.
  - A publish writes only if the entry's generation still matches the one its `get` read.
    Otherwise it drops the resolved value and returns it uncached.
- **Restore** reinstalls the snapshot's entries, origins, repos and order exactly. It runs no
  check: a check may depend on state the test changed, and a check that raised mid-restore would
  leave every later registry unrestored.

**Atomicity:**
- A failed register or register_many changes nothing: not the entries, the origins, the order nor
  the revision.
- **An eager record is atomic.** An invalid overwrite leaves the old entry in place.
- **A record with a `Ref` field commits with the `Ref`.** What it still needs is checked at the
  first `get`, by `check_resolved`. If resolution or the check fails, `get` raises and caches
  nothing, and a retry is possible. The old entry is not restored.

**Reads:**
- **`get`** resolves each `Ref` field, runs `check_resolved`, then publishes.
- **A `Ref` import runs with the test-load refusal suspended**, as today (`registry.py:75-81`).
  Registrations that the imported module makes are credited to that module.
  - The nested-write guard still applies. A `Ref` whose module registers at import, first resolved
    inside a `validate`, is refused and leaves nothing cached. Its next `get` retries.
  - This is right, and must not be special-cased.
- **`peek`, `raw_items`, listing, membership, `origin`, the collision check and snapshots** never
  import.

### 2.3 The record contract

**(v2 ruling)**

- **What counts as a record.** The registry's declared `entry` type, which is one of:
  - a frozen dataclass;
  - a frozen pydantic model, recognised by duck-typing: `type(r).model_config.get("frozen") is
    True`, with fields enumerated through `type(r).model_fields`.

  Anything else raises `IncompleteRegistration`. A bare `Ref` does too, with a message that names
  the fix: put the `Ref` in a record field.
- **The freeze walk** visits each field value. Inside a `tuple`, a `frozenset`, a `FrozenMap`, a
  dataclass or a pydantic model it recurses. It stops at a class, a callable, a `Ref`, a compiled
  pattern, and any other object, which it treats as opaque.

  These raise `IncompleteRegistration`:
  - a `list`, `dict`, `set` or `bytearray` found anywhere on that walk;
  - a dataclass or pydantic model met on the walk that is not itself frozen.

  Wrappers copy their inputs into frozen forms.
- **JSON-shaped data** uses `FrozenMap.freeze_json(value)` and `thaw_json()`:
  - `freeze_json` turns lists into tuples and dicts into `FrozenMap`s;
  - `thaw_json` turns them back.

  The round trip is exact for data that came from JSON or TOML, which has no tuples. This is how
  profile defaults hold `busybox`'s `valid_transfers` list (§3.4). The host factory merges
  `defaults.thaw_json()`.
- **`validate` (an `EntryCheck`)** runs at registration. It receives the name, the record with its
  `Ref` fields unresolved, and the `Proposed` table. It checks the key, every eager field, and any
  rule that spans entries.
  - It must not resolve the record's own `Ref` fields; `check_resolved` checks those.
  - It may read *other* entries, including with `get` (§2.2, nested-write guard).
- **`check_resolved` (a `ResolvedCheck`)** is the runtime contract for `Ref` fields. It runs at the
  first `get`, on the resolved record. Today's per-registry validators move here for the `Ref`
  case: `type_name` for frames, `host_families` for term and transfer.

  The rule is structural: a registry with no `check_resolved` refuses a record holding a `Ref`
  (`IncompleteRegistration`). So no lazy field ever goes unchecked.
- **A validator that applies to both forms** is called by both hooks: by `validate` when the field
  holds a class, and by `check_resolved` when it held a `Ref`. One function, two call sites.
- **Undefined-looking names, defined:**
  - `OptionVerb = Literal["run", "test"]`, the typed form of `OPTION_VERBS` (`params.py:322`);
  - `ProjectParserEntry(parser: MetricParser)`. A parser instance is opaque to the freeze walk.

### 2.4 Attribution

The `origin=` parameter goes. A caller can no longer supply its own origin.

**How the engine captures it:**
- A direct `.register()` credits its immediate caller.
- Each `register_*` wrapper and registering decorator carries a private engine marker that makes
  it a transparent registration boundary. The engine credits the first frame outside that marked
  chain.
- The marker is internal. A third-party helper that wraps a wrapper is credited as the registrant.
  The docs say so.
- Records carry no origin field. `CommandSpec.origin` (`cli/registry.py:73`) is deleted (§3.2).

**Why these rules:**
- **Not "the nearest frame outside otto".** When a test first imports an otto module, that walk
  reaches the test's frame. otto's own built-ins would then be credited to the test and refused.
- **Not the implementation's `__module__`.** That is wrong when a user registers a class imported
  from elsewhere. The record still holds the implementation (the class, or the `Ref` target), so
  provenance stays available for display.

**What reads the origin:**
- the test-load refusal (`registry.py:152`);
- the completion cache's built-in classifier, which today reads `spec.origin`
  (`completion_cache.py:1481`) and will read `CLI_COMMANDS.origin(name)`;
- the isolation fixture's eviction rule (`tests/conftest.py:2408-2420`).

`@cli_command`, `@options`, `@instruction`, the compose-adapter decorator and
`register_project_actions` all register on behalf of user code. Their attribution equals the user
module, and the conformance suite asserts it (§7).

**The repo** comes from `get_registering_repo()` at the write. It replaces the per-seam repo
plumbing:
- `OptionsEntry.repo` (`params.py:360`);
- the `(provider, repo)` storage (`host/product.py:671, 709`);
- `InstructionEntry.registered_by` (§3.3).

### 2.5 Deviations

**The default needs no declaration.** A registry built with no `capabilities=` follows every rule
above.

**A deviation is a member of a closed set** of `Capability` types defined in the engine. It is
declared at the registry's definition, with its reason:

```python
PROJECT_ACTIONS = Registry("project actions", entry=ProjectActionsEntry, register_hint=...,
    capabilities=[Justified(RequireRepo(), reason="one actions class per repo; the repo is the key")])
```

**The set has one permanent member, `RequireRepo`.** It refuses a register or register_many made
while `get_registering_repo()` is `None`, that is, outside a repo's init import. It does not apply
to unregister, restore or publish (§2.2). `PROJECT_ACTIONS` and `COMPOSE_ADAPTERS` use it. Adding a capability type is a change to
the engine.

**No escape hatches.** None of these exists: a duplicate policy, an `on_register` or
`before_register` hook, skipping validation, or opting out of the test-load refusal.

**A seam's schema is not a deviation.** Its record type, key rule, metadata type and checks define
the seam. Key rules that a raw `.register()` must also meet live in `validate`:
- project actions' key equals the registering repo;
- compose adapters are keyed `repo:use_case`, and the use case may not contain `:`;
- options are keyed by the defining `module:qualname`, and a string path to a re-export is refused;
- SNMP metrics are keyed by OID;
- project parsers are keyed by command.

**The approved list** lives in `tests/unit/registry/approved_capabilities.py`. It maps each
registry to its capabilities, their parameters and their reasons. It is shrink-only in the same
way as the cycle ratchet's `BASELINE` (`tests/unit/test_import_cycle_ratchet.py`):
- the test fails on any difference from the live declarations, and on a blank reason;
- the file header says entries are only removed;
- an addition is a visible diff that review must accept.

The last point is policy, and §7 says so.

## 3. A record per seam

### 3.1 Default seams

These seams change only their shape. Each stores a frozen record and gains `overwrite=False`.

| Seam(s) | Record | Notes |
|---|---|---|
| frame, loader, filesystem, impairer, carrier | `ClassEntry[T](cls: type[T] \| Ref)` | Wrappers accept `type \| Ref`. Each existing validator (`type_name`, `host_families`, ...) runs at registration for a class and at the first `get` for a `Ref` (§2.3). |
| login proxy | `LoginProxy(fn, undo, prompt)` | Exists today. |
| session setup | `SessionSetupEntry(fn: SessionSetup \| Ref)` | |
| product and dev-tool kinds | `KindEntry(factory: KindFactory \| Ref)` | `KindFactory` is the seam's parameterized callable type. `KindRegistry` becomes a `KindBuilder` that is *given* a module-level `Registry` (`PRODUCT_KINDS = Registry(...)`; `KindBuilder(PRODUCT_KINDS, ...)`). Variant matching, the class resolver and stamping stay domain logic. |
| SNMP metrics | `SnmpMetric` | Already a frozen pydantic model, so it is a record as is. |
| exact-id host parsers | `HostParsersEntry(parsers: FrozenMap[str, MetricParser])` | |
| pattern host parsers | `PatternParsersEntry(pattern: re.Pattern[str], parsers: FrozenMap[...])` | Keyed by a canonical `flags:pattern`; today's key leaves out the regex flags. The precedence rule (exact id, then pattern, then project, then defaults) and the ambiguity error stay in the read resolver. |
| project parsers | `ProjectParserEntry(parser)`, one per command | `register_parsers` uses `register_many`, so a duplicate at index 2 leaves indexes 0 and 1 unwritten. |
| options | `OptionsEntry(target: type \| Ref, verbs: tuple[OptionVerb, ...])` | The `OptionsRegistrationError` pre-check goes; a duplicate raises `DuplicateRegistration`. The verb errors stay. |
| compose adapters | `ComposeAdapterEntry(use_case: str, fn)` | `RequireRepo`. |
| product and dev-tool providers | `Subscription` | `registered_product_providers()` and `registered_dev_tool_providers()` keep returning `list[tuple[provider, repo]]`, built from the subscription. Only the storage changes. |

**`collision_hint` goes.** The CLI's "pick a unique name" and the options' "name every verb in one
call" become the uniform `DuplicateRegistration` message. A wrapper that re-raises with its own
text is exactly the divergence this spec forbids.

**Four documented behaviours change**, each now needing `overwrite=True`:
- `register_snmp_metric`, to re-teach an OID;
- `register_host_parsers`, to re-register an exact id;
- `register_host_class`, to re-register a name;
- `register_os_profile`, to re-register a name. `custom-host-classes.md:29-34` teaches re-calling
  it to patch a library's profile; that teaching becomes `overwrite=True`.

### 3.2 CLI commands

- **Signature.** `register_cli_command(..., overwrite=False)`; `origin=` goes. `CommandSpec` is the
  record, and its `origin` field is deleted (§2.4).
- **Cache.** `RegistryBackedGroup` caches converted Typer commands and never invalidates the cache
  (`cli/invoke.py:2261-2277`). The cache becomes keyed by the revision of the registry or view
  behind each group:
  - `CLI_COMMANDS` for the root group;
  - `INSTRUCTIONS` for `otto run` (`cli/run.py:155-161`).

  So a replaced command is never served stale.
- **Why the 2026-07-01 rule no longer applies.** That CLI rule ("no `overwrite` for commands")
  kept `--help` from depending on import order. Keying the cache by revision removes the
  stale-command half of that risk. Two repos that both pass `overwrite=True` for one name still
  produce an order-dependent result; that is the explicit choice the duplicate rule asks for.

### 3.3 Instructions and project actions (G-8, G-12, G-13)

**Writable sources:**
- **`STANDALONE_INSTRUCTIONS: Registry[InstructionEntry]`**, written by `@instruction`, which gains
  `overwrite=False`.
  - Its `validate` keeps the `FIRST_PARTY_INSTRUCTIONS` guard (`instructions.py:74-88, 508-517`).
    So a process that never imported `otto.project.actions` still refuses a repo claiming
    `install`, with today's message.
- **`PROJECT_ACTIONS: Registry[ProjectActionsEntry(cls, bodies)]`**, with `RequireRepo`.
  - `register_project_actions` builds each body record from the class's marked methods. It fills
    in any setting the decorator left out, so every body carries its complete walk shape.
  - A body's options class is held as the class, as today.
- **otto's own `ProjectActions` bodies** become a module constant, not a registration. Today they
  are registered when the module is imported (`project/actions.py:675`).

**One constraint check runs before either source commits.** It is the `validate` of both sources.
It reads three things:
- the `Proposed` table of the source being written (§2.1);
- the other source as committed;
- the first-party constant.

It refuses:
- a walk shape different from the one the first declarer fixed. Declaration order is otto's
  constant first, then `PROJECT_ACTIONS` in registration order, which is bootstrap's dependency
  order;
- an override of a first-party instruction whose options class does not inherit the first-party
  one (`instructions.py:262-269`);
- a standalone name that collides with a project name, in either registration order;
- two bodies for one owner and name.

Removal is not checked. No product code removes a contribution, and the views recompute (G-13).

**Derived views:**
- `PROJECT_INSTRUCTIONS` derives from the constant plus `PROJECT_ACTIONS`.
- `INSTRUCTIONS` derives from that plus `STANDALONE_INSTRUCTIONS`.
- A project instruction's entry in `INSTRUCTIONS` keeps today's behaviour in two ways:
  - its `module` is the first declarer's module, which `--list-instructions` uses to choose a panel
    (`cli/run.py:188`; `config/repo.py:594-603`);
  - its repo is `None`, so the dispatch gate never refuses it (`project/commands.py:48-52`).
- A standalone entry carries the engine's origin and repo.
- Bodies keep `PROJECT_ACTIONS` order, which `overwrite` does not change.

**`InstructionEntry.registered_by` is deleted.** Its readers switch to `INSTRUCTIONS.repo(name)`:
- `run_instruction` (`instructions.py:645`), which passes it to `check_instruction_active`, whose
  signature does not change;
- the dispatch gate (`cli/invoke.py:1144`);
- the options lookup (`cli/run.py:149`);
- the collision message (`instructions.py:235`), which moves into the constraint check.

**The cross-repo options-clash check stays a post-loop bootstrap step (G-12).**
`check_project_instruction_options()`:
- runs once after the init loop, uncontained, where today's publish runs (`bootstrap.py:321-330`);
- holds the body of today's `merged_option_params` loop (`project/commands.py:66-70`);
- on a clash, stops otto with an error naming both repos.

**Removed:**
- `publish_project_instructions` and its republish overwrite (`project/commands.py:45-82`);
- `_already_registered` (`project/actions.py:621-634`).

Re-registering the same class is a `DuplicateRegistration` unless `overwrite=True` is passed.

### 3.4 Host classes and OS profiles (G-10, G-11)

**Host classes.** `HOST_CLASSES: Registry[HostClassEntry]`:

```python
@dataclass(frozen=True)
class ProfileFields:
    defaults: FrozenMap[str, object] = FrozenMap()   # JSON-shaped, via freeze_json
    login_prompt: str | None = None
    password_prompt: str | None = None

@dataclass(frozen=True)
class HostClassEntry:
    cls: type[RemoteHost] | Ref
    spec: type[HostSpec] | Ref
    profile: ProfileFields = ProfileFields()

def register_host_class(name, cls, *, spec=None, profile=None, overwrite=False) -> None: ...
```

- `spec=None` still means the nearest registered spec in the class hierarchy. The wrapper looks
  it up before it builds the record, so the lookup never runs inside `validate`.
- `_HOST_SPECS` and the profile write are deleted.

**Explicit profiles.** `OS_PROFILES: Registry[OsProfile]` holds the profiles registered by
extension code. `OsProfile(name, base, fields: ProfileFields)`, and
`register_os_profile(name, base, defaults=None, *, login_prompt=None, password_prompt=None,
overwrite=False)`.

**otto's built-in profiles rank with the host classes, at the lowest layer (v2 ruling):**
- `unix`'s, `embedded`'s and `zephyr`'s profiles live in their `HostClassEntry`. `unix`'s carries
  the getty prompts.
- `busybox` names no class of its own. It lives in a built-in constant beside the host classes,
  `BUILTIN_PROFILES: FrozenMap[str, OsProfile]`, at the same layer.
- So a repo's `[os_profiles.unix]` or `[os_profiles.busybox]` table still replaces the built-in,
  as it does today (built-ins register at import, data at parse).
- An extension's `register_os_profile("unix", ...)` also replaces it, and still logs the
  "overriding built-in" warning.
- **Shadowing a lower layer is not a collision.** It needs no `overwrite=True`, and the wrapper
  makes no layer-aware pre-check. A second `OS_PROFILES` entry under the same name is a
  collision.

**Repo data profiles are never registrations:**
- Settings compile each `[os_profiles.*]` table into the `Repo`. The table's shape (a `base`
  string, a table of defaults) is checked at parse.
- **Their meaning is checked after init (v2 ruling):**
  - is `base` a registered host class;
  - is every defaults key a field of that class. From spec 3b's commit 3b-2 on, the vocabulary is
    the class's spec's profile-eligible inputs instead (`2026-10-06-host-construction-design.md`
    §4.5).

  Two places run this check: a post-loop bootstrap step, `check_data_profiles()`, next to G-12's;
  and the resolver, whenever it selects a data profile. So a data profile over a custom host class
  now works, and a typo is still reported loudly, naming the repo.
- Same-name tables in two repos: the later repo in `OTTO_SUT_DIRS` order wins (G-11, #606).
- This deletes `Repo._register_os_profiles` and its write at parse time.

**One resolver** **(v2 ruling)**: `resolve_os_profile(name, *, data: ProfileContext) ->
OsProfile`.
- `ProfileContext` is the selected repos' compiled data profiles, keyed by name and already merged
  in `OTTO_SUT_DIRS` order. `ProfileContext.empty()` has none.
- **The answer is a whole-profile first hit, as today.** The resolver returns the first of these
  that has the name:
  1. `OS_PROFILES` (an explicit code profile);
  2. `data`;
  3. the host class's own profile, or `BUILTIN_PROFILES`.

  Fields never merge across layers. A data table named after a host class whose profile carries
  prompts replaces the whole profile, prompts included. That matches today: data tables cannot
  carry prompts (`os-profiles.md:21-27`), and a console host under such a table needs its own
  `console_options`.
- `build_os_profile(name)` and `get_os_profile(name)` keep their paths and become
  `resolve_os_profile` with an optional `data=` (default: empty), raising or returning `None` as
  today. `registered_profile_names(data=...)` lists all three layers.

**The data reaches every reader explicitly (v3 ruling).** Nothing reads bootstrap state, and
`otto.host` does not import `otto.bootstrap`. The route runs through the lab-source environment,
so the `LabRepository` protocol does not change.
- **Merging (v4 ruling).** The repos that own the sources and the repos whose profiles apply are
  two separate inputs: `build_lab_sources(repos, *, profiles=None)`.
  - `profiles` is a `ProfileContext`: the *selected* repos' tables, merged in `OTTO_SUT_DIRS`
    order by `ProfileContext.from_repos(selected)`.
  - When it is omitted, it defaults to `ProfileContext.from_repos(repos)`. That is right for a
    caller that passes every selected repo, as session orchestration does.
  - The context reaches every source in `LabSourceEnv.profiles` (§4.2).
- **Inside a repository.** A repository built from that env holds the context. It passes the
  context down on every path that resolves a profile. The json repository does this for:
  - `create_host_from_dict(..., profiles=)`, while loading (`labs/json_repository.py:704-707`);
  - `host_identity(..., profiles=)`, for host summaries and completion (`:525`);
  - `validate_host_dict(..., profiles=)` (`:704`);
  - link addressing (`addressing_from_dict`, called at `:395`; `link/derive.py:55`).
- **The summary route.** `CompositeLabRepository` needs nothing extra, because each child holds
  the context. So `host_summaries` (`labs/summaries.py:20-51`) and
  `SupportsHostSummaries.list_host_summaries` (`labs/protocol.py:219`) are unchanged.
- **Completion** still enumerates one repo at a time (`completion_cache.py:1767, 1815-1817`).
  - Each collector already receives every selected repo. It computes
    `ProfileContext.from_repos(repos)` once, and passes it down to `_enumerate_host_summaries`.
  - That function calls `build_lab_sources([repo], profiles=...)`. So repo B's source sees repo
    A's profiles.
  - `collect_lab_names` builds sources directly (`completion_cache.py:2136-2157`), and passes the
    same context. So lab names and host ids always come from one context.
  - The per-process summary memo (`completion_cache.py:1734-1742`) adds the context's
    `digest()` to its key.
- **A custom lab backend** reads `env.profiles` in its factory and passes it to the public helpers
  it calls. `ExampleLabRepository` does so (`examples/lab_repository.py:222`). This is part of the
  backend hard cutover in §8.2.
- **The `load_lab(search_paths=)` fallback** (`config/lab.py:316-322`) has no repos, so its env
  carries `ProfileContext.empty()`.
- **Outside a repository:**
  - `otto doctor` validates with `ProfileContext.from_repos(selected repos)`
    (`init/doctor.py:398`);
  - completion's profile list passes the same (`completion_cache.py:2218-2226`).
- **Console prompts (v6 ruling).** Today `resolve_console_prompts(options, os_type)` resolves by
  name inside `_build_connections` (`os_profile.py:628-650`; `unix_host.py:553`). That runs at
  construction and again on every `rebuild_connections` (`unix_host.py:499`). A by-name lookup
  without the data would refill the class's getty prompts under a `[os_profiles.unix]` table,
  which breaks the whole-profile rule. So:
  - the host carries the resolved `ProfileFields` in a private `init=False` field,
    `_profile_fields`, which the factory sets. Being private storage, it is outside the dump's
    tracked surface (`2026-10-05-api-dump-design.md:518-520, 623`). It is not a `HostSpec` field,
    so 3b is untouched;
  - `resolve_console_prompts(options, fields: ProfileFields | None)` fills from those fields and
    nothing else, and `rebuild_connections` reuses them;
  - a directly constructed host (no factory, field `None`) gets its fields in `_build_connections`,
    which resolves the host's `os_type` with empty data, as bare library use does. It then passes
    them to `resolve_console_prompts`.
- **Bare library use.** A caller with no repo data passes nothing. It gets code profiles plus
  class and built-in profiles.

A cross-repo case pins the route: repo A declares a data profile and repo B's lab names it. The
host must appear in host summaries, in completion, and in the loaded lab.

**Behaviour change (G-10).** Two cases change:
- **Host class registered after a same-named data table.** Today it overwrites the table with an
  empty profile (`os_profile.py:258-260`), and the user's table is lost. Under G-10 the table wins
  over the class's own profile.
- **Extension code profile versus a same-named data table.** The code profile now wins regardless
  of order, as `os-profiles.md:34-35` already claims.

## 4. Backend seams (G-3, G-9)

### 4.1 `BackendRegistry`

```python
@dataclass(frozen=True)
class Configured(Generic[C, Env]):
    config: C                      # parsed by the entry's own config model
    env: Env                       # supplied by the seam

def configured_backend(*, config: type[C] | Ref, factory: Callable[[Configured[C, Env]], T] | Ref,
                       metadata: M) -> BackendEntry[Env, T, M]: ...
def class_backend(*, cls: type[T] | Ref, metadata: M) -> BackendEntry[Env, T, M]: ...
    # context-only seams: build calls cls.create(env)

@final
class BackendRegistry(Generic[Env, T, M]):          # contains a Registry[BackendEntry[Env, T, M]]
    def __init__(self, kind: str, *, register_hint: str, error: type[Exception],
                 describe_parse_error: Callable[[Exception], str],
                 result: ResultCheck[T], validate: EntryCheck | None = None,
                 check_resolved: ResolvedCheck | None = None) -> None: ...
    # the Registry read and write surface of §2.1, delegated, plus:
    def prepare(self, name: str, raw: Mapping[str, object], env: Env) -> Prepared[Env, T, M]: ...
    def build(self, prepared: Prepared[Env, T, M]) -> T: ...

ResultCheck = Callable[[str, object], None]   # (backend name, built object); raises on a wrong result
```

**One wrapper shape for configured seams (v6 ruling).** Every configured seam's wrapper has this
signature, verbatim. It builds the entry with `configured_backend`:

```python
def register_<seam>(name: str, *, config: type[C] | Ref,
                    factory: Callable[[Configured[C, Env]], T] | Ref,
                    overwrite: bool = False) -> None: ...
```

- The five wrappers are `register_lab_repository`, `register_inventory_backend`,
  `register_creds_backend`, `register_reservation_backend` and `register_power_controller`.
  Today each takes a class (`labs/registry.py:22`, `inventory/registry.py:32`,
  `creds/registry.py:23`, `reservations/registry.py:20`, `host/power.py:141`).
- Inventory adds its one metadata keyword, `snapshot_cache: bool = True`. No other configured seam
  has static metadata.
- The factory receives `Configured`. A built-in whose class takes the config passes a one-line
  module function. For example, power registers
  `configured_backend(config=Ref("…:CommandPowerConfig"), factory=Ref("…:_command_power"), …)`,
  where `_command_power(c)` returns `CommandPowerController(c.config)`.
- Term and transfer keep today's wrapper signatures (§4.1, metadata).

**Entries:**
- `BackendEntry` is an engine-owned frozen record, built only by the two helpers.
- The config model `C` exists only inside `configured_backend`'s closure, so no public surface
  types it as `Callable[..., Any]`.

**Metadata and prepared facts are different things (v2 ruling):**
- **`metadata: M`** is static. It is declared at registration and read without importing anything.
  - Built-ins state theirs literally beside their `Ref`.
  - **Term metadata is per entry (v3 ruling).** `ssh`, `telnet` and `console` share one
    `ConnectionManager` class, but each declares its own families and `dials_host`
    (`connections.py:1287-1298`). So `register_term_backend` keeps its `host_families`,
    `authenticates` and `dials_host` arguments (`:1233-1237`). It builds the metadata from them,
    as today's `TermBackend` record does.
  - **Transfer metadata is per class.** `register_transfer_backend(name, cls)` copies the class
    attributes (`host_families`, `progress_granularity`, `authenticates`) into `metadata` at
    registration.
    - For a built-in `Ref`, the attributes are stated literally beside the `Ref`.
    - `check_resolved` asserts that the resolved class still agrees, so the two cannot drift.
- **Prepared facts** are derived from the parsed config, per source. A config model may define
  `prepared_facts(self) -> F`. Lab sources use this for `file_inputs` (§4.3). Facts are only
  available after `prepare`, which imports the config model.

**`prepare`:**
- resolves the entry's config model;
- parses `raw` once by calling `config.model_validate(raw, context={"env": env})`. This is
  duck-typed, with no pydantic import. A model anchors paths in a validator that reads
  `info.context["env"]`;
- never calls the factory;
- returns a frozen `Prepared` holding:
  - the registry's identity, the backend name, and the entry generation it was prepared against;
  - the `Env`;
  - the parsed config instance, which is what `build` hands the factory;
  - its normalized form, `model_dump(mode="json")`, which is what identity uses;
  - the typed metadata;
  - the prepared facts.

**`build`:**
- refuses a `Prepared` from another registry, or from a stale entry generation, without calling the
  factory;
- otherwise parses nothing, calls the factory once with `Configured(config, env)`, and checks the
  result with the seam's `result` check. The `config` handed over is a per-call
  `copy.deepcopy` of the parsed instance, so a factory that mutates it cannot change a later
  build from the same `Prepared` (amended by spec 3b §10).
  - A config model must therefore be deep-copyable; the extension docs for every configured seam
    say so.
  - A copy failure raises the seam's construction error with the cause chained, without calling
    the factory.

A publish bumps no generation (§2.2), so building one `Prepared` twice works. The reservation
gate's `backend_factory` does exactly that (`reservations/factory.py:261-269`).

**Context-only seams** (term and transfer) use `class_backend`:
- `raw` must be empty;
- `build` calls `cls.create(env)`;
- `Env` is `TermContext` or `TransferContext`.

A `Ref` to `Class.create` is not used, because `Ref.resolve` does a single `getattr`
(`registry.py:87-93`).

**Identity.** `same_as` and the snapshot slug use the normalized config plus the seam's own
envelope fields (inventory's `cache_ttl`). They never use a revision or a generation, so nothing
process-local enters a persistent key.

**One error pipeline:**
- Each seam declares a construction error, `XConstructionError(XError, ValueError)`, beneath its
  plain domain error `XError(OttoError)`. It passes the construction error to its registry, so the
  engine imports none of them.
- The engine raises that error naming:
  - the stage: lookup, resolution, parse, stale preparation, construction, or result;
  - the origin and the backend.

  The cause is chained.
- **A parse failure (v6 ruling)** is described by the seam's `describe_parse_error`.
  - Every seam passes a seam-local function that imports `compact_validation_error`
    (`models/base.py:39-57`) at call time. So the light `*/registry.py` modules gain no
    module-level pydantic import.
  - The engine builds the message from the stage, the origin, the backend and that string. So a
    rejected value is never echoed back, and the engine still imports no pydantic.
  - The backend conformance case asserts that a parse failure's message does not contain the
    rejected value.
- Construction catches `Exception`. Process-control exceptions pass through untouched
  (`errors.py:114-133`).
- The domain error stays a plain `OttoError`. Its runtime query failures (network, auth) do not
  become `ValueError`s that code catching `ValueError` would swallow (`host/factory.py:144-148`).

### 4.2 The seven seams

| Seam | `Env` (a light module) | Built-in config model | Static metadata | Domain / construction error | Stays in seam code |
|---|---|---|---|---|---|
| lab sources | `LabSourceEnv(repo_dir, label, origin, profiles)` | `JsonLabSourceConfig(paths)`, anchored to `repo_dir`; prepared fact `file_inputs` (§4.3) | none | `LabRepositoryError` / `LabSourceConstructionError` | label derivation, `CompositeLabRepository` |
| inventory | `InventoryEnv(anchor_dir, origin)` | `JsonInventoryConfig(path, supplies)` | `snapshot_cache: bool` (json: `False`) | `InventoryError` / `InventoryConstructionError` | the `cache_ttl` envelope; `_maybe_cached` reads `snapshot_cache`, not the name; the creds overlay (cache inside, overlay outside) |
| creds | `CredsEnv(anchor_dir, origin)` | `JsonCredsConfig(path)` | none | `CredsError` / `CredsConstructionError` | the `_resolve_creds` walk, `same_as` |
| reservations | `ReservationEnv(repo_dir, username, origin, url)` | the `[reservations.<name>]` sub-table; `none` has an empty model | none | `ReservationBackendError` / `ReservationConstructionError` | the `-R` lazy skip: `prepare` and `build` both run inside its closure (`reservations/factory.py:261-262`) |
| power | `PowerEnv(host_id)` | per controller, from the lab dict minus `type`; `CommandPowerController`'s fields move into its model | none | `PowerControlError` / `PowerConstructionError` (both new) | the host is passed per call |
| term | `TermContext` | none | `host_families`, `authenticates`, `dials_host` (wrapper arguments, per entry) | `TermBackendError` / `TermConstructionError` (both new) | — |
| transfer | `TransferContext` | none | `host_families`, `progress_granularity`, `authenticates` (class attributes) | `TransferBackendError` / `TransferConstructionError` (both new) | — |

**The public construction functions keep their paths; their contracts change (v2 ruling):**
- `build_term_backend(name, ctx)` and `build_transfer_backend(name, ctx)` return the built backend
  (prepare, then build). Today they return the class (`connections.py:1270-1277`;
  `transfer/registry.py:78-85`).
- `build_power_controller(type_name, config, *, host_id)` returns the built controller.
- `power_control_from_spec(value, *, host_id)` prepares, then builds. Its two callers pass the
  host's id (`unix_host.py:469`, `embedded_host.py:212`). `None` and an existing controller
  instance still pass through. 3b moves `prepare` into `HostSpec` validation.
- `CommandPowerController`'s constructor takes its config model:
  `CommandPowerController(config)`.
- **Inventory and creds.** `compile_inventory`, `compile_creds` and `compile_creds_table` become
  the seam's envelope parse plus `prepare`. `CompiledInventory` and `CompiledCreds` hold a
  `Prepared` in place of `backend` and `kwargs`. `construct_inventory` and
  `construct_creds_store` call `build`. These names stay public, so nothing retires in P1.
- **`get_inventory_backend_class`, `get_creds_backend_class`, `get_reservation_backend_class` and
  `get_lab_repository_class` are deleted.** The first two are public, so they retire in P1 (§8.1).
  The other two are internal.

**Reservations:**
- `url` is an envelope field. It moves into the environment, and a backend that wants it reads
  `env.url`.
- An absent `[reservations]` table selects `none` as an ordinary default, not as a branch.
- The protocol check becomes the seam's `result` check.
- The half-ported and stale-method diagnostics, their once-per-process warning, and
  `reset_half_ported_warnings` are deleted.

**The direct `JsonFileLabRepository(...)` fallback** (`config/lab.py:316-322`) goes through the
registry, with `LabSourceEnv(repo_dir=Path.cwd(), label="json", origin="load_lab(search_paths=)",
profiles=ProfileContext.empty())`.

### 4.3 Lab sources before and after init (G-9)

**Today:**
- Settings compile lab sources at discovery.
- The warm completion and root-help paths (`cli/main.py:1066-1091`) recompute each source's file
  fingerprint from `src.lab_files()` (`config/cache_sections.py:82-98, 214-224`), with no init
  import.

**At settings parse:** only the envelope is validated, into `PendingLabSource(backend, label, raw,
origin, repo_dir)`. `Repo.lab_sources` holds these.

**Preparation is lazy:**
- `prepared_lab_sources(repo, profiles)` prepares on first read and caches by
  `(repo.sut_dir, LAB_REPOSITORIES.revision, profiles)` **(v4 ruling)**. `Repo` itself is not
  hashable; the summary memo keys on `sut_dir` the same way (`completion_cache.py:1734`).
  - `ProfileContext` is frozen and hashable, so a change of selected repos with no registry change
    still prepares afresh.
  - A test pins that: it builds `[B]`, then `[A, B]`, with one revision between them.
- **The barrier (v2 ruling):** while an init import is running (`get_registering_repo()` is not
  `None`), `prepared_lab_sources` refuses with an error naming the caller. Registration is not
  complete then. Outside an init import it prepares, so library callers with hand-built repos keep
  working.
- A backend still unregistered when its source is read reports `file_inputs` as unknown, never as
  "not file-backed". Building it raises `LabSourceConstructionError`, the way an unknown name
  raises `LabRepositoryError` today (`labs/sources.py:139`).

**The warm path never recomputes the fingerprint:**
- **The writer.** The cache writer runs after init. From each source's prepared facts it computes
  the key paths (files and watched directories). It stores each one with its file metadata, the
  way the completion shim already does (`completion_tree.py:298`; `_shim_complete.py:782-792`).
- **The reader.** The warm reader validates against those stored values and never calls
  `lab_files()`.
  - Missing or incompatible stored data is a cache miss.
  - A source that is not file-backed stores its TTL class with the entry, so the reader does not
    recompute that either (`completion_cache.py:484`).
- **New files.** A new file matching a glob changes the watched directory's metadata, so it still
  invalidates the cache.

**Pre-init readers no longer branch on the name:**
- `cli/cache.py:331-336` reports "unprepared" when it cannot tell whether a source reads files.
- `init/areas.py:76-81` reports backend-dependent checks as **deferred**.

**Import cost.** The warm reader is not expected to prepare. The `completion_repo_warm`
measurement in §10 proves that no warm reader still does; a non-zero delta is a defect, not a
budget.

## 5. #601: embedded hosts build through the term registry

This is 3a's first product commit, **pinned before P1 (v2 ruling)**. P1's inventory of taught
underscore keywords to rename (dump spec §7.4) therefore no longer contains `_connection_factory`.

**The test comes first:**
- Register a recording replacement for `telnet` with `overwrite=True`, then build and rebuild an
  embedded host.
- Assert that the factory ran once per construction and received a `TermContext`, and that the
  backend it returned became the host's connections.
- Assert that:
  - the embedded telnet options still force `login=False` and `single_client_console=True`;
  - the console options still force `login=False`;
  - the console endpoint still arrives as a callable.
- Repeat for `console`.

**The fix.** It uses today's registry API, because the engine lands later:
`embedded_host.py:265-276` builds through `build_term_backend(self.term).create(TermContext(...))`,
carrying the embedded options into the context. Commit 5.8 then moves it to
`build_term_backend(name, ctx)`.

**`_connection_factory` is deleted:**
- the field (`remote_host.py:325`);
- the branches (`unix_host.py:538, 558`; `embedded_host.py:266`);
- the teaching: the docstrings at `connections.py:20, 421-439`, and
  `docs/cookbook/extending/extending-backends.md:136`.

**Test migrations:**
- Four test constructor sites register a replacement under isolation instead:
  `tests/unit/host/test_docker_host.py:105, 1986`; `test_host_backend_construction.py:57, 128`.
- `test_connection_factory_override_still_wins` is deleted.
- `tests/unit/host/test_hop.py:4` needs a docstring fix.
- The synthetic string fixtures in `tests/unit/scripts/test_api_dump_child.py` and
  `test_api_teaching.py` are untouched.

**The golden.** `_connection_factory` is an init keyword (`init=True`), not init=False storage.
- If the v2 producer records it as a constructor keyword, the commit regenerates the golden and
  carries a mark.
- Otherwise the commit regenerates the golden with no mark, and its message says which applied.

## 6. Migration: one engine, a private legacy list

The strict engine cannot land before its callers are migrated:
- `KindRegistry` subclasses `Registry`;
- registries hold classes, functions, dicts, tuples and parser instances, not records;
- records hold lists and dicts;
- 36 calls pass `origin=`;
- three registries use `collision_hint`.

**Commit 4 lands the strict engine with a private legacy mechanism:**
- **In src**, the engine accepts a private constructor keyword, `_legacy=`. It takes a set of four
  allowances:

  | Allowance | Lets a registry |
  |---|---|
  | `LegacyUntyped` | store any value. It skips the `entry=` requirement and step 5's record-type and freeze checks. Its validator keeps today's two-argument contract, `(name, value)`, and today's timing: called at registration for a value, and at the first `get` for a bare `Ref` (v3 ruling) |
  | `LegacyOrigin` | accept `origin=` |
  | `LegacySubclass` | be subclassed, for `KindRegistry`. Its constructions (`host/product.py:712`, `host/dev_tool.py:187`) stay in discovery until 5.1 removes them |
  | `LegacyCollisionHint` | keep an old `collision_hint`, which `CLI_COMMANDS`, `PROJECT_ACTIONS` and `COMPOSE_ADAPTERS` use. An always-overwrite wrapper needs no allowance: `overwrite=True` is legal, and only the ast-grep rule, which turns on at commit 6, objects |

  These are not `Capability` types, and they are not taught.
- **In the tests**, `tests/unit/registry/legacy.py` lists every registry that uses `_legacy=` and
  its allowances.
  - The test fails if the list and the live constructors differ.
  - The list only shrinks, by the same mechanism as §2.5.

**Commit 4 also switches `_isolate_registries` to `otto.registry.instances()`:**
- It covers registries, backend registries and subscriptions, and bumps the revision on restore.
- Views are not snapshotted; they follow their sources by revision.
- Today's eviction of newly imported registration modules stays (`tests/conftest.py:2379-2397`),
  and so does the handling of registries created after the snapshot (`:2429`).

**All the engine types land in commit 4:** `Subscription`, `RegistryView`, `BackendRegistry`,
`FrozenMap` and the errors. Later commits only use them.

**Each seam commit:**
- deletes its own `_legacy=` and its line in the list;
- updates its reference guard (`test_registry_refs_guard.py`);
- adds its seam to the conformance suite and the replacement test.

**The closing commit:**
- deletes `_legacy=`, the four allowances, `origin=`, and `caller_module`, which nothing uses
  once `origin=` is gone;
- makes the classes `@final`;
- turns on the ast-grep rules.

After it, only the two `RequireRepo` approvals remain.

## 7. Enforcement

**Discovery:**
- An AST scan of `src/otto` yields `(module, bound name)` for every **module-level** construction
  of `Registry(`, `BackendRegistry(`, `Subscription(` and `RegistryView(`.
- Until 5.1 the scan also yields the legacy `KindRegistry(` constructions, as today's discovery
  does (`tests/unit/test_registry_loading.py:62-80`).
- Constructions inside `otto.registry` itself are skipped. They are the engine's owned inner
  tables.
- After those modules are imported, the scan must equal `otto.registry.instances()` restricted to
  otto-defined objects, compared on `(defined_in, kind)`.
- A construction inside a function or a class body fails the test. Under the `KindBuilder` change
  (§3.1), no domain module needs one.

**Conformance: five kinds of case**, sharing the duplicate, overwrite and attribution assertions.
- From commit 4 on, every live registry not on the legacy list must have a case.
- A legacy-listed registry gains its case in its seam commit.
- At commit 6 the rule covers every registry.

| Kind | Asserts |
|---|---|
| raw registry | A duplicate raises and changes nothing. `overwrite=True` replaces the entry completely, with new attribution, the same position, the revision bumped once, and a new generation. An invalid eager overwrite leaves the old entry. A bare class, a dict, a wrong record or a bare `Ref` raises `IncompleteRegistration`, and so does a record holding a `list`, `dict` or `set`. Listing, membership, origin, `peek` and snapshots import nothing. A wrongly typed resolved field raises before caching and can be retried. A `get` that publishes bumps neither revision nor generation. Under test loading, the refusal fires before `validate` and changes nothing. Batches are atomic: a failing last item, or a repeated key, commits nothing. A `validate` that tries to write to the same registry or another one is refused, and one that reads a `Ref` entry with `get` succeeds. Teardown `unregister` and restore succeed on a `RequireRepo` registry outside any init import. Live capabilities equal the approved list. |
| wrapper | For the same logical payload, the wrapper and the raw path produce the same record, attribution, duplicate behaviour and overwrite behaviour. Attribution is the user module, for every decorator wrapper. |
| backend | The same spy entry registered under `"json"`, `"none"` and any other name behaves the same. `prepare` makes one parse and no factory call. A valid `build` makes no parse and one factory call, and building one `Prepared` twice works. A stale or foreign `Prepared` makes neither call. A snapshot-exempt inventory entry makes no fingerprint call, and a replacement named `"json"` without the flag follows normal caching. Under `-R`, the reservation counters stay at zero. Static-metadata reads import neither the factory nor the config model. |
| subscription | Order is preserved. Two subscriptions of one callable stay two. Cancelling one token twice removes exactly one occurrence. Origin and repo are recorded. The refusal applies. |
| view | Derivation is checked. There is no mutation method. Replacing an actions entry drops its old bodies. A rejected contribution leaves both sources and both views unchanged. A standalone/project clash fails in either order. The `otto run` group serves a replaced instruction. |

**Replacement differential.** For each class-valued seam and each backend seam:
- register a replacement under every built-in name, with `overwrite=True`;
- build through the real product path;
- assert the replacement was used.

The product paths:
- `create_host_from_dict` with `os_type=`, for host classes;
- a Unix host and an embedded host, for term and transfer;
- `build_command_frame` and its siblings, for the no-argument seams;
- `build_lab_sources`, `build_inventory` and `gate_from_settings`, for the settings seams;
- `power_control_from_spec`, for power.

This is #601's test, generalised, and it is the real guard against direct construction. Calling
`REGISTRY.build` directly would guard nothing.

**Adversarial cases:**
- eager invalid metadata next to an unresolved `Ref`;
- a failed resolution after another operation replaced the entry;
- a repeated batch key passed through a wrapper;
- an option-field clash between two repos, caught by the post-loop step;
- a stale `Prepared` after removal and reinsertion, and after a test restore;
- warm completion with no init import, including a newly appearing glob match;
- the three profile layers, each shadowing the one below as a whole profile;
- a repo `[os_profiles.unix]` table replacing the built-in, prompts included, also after
  `rebuild_connections`;
- a data profile over a custom host class, which resolves after init;
- a host on a data profile appearing in host summaries.

**ty:**
- `Registry`, `BackendRegistry`, `Subscription` and `RegistryView` are `@final`.
- No new registration or construction surface uses a bare `Registry`, a bare `Ref`, `Any` or
  `Callable[..., Any]`.
- Typing fixtures live in `tests/unit/registry/typing_fixtures/` and run through an explicit
  `ty check` invocation in `make typecheck`, because `pyproject.toml` excludes tests from `ty`.
  - A positive fixture must check clean.
  - A negative fixture carries a rule-specific `# ty: ignore[<rule>]` on the line that must
    fail, and the invocation sets ty's unused-suppression rule to error. So a negative that stops
    failing turns the check red.

**ast-grep.** Each rule comes with positive and negative fixtures, aliases and qualified imports
included. A Python AST binding scan covers the cases ast-grep cannot resolve. The rules turn on in
commit 6.
- `registry-overwrite-only-approved`: a literal `overwrite=True` in `src/otto`.
  - Today's sites are `os_profile.py:255, 258, 435`, `snmp.py:141`, `parsers.py:631` and
    `instructions.py:273`.
  - Commits 5.3, 5.6 and 5.9 remove them, so none remains at commit 6.
- `no-adhoc-registry` **(v3 ruling)** flags a mutation that meets all three conditions:
  - it mutates an UPPER_CASE module global bound to a `dict` or `list` literal, including
    leading-underscore names (`_HOST_SPECS`, `_PRODUCT_PROVIDERS`) and annotated assignments;
  - it is outside `otto.registry`;
  - it is **inside a registration-shaped function**: one named `register_*`, `add_*`,
    `subscribe*` or `*_provider`, or one used as a decorator.

  What passes:
  - **Operational caches** written by other functions are not registries. Negative fixtures pin
    two: `_ADD_LOCKS` with its `setdefault` (`tunnel/manage.py:70, 93`), and `_LIB_DEP_STATS`
    with its indexed writes (`suite/plugin.py:158, 945-947`).
  - **Static tables** such as `DEFAULT_PARSERS` and `cache_sections.SECTIONS` are never mutated.

  A registry written from a function with any other name escapes the rule. Only review covers
  that case (see "What remains policy" below).
- `no-registry-subclass`: a subclass of an engine type outside the engine.
- `backend-no-name-dispatch`: a comparison (`==`, `!=`, `in`) against the string literal `"json"`
  or `"none"`. It applies in the modules §1 lists and in the five configured seams' `registry.py`
  modules. The thirteen sites in §1 are its positive fixtures.
- `backend-construction-through-entry`: a call to a registered built-in's constructor in an
  orchestration module. This rule is bounded; the differential is the real guard.

**What remains policy:**
- whether a reason is a good reason;
- whether an approved-list addition is accepted;
- whether an arbitrary helper eventually constructs a built-in;
- whether a module keeps a registry in a plain dict written from an unconventionally named
  function.

Review and the replacement differential cover these. The design does not claim otherwise.

## 8. Public surface

### 8.1 P1 delta (path changes; they land in P1)

**`otto.registry`'s first `__all__`:** the four names already in appendix F.

**Three retirements**, recorded in an appendix G addendum:
- `otto.inventory:get_inventory_backend_class`;
- `otto.creds:get_creds_backend_class`;
- `otto.reservations:reset_half_ported_warnings`.

All three are in their facades' `__all__` today, and 3a's seam commits delete them. P1:
- drops them from those `__all__` lists. This is the second exception to spec 1 Q4's "no
  name-level narrowing";
- rewrites the two sentences that teach the first two (`inventory-backends.md:103`,
  `creds-backends.md:99`) to say that an unregistered name raises when otto builds the store,
  listing the registered names. That is true both before and after 3a.

No replacement is taught: users register backends, and otto builds them.

**Not public:**
- `INSTRUCTIONS` and `PROJECT_INSTRUCTIONS`. Appendix F lists `otto.instructions` as `instruction,
  run_instruction` (`…-manifest-appendix.md:322`), so turning these tables into views needs no P1
  line and no mark. `PROJECT_ACTIONS` is public and keeps its path.
- `INVENTORY_BACKENDS` and `CREDS_BACKENDS` stay internal.

**Every engine type 3a adds is an addition after P1, and additions are free.** Their paths:

| Path | Names |
|---|---|
| `otto.registry` | `DuplicateRegistration`, `IncompleteRegistration`, `Justified`, `RequireRepo`, `ClassEntry`, `Subscription`, `RegistryView`, `BackendRegistry`, `Configured`, `Prepared`, `FrozenMap`, `configured_backend`, `class_backend` |
| `otto.host` | `ProfileFields`, `HostClassEntry`, `resolve_os_profile`, `ProfileContext`, `PowerControlError`, `PowerConstructionError`, `TermBackendError`, `TermConstructionError`, `TransferBackendError`, `TransferConstructionError`, each seam's `Env` |
| `otto.inventory` / `otto.creds` / `otto.reservations` / `otto.labs` | that seam's construction error and `Env` |

`STANDALONE_INSTRUCTIONS` is internal, like its sibling views. Other record types are internal unless
a public attribute exposes them, such as `PendingLabSource` through `Repo.lab_sources`. The dump's
placement rule then applies.

### 8.2 Contract changes after P1 (each marked; the dump flags them)

**The engine:**
- `Registry.register` drops `origin=`, and its entry type is `E`, not `T | Ref`. A bare `Ref`
  registration, which `registries.md:78-88` teaches today, is refused.
- `Registry(...)` changes:
  - it requires `entry=`;
  - `validate` takes three arguments (`name, entry, proposed`);
  - `check_resolved` is new;
  - `collision_hint` is gone;
  - `Registry` cannot be subclassed.
- `Registry.instances()` is removed, and `otto.registry.instances()` replaces it.
- `.get()` returns records, where it returned a bare value, on:
  - `HOST_CLASSES`, `FRAME_CLASSES`, `IMPAIRERS`, `CARRIERS` and `SESSION_SETUPS`;
  - `PROJECT_ACTIONS`, `OPTIONS` and `PRODUCT_KINDS`/`DEV_TOOL_KINDS`;
  - `HOST_PARSERS`, `HOST_PATTERN_PARSERS` and `PROJECT_PARSERS`;
  - `LAB_REPOSITORIES`, `POWER_CONTROLLERS` and every `*_BACKENDS`.

  `LOGIN_PROXIES` already stores records.

**Registration wrappers:**
- The wrappers that had no `overwrite` gain it.
- These now raise on a duplicate, where they always overwrote: `register_host_class`,
  `register_os_profile`, `register_snmp_metric` and exact-id `register_host_parsers`.
- `register_host_class`: `spec` becomes keyword-only, it gains `profile=`, and its auto-profile
  write is gone.
- `register_cli_command` loses `origin=`, and `CommandSpec` loses `origin`.
- `register_options` refuses a string path that names a re-export. A duplicate raises
  `DuplicateRegistration` instead of `OptionsRegistrationError` (`params.py:404-408`). Both are
  `ValueError`s.

**Records and lookups:**
- `OsProfile`'s fields become `name`, `base`, `fields: ProfileFields`.
- `build_os_profile`, `get_os_profile` and `registered_profile_names` gain `data=`. Without it,
  they no longer see data profiles.
- `OPTIONS` entries lose `repo`, and their `verbs` become a tuple.

**Lab loading:**
- `create_host_from_dict`, `validate_host_dict` and `host_identity` gain `profiles=`.
  `build_lab_sources` gains `profiles=` (§3.4).
- The `LabRepository` protocol does not change. A lab backend receives the profiles in
  `LabSourceEnv.profiles`, as part of the backend cutover below.
- `Repo.lab_sources` holds `PendingLabSource`s.
- These refuse during an init import:
  - `prepared_lab_sources`;
  - `build_lab_sources`;
  - the public `otto.session.build_lab`, which calls it (`session/lab.py:60-65`).

**Backend construction:**
- `build_term_backend` and `build_transfer_backend` take a context and return a built backend.
  `build_power_controller` and `power_control_from_spec` take `host_id`. `CommandPowerController`
  takes its config model.
- `compile_*` / `construct_*` / `Compiled*` in inventory and creds carry a `Prepared`.
- The construction contract moves to `factory(ctx)` with config models. This is a hard cutover
  for every custom lab, inventory, creds, reservation and power backend.
- `register_lab_repository`, `register_inventory_backend`, `register_creds_backend`,
  `register_reservation_backend` and `register_power_controller` take
  `(name, *, config, factory, overwrite=False)` (§4.1). Inventory also takes `snapshot_cache=`.

**Unchanged:**
- `RegistrationRefused`.
- `registered_product_providers`, `registered_dev_tool_providers` and `check_instruction_active`
  keep their signatures.

## 9. Commits

| # | Commit | Lands |
|---|---|---|
| 1 | `docs(spec)`: this spec and the spec 1 amendments (§12) | now |
| 2 | `fix(host)`: #601 (§5) | **before P1** |
| 3 | P1 lines: `otto.registry` `__all__`, the three retirements and their two docs sentences (§8.1) | inside P1 |
| 4 | `feat(registry)!`: the strict engine with every engine type, the legacy mechanism, the conformance and discovery suites, isolation through `instances()` (§2, §6, §7) | after P1 |
| 5.1 | the no-argument class seams (frame, loader, filesystem, impairer, carrier), login proxy, session setup, and the product and dev-tool kinds (`KindBuilder`) | after 4 |
| 5.2 | lab sources (§4.3): `LabSourceEnv(repo_dir, label, origin)` | |
| 5.3 | instructions and project actions (§3.3), plus the revision-keyed `RegistryBackedGroup` cache (§3.2), which the view case needs | |
| 5.4 | CLI commands (§3.2), whose group cache 5.3 already keyed | |
| 5.5 | options | |
| 5.6 | monitor parsers and SNMP | |
| 5.7 | product and dev-tool providers (`Subscription`) | |
| 5.8 | term and transfer | |
| 5.9 | host classes and OS profiles (§3.4). It adds `profiles` to `LabSourceEnv`, and `profiles=` to `build_lab_sources`, and threads them. Until 5.9, data profiles keep registering at parse, as today, so they keep working (v4 ruling) | |
| 5.10 | inventory and creds | |
| 5.11 | reservations | |
| 5.12 | power | |
| 5.13 | compose adapters: `ComposeAdapterEntry`, `RequireRepo`, the decorator's attribution, the reader, the conformance case, the legacy-list removal (`docker/adapter.py:37-40, 72`) | before 6 |
| 6 | `refactor(registry)!`: the legacy mechanism and `origin=` deleted, `@final`, the ast-grep rules on | last |

**Commits that cross seams**, and so deserve extra budget. Counts are `grep -rn … tests` lines:
- **5.3:**
  - 24 lines in 4 files mention `publish_project_instructions`, about 15 test functions;
  - 25 lines call `.register`/`.unregister` on `INSTRUCTIONS` or `PROJECT_ACTIONS`;
  - 14 lines call `register_project_instruction_body(` and 15 call `…_bodies(`.
- **5.9:**
  - 12 `setattr` lines replace `HOST_CLASSES` with a dict (3 in `test_dry_run_seam.py`, 9 in
    `test_dynamic_host_commands.py`);
  - 14 lines outside `conftest.py` reference `_HOST_SPECS`.

**Docs, each with the commit that changes the behaviour:**
- `docs/architecture/subsystems/registries.md`, `extension-points.md:105`, `execution.md:56`,
  `data-boundary.md:99`;
- `extending-backends.md`, the four backend cookbook pages, and a new power-controller section;
- `custom-host-classes.md:29-46`, `os-profiles.md`, `docs/configuration/settings.md:332`;
- `extending-cli.md:125`, `custom-parsers.md`, `options-classes.md`;
- `host-sources.md`, `lab-config.md`.

**In-tree examples, migrated with their seam:**
- `src/otto/examples/lab_repository.py` (`profiles=`);
- `src/otto/examples/reservations.py`;
- gs_example's `TeamFileBackend` (`docs/examples/getting-started/libs/gs_example/__init__.py:45-55`).

The gs_example `.names()` guards keep working.

## 10. File operations and the cycle ratchet

**File operations.** Each commit from 4 on carries an `import_budget.py --report-json`
before/after table for `import_otto`, `bootstrap_repo`, `completion_repo_warm` and
`help_repo_warm`.
- Record placement alone does not settle the cost:
  - static-metadata reads use `peek`;
  - preparation resolves config models;
  - construction resolves factories.

  So each surface is measured.
- A ceiling raise is a design defect.

**The ratchet:**
- `otto.registry` stays a leaf.
- Domain records reference cycle types only under `TYPE_CHECKING` or as a `Ref`.
- The explicit profile input removes the settings-time import of `otto.host` and the write into it
  (`config/repo.py:698, 702`). But `config → host` stays, for other imports.
- `BASELINE` is not expected to change. Each commit records the measured graph.

## 11. Out of scope

- **3b** (`2026-10-06-host-construction-design.md`): `HostSpec`, `to_host`, custom host fields,
  spec-time validation (including moving power's `prepare` into spec validation), and the
  class/spec field rule.
- **#600:** the term-backend ABC and its conformance helper.
- **#606:** refusing unequal same-name data profiles. It also removes a pre-existing
  order-sensitivity in persisted completion **(v5 ruling)**:
  - Today, as under G-11, the later repo's same-name table wins. Yet the persisted caches key on
    sorted repo paths: workspace identity (`config/home.py:90-114`) and section fingerprints
    (`config/cache_sections.py:196-198, 219-222`). The shim's marker shortcut does not compare
    repo order either (`_shim_complete.py:831-838, 1053-1064`).
  - So reversing `OTTO_SUT_DIRS`, with unchanged files and two *unequal* same-name tables, can
    serve completion computed under the previous winner.
  - 3a neither causes this nor worsens it. Once #606 refuses unequal tables, the winner no longer
    depends on order, and the sorted keys become correct. #606 also assesses the persisted
    tunnel-id fingerprint, which sorts repos too (`completion_cache.py:571-574`).
- **Force-exit hooks:** a runtime lifecycle API.
- **Spec 2's text** names `KindRegistry.build` (`run-state-contracts-design.md:218`). It becomes
  `KindBuilder.build`, and spec 2's next touch fixes the name.

## 12. Spec 1 amendments (made in this spec's commits)

- **Design §7, pending seams:**
  - the registry-objects line is marked settled and links here;
  - the host-construction line names 3b;
  - the term-backend line says that 3a settles the construction contract (`TermContext`,
    `build_term_backend(name, ctx)`), while the ABC stays with #600.
- **Q3 and the follow-up list:** both are marked settled by 3a.
- **Q4:** the internal list is no longer the only narrowing exception; the appendix G addendum is
  the second.
- **Q5, and appendix B's `TermContext` row** (`:104`): "no contract yet" becomes "construction
  contract in spec 3a; ABC in #600".
- **Appendix A** (`otto.registry`, `:35`) and **appendix B** (the `otto.registry` rows' pending
  column, `:163-166`): "registration-semantics spec (Q3)" becomes "spec 3a".
- **Appendix D** (`otto.declared`, `:307`): "pending spec 3" becomes "spec 3a (`KindBuilder`)".
  Spec 3b §9 records that no declared-entry seam is pending.
- **Appendix G:** an addendum retiring `get_inventory_backend_class`, `get_creds_backend_class`
  and `reset_half_ported_warnings`, naming this spec.
- **The P1 checklist** (`todo/590-p1-public-surface-switch-on.md`) gains a "Spec 3a's P1 work"
  section, with commit 2 before P1, the §8.1 items, and the after-P1 commits 4–6.
