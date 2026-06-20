# Host Interface Ergonomics — Design

**Date:** 2026-06-19
**Status:** Approved (brainstorm) — ready for phased implementation plans
**Source:** `todo/host_helper_commands.md`

## 1. Motivation

The `Host` interface today gives test/script authors command execution
(`run`/`oneshot`/`send`/`expect`), file transfer (`put`/`get`), and periodic
commands (`start_repeat`). It does **not** give them the common operational
verbs every test bed needs: reboot a host, power it on/off, deploy/remove the
software-under-test, elevate privileges, or manage files already on the remote
host. Today each project re-implements these ad hoc.

This design adds those verbs to the default interface so that — as much as
possible — a project gets reboot/power/install/file-management "for free," and
the project-specific parts plug in through the **same dependency-injection
strategy pattern** otto already uses for `command_frame`, `loader`,
`filesystem`, and `toolchain`.

## 2. Guiding principles

1. **DI strategy symmetry.** Every project-specific behavior is an injected
   strategy object resolved the way otto already resolves `command_frame` /
   `loader` / `filesystem`: code-injected *or* declared by a lab-data string →
   registry → instance. First-party (otto's built-ins) and third-party
   (project) extension use the *identical* mechanism.
2. **Orchestration on the base, behavior in strategies.** The universal host
   methods carry no project logic; they orchestrate injected strategy objects.
3. **Fail loud by default.** When a capability needs a strategy that isn't
   configured (no power controller, no `su` on embedded), the method raises a
   clear, self-explaining error rather than silently no-op'ing — matching the
   existing `EmbeddedHost` "no `command_frame`" / "no loader" conventions.
4. **Additive and backward-compatible.** Empty `products`, `power_control=None`,
   and `sudo=False` defaults keep every existing call site byte-for-byte
   unchanged.
5. **Honest interfaces.** Universal verbs live on `BaseHost` (behind the `Host`
   Protocol). Capabilities only some hosts have (posix file ops) are a
   *family-level* mixin, not forced onto the universal Protocol.

## 3. Architecture context

Host hierarchy (unchanged by this design):

```text
Host (Protocol)
└── BaseHost (ABC) ........ run/oneshot/repeat/logging/dry-run + NEW universal verbs
    ├── RemoteHost (ABC) .. naming/hop/addressing/dest-resolution
    │   ├── UnixHost ...... SSH/Telnet bash
    │   └── EmbeddedHost .. telnet RTOS
    │       └── ZephyrHost
    ├── LocalHost ......... local shell
    └── DockerContainerHost  delegates to a parent host
```

Existing DI strategy precedents this design mirrors:

| Strategy        | Base shape        | Registry                       | Default |
|-----------------|-------------------|--------------------------------|---------|
| `command_frame` | `CommandFrame` ABC | `register_command_frame`       | family-supplied / fail loud |
| `loader`        | `BinaryLoader` ABC | `register_binary_loader`       | `None` → `_require_loader()` fail loud |
| `filesystem`    | `EmbeddedFileSystem` ABC | `register_filesystem`    | `NoFileSystem` |
| `os_type`       | `HostSpec`/`OsProfile` (pydantic boundary + dataclass) | `register_host_class` / `register_os_profile` | `unix` |

This design adds two more rows in the same shape: **`Product`** and
**`PowerController`**.

## 4. Phasing overview

One cohesive design; five independently-shippable phases, each gets its own
implementation plan.

| Phase | Scope | Depends on |
|-------|-------|------------|
| **P1** | Product lifecycle (DI) | — |
| **P2** | Privilege elevation (posix) | — |
| **P3** | Power, reboot, lifecycle waits | P2 (soft reboot uses `sudo`) |
| **P4** | Remote file operations (posix + embedded subset) | — (enhances P1) |
| **P5** | Dynamic CLI exposure (feasibility-gated) | P1–P4 |

Recommended order: **P1 → P2 → P3 → P4 → P5**. Cross-dependencies are soft
(noted per phase); each phase is mergeable on its own.

---

## 5. Phase 1 — Product lifecycle

### 5.1 `Product` strategy (ABC)

A `Product` is the lifecycle analog of `BinaryLoader`: a **behavior contract**,
an `ABC` with `@abstractmethod`s, resolved/injected per host. It is *not* a
pydantic model (that would force every project product into pydantic and
diverge from the four sibling strategies). Concrete subclasses choose their own
data representation (`@dataclass` or `OttoModel`).

```python
# otto/host/product.py  (new)
class Product(ABC):
    """A unit of software-under-test deployed to a host. Behavior contract;
    projects subclass and inject instances via Host.products."""

    name: str
    """Logical identity — used for logging, is_installed lookups, CLI args,
    and dedup. Not a file path (a product may be multi-file or repo-installed)."""

    @abstractmethod
    async def stage(self, host: "Host") -> tuple[Status, str]: ...
    @abstractmethod
    async def install(self, host: "Host") -> tuple[Status, str]: ...
    @abstractmethod
    async def uninstall(self, host: "Host") -> tuple[Status, str]: ...
    @abstractmethod
    async def is_installed(self, host: "Host") -> bool: ...
```

Return conventions match `put`/`get`/`load`: `tuple[Status, str]` for the
mutating verbs (the `str` carries failure detail), `bool` for `is_installed`.

### 5.2 `FileProduct` convenience

For the common "a product *is* one artifact file" case:

```python
@dataclass(slots=True)
class FileProduct(Product):
    artifact: Path
    name: str = ""           # __post_init__: defaults to artifact.name
    dest_dir: Path = Path()  # where stage() puts it; resolved via host.default_dest_dir

    async def stage(self, host) -> tuple[Status, str]:
        return await host.put(self.artifact, self.dest_dir)
    # install / uninstall / is_installed stay abstract — project-specific.
```

`FileProduct.is_installed` is intentionally left to the project; once Phase 4
lands, the natural implementation is `await host.exists(self.dest_dir /
self.artifact.name)`.

### 5.3 `products` field

A new field on every concrete host dataclass, default empty:

```python
products: list[Product] = field(default_factory=list)
```

Placement follows the established pattern: a **bare-annotation contract** entry
`products: list[Product]` on `BaseHost` (so the orchestration methods and
Protocol type-check) and on `RemoteHost`; a real `@dataclass` field on
`UnixHost`, `EmbeddedHost`, `LocalHost`, `DockerContainerHost`.

### 5.4 `BaseHost` orchestration methods (default, overridable)

```python
async def stage(self) -> tuple[Status, str]:
    """Stage every product onto this host."""
async def install(self, stage_only: bool = False) -> tuple[Status, str]:
    """Stage, then install (unless stage_only) every product.
    Stops and returns the first non-ok status."""
async def uninstall(self) -> tuple[Status, str]:
    """Uninstall every product."""
async def is_installed(self) -> bool:
    """True iff there is at least one product and all report installed."""
async def is_uninstalled(self) -> bool:
    """Inverse of is_installed()."""
```

- `install(stage_only=False)` calls `stage()` first; if `stage_only` or the
  stage step failed, it returns without installing.
- Multi-product aggregation: first non-ok `Status` wins (mirroring `RunResult`).
  A small `_aggregate(results) -> tuple[Status, str]` helper lives alongside.
- **Empty-list semantics (defined):** `stage`/`install`/`uninstall` on an empty
  `products` list are successful no-ops. `is_installed()` is `bool(self.products)
  and all(...)` — so an empty list is **not installed** (and `is_uninstalled()`
  is `True`). This is explicit to avoid the vacuous-truth surprise of `all([])`.
- A project may override any of these on its host subclass for cross-product
  ordering/dependencies; the default iterates `self.products` in declaration
  order.

### 5.5 Product registration (code, not lab data)

P1 ships **code-injected** products (`UnixHost(..., products=[MyApp(...)])`).
Lab-ingested hosts get their products from a code-registered provider —
`register_product_provider(host -> products)` applied at ingest. Declaring
products *in* lab data is deliberately **not** supported: lab data stays
product-agnostic and evolves independently of product code. See
`docs/superpowers/specs/2026-06-20-host-product-providers-design.md`.

---

## 6. Phase 2 — Privilege elevation (posix family)

### 6.1 `run(sudo=False)`

`sudo: bool = False` is added to the shared `run()` signature (`Host` Protocol,
`BaseHost`, and per-host implementations). When `sudo=True`:

- Posix-shell hosts (`UnixHost`, `LocalHost`, `DockerContainerHost`) rewrite the
  command to run under `sudo` (e.g. `sudo -S -p '' <cmd>`) and **auto-inject an
  `Expect`** for the password prompt, sourced from the active user's password in
  `creds`. The password response is sent with output suppressed so it never
  reaches the console or log file.
- Hosts that cannot elevate (`EmbeddedHost`) raise `NotImplementedError` when
  `sudo=True` (fail loud), and are unaffected when `sudo=False`.

Mechanism: `BaseHost.run` forwards `sudo` to a `self._elevate(cmd) -> (cmd,
expects)` hook. `BaseHost._elevate` default raises `NotImplementedError`; a
`PosixPrivilege` mixin (shared by the three posix hosts) implements it. This
keeps `run()`'s signature uniform and CLI-introspectable (Phase 5) while the
behavior is family-scoped.

### 6.2 `as_user()` context manager + `switch_user()`

```python
async with host.as_user("root"):
    await host.run("systemctl restart foo")   # runs as root
# session returns to the original user on exit
```

- `as_user(user="root")` is an async context manager that, on enter, `su`'s the
  **persistent default session** to `user` (handling the password prompt from
  `creds` when available), and on exit returns to the original user (`exit` /
  Ctrl-D). It mutates session state, so it affects subsequent `run()` calls in
  the block.
- `switch_user(user="")` is the imperative form (default `""` → root via `su`).
- Both are posix-only (`PosixPrivilege` mixin). `EmbeddedHost` raises
  `NotImplementedError`.

Open implementation detail for the plan: target-user password sourcing when the
user isn't in `creds` — fail loud with a clear message vs. accept an explicit
password arg. Default to fail-loud; allow an optional `password=` override.

---

## 7. Phase 3 — Power, reboot & lifecycle waits

### 7.1 `PowerController` strategy (ABC) + registry

```python
# otto/host/power.py  (new)
class PowerState(Enum):
    ON = "on"
    OFF = "off"

class PowerController(ABC):
    @abstractmethod
    async def on(self, host: "Host") -> tuple[Status, str]: ...
    @abstractmethod
    async def off(self, host: "Host") -> tuple[Status, str]: ...
    async def cycle(self, host) -> tuple[Status, str]:
        """Default: off then on."""
    async def status(self, host) -> PowerState | None:
        """Current power state, or None when the controller can't report it."""
        return None

def register_power_controller(name: str, cls: type[PowerController]) -> None: ...
def build_power_controller(name: str) -> PowerController: ...
```

The controller embodies **where control happens** — the key insight that power
can't run on the (off) host:

- A built-in **`CommandPowerController`** runs configured commands on a
  *designated controller host* resolved via the host's lab back-reference
  (`host._lab.hosts[controller_id].oneshot(cmd)`), or locally. Config (lab
  `[power]` table): `type="command"`, `controller="<host id>"`, `on="virsh start
  {name}"`, `off="virsh destroy {name}"`, optional `status="virsh domstate
  {name}"` + a small parse. Command templates interpolate the target host's
  fields (`{name}`, `{ip}`, …).
- Project controllers wrap IPMI/redfish/libvirt/cloud-API/PDU/smart-plug, or a
  manual "prompt the operator" controller, and register the same way.

otto ships the framework + `CommandPowerController`; richer backends are
project-registered (matching how otto ships `LlextHexLoader` but lets projects
register other loaders).

### 7.2 `power_control` field + `power()`

- Field `power_control: PowerController | None = None` declared on **every**
  concrete host (so `power()` uniformly finds the attribute), with a
  bare-annotation contract on `BaseHost`/`RemoteHost`. The `RemoteHost` family
  (`UnixHost`/`EmbeddedHost`) coerces a lab-data string → instance in
  `__post_init__` (like `loader`); `LocalHost`/`DockerContainerHost` keep it
  `None` (you can't power-cycle localhost) and there is no lab-data path to set
  it on them → `power()` fails loud.
- `_require_power_control()` raises a clear error when `None`.
- `power(state=None)`:
  - `"on"` → `controller.on(self)`
  - `"off"` → `controller.off(self)`
  - `None` (toggle) → read `status()`; flip. If `status()` returns `None`,
    fail loud ("toggle needs a controller that reports status; pass
    state='on'/'off'").

### 7.3 `reboot(hard=False)`

- **Soft (default):** issue the in-shell reboot command via a per-family hook
  `_soft_reboot()`. `UnixHost`: `reboot` (with `sudo` as needed). `ZephyrHost`:
  `kernel reboot cold`. `BaseHost._soft_reboot` default raises
  `NotImplementedError`. **`LocalHost` deliberately does not implement soft
  reboot** (rebooting the test runner's own machine is a footgun) — it fails
  loud.
- **Hard (`hard=True`):** `_require_power_control().cycle(self)` for a real
  power-cycle.
- Optional ergonomic (decide in plan): a `wait: bool = False` param that, when
  true, calls `wait_until_up()` after issuing the reboot.

### 7.4 `shutdown()`

In-shell OS power-off (`UnixHost`: `shutdown -h now` / `poweroff`, sudo as
needed) — distinct from external `power("off")`. Complements `power("on")` to
bring the host back. `LocalHost` fails loud (don't power off the dev machine);
`EmbeddedHost` fails loud unless a subclass provides a command.

### 7.5 Reachability & wait helpers

```python
async def is_reachable(self, timeout: float = ...) -> bool: ...
async def wait_until_up(self, timeout: float, interval: float = ...) -> bool: ...
async def wait_until_down(self, timeout: float, interval: float = ...) -> bool: ...
```

- `is_reachable()` does a lightweight connection probe (no real command) with a
  short timeout — `UnixHost`/`EmbeddedHost` reuse the existing
  `verify_connection()` connect-without-running logic; `LocalHost` is always
  reachable.
- `wait_until_up` / `wait_until_down` poll `is_reachable()` until the desired
  state or timeout. These are the companions that make `reboot`/`power` usable
  (block until the host settles instead of racing).

---

## 8. Phase 4 — Remote file operations

A posix file-management capability mimicking the unix CLI, implemented as remote
commands over `self.run`/`oneshot`. These manage files **already on / between
locations on** the remote host — complementary to `put`/`get` (local↔remote).

### 8.1 `PosixFileOps` mixin (UnixHost / LocalHost / DockerContainerHost)

| Method | Shell basis | Returns |
|--------|-------------|---------|
| `exists(path)` | `test -e` | `bool` |
| `ls(path=".", all=False, long=False)` | `ls` | `list[str]` |
| `mkdir(path, parents=True)` | `mkdir [-p]` | `tuple[Status, str]` |
| `rm(path, recursive=False, force=False)` | `rm [-r][-f]` | `tuple[Status, str]` |
| `cp(src, dst, recursive=False)` | `cp [-r]` | `tuple[Status, str]` |
| `mv(src, dst)` | `mv` | `tuple[Status, str]` |
| `read_file(path)` | `cat` | `str` (raise on failure) |
| `write_file(path, data, append=False)` | heredoc / `tee` (`log=False` for large payloads) | `tuple[Status, str]` |

Quoting via `shlex.quote`; large `write_file` payloads are sent with logging
suppressed (as `load()` does for encoded payloads today). `chmod`/`chown` are
deferred unless a concrete need appears (YAGNI).

### 8.2 Embedded subset

`EmbeddedHost` implements the subset its `filesystem` strategy already knows —
`exists`/`ls`/`rm`/`mkdir`/`read_file`/`write_file` map onto the device `fs`
command formers the transfer code already uses. `cp`/`mv` (no device analog)
fail loud.

### 8.3 Placement

These are a **family capability**, not on the universal `Host` Protocol (an
embedded host can't do all of them). Callers use the concrete/family type. This
keeps the Protocol honest while giving shell hosts a rich, symmetric surface.

---

## 9. Phase 5 — Dynamic CLI exposure (feasibility-gated)

Goal: introspect a host's public coroutine methods and auto-register them as
`otto host <method> <id> [opts]` — **including methods on project-registered
host classes**. This is the first/third-party symmetry applied to the CLI: a
project that adds `MyHost.flash_firmware(...)` gets `otto host flash-firmware
my-bed` for free, the same way otto's own `reboot`/`install`/`power` appear.

Approach:

- **Opt-in marker** (`@cli_exposed` decorator setting an attribute, or a naming
  convention) so we expose intentional verbs, not every coroutine or
  underscore-prefixed internal.
- **Generic async dispatcher**: for each exposed method, synthesize a Typer
  command that resolves the host by id, maps simple parameters
  (`bool`→flag, `str`/`int`/`Path`→argument/option), runs the coroutine inside
  otto's context via `asyncio.run`, and renders the `CommandStatus`/result.
  Reuse the existing option-synthesis machinery (`options_params` /
  `_wrap_with_options` / `register_suite`).

**Gate:** start with a short feasibility spike against the real lifecycle
signatures (`reboot(hard: bool)`, `power(state: str)`, `install(stage_only:
bool)`, the file ops). The spike answers whether the generic exposer is viable
or whether we hand-write CLI subcommands for the lifecycle verbs instead. Only
after the spike do we commit to the full mechanism.

---

## 10. Cross-cutting concerns

- **Dry-run.** Every new method honors `is_dry_run()` — log `[DRY RUN] …` and
  return a synthetic `Status.Skipped`/success, matching existing methods. No new
  method may perform real I/O under dry-run.
- **Logging.** Issued actions go through `_log_command`; passwords (sudo/su) are
  never logged.
- **Status model.** Reuse `Status`/`CommandStatus`/`tuple[Status, str]`
  conventions already used by `put`/`get`/`load`. Add one `_aggregate` helper
  for multi-step verbs.
- **Backward compatibility.** All additive; defaults preserve current behavior
  exactly.

## 11. Testing strategy

- TDD per phase: unit tests with mocked sessions/connections and fake
  `Product`/`PowerController` doubles (the codebase already uses
  `_connection_factory` injection for test doubles).
- Each strategy's registry gets round-trip tests (register → build → behavior).
- Live-tier validation (`make coverage` / `make nox` against real beds) is
  Chris's to run; unit gate (`make test`, `ty`, `make docs`) is the agent's
  bar per phase.

## 12. Open questions / deferred

- **Product registration**: resolved — products are registered in code via
  `register_product_provider` applied at ingest, not declared in lab data. See
  `docs/superpowers/specs/2026-06-20-host-product-providers-design.md`.
- **`as_user` target-user password sourcing** when not in `creds`: default
  fail-loud + optional `password=` override; finalize in the P2 plan.
- **`reboot(wait=...)` convenience**: decide in the P3 plan vs. leaving
  `wait_until_up()` purely composable.
- **`chmod`/`chown`** and richer `ls` output (structured stat): deferred (YAGNI)
  until a concrete need appears.
- **Phase 5 mechanism vs. hand-written CLI**: decided by the feasibility spike.
