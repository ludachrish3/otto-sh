# Keyword-only public interfaces, and what a class lets a user do: design

**Status:** for owner approval. Reviewed to zero open important findings by two independent
reviewers (Astra, six rounds; a fresh Fable, six rounds) on 2026-11-07, after a builder check.
**Issue:** #934, with the lint rule the owner asked for on 2026-11-07. **Builds on:** the
public-API manifest (spec 2026-10-04) and the API dump (spec 2026-10-05), whose records,
producer and judge this design extends. Where this design and those specs disagree, this one
wins and §10 lists the amendments they receive.

## 1. Intent

otto's recorded public surface is about 4,460 sites across 740 bindings. In the last two
releases most of the breaks that reached users were one defect: a positional slot moved.
`ReportInputs`, `TierConfig`, `CoverageReporter`, `ShellProduct`, `DeclaredProduct` and
`TelnetSession` all broke because a field was inserted, removed or reordered in a record whose
constructor accepted positional arguments. A second class came from plumbing otto constructs and
users only receive (`ConnectionManager`, `TermContext` each gained a required `host_id`): their
constructors are recorded as public shape although nobody is taught to call them.

The owner's direction (2026-11-07): make keyword-only the default for every public interface,
**before any interface is labelled stable**, because it is the most basic stability mechanism and
no interface has adopted it at scale; codify it with a lint rule so it cannot erode; and let a
class declare what a user may do with it, so plumbing stops making promises nobody relies on.

After this lands:

- reordering the parameters of a public callable, or the fields of a public record, is not a
  change at all;
- adding an optional parameter or field is an addition (a new *required* one stays breaking, as
  the dump spec §4.2 already says);
- the few interfaces that read naturally positionally (`host.run("ls")`) say so in the manifest,
  and nowhere else may a positional argument be accepted;
- a class otto constructs for the user records no constructor contract, and a class users may
  not subclass records no implementer obligations, so internal changes there are unmarked.

## 2. Goals, non-goals, anti-goals

**Goals**

1. Every public dataclass record, and every dataclass base it inherits fields from, is
   `kw_only=True`.
2. Every public callable (function, constructor, method, classmethod, staticmethod) accepts its
   parameters by keyword only, except parameters listed as positional exceptions in the manifest.
3. A lint rule, in the static lane every tip runs, fails on any public callable that accepts a
   positional argument outside the exception list.
4. Per-symbol rights in the manifest: whether a user may construct a class and whether they may
   subclass it. The dump, the judge, the teaching check and the API reference honour them.
5. A sweep that applies the rights to today's surface.
6. The whole change is a hard cutover, marked, in one pull request before 0.20.0. No positional
   compatibility shims.

**Non-goals**

