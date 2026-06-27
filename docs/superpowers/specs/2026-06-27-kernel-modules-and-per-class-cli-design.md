# Kernel-module load/unload/lsmod + per-class CLI parsers — design

> Captured 2026-06-27. **Spec B** of the load/unload workstream — the Linux
> kernel-module feature the per-session foundation (Spec A,
> [2026-06-27-per-session-user-and-elevation-design.md](2026-06-27-per-session-user-and-elevation-design.md))
> was built for. Spec B *consumes* Spec A's `current_user` for its sudo
> decision and must be built on top of the Spec A branch. No code has changed
> yet.

---

## 1. Context & motivation

otto's `EmbeddedHost` already has a `load(file, name, …)` / `unload(name)` pair
that pushes a binary into a bare-metal/RTOS target's runtime loader (Zephyr
LLEXT). Linux kernel modules are the conceptual sibling: take a code artifact
and insert it into a running kernel, with a paired removal — distinct from
placing a file on a filesystem. So `UnixHost` should grow the **same verbs**,
each implementing its own version of "inserting" (the decision to keep
`load`/`unload` rather than rename to `insert`/`remove` was settled during
brainstorming: `load` is the umbrella term both ecosystems use — `modprobe`
loads, `llext load_hex` — and `insert`'s natural inverse `remove` collides with
the existing `rm` file-op).

Exposing these to the CLI surfaces a latent limitation in otto's host-verb
synthesizer that this spec also fixes (§3): the same verb name on two host
classes with **different signatures** is not currently representable.

## 2. Goals / non-goals

**Goals**

- `UnixHost` gains `load`, `unload`, `lsmod` — `@cli_exposed`, returning the
  same `tuple[Status, str]` / `list[str]` shapes as the existing file-ops.
- The sudo-or-not decision is **probe-free**, reading Spec A's `current_user`.
- `unload` is idempotent (unloading something not resident succeeds).
- `lsmod` reads the kernel's own source of truth (`/proc/modules`), needing no
  `lsmod` binary and no privilege.
- The CLI host-verb synthesizer resolves a verb's **parser per host class**, so
  `UnixHost.load` and `EmbeddedHost.load` can each keep their natural
  signature. `EmbeddedHost.load`/`unload` become `@cli_exposed` on the back of
  this.
- Additive only: no existing behaviour or call site changes.

**Non-goals (explicit)**

- No `modprobe` / dependency resolution / `depmod` flows.
- No module parameters (`insmod foo.ko debug=1`).
- No `os_profile`-declared module search paths.
- No `load`/`unload` on `LocalHost`/`DockerContainerHost` (the feature is
  `UnixHost`-only; see §5 on why their `current_user` would otherwise be a
  footgun).
- No rename of the `load`/`unload` verbs; no change to `EmbeddedHost.load`'s
  *semantics* (only the `@cli_exposed` marker + CLI annotations are added).
- No autocomplete-cache or completion-schema version bump (verb completion is
  live — confirmed in §3).

## 3. Part 1 — per-class CLI parsers (foundation)

### The gap

`HostGroup` ([src/otto/cli/expose.py](../../../src/otto/cli/expose.py)) lazily
synthesizes `otto host <id> <verb>` commands. Two facts about it today:

1. **Scoping is per-class.** `list_commands`/`get_command` filter the visible
   and dispatchable verbs to `exposed_cli_names(cls)` for the resolved host
   class. A custom method with a *unique* name on a custom host class gets its
   own command from its own signature.
2. **Parsing is global.** `_ensure_dynamic()` builds **one** command object per
   `cli_name` into the group's command dict, from the **first-registered**
   class's method (`iter_exposed_verbs()` is first-wins), guarded by
   `if cli_name in self.commands: continue`. `get_command` returns that same
   global command for every class. The first-wins comment explicitly *assumes a
   consistent signature across classes*.

This works for every shared verb today (`run`/`put`/`get`/`login`/…) because
their signatures are compatible across classes. It breaks the moment two
classes want the **same verb name with divergent signatures** — exactly what
`load` needs (`UnixHost`: `name` optional; `EmbeddedHost`: `name` required).

### The fix

Make `get_command` build the verb's parser from the **resolved host class's
actual method**, not the global sample. Sketch:

