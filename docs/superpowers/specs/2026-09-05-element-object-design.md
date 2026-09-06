# Element Object & Host Identity — Design

**Status:** designed 2026-09-05, approved in discussion, awaiting spec review.
**Lands as:** one branch (`worktree-element-object`), one commit per section
of §11, product work separate from this spec's own `docs(spec):` commit.
**Supersedes** the id-composition and display-name rules of
`2026-07-07-host-id-rules-design.md` (§3, §4, §5) and the "`HostSpec` keeps
`element` and `element_id`" correction of `2026-08-27-lab-definition-v2-design.md`
(§14). Everything else in those specs stands.

Related: `2026-08-28-three-level-reservations-design.md` (element-level
resources), `2026-08-28-host-inventory-layer-design.md` (the `element_id`
cross-check), `2026-08-19-multi-source-lab-data-design.md` §6 (element
replacement across sources).

---

## 1. Motivation

Three things were found wrong with how a host carries its element today:

1. **The element's `id` leaks into the host id.** `make_host_id` renders
   `slug(element) + element_id + _board + slot`. The team was told elements
   have an `id` field; nobody was told that field would become part of every
   host's correlation key. It is the *element's* datum and belongs on the
   element.
2. **Element data is flattened onto the host.** A host carries `element`
   (a string), `element_id`, `element_metadata`, and `element_resources` as
   four unrelated fields, and the loader half-flattens on the way in: name and
   id ride inside the host dict while metadata and resources arrive as
   separate keyword arguments. An API user cannot tell from the host which
   fields are the element's, and there are three spellings of one fact
   (`id` in the file, `element_id` in the flat dict, `element_id` on the
   host).
3. **The display name carries an invented number.** `_generate_name` appends
   a lab-scoped `logical_index` when an element name repeats, so a chassis
   with three boards displays as `chassis 1 cpu 1`, `chassis 2 cpu 2`,
   `chassis 3 io 7`. That number is otto's, not the lab author's, and it is
   different for the same host in different labs.

Two designs were considered and rejected on the way here, and the reasons
are recorded so they are not re-litigated:

- **Element id as the disambiguator, shown as a small counter** (today's
  design). Rejected because the id is data, not identity (§2.1).
- **Auto-numbered ids** (`server1`, `server2` assigned by otto when a name
  repeats). Rejected because a host id must be a function of what the author
  wrote for that element and host, never of its siblings: adding or removing
  a sibling would silently repoint every id reference (`hop`, link endpoints,
  `host_preferences` selectors, reservation identifiers, monitor rows,
  coverage directories, tunnel sentinels); the numbering needs a scope and
  both lab-scoped and source-scoped numbering fail under `a + b` merges and
  multi-source replacement; and the composition is not injective (`server`
  number 1 collides with a literal `server1`). Otto never invents identity
  anywhere else, and the id is the one value every subsystem hashes.

## 2. Decisions

### 2.1 The element `id` is data

`ElementSpec.id` (optional `int >= 0`) stays in the lab file and becomes
`Element.id` at runtime. Otto never reads it to form an id, a name, an
ordering, or a key. It is shown where element data is shown and it is
cross-checked against an inventory record that states it (§8.6). That is
all.

### 2.2 Host id

```text
host.id = slug(element.name) [ + "_" + slug(board) [ + slot ] ]
```

`slug` is unchanged (its stability contract stands). The only structural
delimiter is `_` between the element slug and the board slug; `slot` never
appears without a board. `make_host_id(element_name, board, slot)` loses its
`element_id` parameter.

### 2.3 Element identity is the name slug

An element's identity token is `slug(name)`. Within one source it must be
unique: two elements whose names slug to the same token are a duplicate,
refused at parse with both spellings and both positions named. Across
sources the later source replaces the earlier wholesale, keyed by the slug
(§6 of the multi-source spec, with the key narrowed from `(name, id)` to the
slug).

Lab authors who have several elements of one type name them distinctly —
`server1` / `server2`, `Server A` / `Server B`, whatever the site's scheme
is. The number is theirs, written once, and constant across every lab
combination.

### 2.4 Display name

```text
host.name = element.name [ + " " + board [ + " " + slot ] ]
```

Space-joined, parts omitted when absent. The element name and the board
appear exactly as written in the lab entry — otto changes no character's
case in either direction, neither capitalizing a first letter nor lowering
one. Only the id is lower-cased, by `slug`, as it is today. No number is
ever added. A host entry's explicit `name` still overrides the generated
label and still does not affect the id.

The logical index and everything built on it — the positional CLI handles
(`dut1`), `Lab.resolve_handle`, `logical_indices()`,
`Lab._assign_logical_indices`, `_refresh_name`, the shadow warning, and the
completion paths that emit handles — are deleted (§7). A host is named by
its id and nothing else.