- Promoting anything to `stable`. Owner's rule: no stable label until most open interface
  stability issues are resolved (#933, #935, #936, #937, #938, #939, #654 remain open).
- Freezing coverage report or artifact formats.
- Designing the CLI golden (#520). The CLI *does* change here in one general way: its
  argument-versus-option rule reads the Python signature's parameter kind (§6.6), which the
  manifest shaped, so the two surfaces carry one decision. Flag names and everything else about
  the CLI stay as they are.
- Per-symbol *stability* claims (#654). This design introduces the per-symbol manifest table and
  shapes it so #654's claims join it; it does not implement them.

**Anti-goals**

- A rule with an escape hatch outside the manifest: a code-level allowlist, a comment, a lint
  disable. Every exception is one manifest entry, reviewable in one diff. The two carve-outs in
  §5 are read from definitions that already exist in one place each, and are listed here so the
  reader can judge them.
- A second definition of "public", of "parameter kind", or of "conformance". The rule reads the
  API dump; the dump already decides publicness, reads signatures and records protocol
  obligations.
- Two judges for one decision. Each break has one judge (§4.4).
- Hiding behaviour changes. §6.5 lists the ones the conversion makes; they are accepted as part of
  the cutover, not denied.

## 3. Constraints

- Python floor is 3.10: `dataclass(kw_only=True)`, `field(kw_only=True)`, `KW_ONLY` exist.
- The dump records every parameter's kind (`PO`, `PK`, `VP`, `KO`, `VK`), protocol obligations
  (`requires`), abstract members (`abstract`) and real ancestry (`mro`). Protocols are
  structural: no implementer's `mro` lists a protocol (`otto.host:UnixHost`'s lists
  `PosixPrivilege`, `PosixFileOps`, `RemoteHost`, `BaseHost`; never `otto.host:Host`).
- The dump child (`scripts/api_dump_child.py`) runs standard-library-only in a separate
  interpreter and today receives only `--src`, `--assume-dir`, `--format` and namespaces
  (`scripts/api_regen.py`, `run_child`). The manifest is already an archive input for
  `generate_at_commit` (`api_regen.ARCHIVE_INPUTS`).
- `api_compat.compare_params` already judges `PK -> KO` ("narrowed from positional-or-keyword to
  keyword-only"): the conversion commits are `!` commits. The owner accepts the one-time break.
- The manifest spec's principle "the manifest lists namespaces, never names" (spec 2026-10-04
  §4; `scripts/api_manifest.py` docstring) is amended by this design (§10): namespaces decide
  **publicness**; the symbols table carries **per-path facts** and never makes a name public.
- ONE DECISION, ONE PLACE (AGENTS.md).

## 4. The manifest: one per-symbol table

`api/public.toml` gains one table keyed by the dump's binding key (`otto.module:Qualname`, or
`otto.module:Class.method` for a member). Every later per-symbol fact (#654's stability claims
and lifecycle) is a key in this same table.

```toml
[symbols."otto.host:Host.run"]
positional = ["cmds"]
why = "implementer-facing: host classes implement it, otto calls it positionally"

[symbols."otto.lab:get_host"]
positional = ["name"]

[symbols."otto.host:ConnectionManager"]
construct = false
subclass = false
```

### 4.1 Keys

| Key | Applies to | Default | Meaning |
|---|---|---|---|
| `positional` | a callable binding or a method | `[]` | the leading parameters, in order, that callers may pass positionally; a prefix of the signature. Every other parameter is keyword-only. A listed parameter may be `PO` or `PK` in the code; an unlisted one must be `KO`. A `VP` is covered when its name is the list's last element (`positional = ["args"]`); the child's prefix check treats a trailing `VP` name as covering the variadic. |
| `why` | with `positional` | none | one line of justification. **Required** when `positional` has two or more names (a TOML fact, refused by the reader), and when the effective entry covers a `PO` or a `VP` of the callable (signature facts, a lint finding, §5 rule 4). Policy entries (§6.2, §6.3) carry it by convention. Optional otherwise. |
| `construct` | a `class` or `model` binding | `true` | `false`: users do not call the constructor; its contract is withdrawn (§4.2). |
| `subclass` | a `class`, `model` or `protocol` binding | `true` | `false`: users do not implement it; its implementer obligations are withdrawn (§4.2). A model carries `abstract` like a class and is treated as one there. |
| `implementer` | a method | `false` | `true`: users override it and otto calls it, although it is neither abstract nor a protocol obligation (`MetricParser.parse_tick`). It joins the implementer-facing set of §6.3 for both the calling policy and the compat judge. Members in `requires` or `abstract` records are implementer-facing without this key; the key exists for hooks with a default body. |
| `final` | a `class` or `model` binding with `subclass = false` | `true` | `false`: the class is not decorated `@final` because otto itself subclasses it (`Result` → `ShellResult`, `CommandResult` → `NotRunResult`, `ChartSpec` → `ChartSpecRecord`, `UnixFileTransfer` → the transfer kinds). Users still may not subclass it: the right is withdrawn in the manifest and the docs. Requires `subclass = false` beside it and a non-empty `why` naming the internal subclasses (prose, checked by review: the subclasses are private). Never on a protocol: a protocol's subclass right is manifest-only (§4.2). |

Reader rules (`scripts/api_manifest.py`, each a `ManifestError`, over TOML facts only): the key is
a binding or member path under a declared namespace; a class with `construct = false` carries no
`positional`; `why` present when `positional` has two or more names; `final = false` only beside
`subclass = false` and with a `why`. Which keys fit which kind
(`construct` on a class or model, `subclass` on a class, model or protocol, `positional` on a
callable or method, `implementer` on a method, `final` on a class or model) is decided by the child (§4.3), which knows the
runtime kind: a dotted key names a method or a nested class, and only the runtime tells which. The
reader records which keys an entry wrote, so the child judges explicit keys and never an omitted
default. Whether the key exists at runtime and whether
`positional` is a prefix of the real signature is decided **in the dump child** (§4.3), the one
process that has the signature.

### 4.2 Withdrawn contracts in the dump

The dump grammar (`api_records.REQUIRED`) makes `call` and `abstract` mandatory on every class
and `requires` on every protocol, and that is how a producer that silently drops a record is
caught. Rights therefore do not omit records; they put a **withdrawn token** where the contract
would be, the way an inherited builtin constructor is recorded as `@builtin:...`:

| Right | Record | Token |
|---|---|---|
| `construct = false` on a class | `call\t<key>\tsync\t@manifest:construct=false` | replaces the parameter list |
| `construct = false` on a model | one `input\t<key>\t@manifest:construct=false\t-\t-\t-` line | replaces every field input; mixing the token with field inputs is a parse error |
| `subclass = false` on a class | `abstract\t<key>\t@manifest:subclass=false` | replaces the name list |
| `subclass = false` on a protocol | `requires\t<key>\t@manifest:subclass=false` | replaces the name list |

Members, `mro` and `enum` records are unchanged: the user reads the object. Enums and protocols
carry no `call`/`abstract` today and that does not change; `construct` is refused on them by the
reader.

**The `@final` token (amended 2026-11-08).** The dump child records a code fact beside the manifest
fact: a class or model whose definition is decorated with `typing_extensions.final` emits `@final`
as the first token of its `abstract` line: `abstract\t<key>\t@final @manifest:subclass=false`, or
`abstract\t<key>\t@final name name` on an open class (the state lint finding 6 refuses, §5).
**Detection is from the source, not the runtime**: the child parses the **defining** module, located
by `cls.__module__` + `cls.__qualname__` (never the binding's module: most public classes are bound
outside the module that defines them; a dotted qualname walks nested classes, a `<locals>` qualname
is refused), and reads the `ClassDef`'s decorator list for exactly two spellings: a name bound by
`from typing_extensions import final`, or the attribute `typing_extensions.final`. `typing.final`
does not count, and no alias resolution is attempted (`import typing_extensions as te`, `final as
_f`). Two reasons, both load-bearing: `typing.final` sets `__final__` only from Python 3.11, so a
runtime read would make the golden differ between the 3.10 floor and a newer interpreter; and no
committed **class definition** uses `typing_extensions.final` (methods do; they carry no token), so
no committed dump's bytes change under the new producer and the retained-producer clause of the dump
spec (§5.4) is not triggered. A class whose decorator the child cannot see this way (another
spelling, a factory-built class, a rewritten `__qualname__`, a binding with no source such as
`OsType = str`) emits no token; that is loud, not silent: finding 5 fires on any such
`subclass = false` class and the builder answers with the recognised spelling or `final = false` +
`why`. otto therefore imports `final` from `typing_extensions` everywhere (already a direct
dependency, for `override`); the existing `typing.final` sites migrate, and a ruff `banned-api`
entry refuses `typing.final` under `src/otto`. The ban and finding 5 are what make the spelling
convention hold; no test restates the ban. The obligation record gains a `final` flag, default
false, parsed in **one place** (`api_records`); `is_withdrawn`, `implementer_members` and every
other reader take the parsed record, never the raw field, so `@final @manifest:subclass=false` is
still withdrawn and `@final` is never a fictitious member. Protocols (`requires` records) carry
neither the token nor the findings.

**Grammar, not schema.** The token extends the field grammar for these four records
(`api_records` gains a `withdrawn` state on `Call` and on the obligation records, next to the
existing `builtin` state). It is not a producer-schema bump: a tree with no `[symbols]` table
dumps exactly as today, so the tip's producer regenerates every earlier commit unchanged, and a
committed golden written before this design parses under the extended grammar. The dump spec's
"first bump ships a retained producer" clause (§5.4) is therefore not triggered. The reader
refuses a token anywhere else, and a token with any other text after `@manifest:`.

**The judge** (`api_compat._compare_call`, `_obligations`, `_inputs`): a contract becoming the
token is one finding (`<key>: constructor withdrawn`, `<key>: obligations withdrawn`); the token
becoming a contract is an addition (no finding); token to token is nothing. A withdrawn `call`
also empties the binding's `call_params`, which feeds the `taught-private-member` validator
(dump spec §7.3): a withdrawn constructor has no taught parameters.

### 4.3 The child receives the symbols table

`api_regen.run_child` passes each entry as `--symbol KEY k=v[,k=v...]`, mirroring `--format`;
`generate_at_commit` reads it from the archived manifest, so a historical commit is dumped under
its own table. The child:

- refuses an entry whose key resolves to nothing in the declared namespaces, or whose explicitly
  written keys do not apply to the resolved kind (§4.1);
- refuses a `positional` list that is not a prefix of the resolved callable's parameters, by
  name and order, or that names a keyword-only parameter (`positional = ["x"]` on `def f(*, x)`
  is refused: listed means positional in the code). It checks nothing about `why`: the
  justification depends on the effective entry, which only the parent resolves (§5 rule 4);
- refuses two public **binding** paths to one object whose entries disagree. The child's
  `keys_by_id` indexes classes today (`_add_binding`); it is extended to function and callable
  bindings so a function reachable at two paths is covered too. The rule is this design's: *each binding path carries its own entry, and
  entries for one object agree*. A path with no entry has the default (`[]`, `true`, `true`), so
  an entry on one path and none on another is a disagreement: every binding path to an excepted
  object is listed. A nested class is one object under two keys, its own binding and the outer
  class's `classref=` member, and the same rule applies. **Method members are not aliases**:
  `BaseHost.run` and `UnixHost.run` expose one function through inheritance, and that is resolved
  by §5.1's effective entry in the parent process, not by this check, which looks at bindings only;
- emits the withdrawn tokens of §4.2;
- records, for every `positional` entry, nothing new: the exception is a manifest fact, the
  signature is a dump fact, and the rule (§5) joins them in the parent process.

### 4.4 One judge per decision

| Decision | Judged by | Not by |
|---|---|---|
| a `positional` entry is a valid prefix / names a real parameter | the dump child (§4.3) | the lint rule |
| a `why` is present where the signature requires one | the lint rule (§5 rule 4), which has the effective entry | the child, the reader |
| a parameter is positional without an entry | the lint rule (§5) | the compat judge |
| a constructor or obligations are withdrawn | the compat judge, through the token (§4.2) | manifest transitions |
| a parameter stops being positional in code (`PK -> KO`) | the compat judge, as today | the manifest |
| a parameter leaves `positional` while the code keeps it `PK` | the lint rule: the tree is now red | nothing else |
| an implementer-facing member gains a parameter | the compat judge, new rule (§6.3) | review |
| `implementer = true` is removed | the compat judge, which reads both manifests (§6.3) | the lint rule |
| a class is decorated `@final` | the lint rule (§5 finding 6, or finding 5 when it is missing) | the compat judge: `@final` is **invisible** to it. The manifest token is the sole route to `obligations withdrawn`; in a lint-green tree `@final` never appears on an open class, and a second trigger would report the same class twice across two commits. |

There are **no manifest transitions** for the symbols table in `api_manifest.manifest_breaks`,
which keeps judging namespaces only. Removing an entry either changes the dump (a token goes away:
an addition), makes the lint red (a `positional` entry), or, for `implementer`, is judged by the
compat judge from the two manifests it already reads (§6.3), the one exception, named here.

## 5. The lint rule: `check-api-keywords`

`scripts/check_api_keywords.py` runs in the parent process over the dump text that
`api_regen.generate_worktree` returns (the one dump-generation boundary; it is **not** a second
generation sequence, and it never runs inside the child) and the manifest. It fails on:

1. the **positional shape** of a `call` record, or of a method/classmethod/staticmethod `member`
   record (its `PO` and `PK` names in order, then the `VP` name if it has one), differing from the
   effective `positional` entry of that callable (§5.1) in any way: an unlisted positional
   parameter, a listed one that is not positional, a covered `*args` the entry does not end with.
   Equality of shape and entry, not containment, is the invariant the CLI rule (§6.6) rests on;
   rule 3 compares the same shape;
2. a `VP` on any callable, supported dunders included, with no `positional` entry covering it (a
   `def f(*args)` or `__call__(self, *args)` accepts positionals; it needs a `why`; a fixed
   interpreter-slot count never bounds a variadic). `VK` passes: keywords are the point;
3. an override whose positional shape differs from the effective entry it inherits (§5.1);
4. an effective entry that covers a `PO` parameter or a `VP` of this callable with no `why`. The
   justification is checked here, against the **effective** entry and the actual kinds, because an
   inherited entry has no own entry for the child to check: a base `f(self, x)` listed `["x"]`
   without a `why` does not let a subclass's `f(self, x, /)` pass. The child checks nothing about
   `why` (§4.3);
5. a class or model whose effective entry has `subclass = false` and `final` not `false`, and whose
   `abstract` record lacks `@final`: `<key>: subclass withdrawn but the class is not @final —
   decorate it, or set final = false with why`;
6. a class or model whose `abstract` record carries `@final` and whose entry does not withdraw
   `subclass`: `<key>: @final on a class whose subclass right is open — withdraw it in the
   manifest, or drop the decorator`.

For 5 and 6 the manifest decides, the decorator restates the decision where a user's type checker
reads it, and the lint is the one place that holds the two together (the pattern
`check-docs-api-marks` already uses for the reference notes). `final = false` is the sanctioned
exemption; entries agree at every alias key (§5.1), so an exempt object carries its entry at each
of its keys.

It skips a withdrawn `call` (nothing to judge) and the receiver (already dropped by the dump).

**Models.** A pydantic model records `input` lines, not a `call`, and its constructor is its
fields, keyword-only by nature. The boundary is **defined in otto**: a model whose effective
`__init__` is defined in an `otto` module has a constructor the dump does not record and the rule
cannot judge, and the child refuses it (`<key>: a model's constructor is its fields; remove
__init__ or declare a dataclass`), the same shape as the existing D-5 refusals. A constructor
inherited from pydantic (`BaseModel`, `BaseSettings`, as `OttoEnvSettings` does) is the
framework's promise, the stance §3.5 of the dump spec already takes for third-party bases. No
declared model defines one today (audited 2026-11-07 over `src/otto` at `e86596af6`, the v0.19.0
tip, by AST: no `BaseModel`/`BaseSettings`/`OttoModel` subclass defines `__init__`; main is one
commit past it), so no landing range regenerates differently and no committed dump's bytes change.
The builder repeats the audit at the base of the landing.

### 5.1 Effective entry: inheritance, protocols, agreement

A member's effective entry is the union of what applies to it, which must agree:

- its own entry, keyed `Binding.method`;
- an entry on `Base.method` for any `Base` in the binding's dump `mro` (concrete inheritance,
  `BaseHost.run` to `UnixHost.run`);
