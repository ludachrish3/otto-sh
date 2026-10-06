# The API dump: what the golden records and how changes are judged (amends spec 1)

**Status:** v1, for owner approval. **Date:** 2026-10-05.

**Amends:** spec 1 (`2026-10-04-public-api-manifest-design.md`). It replaces spec 1's golden
format, its `Host` lines and its compatibility rules, and it replaces Q1.

**The dump is the only v2 format.** P0's earlier `name`-line golden was never committed live
and never pushed, so it is deleted rather than supported. otto has exactly two golden formats:
v1, today's, and this one. There is one transition between them, in P1. Everything else in
spec 1 still holds: the declaration, the namespaces, the validator, the hard cutover, P1 and the
file-operation accounting. Spec 1 §6 links here instead of restating.

**History.**
- The owner asked whether an industry tool already does this.
- Proposal A used griffe as the diff engine. Codex rejected its defining-path filter as unsound
  (C1).
- The owner chose proposal B instead: a committed runtime dump, the pattern of Kotlin's
  binary-compatibility-validator, .NET's `PublicAPI.Shipped.txt`, metalava and API Extractor.
- Codex reviewed B twice: v1 "adopt with changes", v2 "ready with changes", no critical
  findings. This document is B v2 with every correction from the second review applied.

## 0. Owner decisions (2026-10-05)

| # | Decision |
|---|---|
| D-1 | **Per-commit freshness.** Every v2-era commit's dump is regenerated from that commit, in its own locked dependencies (§5). |
| D-2 | **Implementer obligations are gated.** A class that gains an abstract or protocol member breaks implementers. |
| D-3 | **Version-invariant records.** The dump is byte-identical on every supported Python minor (3.10–3.14). A declared enum that differs across minors fails a matrix lane; there are no per-version dumps. |
| D-4 | **Two relaxations, stated as policy.** `Host` keyword-only parameter *order* is no longer checked. Members inherited from non-otto bases are not enumerated (§3.5). |
| D-5 | **Gated ⟺ public ⟺ no leading underscore,** for members of declared classes. The owner: "a gated symbol and a public symbol are one and the same." Replaces spec 1 Q1 (§7). |
| D-6 | **Keyword interception is not breaking.** Adding `x=0` in front of `**kw` keeps every old call working; `x` is now a named parameter instead of a `kw` entry. |
| D-7 | **Versioned formats are declared,** each with the set of versions otto reads and writes. Dropping a version is a marked break; adding one is how otto grows backwards compatibility (§13). |

## 1. Scope

The dump is produced at runtime by the P0 agreement child (`scripts/api_agreement.py`), keyed by
**public binding**: `ns:N` for each name in a declared namespace's `__all__`, plus nested classes
reached through it (§3.4).

**It covers:** names and kinds, call shapes, constructor inputs, members, property capabilities,
public ancestry, implementer obligations, enum members and encodable default values.

**It does not cover**, and nothing here claims complete visibility:
- annotations and type compatibility;
- behaviour;
- opaque default values (§2.4) and constant values;
- members inherited from non-otto bases (§3.5);
- the precedence between several constructor inputs supplied together (§3.3);
- serialisation aliases.

A break in any of these is still expected to be marked, by convention and review.

## 2. Format

### 2.1 The file

The golden stays at `tests/unit/api_snapshot/public_api.txt`.
- Line 1 is `# api-snapshot v2`. Line 2 is `# producer-schema 1` (§5.4).
- Every other line is one record: tab-separated fields, `<kind>\t<key>[\t<field>…]`. A value
  never contains a raw tab or newline; it is encoded (§2.4).
- A field that holds a list (`mro` entries, `call` parameters, `abstract`/`requires` names,
  `input` routes) separates its elements with single spaces; encoded values never contain a
  space. An empty list is written `-`.
- A name inside a field (a parameter, member, input or enum-member name) is any string
  `str.isidentifier()` accepts: Python's own rule, so every name the producer can meet parses.

### 2.2 Order and identity

- **Records are grouped by binding, and bindings are sorted by key.** Within a binding, they appear in a fixed kind order:
  `name`, `mro`, `call`, `input`, `member`, `abstract`, `requires`, `enum`. Records of the same
  kind are sorted by their sub-key; `enum` records keep definition order.
- **A record's identity** is its kind and key, plus a sub-key for the kinds that repeat:
  - `name`, `mro`, `call`, `abstract` and `requires` are unique per key;
  - `member` repeats per member name;
  - `input` repeats per input name;
  - `enum` repeats per member name;
  - `format` is unique per format name. Format records have no binding and come after every
    binding group (§13.2).
- **Freshness.** Any byte difference from the regenerated dump, a re-sort included, is a
  freshness refusal. The compatibility comparison runs on parsed records, so a re-sort produces
  no compatibility finding.

### 2.3 Strict parsing

The parser refuses:
- an unknown kind;
- a duplicate identity;
- a record whose binding has no `name` record (`format` records excepted, §13.2);
- a binding missing a record its kind requires:

  | Kind | Required records |
  |---|---|
  | `function`, `callable` | `call` |
  | `class` | `mro`, `call`, `abstract` |
  | `model` | `mro`, `abstract` (plus one `input` per field) |
  | `protocol` | `mro`, `requires` |
  | `enum` | `mro` |
  | `module`, `alias`, `typeddict`, `value` | `name` only |

  A required record whose list is empty is still written, with `-`;
- a malformed field.