### 2.5 One runtime `Element`, reached only through `host.element`

A frozen `Element` dataclass is the sole carrier of element data on a host.
`host.element.name`, `host.element.id`, `host.element.metadata`,
`host.element.resources`. The flat fields `element_id`, `element_metadata`,
and `element_resources` are removed from every host class and from the
`Host` protocol, with no aliases.

### 2.6 The factory takes the element as one argument

`create_host_from_dict(host_data, ..., element=Element(...))`. `HostSpec`
drops `element` and `element_id`; `ElementSpec.flatten()` is deleted; the
host dict a backend hands the factory describes the host and nothing else.
Element name, id, metadata, and resources travel together, once, as the
object they belong to.

## 3. The `Element` object

Module: `otto/host/element.py`, beside `lab_info.py` and `inventory_ref.py`
(the host package does not import upward; every package that builds hosts
already depends on `otto.host`).

```python
@dataclass(frozen=True)
class Element:
    name: str
    id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    resources: frozenset[str] = frozenset()

    @property
    def slug(self) -> str: ...   # slug(name) — the identity token and host-id prefix
```

Invariants, enforced in `__post_init__` so a backend constructing one
directly gets the same errors the file layer gives:

- `name` must slug to a non-empty token (the `ElementSpec` rule).
- `id`, when present, must be `>= 0`.
- `resources` entries must be non-empty strings (`resources_nonempty`).
- `metadata` is copied on construction, like `LabInfo`, so no caller shares
  a mutable table with the file layer.

**Shared, not copied.** The loader builds one `Element` per `ElementSpec`
and passes that instance to every host of the element. Hosts of one element
therefore share one object; `host_a.element is host_b.element` holds for
siblings. The frozen dataclass makes that safe; the `metadata` dict is shared
by design and documented as read-only.

Equality and hashing follow `LabInfo`: the dataclass `__eq__` compares every
field; hashing raises because `metadata` is a dict. Key collections by
`element.slug`.

`ElementSpec.to_element()` builds the runtime object from a validated file
entry. `ElementKey` is deleted: the identity token is a string, and error
messages name an element by the `name` the author typed.

## 4. Host contract

- `RemoteHost.element: Element` — required, replaces `element: str`. Every
  concrete remote class (`UnixHost`, `EmbeddedHost`, `ZephyrHost`) declares it
  as a required init field where `element: str` was.
- `BaseHost.element: Element | None` on the `Host` protocol; `LocalHost` and
  `DockerHost` declare `element: Element | None = None` — a container or the
  built-in `local` belongs to no element. Whether a container should inherit
  its parent's element is out of scope (§13). If `ty` rejects the narrower
  `Element` on `RemoteHost` against the protocol's mutable `Element | None`
  attribute, the protocol exposes `element` as a read-only property instead;
  the narrowing is the point and is not given up.
- Removed from every class and from the protocol: `element_id`,
  `element_metadata`, `element_resources`, `logical_index`,
  `_element_id_str`.
- `_generate_id` calls `make_host_id(self.element.name, self.board, self.slot)`;
  `_generate_name` builds §2.4.
- `__str__` is unchanged (returns `name`).

## 5. Loader and backend boundary

| Today | After |
| --- | --- |
| `ElementSpec.flatten()` stamps `element` / `element_id` into each host dict | deleted; the loader iterates `spec.hosts` and builds `spec.to_element()` once |
| `create_host_from_dict(host_data, preferences, lab_name, *, element_metadata, element_resources, inventory_ref)` | `create_host_from_dict(host_data, preferences, lab_name, *, element, inventory_ref)` — `element` required |
| `HostSpec.element: str`, `HostSpec.element_id` | removed; `_COMMON_PLAIN_FIELDS` drops both; the slug validator moves to `Element` |
| `HostSpec.to_host(cls, *, preferences)` | `to_host(cls, *, element, preferences)` |
| `host_identity(host_data) -> HostIdentity(id, ip, element, element_id, docker_capable)` | `host_identity(host_data, element) -> HostIdentity(id, ip, docker_capable)` |
| `addressing_from_dict(host_data)` | `addressing_from_dict(host_data, element)` |
| `resolve_host_entry(host_data, inventory)` | `resolve_host_entry(host_data, inventory, element)` — the `element_id` cross-check reads `element.id` (§8.6) |
| `validate_host_dict(host_data)` | unchanged |
| `HostSummary.element`, `HostSummary.element_id` | removed; they existed to synthesize handles. The conformance suite drops the two comparisons. |
| `Lab.hosts` collision messages: "Differentiate the element string, assign/uniquify element_id, or set board/slot" | "Give the elements distinct names, or set board/slot" |

`HOISTED_HOST_KEYS` and the `ElementSpec` validator that refuses `element` /
`element_id` / `labs` inside a host entry stay: the error names the key. The
json-schema helper `_drop_hoisted_keys` becomes a no-op once `HostSpec` has
no hoisted fields and is deleted with its test.