- an entry on `Protocol.method` for any declared protocol whose `requires` set is a subset of the
  binding's public member names (structural conformance: `Host.run` reaches `BaseHost`,
  `UnixHost`, `LocalHost`, `EmbeddedHost` and the other hosts; `HostSession` does not conform to
  `Host` and `HostSession.run` carries its own entry). Conformance is read from the dump's
  `requires` record, not re-derived. It is wide for one-name protocols (`SupportsStatPaths`,
  `SupportsResourceHolders`, ...): an entry on such a protocol's member reaches every class with a
  same-named method, which is the intended meaning of a structural protocol and is written knowing
  that.

When several sources apply they must carry the same list; a disagreement is a finding naming
both sources. An override that renames or reorders the covered parameters is a finding (rule 3):
callers and otto itself rely on the base's names.

### 5.2 Carve-outs

Both are read from one existing definition, so neither is an escape hatch, and neither covers an
application argument:

- **Interpreter slots of supported dunders.** `api_records.SUPPORTED_DUNDERS` is the list; the
  interpreter passes `__eq__(other)`, `__getitem__(key)`, `__contains__(item)`,
  `__setitem__(key, value)`, `__delitem__(key)`, `__exit__(exc_type, exc, tb)` and
  `__aexit__(...)` positionally. The rule skips exactly the slots the interpreter fills, by a
  fixed per-dunder count in `api_records` next to the list; anything beyond those slots is
  judged like any other parameter. `__call__` has no interpreter slot: `ExpectCollector.__call__
  (condition, msg)` is an ordinary user-facing call and needs an entry.
