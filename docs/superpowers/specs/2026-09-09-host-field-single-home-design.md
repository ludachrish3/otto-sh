# Host fields: one home per field

**Status:** approved 2026-09-09 (Chris), including §3.8 — periodic review 2026-09-02, Tier 2 item 11.

## 1. Problem

A host field is declared in up to seven places today. `dev_tools` is the
worked example: the `Host` protocol (`host.py`), `BaseHost` (a bare
annotation), `RemoteHost` (a bare annotation whose docstring says "see
BaseHost"), and then a real `@dataclass` field with its own default and its
own docstring in `UnixHost`, `EmbeddedHost`, `LocalHost` and
`DockerContainerHost`. The two abstract bases are deliberately NOT
dataclasses (the `remote_host.py` module docstring explains why: the
"no non-default field after a default one" ordering rule made a shared
dataclass base awkward), so their annotations create no runtime attribute,
and each leaf must re-declare everything. That is why the drift guard
`test_every_host_class_declares_every_basehost_contract_field` exists at
all: a bare annotation the type checker credits to every subclass while
producing no runtime value is a standing hazard, and the guard is the net
under it.

The consequences, observed over the last two weeks of work on the Host API:

- Adding one field (`resources`, `element`, `source_lab`, `sw_version`) is a
  four-to-seven-file edit, each copy with its own docstring and default, and
  the docstrings cross-link each other instead of saying the thing once.
- The defaults can disagree silently (`repr=False` on `dev_tools` in two
  leaves, not the other two; `debug_log_globs` `repr=False` in `LocalHost`
  only).
- The models are still moving (item 14, freezing them, was deferred for that
  reason). Every move costs seven edits and a guard round.

## 2. Goal

Every host field has exactly ONE home: the one place its type, its docstring
and its default live. A leaf class declares only what is specific to its
family, plus an enumerated, docstring-free set of *value-policy* overrides
(a different default, `init=False`, or "required here"). A structural test
holds the shape so it cannot drift back.

`kw_only=True` (Python 3.10+) is what makes this possible now: keyword-only
fields are exempt from the ordering rule that ruled out a dataclass base when
`RemoteHost` was written.

Non-goals: no change to any `Host` protocol method, to lab-data schemas
(`HostSpec` and friends), to the public-API golden, or to what any verb
does. This is a declaration refactor whose product is fewer places to edit.

## 3. Design

### 3.1 The three tiers

| Tier | Class | Becomes | Declares |
|---|---|---|---|
| contract | `Host(Protocol)` | unchanged | the public methods and the 12 attributes every host answers |
| shared by all | `BaseHost(ABC)` | `@dataclass(kw_only=True)` | every field common to all five families, once |
| shared by network hosts | `RemoteHost(BaseHost)` | `@dataclass(kw_only=True)` | every field common to `UnixHost` and `EmbeddedHost`, once |
| family | the five leaves | unchanged decorators | family-specific fields + allowlisted overrides |

Neither base gets `slots=True`. Host instances have a `__dict__` today —
the leaves are slotted but their bases are plain classes — and dozens of
unit tests rely on it to patch a method on one instance
(`host._run_one = AsyncMock(...)`, `monkeypatch.setattr(h, "_soft_reboot", …)`).
A slotted base would take that away from every family at once. Keeping the
bases unslotted preserves exactly the memory layout otto has now; the
`__slots__ = ()` line on `RemoteHost` goes, since a dataclass base with
fields cannot be empty-slotted and the harmony it claimed never held.

The `Host` protocol stays. The chat design floated collapsing it into an
alias of `BaseHost` to reach two sites instead of three; that is rejected
here for three reasons found while writing this spec. `BaseHost` carries
four public methods the protocol does not (`app_shell`, `as_user`,
`current_user`, `switch_user`), so the alias would silently widen the public
contract and the golden by four lines. The golden generator walks
`vars(Host)`; on an alias it would document an implementation class. And six
unit-test modules use structural doubles typed against the protocol. The
protocol IS the contract; a structural guard (§3.5) ties its attribute block
to `BaseHost`'s fields so it cannot become a fourth declaration of defaults.

### 3.2 `BaseHost` fields

All keyword-only. Types and docstrings move here verbatim from the richest
existing copy; the defaults are the ones the four leaves already agree on.

| Field | Default | Note |
|---|---|---|
| `id: str` | `field(init=False, repr=False)` | every family computes it in `__post_init__`; `LocalHost` assigns `"local"` there instead of via a field default |
| `name: str` | `""` | `__post_init__` fills an empty name (already the unix/embedded rule); `LocalHost`/`DockerContainerHost` keep `init=False` (§3.4) |
| `log: LogMode` | `LogMode.NORMAL`, `repr=False` | |
| `lab_info: LabInfo` | `default_factory=LabInfo`, `repr=False` | |
| `resources: frozenset[str]` | `default_factory=frozenset`, `repr=False` | the guard keeps asserting the factory is `frozenset` |
| `element: Element \| None` | `None`, `repr=False` | `RemoteHost` narrows to required `Element` (§3.3) |
| `inventory_ref: InventoryRef` | `default_factory=InventoryRef`, `repr=False` | |
| `products: list[Product]` | `default_factory=list` | `repr` shown, as unix/embedded do today |
| `dev_tools: list[DevTool]` | `default_factory=list` | same |
| `toolchain: Toolchain` | `default_factory=Toolchain`, `repr=False` | |
| `power_control: PowerController \| None` | `None` | |
| `debug_log_globs: list[str]` | `default_factory=list` | |
| `source_lab: str` | `""` | already carried a value; unchanged |
| `has_bash: bool` | `True` | moved here by follow-up item 10 (`todo/host-api-followups-2026-09-09.md`); `EmbeddedHost` overrides to `False` |
| `_session_mgr: SessionManager` | `field(init=False, repr=False)` | moved here by the same item; every family fills it in `__post_init__` |

`capabilities` stays a `ClassVar` annotation with no value.

`repr` policy is decided once, here, and the landed rule is: the host's
IDENTITY fields are shown, its BULK and its lab PROVENANCE are hidden.
Where the leaves disagreed (`dev_tools`, `products`, `debug_log_globs`),
that rule — not a head-count of the leaves — decided it, and the change is
listed in the commit body.

### 3.3 `RemoteHost` fields

All keyword-only except `ip`, which stays the first positional parameter
(`field(kw_only=False)`) so `UnixHost("10.0.0.1", creds)` keeps working.

Moves here, once: `ip`, `element` (required, `repr=False` — an override of
`BaseHost.element`, keyword-only so it cannot shift positional order),
`creds` (`default_factory=list`; `UnixHost` overrides to required), `user`,
`board`, `slot`, `site`, `rack`, `shelf`, `hop`, `os_type`, `os_name`,
`os_version`, `hw_version`, `sw_version`, `term`, `transfer`, `valid_terms`,
`valid_transfers`, `is_virtual`, `command_frame`,
`landing_frame`, `session_setup`, `default_dest_dir`, `max_filename_len`,
`telnet_options`, `snmp`, `metadata`, `interfaces`, `_lab` (`log_stdout` was listed here until item 10 removed the unread key).

Rule for membership: a field lives on `RemoteHost` when both remote leaves
declare it with the SAME type. That includes the connection plumbing:
`_connection_factory` (`init=True`, default `None`) and `_connections` as
`field(init=False, repr=False)` that the leaf's `__post_init__` fills
(`_session_mgr` was one of these until item 10 moved it to `BaseHost`,
where all five families share it). They cannot stay as the bare "Connection-state
contract" annotations `RemoteHost` carries today: on a dataclass a bare
annotation IS a field, and without a default it becomes a required
constructor argument. The one field the leaves type differently,
`_file_transfer` (`UnixFileTransfer` vs `EmbeddedFileTransfer`), stays in
the leaves. After the move neither base carries any bare instance
annotation, and the structural guard says so without exemptions.

### 3.4 What a leaf may still declare

1. Family-specific fields, with their docstrings (unix: `docker_capable`,
   `roles`, `impairer`, `valid_impairers`, `ssh_options`, `sftp_options`,
   `scp_options`, `ftp_options`, `nc_options`, `userland_options`,
   `shell_history`, `_file_transfer`, `_user_transfers`, `_userland_cache`,
   …; embedded: `filesystem`, `loader`, `_file_transfer`; docker: `parent`, `container_id`, `project`,
   `service`, `compose_project`, `user`, `mounts`, `is_virtual`, `_pending_run_user`
   and the rest; local: `dry_run_exempt`).
2. Value-policy overrides of a base field: a different default, `init=False`,
   `kw_only=False`, or "required". An override carries NO docstring — the
   docstring lives with the field's home — and appears in the allowlist:

| Leaf | Overrides |
|---|---|
| `UnixHost` | `creds` (required, positional), `os_name="Linux"` |
| `EmbeddedHost` | `os_type="embedded"`, `term="telnet"`, `transfer="console"`, `valid_terms`, `valid_transfers`, `has_bash=False` |
| `ZephyrHost` | `os_type="zephyr"`, `os_name="Zephyr"`, `command_frame` (narrowed to `CommandFrame`, `default_factory=ZephyrFrame`) |
| `LocalHost` | `name` (`"localhost"`, `init=False`) |
| `DockerContainerHost` | `name` (`init=False`) |

No leaf overrides `id`: it is `BaseHost`'s `init=False` field and every
family, `LocalHost` included, assigns it in `__post_init__`.

### 3.5 The structural guard (replaces the drift sweep)

`tests/unit/models/test_host_specs.py` loses
`test_every_host_class_declares_every_basehost_contract_field`,
`test_the_classvar_exclusion_does_not_blind_the_sweep` and their helpers:
the hazard they hunt (a bare annotation with no runtime field) is no longer
expressible on a dataclass base. In their place, one module
`tests/unit/host/test_field_homes.py` with four tests:

1. **No bare instance annotations on a base.** For `BaseHost` and
   `RemoteHost`, every name in `inspect.get_annotations(cls)` that is not a
   `ClassVar` is a dataclass field. Read via `inspect.get_annotations`,
   never by touching `__annotations__` on the class (the Python 3.14 PEP 649
   lesson).
2. **The protocol's attributes have a home.** Every attribute annotation in
   `Host` (the 12 names) is a `BaseHost` field, and the annotation text is
   identical. `element` is a protocol property and a `BaseHost` field; the
   test names that pairing explicitly.
3. **Leaves override only what the allowlist says.** For each leaf, the set
   of its OWN annotations (`inspect.get_annotations(leaf)`) that names a
   field of a base equals the allowlist row for that leaf. A new base field
   re-declared in a leaf, or a stale allowlist entry, both fail.
4. **Overrides carry no docstring.** An `ast` walk of each leaf's class body:
   an `AnnAssign` whose target is an allowlisted override must not be
   followed by a string-constant expression statement. This is the test that
   keeps "one home per docstring" true, since attribute docstrings do not
   exist at runtime.

The existing `resources` `default_factory is frozenset` assertion moves into
test 1's module. The spec-versus-runtime guards
(`test_host_spec_fields_match_runtime_init`, `test_registered_pairs_drift_guard`)
are untouched: the set of `init=True` field NAMES per leaf does not change.

### 3.6 Behaviour that changes, and what does not

Changes, all listed in the commit body:

- `element` becomes keyword-only on `EmbeddedHost`/`ZephyrHost` (it was the
  second positional parameter). No caller in `src/`, `tests/` or `docs/`
  passes it positionally; the commit is marked `!` anyway because a positional
  call would break, and the pre-1.0 convention says breaks bump the minor.
- Field ORDER in `repr`, `dataclasses.fields()` and `__eq__` tuples changes
  (base fields first). No consumer depends on order: `config/fleet.py`
  and the unix override-copy seam use `dataclasses.replace` by name.
- The `repr` visibility of `dev_tools`, `products` and `debug_log_globs`
  is unified (§3.2).

Does not change: every `Host` protocol method and signature (the golden is
byte-identical); every `HostSpec`; lab data; the `capabilities` grid and
its generated docs; `register_host_class`; what any verb does.

### 3.7 Hazards the plan must carry

- **Slots stay exactly as they are.** The leaves keep `slots=True` over
  unslotted bases, which is today's layout: on Python 3.10 a leaf's
  `__slots__` lists every field including the inherited ones (bpo-46382,
  fixed in 3.11), and instances keep a `__dict__` from the base. No test
  may assert slot uniqueness or the absence of `__dict__`. Never add
  `slots=True` to a base: it removes `__dict__` from every family and, before
  Python 3.14, breaks zero-argument `super()` in the rewritten class
  (gh-90562).
- **Type narrowing on override** (`element: Element` over
  `Element | None`) is what `RemoteHost` already does with bare annotations,
  and `ty` accepts it today. If `ty` rejects the dataclass form, the
  fallback is `Element | None` on `RemoteHost` plus a `__post_init__`
  refusal, and the spec is amended.
- **Docs**: `docs/api` autodocs the host classes. Field docstrings move up
  the hierarchy; the plan checks the leaf pages still show inherited fields
  (`:inherited-members:` or an explicit `.. autoattribute::`) and that
  `make docs` (`-W`) is clean.

### 3.8 Widening the `Host` protocol (approved 2026-09-09)

`BaseHost` carries four public members the protocol does not: `app_shell`,
`as_user`, `switch_user` and the `current_user` property. All five families
answer them (the base raises `NotImplementedError` for `as_user` /
`switch_user` where the posix mixin is absent), the user docs already teach
them (the sessions recipe, the Hosts guide, the families page's
session-identity column), and no class can implement `Host` without
`BaseHost` because `register_host_class` demands a `RemoteHost` subclass.
They are public in practice; the protocol records it.

- The protocol gains the four members with `BaseHost`'s signatures.
  `current_user` joins as a property, like `element`.
- `BaseHost.as_user`'s default is reshaped to the real call shape: an
  `@asynccontextmanager` whose body raises `NotImplementedError` before
  yielding, typed `AsyncIterator[BaseHost]`, so the protocol, the base and
  the posix mixin agree and the conformance asserter's signature check
  passes on every family.
- The golden grows by three lines (`app_shell`, `as_user`, `switch_user`;
  `current_user` is a property and the generator skips non-callables). An
  additive change; regenerated with `make api-snapshot`.
- The conformance asserter's behavioural probe learns the session-identity
  column: a family declaring `SessionIdentity.none` must raise
  `NotImplementedError` from `as_user(...)` and `switch_user(...)` under a
  dry run; a family declaring `as_user_scoped` must not. `bound_at_open`
  (containers) is not probed, because the enum's meaning is about `run`'s
  channel, not about `as_user`, and the container family's `as_user` answer
  is the container's own business until a row says otherwise.

## 4. Testing

- New: `tests/unit/host/test_field_homes.py` (§3.5), each test proven red by
  a mutation (a re-declared field, a docstring on an override, a bare
  annotation added to `BaseHost`).
- Retired: the two sweep tests and their helpers in `test_host_specs.py`.
- Gates: `make coverage` (full unit suite; this touches a class every test
  builds), `nox -s tests_hostless-3.14` (slots + PEP 649 behave differently
  there), `make typecheck-python`, `make docs`, `make api-snapshot` check
  unchanged, `make check-breaking` on the range.
- Live: the item 13 test class and the bed conformance lane
  (`make conformance-bed`) once, before the commit lands on `main` — this
  changes how every host is constructed. Run with Chris's go-ahead, since it
  drives the lab VMs.

## 5. Sequencing

Four logical commits, each gated: (1) `feat(host): the Host protocol names
app_shell, as_user, switch_user and current_user` (§3.8, additive golden
regeneration, asserter probe); (2) `refactor(host)!: one home per host
field` (§3.2–3.4, §3.6); (3) `test(host): structural guard over field
homes` (§3.5, retiring the sweep); (4) docs follow-through if `make docs`
needs it. This spec is committed on its own first. Item 10 (the follow-up
decision record) comes after.