The example backend (`otto/examples/lab_repository.py`) restructures its
dataset by element, since it is the reference implementation the
lab-source-backend docs quote.

## 6. Identity rules, worked

| Lab data | Host id | Display name |
| --- | --- | --- |
| element `server`, one board-less host | `server` | `server` |
| element `Lab X Server` | `lab-x-server` | `Lab X Server` |
| element `server`, `id: 103` | `server` | `server` (`host.element.id == 103`) |
| element `server1`, element `server2` | `server1`, `server2` | `server1`, `server2` |
| element `chassis`, hosts cpu/1, cpu/2, io/7 | `chassis_cpu1`, `chassis_cpu2`, `chassis_io7` | `chassis cpu 1`, `chassis cpu 2`, `chassis io 7` |
| element `Edge Router`, board `LineCard`, slot 3 | `edge-router_linecard3` | `Edge Router LineCard 3` |
| element `server`, element `server` | refused: duplicate element | |
| element `server`, element `Server` | refused: both slug to `server` | |
| element `server 1`, element `server_1` | refused: both slug to `server-1` | |
| element `server` with two board-less hosts | refused: duplicate host id `server` | |

Errors:

- Same source, one file, identical names: today's message,
  `Lab file 'x/lab.json': duplicate element 'server' at elements[0] and elements[2] — one element, one entry`.
- Same source, one file, spellings that differ but slug alike:
  `Lab file 'x/lab.json': elements[0] 'Server' and elements[2] 'server' both slug to 'server' — one element, one entry`.
- Same source, two files: the same two forms, naming both files.
- Different sources, same lab: later wins wholesale; the existing warning names both sources.
- Two hosts of one element with the same board/slot (or none): the existing duplicate-id error, reworded per §5.

## 7. Deleted

- `RemoteHost.logical_index`, `_element_id_str`; the `logical_index` fields on `UnixHost` and `EmbeddedHost`
- `Lab._assign_logical_indices`, `_refresh_name`, `logical_indices()`, `Lab.resolve_handle`
  (callers use `lab.hosts.get(handle)`: `OttoContext.get_host`, `docker/deployment.py`, `cli/docker.py`, `cli/remote_completion.py`)
- Completion: handle emission in `collect_host_ids`, the per-lab bucket builder, and `collect_host_classes_by_id`
- `ElementKey`; `HostIdentity.element` / `.element_id`; `HostSummary.element` / `.element_id`
- `ElementSpec.flatten()`; `HostSpec.element` / `.element_id`
- Tests that exist only for the above: `test_logical_index.py`, `test_logical_indices_helper.py`, `test_completion_logical_handles.py`, `test_handle_resolution.py`. `test_display_name.py` survives, rewritten against §2.4.

## 8. Consumers updated

1. **Declared products and dev tools** (`declared.py`): match keys mirror the
   attribute path exactly. `element` → `element.name`, `element_id` →
   `element.id`, `element_metadata.<k>` → `element.metadata.<k>`. The
   dotted walker resolves attributes and then dict keys, so `element.name`
   and `element.metadata.rev` share one code path. An old key is a settings
   error naming the new spelling. `MATCH_KEYS` becomes
   `{id, element.name, element.id, os_type, os_name, os_version, ip, source_lab}`
   plus the two dotted roots `metadata.` and `element.metadata.`.
2. **Provider docstrings** (`product.py`, `dev_tool.py`) and the
   cli-exposed-verbs doc list `element.name`, `element.id`,
   `element.metadata`, `element.resources` in place of the four flat names.
3. **Reservations** (`reservations/check.py`): the element-level owner is
   `element.slug`, matching the host level's use of `host.id`; the check
   duck-types `host.element` as it duck-types today (tach forbids the
   import).
4. **Composite** (`labs/composite.py`): `_element_key` returns
   `host.element.slug` (or `""` for an element-less host).
5. **Monitor** (`monitor/session.py`): `HostSnapshot.element = host.element.name`.
   `ElementRecord.id` remains the element name; it is now unique per lab.
6. **Inventory** (`inventory/resolve.py`): `element_id` stays an
   `INVENTORY_KEY_FIELDS` member — asserted, never filled — because a record
   is per host and an element is shared, so a record cannot fill an
   element-level field. The cross-check compares `record.element_id` with
   `element.id`. Docs reword "identity key" to "cross-checked fact".
7. **Summaries fallback** (`labs/__init__.py`) and both `list_host_summaries`
   implementations stop filling the removed fields.
8. **Init templates** (`cli/init_templates.py`, `cli/init.py`): `id` is
   described as data; the validation loop builds an `Element` per entry.
