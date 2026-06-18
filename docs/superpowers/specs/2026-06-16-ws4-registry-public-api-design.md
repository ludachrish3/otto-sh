# WS#4 — Registry public API design

**Workstream:** Fable-review #4 (Registry public API — the public-API half only).
**Roadmap:** [`2026-06-13-fable-review-sequencing-design.md`](2026-06-13-fable-review-sequencing-design.md) §4 + review decision #4. This is the last critical-path item before the contract **FREEZE**.
**Date:** 2026-06-16.

---

## 1. Goal & scope

Open otto's two remaining closed selector sets — connection **term** (`ssh`/`telnet`) and file **transfer** (`scp`/`sftp`/`ftp`/`nc` + embedded `console`/`tftp`) — to the same string-registry idiom already used for host classes, command frames, filesystems, and binary loaders. After this, a downstream repo can add a connection or transfer backend (`register_transfer_backend('xmodem', XmodemTransfer)`) the same way it adds a host class today, and the config-facing selector is always a registered string.

This is the **public-API half only**. We define and freeze the extension contract; we do **not** refactor the monolithic built-in backends into per-backend classes. That internal refactor — the 1,443-line `transfer.py` split — stays post-freeze.

### In scope
- A **term backend registry** (UnixHost connections) and a **single unified transfer backend registry** (both host families), each with `register_*_backend` / `build_*_backend` and a module-load registration of the built-ins.
- Replace the `TermType` / `FileTransferType` / `EmbeddedTransferType` `Literal`s with `str` selectors validated against the registry via pydantic `field_validator`s on the host specs.
- A uniform **`create(ctx)` construction contract** so a host builds the selected backend (built-in or custom) through one path, without touching the built-ins' internals.
- **Transfer applicability**: each transfer backend declares the host families it serves (`host_families`), so one registry spans unix + embedded and a future cross-family protocol (tftp) is a single entry.
- **Retrofit-all symmetry**: every built-in across *all* registries (the two new ones plus the existing `command_frame` / `filesystem` / `host_class` / `binary_loader`) registers through its public `register_*` path at module load, not via a private dict literal. Resolves [`todo/registry_builtin_registration_symmetry.md`](../../../todo/registry_builtin_registration_symmetry.md).
- Demote the bare-callable injection hooks (`UnixHost._connection_factory`, `FileTransfer`'s injectable `open_session`/`oneshot`) to **code-only conveniences** for tests/programmatic use. The config-facing path is always the registered string.

### Out of scope (deferred to post-freeze)
- Splitting the monolithic `FileTransfer` / `EmbeddedFileTransfer` / `ConnectionManager` into per-backend strategy classes (the `transfer.py` file split — the internal half of decision #4).
- Implementing `tftp` (it remains reserved / `NotImplementedError`). WS#4 only shapes the namespace to welcome it as a single cross-family entry.
- Any new connection/transfer *protocol*. We register the existing built-ins; we add no behavior.
- Encoding per-capability constraints (e.g. console "needs an on-device filesystem") into the registry. Those stay runtime checks where they live today.

---

## 2. Background — current state

Two of otto's selector dimensions are closed `Literal`s dispatched inside monolithic classes; a third concept (embedded transfer) is a separate closed `Literal`:

| Selector | Type today | Where it lives | Dispatch |
|---|---|---|---|
| `term` | `Literal['ssh','telnet']` | `host/connections.py:41` | `ConnectionManager` bakes both protocols into one class |
| unix `transfer` | `Literal['scp','sftp','ftp','nc']` | `host/transfer.py:135` | `FileTransfer` — one class, internal `match self.transfer` |
| embedded `transfer` | `Literal['console','tftp']` | `host/embedded_transfer.py:79` | `EmbeddedFileTransfer` — one class, internal branch |

Contrast the concepts that already use the open idiom — each is a per-backend subclass keyed by `type_name` in a `dict[str, type]`, looked up by a `build_*`, with a public `register_*`:

- `command_frame` → `_FRAME_CLASSES` + `register_command_frame` + `build_command_frame`
- `filesystem` → `_FILESYSTEM_CLASSES` + `register_filesystem` + `build_filesystem`
- host class → `_HOST_CLASSES` / `_HOST_SPECS` + `register_host_class` + `build_host_class`
- binary loader → `register_binary_loader`

The host specs already validate `command_frame` / `filesystem` against their registries with a pydantic `field_validator` ([`models/host.py`](../../../src/otto/models/host.py)). WS#4 brings `term` and `transfer` up to the same standard.

Key facts that shape the design (verified):
- `FileTransfer` and `EmbeddedFileTransfer` both subclass the same `BaseFileTransfer(ABC)` (shared `put_files` / `get_files`), so **one** registry typed `dict[str, type[BaseFileTransfer]]` holds both.
- `term` is **UnixHost-only** — `EmbeddedHostSpec` has no `term` field; an embedded host reaches its console over a fixed telnet bridge, which WS#4 does not model as a term backend.
- `tftp` is reserved/unimplemented in both families (raises `NotImplementedError`); `console` is embedded-only and requires an on-device filesystem (a runtime check, `_NO_FILESYSTEM_MSG`).
- `UnixHost` already carries a `_connection_factory: type[ConnectionManager] | None` injection hook used by tests — the "bare callable" the roadmap demotes to code-only.

---

## 3. The two registries

### 3.1 Term backend registry (UnixHost connections)
Lives in `host/connections.py` beside `ConnectionManager`:

```python
_TERM_BACKENDS: dict[str, type[ConnectionManager]] = {}

def register_term_backend(name: str, cls: type[ConnectionManager]) -> None: ...
def build_term_backend(name: str) -> type[ConnectionManager]: ...   # raises on unknown, lists known
```

Built-ins `ssh` and `telnet` both register `ConnectionManager` (the monolith serves both protocols; it still receives the `term` string and branches internally). A custom term backend is a `ConnectionManager`-compatible class registered under a new name. Term has a **single host family** (unix), so it needs no applicability axis.

### 3.2 Unified transfer backend registry (both host families)
Lives in `host/transfer.py` beside `BaseFileTransfer`:

```python
_TRANSFER_BACKENDS: dict[str, type[BaseFileTransfer]] = {}

def register_transfer_backend(name: str, cls: type[BaseFileTransfer]) -> None: ...
def build_transfer_backend(name: str) -> type[BaseFileTransfer]: ...   # raises on unknown, lists known
```

`embedded_transfer.py` imports `register_transfer_backend` and registers `console` (and reserved `tftp`) into the *same* registry — one namespace for all transfer protocols.

**Applicability.** `BaseFileTransfer` gains a class attribute declaring the host families a backend serves:

```python
class BaseFileTransfer(ABC):
    host_families: frozenset[str] = frozenset()   # subclasses declare; e.g. {'unix'}, {'embedded'}, {'unix','embedded'}
```

| Backend | `host_families` |
|---|---|
| `FileTransfer` (scp/sftp/ftp/nc) | `{'unix'}` |
| `EmbeddedFileTransfer` (console; tftp reserved) | `{'embedded'}` |
| *future* `TftpTransfer` | `{'unix','embedded'}` — one entry |

A "host family" is the spec/runtime base: `unix` (UnixHost / UnixHostSpec) vs `embedded` (EmbeddedHost / EmbeddedHostSpec). The granularity is host-family only; finer capability constraints (console needs a filesystem) stay runtime checks.

---

## 4. The construction seam

Today a host instantiates the monolith directly, e.g.
`FileTransfer(connections=…, transfer=self.transfer, nc_options=…, scp_options=…, get_local_ip=…, exec_cmd=…, max_filename_len=…)`.
Built-in and custom backends have *different* constructor signatures (unix transfer needs `connections`+`exec_cmd`; embedded needs the device session), so the registry can't rely on a shared `__init__`. We introduce one uniform construction classmethod:

```python
@classmethod
def create(cls, ctx: TransferContext) -> "BaseFileTransfer": ...
```

- The host builds a **context object** (`TransferContext` for transfer, `TermContext` for term) bundling the construction inputs it can provide, then calls `cls.create(ctx)`.
- Each built-in's `create` runs **today's exact construction** against `ctx` fields — `FileTransfer.create` does `FileTransfer(connections=ctx.connections, transfer=ctx.name, nc_options=ctx.nc_options, …)`. The monolith **internals are untouched**; only the call site moves behind `create`.
- The context is a single frozen DTO whose fields are the union of what any family's backends need; a unix backend reads the unix fields, an embedded backend the embedded fields. Because the field_validator (§5) rejects a backend on the wrong family *before* construction, a backend never sees a `ctx` missing the fields it needs.
- The exact `TransferContext` / `TermContext` field lists are settled in the implementation plan; the **contract** (a `create(cls, ctx)` classmethod + a host-provided context object) is the frozen public seam.

**Bare callables → code-only.** `UnixHost._connection_factory` and `FileTransfer`'s injectable `open_session`/`oneshot` stay as programmatic/test overrides: if `_connection_factory` is set it supplies the class in place of the registry lookup (`cls = self._connection_factory or build_term_backend(self.term)`), and `create(ctx)` is still the construction path. They are never the config-facing selector — that is always the registered string.

---

## 5. Selector validation (the frozen config contract)

Replace the `Literal` field types on the specs with `str`, validated against the registry — mirroring the existing `command_frame` / `filesystem` validators:

- `UnixHostSpec.term: str = "ssh"` → `field_validator` checks `term in _TERM_BACKENDS`, else raises listing known names.
- `UnixHostSpec.transfer: str = "scp"` → `field_validator` checks the name is registered **and** `'unix' in build_transfer_backend(name).host_families`, else raises.
- `EmbeddedHostSpec.transfer: str = "console"` → same, requiring `'embedded'` applicability.

This yields strictly better errors than the disjoint `Literal`s — e.g. *"`scp` is a unix transfer, not valid on an embedded host"*, *"`console` requires an embedded host"*, and *"`frobnicate` is not a registered transfer backend. Known: …"*. The frozen config-facing contract is: **a registered selector string, validated for host-family applicability.**

The spec → runtime wiring (`to_host`) resolves the selector through `build_*_backend` exactly as it resolves `command_frame` through `build_command_frame` today.

---

## 6. Retrofit-all symmetry

Every built-in registers through its public `register_*` path at module load — including the existing registries, not just the new ones. Concretely, replace the private dict-literal seeding (`_FRAME_CLASSES = {BashFrame.type_name: BashFrame, …}`, `_FILESYSTEM_CLASSES = {…}`, the host-class built-in seeding, binary-loader seeding) with explicit `register_command_frame(...)` / `register_filesystem(...)` / `register_host_class(...)` / `register_binary_loader(...)` calls in a `_register_builtins()` bootstrap, mirroring the symmetric `_register_builtin_metrics()` pattern Plan 4 used for SNMP metrics.

Outcome: one uniform, frozen idiom across **every** extension point — built-ins and third-party registrations travel the same code path — and [`todo/registry_builtin_registration_symmetry.md`](../../../todo/registry_builtin_registration_symmetry.md) is resolved (delete the todo as part of this work).

---

## 7. Frozen public API surface

After WS#4 these are the frozen extension points (the freeze line locks them):

```
otto.host.connections.register_term_backend(name: str, cls: type[ConnectionManager]) -> None
otto.host.transfer.register_transfer_backend(name: str, cls: type[BaseFileTransfer]) -> None
otto.host.os_profile.register_host_class(name, cls, spec=None) -> None        # existing
otto.host.command_frame.register_command_frame(type_name, cls) -> None         # existing
otto.host.embedded_filesystem.register_filesystem(type_name, cls) -> None      # existing
otto.host.binary_loader.register_binary_loader(type_name, cls) -> None         # existing
otto.host.os_profile.register_os_profile(...) -> None                          # existing

# Backend authoring contracts (frozen):
BaseFileTransfer:  host_families: frozenset[str];  classmethod create(cls, ctx) -> BaseFileTransfer
ConnectionManager: classmethod create(cls, ctx) -> ConnectionManager

# Config-facing selectors (frozen): host-dict `term` / `transfer` are registered strings.
```

Re-exports of `register_term_backend` / `register_transfer_backend` from `otto.host` (alongside the existing `register_host_class` re-export in `host/__init__.py`) so users import them the same way.

---

## 8. File-by-file change map

- `src/otto/host/connections.py` — add `_TERM_BACKENDS` + `register_term_backend` + `build_term_backend`; register `ssh`/`telnet` at load; add `ConnectionManager.create(cls, ctx)`; remove the `TermType` Literal (the property/param annotations become `str`).
- `src/otto/host/transfer.py` — add `_TRANSFER_BACKENDS` + `register_transfer_backend` + `build_transfer_backend`; add `BaseFileTransfer.host_families` + `BaseFileTransfer.create`; `FileTransfer.host_families = {'unix'}` + `FileTransfer.create`; register `scp`/`sftp`/`ftp`/`nc` at load; remove `FileTransferType` Literal.
- `src/otto/host/embedded_transfer.py` — `EmbeddedFileTransfer.host_families = {'embedded'}` + `.create`; register `console` (+ reserved `tftp`) into the shared registry; remove `EmbeddedTransferType` Literal.
- `src/otto/models/host.py` — `term`/`transfer` become `str` with registry+applicability `field_validator`s; `to_host` resolves via `build_*_backend`.
- `src/otto/host/unix_host.py` — connection + transfer construction goes through `build_*_backend(...).create(ctx)`; `_connection_factory` reframed as a code-only override that supplies the class.
- `src/otto/host/embedded_host.py` — transfer construction via `build_transfer_backend(...).create(ctx)`.
- `src/otto/host/__init__.py` — re-export `register_term_backend` / `register_transfer_backend`.
- Retrofit symmetry: `command_frame.py`, `embedded_filesystem.py`, `os_profile.py`, `binary_loader.py` — built-ins via `_register_builtins()` public-path calls.
- `todo/registry_builtin_registration_symmetry.md` — deleted (resolved).
- Docs — a "custom backends" guide page (or extend the existing host-class extension docs) covering `register_term_backend` / `register_transfer_backend`, the `create(ctx)` contract, and `host_families`.
- Tests — see §10.

---

## 9. Error handling

- `build_term_backend` / `build_transfer_backend` raise `ValueError` (or the established registry error) on an unknown name, listing the known names — matching `build_command_frame`.
- `register_*_backend` validates inputs consistent with the existing `register_*` (e.g. type checks); a transfer backend with empty `host_families` is rejected at registration with a clear message (it could never validate against any host).
- The spec `field_validator`s raise `ValueError` (→ pydantic `ValidationError`) with the host-family-aware messages of §5. Because `ValidationError` is a `ValueError` subclass, the existing `json_repository` / `completion_cache` catch sites are unaffected (a typo'd selector drops the host from a lab / from tab-completion, no crash — same contract as the §2b factory collapse).
- Reserved `tftp` keeps raising `NotImplementedError` at transfer time (registered, applicable, but unimplemented).

---

## 10. Testing

Per registry (term, transfer):
- A fake custom backend registered at test time is accepted by the spec `field_validator` and constructed by the host via `create(ctx)`.
- An unregistered selector raises `ValidationError` listing known names.
- Applicability: a unix-only transfer on an `EmbeddedHostSpec` (and vice versa) raises with the family-aware message; a `{'unix','embedded'}` fake validates on both.
- Built-ins still resolve: `ssh`/`telnet`, `scp`/`sftp`/`ftp`/`nc`, `console` validate and construct exactly as before; `tftp` validates but raises `NotImplementedError` on use.
- Symmetry retrofit: each registry's built-ins are present after import (the public-path bootstrap ran) and re-registering a built-in name behaves per the existing override policy.
- Registry isolation: tests that register fakes restore the registry (snapshot/restore fixture) so global state never leaks between tests.

Full gate (this touches host construction): `make test` (live VM tiers — do not kill), `make coverage` (≥ 90%), `make nox` (all Pythons), `ty` clean, `make docs`.

The existing connection/transfer integration suites (real ssh/telnet/scp/sftp/ftp/nc against the Unix lab VMs; console against the Zephyr bed) are the authoritative proof the seam preserves behavior — they must stay green unchanged.

---

## 11. Risks & notes

- **Construction-context creep.** The `create(ctx)` context risks pulling host internals into a public DTO. Mitigation: the DTO carries only what the *built-ins* already receive at their call sites (no new coupling); its exact shape is pinned in the plan and reviewed against "could a third-party backend be built from this?"
- **`term` entanglement.** `ConnectionManager` is a shared multi-protocol object (pooling/tunnel), so a custom term backend is a heavier lift than a transfer backend. WS#4 only freezes the *seam* (register + `create`); it does not promise that ssh/telnet are cleanly separable — that's the post-freeze connection refactor.
- **Behavior change from `extra='forbid'`-style strictness.** Selectors are already constrained today (closed `Literal`s); moving to registry validation keeps them constrained, so no new rejection surface beyond unknown custom names.
- **Re-export surface.** Adding `register_*_backend` to `host/__init__` widens the frozen import surface; intended.

---

## 12. Sequencing

WS#4 is the final critical-path item before the **FREEZE**. Per the current decision, the freeze is not being taken strictly right now (ahead of schedule, zero users), so WS#4 ships as a normal change; the frozen *surface* it establishes is what later work treats as stable. WS#4 is independent of Pydantic Phase B (different subsystem) and can land before or alongside it.

---

## 13. Tab completion for `term` / `transfer` (added 2026-06-17)

`otto host` already tab-completes `--term` / `--transfer` today, but via a **static** `click.Choice(get_args(<Literal>))` — a snapshot taken at module import, before any repo `init` module runs its `register_*` calls. Opening the selectors to a registry would silently *lose* completion for anything beyond the built-ins unless completion is reworked. WS#4 reworks it to a **dynamic, registry-driven** completer, designed in three tiers:

- **Tier 1 — built-in defaults (guaranteed).** The completer reads the live registry as a fallback; built-ins are always registered at module import, so `ssh`/`telnet` and the unix transfers always complete.
- **Tier 2 — custom per-repo backends.** A repo's `register_term_backend` / `register_transfer_backend` calls run when its `init` modules load (the completion *slow path*), so custom backends complete there immediately. For the *fast path* (completion without re-running user code), the completion cache (`completion_cache.json`) is extended to snapshot the registered backend names; its fingerprint already covers every `init`-module file, so a new registration invalidates the cache automatically.
- **Tier 3 — per-host declared lists (future).** A `hosts.json` entry could declare its supported `term` / `transfer` sets, and the completer — which receives the Click `ctx` and can read the already-typed `host_id` from `ctx.params` — could narrow completion to that host's declared set. WS#4 does **not** build this, but the dynamic `ctx`-receiving completer keeps it a *pure addition*; a static `click.Choice` would foreclose it. Recorded as a backlog item.

Validation of an explicit `--term` / `--transfer` value moves from `click.Choice`'s parse-time check to a `typer.BadParameter` wrapper around the registry check in `set_term_type` / `set_transfer_type`, preserving a clean CLI error. The implementation tiers (1+2 built now, 3 deferred) are detailed in the plan.

**Editor-schema parallel (added 2026-06-17).** The same widening (`Literal`→`str`) would drop the `term`/`transfer` `enum` from the generated `hosts.json` JSON Schema — the editor autocomplete Plan 6 (§6) built. WS#4 re-injects that `enum` from the **registry** in the schema export (`models/jsonschema.py`), applicability-filtered per host family. Because `otto schema export` runs after init modules load, the enum includes custom per-repo backends too — so the editor suggestions for `term`/`transfer` are preserved and *improved* over the old static `Literal`. Both the per-spec schemas and the `hosts`-array `$defs` carry the enum.