A refusal is never read as "no change". Every record round-trips: `render(parse(line)) == line`,
and that is tested.

### 2.4 Value encoding

Values carry a type tag, so the text does not depend on `repr`:

| Value | Encoding |
|---|---|
| `None` | `N` |
| `bool` | `B:true`, `B:false` |
| `int` | `I:<decimal>` |
| `float` | `F:<float.hex()>`; `F:nan`, `F:inf`, `F:-inf` |
| `str` | `S:<json.dumps(v, ensure_ascii=True)>` |
| `bytes` | `Y:<hex>` |
| `tuple` | `T:[<encoded>,…]` |
| `frozenset` | `Z:[<encoded>,…]`, sorted by the encoded text |
| enum member | `E:<class>:<name or *>=<encoded value>` |
| anything else | `O` (opaque: "a default is present, its value is not tracked") |

**Enum values are dispatched before primitive types,** so an `IntEnum` member is an enum, not an
`int`.
- `<class>` is the sorted set of **all** the class's public bindings, written as in §3.1. Two
  `E:` values are equal when their class sets intersect and their name and value match, so adding
  an alias never reads as a default change.
- An enum default whose class has **no** public binding is a producer refusal: "declare the
  enum". A module path would make a spec-5 move of the enum read as a changed default.
- `<name>` is the member name only when the value belongs to a member declared in
  `__members__`. A composite or unnamed flag value writes `*`: its runtime name is generated, and
  differs between 3.10 (`None`) and 3.14 (`"A|B"`).

An enum default therefore records both its identity and its value, so a value change is caught
even when the change is to the enum, not to the function.

## 3. Records

### 3.1 Bindings

`name\t<key>\t<kind>`, one per binding. Classification is tried in this order:
1. a module: `module`;
2. a typing alias (`typing.get_origin(x) is not None`): `alias`;
3. a TypedDict (`typing_extensions.is_typeddict`, which also recognises the backport): `typeddict`;
4. an enum: `enum`;
5. a protocol (`_is_protocol` on the class itself): `protocol`;
6. a pydantic model: `model`;
7. any other class: `class`;
8. a function: `function`;
9. any other callable with a signature (`functools.partial`, a callable instance): `callable`;
10. anything else: `value`.

A `functools.partial` is tested for before rule 8. Python 3.14's `inspect.isroutine` reports one
as a routine and earlier versions do not, so a binding is `callable` on every version.

**Ancestry:** `mro\t<key>\t<entries>` for `class`, `model`, `enum` and `protocol` bindings.
- An entry is a base that is a declared public binding, or a `builtins` class other than
  `object` (exception bases, `str`, `int`, …), written `@builtin:builtins.ValueError` as in
  §3.2. Private and otto-internal bases have no entry.
- A `builtins` class is always written `@builtin:`, even when a public binding is bound to it
  (`otto.host.OsType = str`). A binding never stands in for a builtin, so binding one changes no
  other class's ancestry.
- `mro` records the `isinstance` facts only. Members are recorded separately: a class's `member`
  records are its effective members along the whole MRO, public bases included (§3.4).
- A public base is written as the sorted set of **all** its public bindings, such as
  `{otto.host:BaseHost}`. Two entries name the same base when their sets intersect, so adding a
  public alias for a base never reads as an ancestry removal.

### 3.2 Calls

`call\t<key>\t<callkind>\t<params>` for `function`, `callable` and `class` bindings. For a class,
the call is its constructor.
- **`callkind`** is `sync`, `coroutine` or `asyncgen`. It is read from the object whose
  signature is recorded, after following `__wrapped__` as `inspect.signature` does. For a callable
  instance that is `type(obj).__call__`, and for a `functools.partial` the wrapped function. A
  class is `sync`.
- **`params`** is a list of `<kind>:<name>:<default>` tokens. `kind` is one of `PO`, `PK`, `VP`,
  `KO` or `VK`. `default` is `-` for none, or an encoded value (§2.4).
- **The receiver.** For a method or classmethod, the first parameter is dropped only when it is a
  `PO` or `PK` parameter. A `def m(*args)` keeps its `VP`.
- **Unreadable signatures.**
  - A class whose constructor is inherited unchanged from a builtin records
    `call\t<key>\tsync\t@builtin:<module>.<qualname>`, naming the first `builtins` class on its
    MRO, for example `@builtin:builtins.str`, or `@builtin:builtins.Exception` for
    `class OttoError(Exception)`.
  - Any other failure is a **producer refusal** naming the binding. Nothing falls back to a
    placeholder.
- **A metaclass `__call__`.** A class whose metaclass defines its own `__call__` (anything but
  `type.__call__`; enum metaclasses excepted, §3.7) is a producer refusal: its constructor
  contract lives in that `__call__`, which the dump does not model.

### 3.3 Constructor inputs

`input\t<key>\t<input>\t<required|optional>\t<default>\t<routes>`, for bindings whose synthetic
signature is not the real input contract. `default` is `-` for a required input, or an encoded
value (§2.4). A `default_factory` is `O`; the factory is never called.
- **Pydantic models** get `input` records instead of a `call` record. Pydantic's synthetic
  `__init__` signature can be wrong: with `validate_by_name=False` it still shows the alias alone,
  whatever the field accepts.
  - There is one record per model field.
  - `routes` is the sorted list of accepted validation routes, computed from the model's
    **effective configuration**. A route is a string key (`S:"a"`) or a key/index path
    (`T:[S:"payload",I:0]`).
  - `AliasChoices` contributes every choice. `validate_by_name`/`populate_by_name` add the field
    name. `validate_by_alias=False` removes the alias routes.