```python
def get_command(self, ctx, cmd_name):
    self._ensure_dynamic()
    cls = self._class_for(ctx)
    if cls is None:
        # completion / unresolved host → unscoped global command (today's behaviour)
        return super().get_command(ctx, cmd_name)
    verbs = collect_exposed_methods(cls)        # {cli_name: attr_name} for this class
    if cmd_name in self._dynamic_names and cmd_name not in verbs:
        return None                              # dynamic verb not on this class → hidden
    if cmd_name in verbs:
        return self._class_command(cls, cmd_name, verbs[cmd_name])   # per-class, cached
    return super().get_command(ctx, cmd_name)    # non-dynamic/static commands unchanged
```

`_class_command(cls, cmd_name, attr_name)` builds (and caches per
`(cls, cmd_name)`) a command via the existing `_synthesize_command`, passing
`inspect.getattr_static(cls, attr_name)` as the sample so the binding comes from
that class's signature. Help text comes from the method's `__cli_help__` /
docstring, as today.

- **Completion path unchanged:** during `resilient_parsing` / unresolved id,
  `_class_for` returns `None`, so we fall back to the unscoped global command —
  completion still offers the full verb set with no host build. No cache bump.
- **`_ensure_dynamic()` / `list_commands` unchanged:** they still build the
  global set (used for completion and as the fallback) and list scoped names.
- **Cost:** one build per `(class, verb)` first use, then cached; `_class_for`
  is already called on this path for scoping, so no new host resolution.

### Test (Part 1)

Two host classes exposing the *same* `cli_name` with *divergent* signatures
each resolve to the correct parser. Concretely, after Part 2 lands:
`EmbeddedHost`'s `load` parser requires a positional `name`
(`load <file> <name>`) while `UnixHost`'s exposes `--name`
(`load <file> [--name]`). Assert via the synthesized command's parameters (or a
`CliRunner` invocation: embedded `load f.ko` with no name errors on the missing
argument; unix `load f.ko` succeeds).

## 4. Part 2 — `UnixHost.load` / `unload` / `lsmod`

All three live on `UnixHost`
([src/otto/host/unix_host.py](../../../src/otto/host/unix_host.py)), beside
`put`/`get`/`rm`, and follow the existing `@cli_exposed` conventions (async,
`tuple[Status, str]` / `list[str]` return, `Arg`/`Opt`/`Exclude` overlay).

### 4.1 `load`

```python
@cli_exposed(success="Module loaded.")
async def load(
    self,
    file: Annotated[Path, Arg(help="Kernel module .ko to insert.")],
    name: Annotated[str | None, Opt(help="Module name; defaults to the file stem.")] = None,
    dest_dir: Annotated[Path, Exclude] = Path("/tmp"),
    show_progress: Annotated[bool, Exclude] = False,
) -> tuple[Status, str]:
```

Flow:
1. `resolved = (name or file.stem).replace("-", "_")` — the kernel normalizes
   `-`→`_` in module names; `resolved` is the handle for messaging/unload.
2. `dest = dest_dir / file.name`.
3. `status, msg = await self.put(file, dest_dir, show_progress=show_progress)`;
   on failure return `(status, f"staging {file} failed: {msg}")`.
4. `need_sudo = self.current_user != "root"` (Spec A).
5. `result = await self.run(f"insmod {shlex.quote(str(dest))}", sudo=need_sudo)`.
6. Best-effort remove the staged `.ko` (`await self.rm(dest, force=True)` — the
   module now lives in kernel memory; the file is disposable; the staged file
   is owned by the login user from the `put`, so cleanup needs no sudo).
7. On `result.status.is_ok` → `(Status.Success, "")`; else
   `(Status.Error, f"insmod {resolved} failed: {result.only.output.strip()}")`.

CLI: `otto host <id> load <file> [--name NAME]` (`dest_dir`/`show_progress`
hidden via `Exclude`).

### 4.2 `unload`

```python
@cli_exposed(success="Module unloaded.")
async def unload(
    self,
    name: Annotated[str, Arg(help="Module name to remove.")],
) -> tuple[Status, str]:
```