9. **Test fixtures** (`tests/_fixtures/labdata.py`): `host_data` /
   `flat_hosts` / `make_host` stop stamping and instead return or accept an
   `Element`; `test_pinned_identities.py` pins `(host.id, host.element.name,
   host.element.id)`. `tests/_fixtures/shim_repo.py` declares two elements named
   `dut` (ids 1 and 2); they become `dut1` and `dut2` with no ids, which
   leaves their host ids and links unchanged.

## 9. Docs

- `guide/configuration/lab-config.md` § Host identity & naming: rewritten to
  §2.2–§2.4 with the §6 table; the `name` row of the host-field table drops
  "its logical number"; the migration appendix's "group by element +
  element_id" wording updates.
- `library/lab-source-backends.md`: the `HostSummary` field list, the
  `create_host_from_dict` / `host_identity` call shapes, the example backend.
- `library/cli-exposed-verbs.md`, `guide/configuration/declared-products-tools.md`:
  attribute names and match keys.
- `guide/configuration/inventory.md`, `library/inventory-backends.md`:
  `element_id` wording.
- `architecture/subsystems/hosts.md`, `architecture/subsystems/completion.md`,
  `architecture/subsystems/data-boundary.md`: remove handles and the flattening
  description.
- `guide/cli/schema/editors.md`: hoisted-key sentence.
- `guide/cli/reservation/check.md`: the `owner` row reads "the lab name,
  the element slug, or the host id" and the sample table follows.
- `library/custom-host-classes.md` (the contract fields a custom class
  declares) and `architecture/subsystems/reservations.md` (`host.element_resources`):
  the flat names become `element`.
- `library/index.md`: the doctest that builds hosts with `element` keys passes
  `element=` and reads `.element.name`.
- `api/host/element.rst` (new `automodule`) listed in `api/host/index.rst`
  beside `lab_info` and `inventory_ref`.
- The `2026-07-07` and `2026-08-27` specs get a one-line "superseded by" note
  at the top pointing here; their bodies are history and are not edited.

## 10. Testing

Every item lands test-first, and each new test is shown red against the
pre-change code before the change (mutate-and-observe, per the repo rule):

- `Element`: invariants, copy-on-construct, `slug`, `to_element()` round-trip.
- `make_host_id` / `_generate_name`: the §6 table as parametrized literals.
- Duplicate detection: every refused row of §6 with the exact message;
  cross-file and cross-source cases.
- Factory: `element=` required (a `TypeError` without it); the built host's
  `element is` the passed instance; `host_identity` equals the built id.
- Removed surface: `host.element_id` etc. raise `AttributeError`;
  `Lab.resolve_handle` is gone; completion offers no `<slug><N>` handle for a
  multi-host element.
- Declared match keys: `element.name` / `element.id` / `element.metadata.x`
  match; the old spellings are settings errors naming the new one.
- Reservations: the element-level origin owner is the slug.
- Conformance suite: a backend whose summaries carry the removed fields is
  no longer possible; the field comparison list is the new one.
- Pinned identities: fixture literals rewritten from the recorded v1 values
  (none of the fixtures declares an element id or a repeated name, so every
  pinned id is unchanged).

Gates: `make coverage` per item, `nox -s tests_hostless-3.14` and `make
docs` before hand-over.

## 11. Commit order

1. `feat(host)!: Element — one object for an element's name, id, metadata, resources`
   (§3, §4, §8.2–§8.5, §8.7, §8.9). At the end of this commit the factory
   itself builds the `Element` from today's flat inputs (`element` /
   `element_id` in the dict, `element_metadata` / `element_resources`
   keywords) and the id format is unchanged; commit 3 moves that
   construction to the caller.
2. `feat(host)!: host id and name come from the element name, board, and slot`
   (§2.2–§2.4, §6, §7)
3. `refactor(labs)!: the factory takes the element as one argument`
   (§2.6, §5, §8.6, §8.8)
4. `feat(declared)!: match keys mirror the host attribute path`
   (§8.1, §8.2)
5. `docs: element object and host identity` (§9)

## 12. Compatibility

Any lab whose elements declare an `id` gets new host ids, so its recorded
static link ids, `host_preferences` selectors, reservation identifiers that
embed a host id, and live tunnel markers change once. The shipped fixtures
and the getting-started examples declare no element ids and no repeated
names, so every shipped id is unchanged. Lab files whose elements repeat a
name must be renamed before they load. Settings files using the old
declared-product match keys get a settings error naming the new key. Custom
lab-source backends change their factory calls per §5. This cost was
weighed against the installed base and accepted.

## 13. Out of scope

- A container inheriting the element of the host it runs on.
- Element-level verbs or an element registry on `Lab`
  (`lab.elements`, `lab.hosts_of(element)`); `host.element` identity makes
  these cheap to add later if wanted.
- Letting an inventory record supply `element.id`.
- Any change to `slug`, `make_link_id`, or the tunnel sentinel format.