- **TypedDicts** get one record per key, from `__required_keys__` and `__optional_keys__`. The
  route is the key itself, and the default is `-`.
- Dataclasses and NamedTuples keep their `call` record, because their synthesised signature is
  accurate.

Input names are data keys, not Python members. D-5 does not apply to them.

### 3.4 Members

`member\t<key>.<m>\t<mkind>[\t<callkind>\t<params>]`, for every **public** effective member of a
`class`, `model`, `protocol` or `enum` binding.

**Kinds:**
- **`method`, `classmethod`, `staticmethod`.** Each carries a call (§3.2).
- **`property:<g|-><s|-><d|->`.** Records getter, setter and deleter presence separately. A
  `functools.cached_property` is `property:g--`.
- **`field`.** An instance attribute declared through a dataclass field, a pydantic model field, a
  NamedTuple field, or a protocol data member. A protocol data member is an annotation-only
  `x: int`, own or inherited, found through `typing_extensions.get_protocol_members`. These are
  absent from the class `__dict__` when they have no default, so ordinary member discovery misses
  them. Types are not recorded. A `__slots__` entry (a member descriptor) is a `field` too.
- **`attribute`.** Any other class attribute that is not a descriptor. A `functools.partial`
  class attribute is an `attribute` on every version: 3.13 gave it `__get__`, but how a partial
  binds as a method is Python's behaviour, not otto's.
- **`class`.** A nested public class. It is also emitted as its own binding `<key>.<m>`, with a
  full record set.
- **`classref=<binding>`.** A nested class reference back to a class already on the current
  traversal path, such as `C.Self = C`. It is not traversed again, which keeps traversal finite.
  A class reached by two non-cyclic paths is emitted under both, so each exposure path is gated.

**Public** means: the name does not start with `_`, or it is one of the supported dunders:
- `__call__`;
- `__enter__`, `__exit__`, `__aenter__`, `__aexit__`;
- `__iter__`, `__next__`, `__aiter__`, `__anext__`;
- `__len__`, `__contains__`, `__getitem__`, `__setitem__`, `__delitem__`;
- `__eq__`, `__hash__`.

**Discovery reads `__dict__` along the MRO,** never `dir()`, so a custom `__dir__` hides nothing.
It covers every otto class on the MRO, the class itself included, public or private, plus each
one's dataclass, pydantic or NamedTuple field metadata. A class is an otto class when its module
is `otto` or `otto.*`. A public binding bound to a class otto does not define (`OsType = str`)
gets its `name`, `mro`, `call` and `abstract` records, and no `member` records (§3.5). For an `enum` binding, names in
`cls.__members__` are excluded: the `enum` records carry them (§3.7).

**Unclassifiable members are refused.** A public member whose value defines `__get__`, `__set__`
or `__delete__` and is none of the above (a function, `classmethod`, `staticmethod`, `property`,
`cached_property`, a field, or a class) is a producer refusal, not a skip.

### 3.5 External bases (D-4)

- Builtin bases appear in `mro`, so `except ValueError` stays protected. Their members are never
  enumerated: they vary by Python version, and they are the standard library's to promise. The
  same holds when a public binding *is* such a class.
- Members inherited from a third-party base are not enumerated either. Replacing such a base can
  remove methods users rely on, and that is **not detected**. This is a stated coverage limit.

### 3.6 Implementer obligations

- **`abstract\t<key>\t<names>`:** `sorted(cls.__abstractmethods__)`, the effective set after
  inheritance and overrides.
- **`requires\t<key>\t<names>`:** for a protocol,
  `sorted(typing_extensions.get_protocol_members(cls))`. That excludes the protocol machinery
  (`_is_protocol`, `_abc_impl`, …).
- **Producer refusal (D-5):** a declared class whose `abstract` or `requires` set contains an
  underscore name, dunders excepted. "Hidden obligation `X._open`: give it a public name."

### 3.7 Enums

`enum\t<key>\t<member>\t<value>` over `cls.__members__.items()`, so aliases, zero flags and
declared composite flags are all recorded, in definition order. A member whose value cannot be
encoded is a producer refusal: its encoding is `O`, or holds `O` anywhere inside a `T:` or `Z:`.

## 4. Compatibility rules

Rules compare a commit with its parent, record by record. Shape rules apply only to bindings that
exist on both sides; a new binding is an addition, even an abstract one. Spec 1's name, namespace
and stability rules (P0) still apply independently.

### 4.1 The call invariant

**A call shape change is safe when every call the old signature accepted is still accepted, and
every positional argument still lands in the same parameter slot.** Keyword interception is
allowed (D-6). Positional-only parameters are identified by position, so their names never
matter.

The rules below implement the invariant. Where a rule here and §4.3 seem to overlap, §4.2 wins.

### 4.2 Breaking: needs `!` or a `BREAKING CHANGE:` footer

**Names and kinds:**
- a `name` or `member` record removed;
- a binding's or member's kind changed, including `callkind` (`sync` ↔ `coroutine` ↔
  `asyncgen`), and including `member` ↔ `classref`. A `property` member's capability letters are
  compared by the Properties rule below, not as a kind change;
- a `@builtin` constructor reference changed.

**Calls:**
1. **Removed named parameter.** A `PK` or `KO` parameter is removed (a lost `PO` is a lost slot,
   rule 3). This applies even
   when `**kw` would absorb a removed keyword: a conservative rule beyond the invariant.