Flow: `resolved = name.replace("-", "_")`. **Idempotent** — if `resolved` is not
in `await self._loaded_modules()`, return `(Status.Success, "")` without running
`rmmod` (mirrors `EmbeddedHost.unload`'s "unloading something not loaded
succeeds"). Otherwise
`result = await self.run(f"rmmod {shlex.quote(resolved)}", sudo=self.current_user != "root")`;
`(Status.Success, "")` on ok, else
`(Status.Error, f"rmmod {resolved} failed: {result.only.output.strip()}")`.

CLI: `otto host <id> unload <name>`.

### 4.3 `lsmod` + `_loaded_modules`

```python
@cli_exposed
async def lsmod(self) -> list[str]:
    """List the kernel modules currently loaded on the host."""
    return await self._loaded_modules()

async def _loaded_modules(self) -> list[str]:
    """Loaded module names, read from /proc/modules (the source `lsmod`
    formats — robust, no binary dependency, world-readable so no sudo).
    Returns [] under dry-run; log=False keeps the dump out of the log."""
    if is_dry_run():
        return []
    result = await self.oneshot("cat /proc/modules", log=False)
    if not result.status.is_ok:
        return []
    return [line.split()[0] for line in result.output.splitlines() if line.strip()]
```

`/proc/modules` columns are `name size refcount deps state address`; column 1 is
the module name, already `-`→`_` normalized by the kernel. The synthesizer
renders a `list[str]` newline-separated (same as `ls`). `_loaded_modules` is the
single source of truth shared by `lsmod` and `unload`'s idempotency check.

CLI: `otto host <id> lsmod`.

### 4.4 Embedded retrofit

Add `@cli_exposed(success="…")` to the existing `EmbeddedHost.load`/`unload`
and the `Arg`/`Exclude` CLI annotations, **keeping their current signatures**
(`load(file, name, show_progress=False, timeout=120.0)` with `name` *required*;
`unload(name, timeout=20.0)`). `file`→positional `Arg`, `name`→positional `Arg`
(required), `show_progress`/`timeout`→`Exclude`. CLI: embedded
`load <file> <name>`, `unload <name>`. Per-class parsing (§3) is what lets these
coexist with `UnixHost`'s differently-shaped `load`/`unload` under the same verb
names. Each class supplies its own `__cli_success__` (e.g. `UnixHost`: "Module
loaded."; `EmbeddedHost`: keep its binary-loader wording).

## 5. Cross-cutting

- **sudo soundness.** On `UnixHost`, `current_user` seeds from the login creds
  (`credentials[0]`, a real username), so `current_user != "root"` is the
  correct sudo predicate. Spec A's finding #6 — `LocalHost`/`DockerHost` report
  `current_user == ''` because they build their `SessionManager` with no
  `ConnectionManager` — does **not** bite here, because `load`/`unload` are
  `UnixHost`-only. Even the degenerate `current_user == ''` on a `UnixHost`
  errs safe: `'' != "root"` → adds sudo (a no-op if already root, a clear
  failure if sudo is unavailable). This is exactly why §2 lists "no load on
  Local/Docker" as a non-goal.
- **Dry-run.** `load` composes over `put`/`run`, which already honor dry-run
  (`_dry_run_transfer`/`_dry_run_result`), so its `put`/`insmod` are shown.
  `_loaded_modules` short-circuits to `[]` under dry-run (the live module set is
  unknowable, and the skipped read would otherwise echo the dry-run banner), so
  `lsmod` reports nothing. `unload` skips its idempotency check under dry-run so
  the would-be `rmmod` is still issued — symmetric with `load`'s dry-run
  `insmod`.
- **Transports are independent.** The `.ko` `put` lands as the login/transfer
  user via the file-transfer backend; the `insmod` runs in the shell session as
  `current_user`. Expected and correct.

## 6. Testing (unit tier)

Mirroring `tests/unit/host/` patterns (real hosts + mocked `put`/`run`):

- **Part 1:** per-class parser resolution — same verb name, divergent
  signatures → correct parser per class (introspection or `CliRunner`).
- **`load`:** stages via `put` then `insmod <dest>`; `name` defaults to
  `file.stem` with `-`→`_`; sudo on iff `current_user != "root"`; staged file
  removed; error mapping from a non-ok `RunResult`; put-failure short-circuits.
- **`unload`:** `rmmod <name>` with the right sudo; **idempotent** when the
  module is absent from `/proc/modules` (no `rmmod` issued, `Success`); error
  mapping when resident and `rmmod` fails.
- **`lsmod`/`_loaded_modules`:** parses column 1 of `/proc/modules`; empty/no-ok
  output → `[]`; never uses sudo.
- **CLI surface:** `load`/`unload`/`lsmod` appear as `otto host` verbs on a unix
  host; embedded shows `load`/`unload` (no `lsmod`); the new embedded verbs are
  dispatchable with their required-`name` signature.

## 7. Sequencing & build location

Part 1 (synthesizer) lands first with its own tests, then Part 2 (the feature)
builds on it. The whole spec is built **on top of the Spec A branch**
(`worktree-per-session-user-elevation`), since `load`/`unload` read Spec A's
`current_user`; it merges after (or together with) Spec A.
