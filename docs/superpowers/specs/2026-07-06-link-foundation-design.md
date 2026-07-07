# `Link` foundation — model + `lab.json` (design spec)

**Date:** 2026-07-06

**Status:** DESIGN COMPLETE (brainstormed with Chris 2026-07-06). Awaiting review
of this consolidated spec → then `writing-plans`. Build in a worktree.

**Working notes:** `todo/link.md` (living scratchpad; this spec is the formal
distillation of sub-project **#1**).

---

## 1. Context & goals

otto has **no first-class edge object**. Connectivity is only *implied* today by
three flat things on a host: `hop` (the single upstream SSH-jump host id;
`src/otto/host/remote_host.py:140`), the near-vestigial `interfaces` name→IP map
(`remote_host.py:179`, essentially unused at connect time), and shared
`element`/`element_id` grouping. Meanwhile two roadmaps need an edge model:

- **The monitor redesign** (`docs/superpowers/specs/2026-07-05-monitor-untitled-ui-redesign-design.md`)
  makes **links first-class, selectable objects** with an inspector (§10), needs
  a topology derived from element grouping + hop edges, and calls the
  element/link/source model **"the biggest net-new backend surface"** that Phase 2
  depends on (§18).
- **`todo/link.md`** wants an `otto link` CLI to add/impair/capture links.

A `Link` promotes "an edge between two endpoints" to a real object with three
**provenances**: **implicit** (derived from `hop`), **declared** (in the lab
file), and **dynamic** (created at runtime by `otto link add`). Building the
model once lights up **two consumers** — the CLI and the monitor GUI.

This spec is **sub-project #1: the pure data-model-and-contract layer**, with
**no live network side effects**. Everything here is unit-testable as pure
functions; the live/side-effecting machinery lands in later specs.

---

## 2. Scope & staging

`todo/link.md` is ~6 sub-projects on one foundation. Each gets its own
spec→plan→implement cycle:

| # | Sub-project | This spec? |
|---|---|---|
| **1** | **Foundation: `Link` model + `lab.json`** | ✅ **in scope** |
| 2 | `otto link` CLI + add/remove/list + live tunnels (socat/forward) + `asyncio.gather` discovery wiring | deferred |
| 3 | Impairment: `LinkImpairer` protocol + NetEm + `otto link impair` | deferred |
| 4 | Capture: `otto link capture` / `otto host capture` (tcpdump) | deferred |
| 5 | Management hosts (monitor source-attribution to other elements) | deferred |
| 6 | GUI Phase 2 topology + link inspector | deferred |

**In scope (this spec):** the `lab.json` format + hard cutover, the interface
object evolution, the declared-link schema + validation, the unified runtime
`Link` type, the **static** (implicit + declared) derivation, and the dynamic
**sentinel format + discovery parser as pure functions**.

**Out of scope (later specs):** live tunnel spawning, the live `asyncio.gather`
discovery, all `otto link` CLI verbs, impairment, capture, management hosts, GUI.
The open items in `todo/link.md` (tunnel mechanism, marker robustness across host
families, management-host definition, capture home) belong to those specs.

**Hard cutover:** no existing-user migration. `hosts.json` → `lab.json` is a
clean break — no dual-format loader, no back-compat array reader.

---

## 3. `lab.json` format

Rename `hosts.json` → **`lab.json`** and change the top level from a bare JSON
array to an **object with two sections**:

```jsonc
{
  "hosts": [ /* existing host objects, schema UNCHANGED */ ],
  "links": [ /* new declared-link objects, §5 */ ]
}
```

- **`hosts`** is the existing array of host dicts, **schema untouched** — host
  ids stay derived from `element`/`board`/`slot` (`remote_host.py:52`
  `make_host_id`), not authored keys.
- **`links`** is a new array of declared-link objects.

**Loader changes** (`src/otto/storage/json_repository.py`):

- `HOSTS_FILENAME = "hosts.json"` → `LAB_FILENAME = "lab.json"` (`:20`); the
  `_find_hosts_files` glob (`:116`) and `_load_json_hosts` (`:139`) update.
- `_load_json_hosts`'s `isinstance(data, list)` guard (`:156`) becomes an
  **object-with-sections** guard (`dict` with optional `hosts`/`links` arrays).
- Multi-dir merge (`load_lab`, `:36`) changes from **array-concat** to
  **per-section union**: union all files' `hosts`, union all files' `links`.
- Host membership filtering by `labs` (`:60`) is unchanged; link membership is
  **derived** (§5).

**`otto init`** (`src/otto/cli/init.py`): `HOSTS_JSON_ENTRY` (`:40`) becomes a
`lab.json` object template with a `hosts` array (+ optional example `links`);
`_scaffold_lab` (`:233`), `_detect_lab` glob (`:291`), `_validate_lab` (`:354`),
and the `labs = [...]` path comment in `SETTINGS_TEMPLATE` (`:19`) update to the
new filename.

**Schema export** (`src/otto/models/jsonschema.py`): the `hosts` array schema
(`_host_array_schema`, `:110`) is wrapped in the new `lab` object schema
(`hosts` + `links`); `build_schemas` (`:141`) emits a `lab` + a `link` schema.

---

## 4. Interface model

Evolve the host `interfaces` field from `dict[name → ip-string]` to
`dict[netdev-name → InterfaceSpec]`.

- **Key = the network-device name** (`eth0`, `eth1`, …) so impairment/capture
  (later specs) read the device straight off the key.
- **Value = `InterfaceSpec`** — an extensible pydantic model, `{ "ip": "…" }`
  today, room for `mac`/`cidr`/`role`/`speed`/… later without reshaping.
- **String shorthand:** a `BeforeValidator` coerces a bare `"eth0": "10.0.0.5"`
  to `{"eth0": {"ip": "10.0.0.5"}}` so simple labs stay terse (ergonomic only —
  not a migration bridge).
- **Files:** `InterfaceSpec` in `src/otto/models/host.py` (near `HostSpec`, the
  `interfaces` field is at `:175`, IP validation at `:221`); the runtime host
  gains a small `Interface` dataclass (or reuses the spec) where
  `RemoteHost.interfaces` is declared (`remote_host.py:179`,
  `unix_host.py:241`, `embedded_host.py:239`).
- **Touch-points:** `RemoteHost.address_for()` (`remote_host.py:286`) and
  `SnmpOptions.address` (`src/otto/host/options.py:522`) resolve by key today and
  keep working — they now read `.ip` off the object.

---

## 5. Declared link entry (schema + validation)

A `links` entry describes a data-plane route:

```jsonc
{
  "name": "data-plane-a",                 // optional friendly handle
  "endpoints": [
    { "host": "carrot", "interface": "eth1" },
    { "host": "tomato", "interface": "eth1" }
  ],
  "protocol": "udp",                       // optional; defaults to "tcp"
  "impair": "netem",                       // optional; reserved for #3
  "management": "mgmt-01"                  // optional; reserved for #5
}
```

- **`endpoints`** — exactly 2, each `{ host, interface? }`. **Interface is
  required only when the host has >1 interface defined**; with a single interface
  (or none) otto assumes it and its IP, resolving the endpoint at load. Omitting
  it on a host with >1 interface **is a load-time validation error** ("ambiguous
  interface — specify one of {…}"), since otto can't disambiguate.
- **`protocol`** — optional in JSON, **defaults to `"tcp"`** (no `None`).
  Informational for declared links; becomes functional for dynamic links (#2,
  drives socat UDP-vs-TCP).
- **`name`** — optional; the id is otherwise derived from the endpoints.
- **`impair` / `management`** — optional, **reserved** (validated as
  strings/ignored beyond presence in #1; wired in #3/#5).

**Validation** (new `LinkSpec` / `LinkEndpointSpec` in `src/otto/models/link.py`,
mirroring the `HostSpec` boundary pattern; `OttoModel` `extra="forbid"`):

- exactly two endpoints;
- endpoint `host` ids resolve to known hosts (checked at lab-load, where the host
  set is known — `load_lab`);
- endpoint `interface`, when given, is a key in that host's `interfaces` map;
  when omitted, the host must have ≤1 interface (else a clear "ambiguous
  interface — specify one of {…}" error);
- `protocol` free-string (no enum lock-in yet).

**Lab membership — derived, may span labs.** A link belongs to **every lab that
either endpoint belongs to** (union of the endpoints' `labs`). A link can
legitimately **span labs**, so it is never forced into one and carries **no
`labs` field**. Loading lab L surfaces every link with ≥1 endpoint in L; a
cross-lab link appears in both, its out-of-lab endpoint rendered as a
dangling/stub node in that lab's topology.

---

## 6. Runtime `Link` type

One `Link` object regardless of provenance, in a new `src/otto/link/` package
(parallels `src/otto/host/`):

```python
class Provenance(enum.Enum):
    IMPLICIT = "implicit"   # derived from a host's hop
    DECLARED = "declared"   # from lab.json links
    DYNAMIC  = "dynamic"    # from live discovery (#2)

@dataclass(frozen=True)
class LinkEndpoint:
    host: str               # host id
    interface: str | None   # netdev name (None ⇒ assume the host's sole iface)
    ip: str                 # resolved address

@dataclass(frozen=True)
class Link:
    a: LinkEndpoint
    b: LinkEndpoint
    protocol: str = "tcp"   # "udp"/… otherwise
    provenance: Provenance = Provenance.DECLARED
    id: str = ""            # deterministic; see below
    name: str | None = None
    # impair / management reserved for #3 / #5; NO owner field (§8)
```

- **Deterministic `id`** — a stable hash of the **normalized** endpoints (+ ports
  + protocol for dynamic). Endpoint order is normalized (sort by host id) so
  `a↔b` and `b↔a` yield the same id. Makes `add` idempotent and a collision a
  genuine duplicate; it's also the reconciliation key across provenances.

**Accessors — split by cost** (three provenances have very different costs):

- **`lab.static_links() -> list[Link]` (sync)** — implicit (§7) ∪ declared (§5),
  straight off the loaded `Lab`. Free. Powers the GUI base topology and the
  implicit/declared rows of `otto link list --all`. **Implemented in #1.**
- **`discover_dynamic_links(lab) -> list[Link]` (async)** — the live-discovery
  layer. **Signature + return type defined in #1; live `asyncio.gather` wiring
  implemented in #2.**
- **`all_links(lab) -> list[Link]` (async)** — union, reconciled by `id`. A
  dynamic link coinciding with a declared/implicit one shares the id and
  **merges** (higher-fidelity provenance wins for display). Used by `otto link
  list --all` and #2's conflict check.

---

## 7. Implicit link derivation (from `hop`)

Pure function over the loaded `Lab` (`src/otto/link/derive.py`): for every host
with `hop` set, emit `Link(a=host, b=hop_host, provenance=IMPLICIT,
protocol=<mgmt term: ssh/telnet>)`. Endpoints resolve to the mgmt `ip` (the SSH
path). Chained hops naturally produce a chain of edges (each host contributes its
own edge). Hosts with no `hop` are implicitly attached to the root/local node
(matches `todo/topology_plan.md`). This is the same derivation the retired
`/api/topology` sketch described, now a reusable pure function.

---

## 8. Dynamic-link discovery contract (pure parts only)

Dynamic links are **host-resident and outlive the otto process**, so state is
**live-discovered, never stored** — the running tagged processes are the single
source of truth. #1 defines the **contract**; #2 runs it live.

- **Sentinel format** — every tunnel process (spawned in #2) carries a structured
  argv marker: `otto-link:<id>:<proto>:<a-host>:<a-if>:<a-port>:<b-host>:<b-if>:<b-port>`,
  set via `exec -a` so it's the process name. **Owner-agnostic** — no username —
  so *any* user can `pgrep -af '^otto-link:'` and reap *all* otto tunnels with
  zero friction. **Zero persisted state** (no file, no DB, no host-local marker).
- **Encode/parse — pure functions in `src/otto/link/sentinel.py`** (built + unit
  tested in #1): `encode(link) -> str` and `parse(argv_line) -> Link | None`
  (rejects non-otto lines). One link = several tagged processes (socat on A,
  forward on the hop, socat on B) sharing the same `id`; the **discovery parser**
  (`parse_discovery(ps_output) -> list[Link]`) groups per-`id` across a host's
  process list. **age** is read from the OS (`ps -o etimes`), never stored.
- **Deferred to #2:** the live `asyncio.gather` of the discovery command across
  lab Unix hosts (embedded hosts can't host tunnels → skipped), the TTL cache for
  GUI polling, and the actual spawn/teardown.

---

## 9. Topology derivation (static layer for the GUI)

`static_links()` (§6) + the existing element grouping (shared
`element`/`element_id`, `make_host_id`) give the monitor's Phase 2 base topology
**without any new live backend**: element/collection nodes, hop edges, and
declared links, rendered from the loaded lab. The dynamic overlay and the
management-host "Sources" overlay come later (#2/#5). This directly satisfies the
monitor spec's §10/§14 link layer for the static case; the monitor spec's Phase 2
consumes this module rather than re-deriving.

---

## 10. Affected code (concrete)

- **New:** `src/otto/models/link.py` (`LinkSpec`, `LinkEndpointSpec`);
  `src/otto/link/` package (`model.py` — `Link`/`LinkEndpoint`/`Provenance`;
  `derive.py` — implicit + static derivation; `sentinel.py` —
  encode/parse/parse_discovery).
- **Changed:** `src/otto/models/host.py` (new `InterfaceSpec`; `interfaces` field
  type + shorthand validator, `:175`/`:221`); `src/otto/host/remote_host.py`,`unix_host.py`,
  `embedded_host.py` (runtime `interfaces` type; `address_for` reads `.ip`);
  `src/otto/storage/json_repository.py` (filename, object-sections guard,
  per-section merge); `src/otto/storage/factory.py` (link validation entry, near
  `validate_host_dict` `:101`); `src/otto/configmodule/lab.py` (`Lab` gains
  `links` + `static_links()`; `load_lab` populates links, `:83`); `src/otto/cli/
  init.py` (template + globs + filename); `src/otto/models/jsonschema.py` (`lab`
  + `link` schemas).
- **Completion cache** (`src/otto/configmodule/completion_cache.py`): the
  `HOSTS_FILENAME` reference (`:139`) and cache `SCHEMA_VERSION` (`:137`, bump)
  update for the new filename; link ids can be added to the cache later (#2 for
  `otto link` completion).

---

## 11. Testing (all unit — this spec has no live side effects)

- `lab.json` parse + multi-dir per-section merge (object-sections guard, union).
- `InterfaceSpec` object form **and** string-shorthand coercion; `address_for`/
  `snmp.address` read `.ip`.
- `LinkSpec` validation: 2-endpoint rule, host-ref resolution, interface
  required-iff-multiple rule, protocol default `"tcp"`.
- Derived membership: union-of-endpoint-labs incl. a cross-lab link surfacing in
  both labs; dangling out-of-lab endpoint.
- Implicit derivation from `hop` (incl. chains, no-hop → root).
- `Link.id` determinism + endpoint-order normalization; `all_links` reconciliation
  merges same-id across provenances.
- Sentinel **encode↔parse round-trip**; `parse_discovery` groups multi-process
  links by id and **excludes non-otto** `socat` lines (canned `ps`/`pgrep` text).

All hostless; runs in CI. No bed required for #1. (Live-bed e2e for tunnels/tc/
tcpdump — with the mandatory mgmt-interface refusal and optional impairment
`--expire` — arrives with #2/#3/#4, per `todo/link.md` § Testing strategy.)

---

## 12. GUI alignment

The monitor spec (`2026-07-05-monitor-untitled-ui-redesign-design.md`) already
reserves this exact backend (§10 links-as-objects + inspector, §14 element/link/
source contract, Phase 2). **No change to the monitor spec is required for #1** —
this spec *supplies* the static link/topology layer it depends on. When #2 lands
the dynamic overlay and #5 the Sources overlay, the monitor spec's Phase 2 tasks
consume `otto.link` directly. (A one-line pointer from the monitor spec's §14 to
this spec is a nice-to-have, not a blocker.)

---

## 13. Risks / notes

- **Hard cutover** breaks any existing `hosts.json` in the wild — accepted
  (no migration). Every in-repo fixture (`tests/_fixtures/lab_data/*/hosts.json`)
  and `otto init` template must convert to `lab.json` in the same change, or the
  suite goes red (cf. the host-field drift-guard lesson — schema changes must land
  atomically with fixtures).
- **`extra="forbid"`** means the new `links` section and `InterfaceSpec` fields
  must be threaded through every schema/validation path or loads fail loudly
  (which is the desired fail-loud behavior).
- **Sentinel/parser is a pure contract in #1** but its real-world robustness
  (argv visibility via `ps`/`pgrep` across the Unix host family) is only proven in
  #2's live e2e — flagged as a #2 open item, not a #1 risk.

---

## 14. Resume checklist

1. Chris reviews this spec; adjust if needed.
2. Invoke `writing-plans` → implementation plan for **#1 only**.
3. Build in a worktree (isolation from main).
4. Land schema + all fixtures + `otto init` template atomically.