2. **Renamed keyword.** A `PK` or `KO` parameter is renamed. Renaming `PO`, `VP` or `VK` is
   not breaking.
3. **Moved positional slot.** Slot *n* (`PO` or `PK`) is gone, or a `PK` slot now holds a
   parameter with a different name. A `PO` slot is identified by position alone.
4. **Positional capture.** The old signature had `*args`, and the number of positional slots in
   front of it grew. That covers both an inserted optional positional and a `KO` parameter
   turned positional.
5. **New required parameter.** A new `PO`, `PK` or `KO` parameter has no default.
6. **Narrowed kind.** `PK` → `KO`, `PK` → `PO`, `PO` → `KO` or `KO` → `PO`.
7. **Keyword collision.** `PO` → `PK` while `**kw` exists: `f(1, x=2)` used to put `x` in `kw`
   and now raises `TypeError`.
8. **Default removed.**
9. **Encoded default changed.** That includes simple ↔ opaque, in either direction (a
   conservative rule).
10. **`VP` or `VK` removed.**

**Properties:** a getter, setter or deleter is lost.

**Inputs:**
- an `input` removed;
- an `input`'s encoded default changed or removed, by rules 8 and 9 above;
- a route removed from an `input`;
- an `input` going from `optional` to `required`;
- a new `required` input on an existing binding.

**Ancestry:** an `mro` entry with no matching entry (§3.1) on the new side.

**Obligations (D-2):** an `abstract` or `requires` set gains a name, on a binding that existed in
the parent.

**Enums:** an `enum` member removed, or its encoded value changed.

**Formats:** a version removed from a format's `reads` or `writes`, or a declared format removed
(§13.3).

### 4.3 Not breaking

- a new binding, member, field or `optional` input;
- a new route on an existing input;
- an optional `KO` parameter;
- an optional positional appended when there is no `*args`;
- an optional parameter that intercepts keywords from `**kw` (D-6);
- a `VP` or `VK` added;
- a default added;
- a widened kind that is not rule 4 or rule 7;
- a renamed `PO` or collector;
- a property capability added;
- an `mro` entry added. This does not prove behaviour is unchanged: method resolution changes
  remain a review matter;
- an obligation dropped. A protocol data member removed outright is still a member removal
  (§4.2), because it is also a `field` record;
- an enum member added;
- a re-sorted dump (caught by freshness instead, §2.2).

### 4.4 Marks never excuse refusals

A mark excuses §4.2 findings only. It never excuses:
- a stale or non-canonical dump;
- a producer refusal;
- a parse or schema refusal;
- an environment or provenance refusal (§5);
- spec 1's rollback, deletion, merge-leaves-v2 and manifest refusals.

## 5. Per-commit generation (D-1)

### 5.1 Inputs

For each v2-era commit, the producer reads only that commit:
- `git archive <full sha> -- <paths>` of only the paths regeneration reads: `src/`,
  `pyproject.toml`, `uv.lock` and the manifest. The build backend under `scripts/` is never
  needed, because otto is never built or installed (§5.2). A path the archive omits but uv
  needs fails `uv sync --locked` loudly; it cannot produce a wrong dump. Extraction cost grows
  with the number of members (§5.5), so the archive holds nothing else;
- that commit's `api/public.toml`, `pyproject.toml` and `uv.lock`.

The checker never reads the working tree or an installed otto. That is why CI's detached checkout
(`.github/workflows/ci.yml:425-434`) and `make gate-fresh` see committed content.

**The developer loop.** `make api-snapshot` and `make check-api-snapshot` run the same child
(§5.3) on the working tree, with `<repo>/src` as the provenance root and the developer's own
environment for dependencies. So a developer regenerates the dump before committing, and the
checker then proves that each commit equals its own regeneration. Both paths share one producer,
so their bytes agree.

### 5.2 Dependencies

- **Dependencies are cached apart from otto.** The cache is built with
  `uv sync --locked --no-install-project` and `--no-default-groups`, so only otto's runtime dependencies are present. Its key covers
  the `uv.lock` hash, the dependency sections of `pyproject.toml`, the full interpreter version,
  the platform tag and the uv version.
- **Lock agreement.** `uv sync --locked` validates the lock against `pyproject.toml` when a cache
  entry is built. The key covers both, so a cache hit means a pair already validated.
- **otto itself is never installed into the cache.** The child puts the archive's `src` first on
  `sys.path`. An editable install would pin the cache to whichever archive built it, because the
  lock installs otto editable (`uv.lock:1260`).
- **Distribution metadata is not provided.** otto reads its version lazily (`version.py:8`), not
  at import. If a namespace's import ever needs the metadata, that is a refusal until this spec
  says how to build it from the same commit.
- **A lock that no longer resolves** (a yanked package, say) is an environment refusal, distinct
  from a stale dump.
- **uv acts on the archive alone.** Inherited uv environment variables that select which
  project, lock, environment or working directory uv uses (`UV_PROJECT` and its kin) are
  removed before `uv lock --check` and `uv sync`; the key cannot see them. Cache, index,
  network and offline settings pass through, so an air-gapped index still works.

### 5.3 The child process