- **Constructors inherited from a builtin.** An error class whose constructor is `Exception`'s
  records `@builtin:builtins.Exception` and no parameters; `CoverageConfigError("msg")` stays
  positional like every Python exception. The rule has nothing to judge. A class with its own
  `__init__` is an ordinary callable.

### 5.3 Third-party-dictated signatures

Some public callables have shapes a third party calls: `DockerImageSpec.model_post_init(context,
/)` is pydantic's hook; `options(cls, /, *, ...)` is a
decorator factory whose `cls` must stay positional-only beside `**dataclass_kwargs`. These get a
`positional` entry with `why = "third-party: <who calls it>"`; the `PO` is allowed because it is
listed. Nothing in the rule knows about third parties: it sees a listed parameter.

### 5.4 Where it runs

`lint-python` today is ruff plus `lint-arch`; `check-api-snapshot` is a standalone target run by no
lane, and the golden's freshness is proved by
`tests/unit/api_snapshot/test_public_api_snapshot.py` and by `check-breaking`. The rule's homes:

- a `make check-api-keywords` target, added to `lint-python` (so `make check-python` and every
  tip gate's static lane run it) **and** to the nox `lint` session that CI's `lint-python` job
  runs (`uv run nox -s lint`; the session is the Makefile recipe's twin and names each step
  itself, so the step is added there too, the way ruff, tach and ast-grep already are). The job's
  synced venv is what `api_regen.generate_worktree` needs. Its cost is one dump child, the same
  cost the unit lane already pays for the freshness test; the lane's timing is measured at the
  tip, not estimated here;
- nowhere else. No unit-test duplicate: the lane is the one home, and an intermediate commit that
  is red under it is expected (§6.6).

Message shape, one line per finding, binding key first:

```
otto.coverage:TierConfig: positional parameter max_age_days — make the record kw_only, or list it under [symbols."otto.coverage:TierConfig"].positional
```

## 6. The conversion

### 6.1 Records

Every public dataclass becomes `@dataclass(kw_only=True)` with its existing `frozen`/`slots`.
`kw_only` is **not inherited**: every dataclass base in the chain converts too, private bases
included (`Result` under `CommandResult`, `Product` under `ShellProduct`, `KmodTool` under
`KmodcovTool`), and every explicit `field(kw_only=False)` goes (eight today, in
`host/remote_host.py`, `host/unix_host.py` and `host/docker_host.py`). The sweep inspects fields,
not only decorators; the rule catches what it misses.

Pydantic dataclasses (`@options` classes built by `otto.params.options`, `otto.host.options`) are
dataclasses to the dump, not models: they convert by passing `kw_only=True` through the shared
builder, so every options record converts in one place. Today nine of them dump as
`(*args, **kwargs)` because the builder defers the dataclass: `otto.examples.options`'s
`ProjectOptions` and `DeviceTestOptions`, and `otto.project`'s `CleanupOptions`,
`GetLogsOptions`, `InstallOptions`, `InstallToolsOptions`, `StatusOptions`, `UninstallOptions`,
`UninstallToolsOptions` (`otto:app`, the Typer application object, is the tenth `(*args,
**kwargs)` call and is a callable instance, not a record; it gets the same kind of entry). Making
the producer read their real fields would change the bytes of a committed dump, which the dump
spec §5.4 defines as a schema bump with a retained-producer obligation; that is **out of this
design** and filed as #950. Until it lands, each carries a `positional` entry covering
the `VP` with `why = "deferred dataclass; real fields unrecorded, see #950"`, so the rule sees
a listed, justified exception rather than a gap.

Public `NamedTuple`s (`MetricDataPoint`, `LogEvent`; `HostKeyMatch` is in an undeclared module
and is not public) **stay NamedTuples**: a
NamedTuple's contract is tuple-ness (iteration, indexing, hashing, `_replace`, `_asdict`, tuple
ancestry recorded in `mro`), and converting one is a behaviour change, not a calling-convention
change. Each gets a `positional` entry listing its fields in order with `why = "tuple record"`.
Any field change to a NamedTuple is a marked break: an insertion shifts slots, and even a
defaulted append changes the tuple's length and breaks fixed-length unpacking (a required append
is already refused by the judge; the defaulted one is not). The judge gains the rule: for a
binding whose `mro` is `@builtin:builtins.tuple`, any change to the `call` parameter list is a
finding (`<key>: tuple record changed shape`). That is the price of a tuple and is stated at the
class in the reference.