- **Environment allowlist.** The child starts from an empty environment plus:
  - `PATH`;
  - a temporary `HOME` and `XDG_*`;
  - `LANG=C.UTF-8` and `TZ=UTC`;
  - a `PYTHONHASHSEED` the caller supplies: the gate passes one fixed value, and the §6 lane a
    different one per interpreter;
  - `PYTHONDONTWRITEBYTECODE=1` and `PYTHONNOUSERSITE=1`.

  Nothing else is inherited, credentials included. The working directory is a temporary
  directory. This is hygiene, not a sandbox.
- **Provenance.** After **all** traversal, lazy exports included, every `sys.modules` entry named
  `otto` or `otto.*` must resolve, through `os.path.realpath`, inside the archive. The check covers
  `__file__` and every `__path__` entry. A module with neither is unprovable and refused.
- **Failures.** A hard timeout kills the process group. A crash, timeout or malformed output is a
  refusal with its own diagnostic.

### 5.4 Producer schema

- The dump declares `# producer-schema <n>`. Today there is one schema, `1`. HEAD's dump must use
  HEAD's current schema.
- **The schema never decreases** along any parent edge, merge parents included. A decrease is
  refused, like a v2 → v1 rollback.
- **What counts as a bump.** A producer change that alters the bytes of any committed dump under
  the current schema is a schema bump. A change that does not (a refactor, a diagnostic) is not.
- **A schema bump is a change to this spec.** The bumping commit must add a row for the new number
  to the table below. Whether the change is meaningful is a review matter, not a mechanical one.
- **Which commits are regenerated.** Every commit inside the checked range is regenerated. A
  comparison parent outside the range, such as the landing parent on `main`, is read as its
  committed dump, parsed under the schema it declares; its own freshness was proved when it
  landed.
- **When the first bump happens**, it must also ship a retained producer for the older schema.
  That covers extraction, normalisation and refusal, not just rendering, so commits in a range
  that predate the bump can still be regenerated. Comparisons across the bump run on the facts
  both schemas carry:
  - facts new in the bump have no retroactive obligation;
  - facts the older schema protected stay protected.

  No migration framework is built before then.

| Schema | Introduced by | Change |
|---|---|---|
| 1 | P0 rework | initial v2 records (this spec) |

### 5.5 Cost

Cost is measured in **file operations**, never wall-clock time: the count does not move with load
or host, and file operations are what a network filesystem charges for. The count is
`strace -f -e trace=%file,getdents64` over the whole process tree (git and uv included), less
`/proc`, `/sys` and `/dev`, as the import budget counts it.

Per commit, the producer does one archive extraction and runs one child, and it builds a
dependency cache only per new key. Measured in P0 on a real range with a dependency change
(`0b29aa0a^..0b29aa0a`, a hypothesis bump; 3,210 records):

| Commit | Archive | Environment | Child | Total |
|---|---|---|---|---|
| cold: new key, empty uv cache | 10,063 | 9,602 | 8,515 | 28,180 |
| new key, uv cache warm | 10,063 | 5,724 | 8,512 | 24,299 |
| warm: cache hit | 10,063 | 309 | 8,511 | 18,883 |

Extracting the whole tree instead (2,578 files) cost 67,485 operations per commit: tarfile's safe
extraction resolves every member's path, statting each ancestor of the destination. That is why
§5.1 archives only the inputs. A squash-landed branch is checked as its squash commit against the
landing parent; no extra policy is needed.

## 6. Version invariance (D-3)

- **A matrix lane** produces HEAD's dump on 3.10, 3.11, 3.12, 3.13 and 3.14, each under a
  different `PYTHONHASHSEED`, and asserts all five are byte-identical to the committed dump.
- **Fixture packages run on the same matrix.** They cover dataclass, pydantic, NamedTuple,
  TypedDict (both `typing` and `typing_extensions`), Flag and IntFlag including composites,
  aliases, Unicode defaults and nested classes.
- **A declared enum whose records differ across minors fails the lane.** For example, `auto()`
  mixed with explicit values. The fix is explicit values.

## 7. D-5 in practice

### 7.1 The rule