### 6.2 Callables

Every public function, method, classmethod and staticmethod puts `*` before its first parameter,
except the leading parameters its effective `positional` entry names. The standard for an entry
users call is **the one obvious subject**: `host.run(cmds)`, `get_host(name)`, an error's
`message`, a path-first helper's `path`. Two or more need a `why` the review reads.

### 6.3 Implementer-facing methods

A protocol's `requires` members, a class's `abstract` members and the documented hooks are the one
class of callable where the convention is otto's and not the user's: otto calls them, the user
writes them. Policy:

- otto calls an implementer-facing method **positionally for its subject parameters and by
  keyword for the rest**, as `parse_tick` already does (`self.parse(output, ctx=ctx)`), and the
  subject parameters are listed `positional` with `why = "implementer-facing"`. Parameter names
  are therefore part of the implementer contract from the subject onward, which #935's
  conformance tests also want;
- **adding any parameter to an implementer-facing member is a breaking change**, optional or
  not: an existing override without `**kwargs` stops working the moment otto passes it. The
  compat judge gains this rule: for an **implementer-facing member**, a new parameter other than
  `VK` is a finding (`<key>: new parameter <name> on an implementer-facing member`; a new
  `**kwargs` on the base breaks no override). The judge therefore gains a manifest input: a member
  is implementer-facing for the comparison when **either** commit's manifest or dump says so, so
  an `implementer` entry added in the same commit as the widened parameter is judged, and one
  removed in that commit does not excuse it. Removing `implementer = true` is itself a finding
  (`<key>: implementer status withdrawn`): the user's overrides lose a promise. Adding it is free.

The implementer-facing set is one machine-readable definition, used by the policy, the judge and
the exception list alike: a member named in a `requires` or `abstract` record of a binding whose
`subclass` is not withdrawn, **or** a method whose manifest entry says `implementer = true`
(§4.1), for hooks with a default body that subclasses override and otto calls
(`MetricParser.parse_tick`, a non-abstract `Host` verb that host classes override; the
`otto.examples.*` bases carry no `implementer` entry, since no docs sentence presents a method of
theirs as overridable, and the sweep marks only what the docs teach). The sweep (§6.4) proposes the `implementer` entries from the
documented extension points; a hook left unmarked is judged like any other method, which the
contributing checklist (§7) names as the thing to check when a method is documented as
overridable.

A method that users both call and implement (`host.run`) follows this policy; today the subject
is the only positional argument otto passes.

### 6.4 Rights sweep

Candidates for `construct = false`: declared classes whose constructor no docs page, README or
shipped example calls. `scripts/api_teaching.py` already parses that teaching boundary; it gains
`--untaught-constructors`, listing declared constructible classes with no taught construction,
for the builder to review (a class the docs describe as something the user builds, without a
literal call, stays constructible). Errors are fixed: every `OttoError` subclass is
constructible and subclassable (users raise and extend them).

The converse is enforced, not reviewed: `check-api-teaching` gains a finding for a taught
construction of a class whose `construct` is withdrawn (`visit_Call` already sees the call), so
`construct = false` cannot be used to hide a constructor the docs still teach.

Candidates for `subclass = false`: concrete classes with no documented extension point. The
documented extension points (`Host` and its hooks, the `otto.host.*` protocols, monitor parsers,
the `otto.examples.*` bases) stay `true`.

Every `subclass = false` class and model is decorated `@final` (from `typing_extensions`, §4.2),
except the ones otto subclasses itself (`Result`, `CommandResult`, `HostSession`, `HostReport`,
`PosixFileOps`, `PosixPrivilege`, `ShellSession`, `UnixFileTransfer`, `EmbeddedFileTransfer`, and
the models `ChartSpec`, `TabSpec`: eleven at the 2026-11-08 sweep; the builder verifies each with
`__subclasses__()` and the list follows the code), which carry `final = false` with a `why`. The decorators land in the sweep commit,
with the right they restate (§6.7).

### 6.5 Behaviour changes, accepted

The conversion changes behaviour in these ways; each is part of the marked cutover and is stated
in `docs/api/stability.md`'s calling-conventions section:

- `__match_args__` of a `kw_only` dataclass is empty: a positional `case Cls(a, b)` pattern raises
  `TypeError` at match time (more positional sub-patterns than `__match_args__` allows). otto's own
  patterns are keyword patterns already (`case Unreadable(error=error)`); user code with positional
  patterns breaks once.
- Exceptions: `BaseException.__reduce__` rebuilds as `cls(*self.args)`. An error class whose
  `__init__` is keyword-only or whose `args` is not constructor-shaped is unpicklable, and
  `otto.coverage.errors` relies on round-tripping. Rule for every error with its own `__init__`:
  `cls(*self.args)` must succeed, so every parameter not carried in `args` has a default
  (`CoverageNotInstrumentedError(message, report=None)` round-trips today); otherwise the class
  defines `__reduce__`. `BootstrapError(root, source, cause)` is already broken this way and is
  fixed in the conversion.
- Positional construction and positional calls that worked before fail with `TypeError`. That
  is the point.

### 6.6 CLI parity: the verb reads the same decision

`otto host <id> <verb>` commands are synthesised from the `@cli_exposed` host methods by
`src/otto/cli/param_synth.py:build_cli_binding`. Today it decides argument-versus-option from the
`Arg`/`Opt` annotation markers (`otto.utils.Arg`, `Opt`) first, and from the **default** for an
unmarked parameter (no default: a positional `typer.Argument`; default: a `--option`). Both are
decisions about what is positional made outside the Python signature, and they can disagree with
it (a required keyword-only parameter marked `Arg` would still be a CLI positional). The owner's
ruling: **the function signature drives the CLI**; the manifest never does at runtime. The manifest
shapes the signature at design time, through the lint rule, and the CLI follows the signature.

The owner's direction (2026-11-07): one general mechanism for parity between the Python API and
the CLI verb, no special cases. The manifest is not shipped in the wheel and `src/otto` does not
import `scripts`, so the CLI does not read the manifest. It does not need to: after the
conversion, **the Python signature is the manifest's decision made real** (a parameter is `PO`
or `PK` exactly when its `positional` entry lists it, and the lint rule keeps that true at every
tip). `build_cli_binding` therefore reads `inspect.signature`, as it already does, and applies one
rule on the parameter **kind**:

| Python parameter | CLI |
|---|---|
| positional (`PK`; for otto's own verbs the lint guarantees this means "listed"; a project-registered host class's verb is not in the dump, and the signature still decides) | a positional argument, in signature order, required or defaulted alike |
| positional-only (`PO`) | refused at synthesis, for otto's and project-registered verbs alike: the synthesised command dispatches by keyword (`method(**call_kw)`), which cannot reach a `PO`; no exposed verb has one |
| positional and list-typed (`list[X]`, or a union containing `list[X]`, as `src_files: list[Path] | Path`; `X` names the element type) | the one variadic argument; a verb has at most one. It absorbs the command-line arguments the other positional arguments do not take, wherever it sits (click's `nargs=-1`), so `otto host <id> put a.txt b.txt /tmp/` keeps its documented shape with the variadic first |
| keyword-only, no default | a required `--option` (for a `bool`, a required `--flag/--no-flag`) |
| keyword-only, with a default | an optional `--option` with that default |
| `Exclude`-marked | not on the CLI; its default is passed through, as today |
| `*args` on a verb | refused when the verb is synthesised: no exposed verb has one, and a variadic is spelled as a positional list parameter |
| `**kwargs` | not exposed, as today |

`Arg` and `Opt` keep **only their presentation facts** (every field except the
argument-versus-option meaning and `variadic`: name, metavar, help, short, completer, `elem_type`,
`min`, `host_user`, `remote_path`, ...) and no longer decide argument-versus-option; `variadic`
is implied by a positional list type. A marker that contradicts the signature (`Arg` on a
keyword-only parameter, `Opt` on a positional one) is refused by `build_cli_binding`, so the two
cannot drift: the markers describe, the signature decides, the manifest decided the signature.

Every verb gets the same shape from the same signature. The exposed verb for a command is `exec`
(`Host.run` is the library verb and is not `@cli_exposed`): `otto host <id> exec '<command>'` is
positional because `cmd` is positional in Python, which the manifest's entry decided. A
`positional` entry therefore moves both surfaces at once, with no runtime file and no second
decision. The synthesised command calls the method entirely by keyword (`method(**call_kw)`),
which a `PK` subject accepts; §6.3's "positionally for the subject" is what otto's library-side
calls do and what implementers may rely on, and the CLI relying on every parameter name is covered
by the override rule (§5 rule 3) for listed names and by #935's conformance for the rest.

Verbs whose CLI shape changes under this rule (a required non-subject parameter that was a
positional argument, or a marker that contradicted the signature) are a marked break, part of the
same cutover. The headline case is settled here rather than left to the list: `get` and `put`
keep **two** positional subjects, `src_files` then `dest_dir`, and the exposed `cp(src, dst)` and
`mv(src, dst)` keep theirs, all four with the one `why = "transfer: sources then destination, the
cp convention the docs teach"`; their documented command lines do not change. #520 (the CLI golden) is open, so nothing mechanical records the reshaped verbs yet:
the builder lists them from the dump before commit 3 and the list goes in the pull request body
and the changelog, like the §8 churn number.

### 6.7 Commit shape

Following the PR shape rule (minimum commits, one per logical item), one pull request:

1. `feat(api)!: ...` the symbols table and reader rules, the child's `--symbol` plumbing and
   refusals, the withdrawn tokens and the judge's treatment, the implementer-facing judge rule,
   `check-api-keywords` wired into `lint-python` and the nox `lint` session, the teaching
   check's converse, the docs and the two spec amendments; the `final` key, the `@final` token and
   its one parse site, lint findings 5 and 6, the `typing_extensions` migration and the ruff ban.
2. `refactor(api)!: ...` the rights sweep, with the `@final` decorators and the `final = false`
   entries. Before the conversion, so withdrawn constructors are never converted.
3. `refactor(api)!: ...` the demotion of §6.8. Before the conversion: a demoted class needs none.
4. `refactor(api)!: ...` the conversion: records, bases, fields, callables, NamedTuple and
   third-party entries, every call site in `src/`, `tests/`, `docs/` and shipped examples, and
   the CLI rule of §6.6 with the verbs it reshapes. The builder may split it per area; each split
   leaves the tree importable and the test suite green.

The rule is red from commit 1 until commit 4 completes; only the pull request's tip is gated
(AGENTS.md), and it is green there. The golden is regenerated in each commit that changes it.

### 6.8 Demotion: a class nothing reaches leaves the surface

A class with both rights withdrawn is public only for what a user can **receive**: an instance a
public callable returns, a parameter or field type, a constant's type parameter. A registry string
(`"json"`, `"ftp"`, `"netbox"`, `"command"`) selects a registered factory under a config contract
and promises the protocol or base the factory returns, never the concrete class; a user reaches
that class only through `type()`. When no public signature, field, constant or `__init__`
annotation names the class and no docs page names it, nothing promises it to anyone, and the
honest signal is the Python one: a leading underscore.

Measured 2026-11-08 over the committed golden and manifest (class, `__init__` and callable hints
with forward references matched by name, generics and `Callable` argument lists descended,
dataclass fields, module constants' declared types): 341 public classes; 160 with both rights
withdrawn; 124 reached by a public annotation; 36 not; 10 of those named in no docs page. The
pull request demotes exactly those ten: `otto.coverage:GcdaFetcher`, `otto.creds:JsonCredsStore`,
`otto.host:CommandPowerController`, `otto.host:HostFilter`, `otto.host:SshHopTransport`,
`otto.host.transfer:FtpFileTransfer`, `otto.host.transfer:TftpFileTransfer`,
`otto.inventory:NetBoxInventory`, `otto.labs:JsonFileLabRepository`,
`otto.reservations:JsonReservationBackend`.

Mechanics: rename `_Name` in its own module (no relocation); remove it from every `__all__`, lazy
export mapping (`otto.host.__getattr__`'s table) and `TYPE_CHECKING` re-export; delete its
`[symbols]` entries; point the `:class:` cross-references that name it at the protocol or base the
factory returns, or at the kind string; registry registrations and config models are unchanged.
Cross-module imports of the private name inside `otto` are allowed: the package is the unit of
privacy. The compat judge reports each as a removal and any MRO change it causes; it is a marked
break. §6.1's rule that private dataclass bases inherited by public records still convert applies
to a demoted class a public record inherits from.

Scope boundary, not an exemption: the remaining fully-withdrawn classes no annotation reaches but a
docs page names (26 at the measurement; issue #969 carries the reviewed list) (mostly the reference page only: `HostReport`, `LocalSession`, `PosixFileOps`,
`TelnetSession`, the transfer kinds, `MonitorHandle`, `ZephyrHost`, ...) stay public in this pull
request. A per-class decision needs a route analysis the scan does not do (instance attributes,
`type()` of a registry product, public descendants' recorded MRO) and goes to one follow-up GitHub
issue listing them with the test: demote unless a user receives the object by a route the
annotations miss. The owner's direction (2026-11-08): an honest first stab, and the machinery to
track interface changes; scope grows later if users ask.

A demoted class that a later change needs to hand to users comes back by the normal route: a
public name, an `__all__` entry, a reference page, and a `[symbols]` entry saying what the user
may do with it.

## 7. Documentation

- `docs/api/stability.md` gains **Calling conventions**: keyword-only by default; the listed
  positional subjects are the exceptions and the reference shows them; the `__match_args__` and
  pickling notes of §6.5; a class marked *not constructed by callers* or *not for subclassing*
  makes no promise about its constructor or abstract members; a NamedTuple record's fields are
  positional and an insertion is a break.
- The API reference (`scripts/docs_api_reference.py`) renders, from the manifest, a note under
  each class with withdrawn rights and the positional subjects under each callable with an entry;
  `scripts/check_docs_api_marks.py` confirms the notes match the manifest.
- `docs/contributing.md`'s review checklist: a new public callable is keyword-only unless its
  manifest entry says otherwise; a new public record is `kw_only`, bases included; a class otto
  constructs for the user is declared `construct = false`; an implementer-facing member never
  gains a parameter without a mark.

## 8. Verification

- Manifest reader: each refusal in §4.1, each default.
- Child: each refusal in §4.3 (unknown key, wrong kind, non-prefix, disagreeing paths to one
  object); each token in §4.2; a tree without a symbols table dumps byte-identically to today; a
  historical commit regenerates under its archived manifest.
- `api_records`: tokens parse into the `withdrawn` state; a token with other text, or mixed with
  field inputs, is refused; a pre-design golden parses.
- `api_compat`: contract to token is one finding; token to contract and token to token are
  nothing; a new parameter on an implementer-facing member is a finding, optional or not; the same
  on a member whose owner has `subclass = false` is not.
- `check_api_keywords`: each rule in §5 with planted dumps and manifests; a `PO` without an entry
  fails; a listed `PO` passes; dunder interpreter slots pass and an application argument on a
  dunder fails; `VP` without an entry fails; concrete inheritance, structural conformance and
  disagreeing sources each behave as §5.1; an override with renamed leading parameters fails; a
  withdrawn call is skipped.
- `api_teaching`: a taught construction of a `construct = false` class is a finding;
  `--untaught-constructors` lists the right classes on a planted docs tree.
- CLI parity: a test drives `build_cli_binding` with a verb whose signature mixes positional,
  required keyword-only and defaulted keyword-only parameters and asserts the CLI's positional
  arguments are exactly the positional parameters, in order, and every other parameter is an
  option, required iff it has no default. A second test, over the committed golden, asserts that
  every `@cli_exposed` verb's positional parameters equal its effective `positional` entry, which
  is the link between the two surfaces.
- The suites prove the conversion: zero failures and zero errors across `make coverage`'s
  selection (the bed half locally through `make gate-tip`, the hostless half in CI). The
  pickling rule of §6.5 gets a round-trip test over every public error class.
- `scripts/interface_churn.py` at the tip records the surface shrink and the conversion's one-time
  break count in the PR body.

**`@final` and demotion (amended 2026-11-08).**

- `tests/unit/scripts/test_check_api_keywords.py`: a `subclass = false` class without `@final` is
  finding 5; with `final = false` and a `why` it is clean; `@final` on an open class is finding 6;
  a model in each state behaves the same; a protocol with `subclass = false` raises neither.
- Reader: `final = false` without `why`, or without `subclass = false`, is refused. Child: `final`
  on a protocol key is refused by `_APPLIES`.
- Child, over a fixture tree with real emission: a `typing_extensions.final` class emits the token;
  a `typing.final` class does not; a subclass of a final class does not; a dataclass with `@final`
  above and below `@dataclass` does; a pydantic model does; a class bound in a module other than
  its defining one is found by `__module__` + `__qualname__`.
- `api_records`: `@final @manifest:subclass=false` parses as withdrawn and final; `@final a b` as
  final with members `a`, `b`; `@final` elsewhere on the line, or twice, is a parse error; a golden
  line without it parses unchanged (byte-identical regeneration of a pre-amendment golden).
- `api_compat`: the token appearing, disappearing, or combined with a withdrawal transition
  changes no verdict; the manifest token alone does.
- Demotion: a test over the golden asserts none of the ten keys of §6.8 is present and
  `check-api-teaching` is green; the judge run over the landing range lists the ten removals.

## 9. Left to the builder

- The exception list (§6.2, §6.3, §5.3, §6.1's NamedTuples), proposed from the dump with policy
  entries marked, for the owner to read before commit 3.
- The `--untaught-constructors` output and the reviewed demotion list (§6.4).
- The per-dunder interpreter-slot counts (§5.2), kept in `api_records` beside `SUPPORTED_DUNDERS`.
- Whether commit 3 is split per area, and how.
- The list of verbs whose CLI shape changes under §6.6.
- Repeating the model-constructor audit at the landing's base (§5, Models).
- The `--symbol` argv spelling, the `Call`/obligation record shapes for the withdrawn state, test
  fixture layout, message wording, and the docs' exact phrasing within §7.

## 10. Amendments to the two earlier specs

- Manifest spec (2026-10-04) §4: the principle becomes "namespaces decide publicness; the
  `[symbols]` table carries per-path facts about names that are already public and never makes a
  name public". `scripts/api_manifest.py`'s docstring says the same.
- Dump spec (2026-10-05): a new §3.8 for the withdrawn tokens and the `--symbol` input; §4.2 gains
  the implementer-facing rule and the withdrawal finding; §5.4's schema clause is noted as not
  triggered (grammar extension, not a bump: no committed dump's bytes change under the new
  producer, because no committed tree has a symbols table).