- **What it says.** A member of a declared class is gated if and only if it is public, and it is
  public if and only if its name has no leading underscore (dunders follow §3.4's list). "Every
  public member" means the supported members of §3.4, within §1's coverage.
- **What it does not cover:**
  - **Modules.** Module internality stays declared by `api/public.toml`.
  - **Input names and TypedDict keys.** These are data, not members.
- **Allowed underscore storage.** Private storage behind a public member is normal: pydantic
  requires `PrivateAttr` names to start with an underscore. Dataclass fields that are pure
  storage, such as `RemoteHost._connections`, stay private.
- **External hooks.** A hook whose spelling an external framework fixes stays under that spelling.
  If otto users need to override it, an internal adapter calls a public otto hook.

### 7.2 Producer enforcement

The producer refusal of §3.6 makes a hidden obligation impossible from P1 on. It does not find
optional concrete hooks, or seams that exist only as constructor inputs. The validator and the
rename inventory cover those.

### 7.3 The docs validator: `taught-private-member`

Spec 1 §6's validator gains one check. Within its existing teaching boundary and binding scopes, it
reports taught code that does any of the following. Supported dunders (§3.4) are public, so
overriding `__aenter__` is never a finding.
- **overrides an otto underscore member.** That means a `def`, `async def`, assignment or
  annotated assignment of `_x` in a class body, where `_x` is an effective member of an otto base
  class;
- **passes an underscore constructor keyword** to an otto class, such as
  `UnixHost(_connection_factory=…)`;
- **reads or writes `_x`** on an otto class, on an object it constructed, or on `self` inside a
  subclass of an otto class, where `_x` is an effective member of that otto class.

**Scope.**
- The validator tracks explicit imports of declared otto classes, simple aliases, local subclasses
  in the same scope, and names bound by constructing an otto class.
- It learns each class's underscore members from the runtime class, in the agreement child.
- A reader's own helper (`def _my_helper` in their subclass, where otto defines no `_my_helper`) is
  not a finding.
- A construct it cannot follow fails loudly, as spec 1 §6 already requires. It does not try to
  infer types.

### 7.4 The rename inventory (P1)

P1 gives every contract underscore member a public name, once, in the cutover. A sixth P1 footer
inventory records each rename, **frozen from the pre-cutover tree**.
- **What it covers, exactly:**
  1. every underscore name in a declared class's `abstract` or `requires` set;
  2. every underscore member, attribute or constructor keyword that the §7.3 check reports on the
     pre-cutover teaching corpus.

  Nothing else is renamed in P1: underscore storage nobody is taught stays private.
- **The rename commit** updates implementations, overrides, callers, docs and examples together.
- **Independent of the golden.** v1 never recorded underscore members (`scripts/api_snapshot.py:266`,
  `:286-290`), so these renames are invisible to the golden conversion. A test asserts that the
  footer names every inventory entry.

**Today's evidence, not the inventory:**
- abstract: `_run_put`, `_run_get`, `_open`, `_write`, `_read_until_pattern`;
- taught: `_dispatch_per_file`, `_apply_mode`, `ZephyrFrame._region_before_end`,
  `_connection_factory`, `_login`, possibly `_put_one`/`_get_one`.

The P1 plan derives the full inventory by running both mechanisms on the pre-cutover tree. New names are chosen per seam in P1's
review, following the standard-library precedent of descriptive hooks documented "override, don't
call" (`logging.Handler.emit`, `asyncio.Protocol.data_received`).

## 8. The v1 → v2 conversion (P1)

The conversion stays mechanical:
- **v1 root and deep lines** must reappear as `name` records.
- **Bare v1 module lines** map to a namespace in `api/public.toml`.
- **v1 `Host` lines** map through a fixed table to `member otto.host:Host.<m>` records. `Host` is
  already in `otto.host.__all__`, so these exist as soon as P1 declares the `otto.host`
  namespace. Each is compared under v1's projection: `PO` and `VP`
  are excluded, and `VK` is the `"**"` sentinel (`src/otto/testing/conformance_host.py:94-103`).
  P0's safe-widening rule (`scripts/check_breaking_marks.py:535-542`) still holds, so an optional
  trailing parameter is not a break. Keyword-only order is not compared (D-4).
- **Every v1 `Host` line must be accounted for** by the table.
- **New records** carry no retroactive obligation. Underscore renames are handled by §7.4, not by
  the conversion.

## 9. Merges

- A merge is judged against its first parent, as in P0.
- A merge whose result leaves v2 while a merged parent carries v2 is refused, marked or not
  (`scripts/check_breaking_marks.py:618-623`, `:643-650`). So is a producer-schema decrease
  through any parent (§5.4).
- A merge of a v1 first parent with a v2 second parent, whose result **keeps** v2, takes the
  conversion path.
- A breaking first-parent diff needs the merge's own mark.

## 10. Review aids

- **One file.** About 230 KB at today's surface, in the per-binding order of §2.2.
- **The checker's output is the review aid.** It lists its findings breaking first, grouping
  identical findings and naming every exposure path. A separate `api-diff` target is added only
  when someone needs one.

## 11. Proofs

Each proof runs the real producer → parser → comparator pipeline on fixture packages, never
hand-written records. A proof that uses `inspect.Signature.bind` as an oracle must allow for
3.10's false rejection of `(x=0, /, **kw)` bound with `x=1`, which a real call accepts. It must not
turn that into a breaking rule. This table is the single authority: it supersedes B v1 §8 and Codex's B v1
table, and every P0 proof it does not mention is kept.

| Area | Red: refused, or breaking when unmarked | Must not flag |
|---|---|---|
| Grammar | a binding missing a required record; a duplicate identity; an unknown kind | round trip of escapes, tuples, frozensets, non-finite floats and Unicode; a re-sort gives no compatibility finding but fails freshness |
| Bindings | a facade retargeted to an incompatible object; one of two facades retargeted | an implementation move with the same binding and shape; a new public alias of an `mro` base |
| Constructors | an unreadable signature outside `@builtin` is refused; a callable instance going sync → async; a partial's signature break | builtin-derived classes (`OsType(str)`, `OttoError` family) produce `@builtin` records |
| Members | an inherited method changed through a private base; a nested class's method; an enum method; a deleted `__setitem__`; a member hidden by a custom `__dir__`; a member turned `classref` | private base relocation; a method moved into an equivalent private base; a `C.Self = C` cycle terminates |
| Inputs and fields | a dataclass field removed (public, `init=False`); a pydantic field renamed while keeping its alias; a pydantic default changed (`x: int = 0` → `= 1`); an `AliasChoices` route removed; a `validate_by_name` switch-off; a new required TypedDict key; an annotation-only protocol attribute removed | default-factory presence stable, with the factory never called; a new `AliasChoices` route; private dataclass storage (`_connections`) changed; a `typing_extensions.TypedDict` classified as `typeddict` |
| Calls | `PK` → `PO` (a trailing `/` added); `PO` → `KO`; `PO` → `PK` with `**kw`; an optional positional inserted before `*args`; `(*args, x=0)` → `(x=0, *args)`; a keyword removed despite `**kw`; a default removed; a simple default changed; a positional inserted | an optional `KO` parameter; an optional positional appended without `*args`; `(**kw)` → `(x=0, **kw)` (D-6); a `PO` rename `(x, /)` → `(y, /)`; `*args` → `*items`; true widening without collision |
| Receivers | — | `def m(*args)` keeps `VP` |
| Properties and async | getter or deleter lost; `coroutine` ↔ `asyncgen` | a setter or deleter added |
| Enums and defaults | an alias, zero flag or composite flag deleted; an undeclared enum default's value changed; an `auto()` reorder; a value changed through an indirect constant | explicit equivalent values; an alias added; composite `Flag` defaults identical on 3.10 and 3.14 |
| Obligations | an ABC's first abstract method; concrete → abstract; an inherited method turned abstract; a protocol member added with a default; an underscore obligation is a producer refusal | a whole new abstract class; a concrete underscore helper changed; a concrete method added to an ABC or mixin |
| Ancestry | an exception class loses `ValueError` | a public alias added for a base |
| D-5 validator | a taught override of an otto `_x`; a taught `_x = Fake` class attribute; a taught `UnixHost(_connection_factory=…)`; a taught `obj._x` on a constructed otto object; an unfollowable construct fails loudly | a reader's own `_my_helper` |
| Freshness | a stale intermediate dump, even marked; a break restored by HEAD still flagged on its own commit; a namespace import failure, even marked | a dependency-changing history regenerated under each commit's own lock; an unchanged historical commit still valid after producer maintenance |
| Environment | a lock that disagrees with `pyproject.toml`; an unresolvable lock; an otto module loaded from outside the archive (editable fallback); a crash, a timeout, malformed output | detached CI and `gate-fresh` producing from committed content despite a dirty tree elsewhere |
| Schema | a producer-schema decrease, ordinary or through a merge; a schema bump without a table row | — |
| Conversion | a `Host` method loses a keyword name v1 recorded; an unaccounted v1 `Host` line; a v1 deep line dropped; an omitted rename-inventory entry in the P1 footer | a representation-only conversion; safe `Host` widening; a `Host` keyword-only reorder (D-4); a kind narrowing v1's projection never recorded, such as `(self, x)` → `(self, *, x)` (it has no retroactive obligation) |
| Merges | a merge-only shape break; a merge leaving v2; a v2 rollback through a merged parent | a harmless v2 merge; a v1-first, v2-second merge that keeps v2 |
| Formats | a version dropped from `reads`; a version dropped from `writes`; a format removed; a format constant missing, non-literal or holding a duplicate (refusal); a format module that does not import (refusal) | a version added; a new format; a constant moved to another module with the same values; a marked removal passes; a string version (`"otto-check/1"`) |
| Marks | a marked §4.4 refusal still fails, for each refusal kind | either mark form makes a §4.2 finding pass |
| Matrix (CI lane) | a divergent `auto()` enum | the real surface and every fixture byte-identical on 3.10–3.14 under two hash seeds |

## 12. P0, rebuilt (still dormant)

P0 is rebuilt on its unpushed branch and re-squashed into a single commit, so no commit in
history carries the `name`-line format. Nothing live is enforced before P1.
- **`scripts/api_agreement.py`:** the child emits the §3 records, plus the D-5 member data the
  validator needs.
- **Deleted:** P0's `name`-line format. That is `name_line`, `parse_name_line`, `_NAME_RE` and
  `render_v2` in `scripts/api_lines.py`, `surface_v2_lines` in `scripts/api_agreement.py`, and
  `_main_v2` in `scripts/api_snapshot.py`, with their tests. CI's header check for
  `# api-snapshot v2` (`.github/workflows/ci.yml:436-439`) stays.
- **`scripts/api_lines.py`:** the v2 grammar (render and parse). v1 parsing is unchanged.
- **`scripts/api_snapshot.py`:** writes the dump. `host_protocol_lines` serves only the v1 path
  and the conversion table.
- **`scripts/check_breaking_marks.py`:** §4's comparators, §5's per-commit generation, §8's
  conversion and §9's merges. The v1 → v1 path is untouched.
- **`scripts/api_teaching.py`:** gains `taught-private-member` (§7.3).
- **`scripts/api_manifest.py`:** parses `[formats.*]` (§13.1). The child resolves the constants
  and writes `format` records; `api_compat` applies §13.3. No format is declared in P0, and
  conformance samples are P1 work (§13.5).
- **No new dependency.** `typing_extensions` is already a direct dependency
  (`pyproject.toml:100`). File-operation accounting is all zeros, since only dev scripts change.

## 13. Versioned formats (D-7)

**The question.** Does a bump in a version number mean a breaking change? Not necessarily. What
decides it is whether anything outside the running otto keeps data or a process in the old
version and can no longer use it.

**Disposable storage is exempt.** Bumping a cache's schema, and so invalidating it, needs no
mark: otto discards and rebuilds it. This covers the completion cache and the shim, the
dynamic-tunnel and docker-observed caches, collected tests and the remote completion cache. The
exemption covers the schema bump only. A user-facing behaviour change, or a change to a
document that is also reused outside the cache, keeps the normal policy. The inventory snapshot,
for example, is also a supported export.

### 13.1 Declaration

`api/public.toml` gains one `[formats.<name>]` table per retained versioned interface. `<name>`
is a kebab-case identifier. Each table points at runtime constants and holds nothing else:

```toml
[formats.link-sentinel]
reads  = "otto.link.sentinel:READ_VERSIONS"    # the versions otto accepts
writes = "otto.link.sentinel:WRITE_VERSIONS"   # the versions otto emits
```

- **`reads`** is the set of versions otto accepts as **retained input**, whoever wrote it:
  otto, an older otto, a user or another tool. **`writes`** is the set of versions otto emits for
  others to read. A format declares at least one of the two. They are independent: a reader
  need not have a single "current" version, and the link sentinel already reads `"v1"`, `"v2"`
  and `"v3"` while writing `"v1"` and `"v3"`.
- **A version is an `int` or a `str`.** The `otto check` report's marker is `"otto-check/1"`.
- **Each constant is a literal list,** and must live in a dependency-light module. The child
  imports that module, and never imports or runs a reader to learn a version. If the module
  does not import, that is a refusal; the producer never falls back to a static value.
- **Every acceptance and dispatch path uses the declared `reads` constant,** including the
  browser's reader of the monitor export, and **every writer chooses or validates its emitted
  version against `writes`.** P1 wires each one. A path that checks its own literal is a defect
  that review catches; the gate cannot see it.
- **`reads` promises reading only.** Appending to or editing a file of an older version needs
  its own migration or compatible writer. Until one exists, mutation stays restricted to the
  current shape (the monitor database is the case today). An incompatible change of shape
  needs a new version, even when the old number could be kept.

### 13.2 Record

`format\t<name>\t<reads>\t<writes>`. Both are lists of encoded versions (`I:` or `S:`),
sorted by encoded text, `-` when empty.
- **Standalone.** A `format` record has no binding; §2.3's owner rule does not apply to it.
- **Identity** is `(format, <name>)`.
- **Position.** All `format` records come after every binding group, sorted by name.
- **Producer refusals:**
  - a missing constant;
  - a constant that is not a literal list of `int`/`str`;
  - a duplicate version;
  - a declared constant whose list is empty. A pointer that names no version declares
    nothing; a side with no versions omits its pointer instead;
  - a module that does not import.

### 13.3 Compatibility rules

**Breaking (§4.2):**
- a version removed from `reads` or from `writes`;
- a declared format removed.

Moving a constant to another module, with the same facts, is not a change: the record carries
values, not pointers.

**Not breaking:** a version added to either set (how otto grows backwards compatibility), or a
new format declared.

**Not detected:**
- an incompatible change that keeps its version number, such as a field dropped from
  `tickets.json` while it still says 2;
- a reader whose validation silently narrows what a version accepts.

Both are review matters. P1's conformance tests (§13.5) are the evidence for the second.

**Marks and refusals.** A format support removal is a markable §4.2 finding. It is a data-format
version, not the producer schema: lowering it with a mark is allowed. The producer-schema,
golden and manifest refusals of §4.4 and §5.4 are unchanged, and no mark excuses them. Merges are
judged against the first parent, as for every other record (§9).

### 13.4 Inventory, from the code on `6d2093b9`

| Format | Versions today | Declared |
|---|---|---|
| Coverage store | reads/writes 8 | yes |
| Coverage capture | reads 3; writes 3 by default, though a caller can override the stamp today | yes |
| Monitor database | reads/writes 2 (`PRAGMA user_version`; existing v2 files also differ by columns) | yes |
| Monitor JSON export | reads/writes 1 (Python and the browser) | yes |
| Reservations JSON | reads 1 (user-maintained input) | yes |
| `tickets.json` | writes 2 | yes |
| `otto check` report | writes `"otto-check/1"` | yes |
| Link impairment sentinel | reads `"v1"`, `"v2"`, `"v3"`; writes `"v1"`, `"v3"` (remote process argv) | yes |
| Tunnel and check-echo sentinels | reads/writes `"v1"` (remote process argv) | yes |
| kmodcov interface | reads 2 (`.ko` metadata) | yes |
| Caches (completion, shim, tunnels, docker, collected tests, remote completion) | — | no: disposable |
| gcov `.gcda` stamps | GCC's and Clang's formats | no: external; dropping a toolchain dialect is a review matter |

**Retained but unversioned, recorded for the schema-diff design** (spec 1 §9):
- lab files;
- project and user settings;
- inventory documents and snapshots;
- credentials;
- coverage overrides;
- coverage metadata (`.otto_cov_meta.json`).

They get no invented version constants here.

**Other emitted formats, out of scope:**
- monitor server-sent-event fragments;
- generated JSON Schemas;
- coverage-report JavaScript chunks, which are bundled implementation;
- JUnit results, an external standard.

They are not part of this gate. One that becomes an independently consumed protocol is declared
then.

### 13.5 Conformance (P1)

Every declared `reads` version gets a frozen, **populated** sample input, and **every reader**
of the format gets a domain test that loads it through that reader's real entry point and
asserts semantic results, not just "did not raise". For the monitor export, that means Python
review, the browser's `parseExportDocument` and the SQLite `build_db_export`, including its
nested-JSON decoding. Each declared `writes` version gets an emission test. Where a format can
be mutated (append, edit), a test runs the mutation on a copy of each older sample. It shows
either a refusal, or a migration that keeps the rows and stamps the version correctly. Historical samples are frozen independently of today's writers: a SQLite sample is a
checkpointed database or frozen SQL with its historical schema, `user_version` and rows. Removing
a version deletes its sample in the same, marked commit. A failing conformance test is an
inconsistency to fix; a mark never excuses it.
