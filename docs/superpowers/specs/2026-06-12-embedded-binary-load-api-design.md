# Embedded binary load API (`load()` / `unload()` + `BinaryLoader` strategy) — design

**Date:** 2026-06-12
**Status:** Approved for planning
**Origin:** Follow-up to the embedded log-hygiene work. repo3 installs an LLEXT extension by
hand-rolling `host.oneshot("llext load_hex <name> <hex>")` — a runtime *load*, not a file
*transfer*. It belongs in a first-class embedded API, distinct from `put`/`get`, with a real
progress representation for the slow paced-telnet hex push.

## Problem

`llext load_hex <name> <hex>` loads an ELF extension into Zephyr's LLEXT runtime by passing the
hex-encoded binary inline as a single shell-command argument. This is **not** a file transfer:

- There is no destination file or filesystem — the cov beds declare `transfer = "console"` but no
  `filesystem` (so `NoFileSystem`); `put`/`get` would short-circuit with "requires an on-device
  filesystem." The binary goes straight into the kernel's loader, not onto storage.
- It is currently a raw `oneshot` in [test_embedded_coverage.py](../../../tests/repo3/tests/test_embedded_coverage.py):
  the multi-KB hex floods the log (now mitigated by `log=False`), there is no progress feedback for
  the ~4 s paced write (cov beds write the hex in 64-byte chunks at 15 ms spacing —
  [hosts.json](../../../tests/lab_data/tech1/hosts.json)), and the success check ("Successfully
  loaded extension") and the refcount-eviction loop are hand-rolled in the test.

We want a first-class, extensible embedded capability for "load/unload a binary into the device
runtime," parallel to but distinct from `put`/`get`.

## Goals

- `EmbeddedHost.load(file, name)` and `EmbeddedHost.unload(name)` returning `tuple[Status, str]`
  (like `put`/`get`).
- The *how* (device commands, success detection, eviction depth) is a **profile-declared
  `BinaryLoader` strategy**, mirroring `command_frame` / `transfer` / `filesystem`. Built-in:
  `LlextHexLoader`. Future loaders (mcumgr, DFU, vendor) plug in via a registry — additive, no API
  change.
- An **opt-in** transfer-style progress bar (`show_progress=False` default) driven from the only
  measurable source — the telnet chunked write of the payload.
- The hex never reaches console/log (`log=False`); silent by default, consistent with `put`/`get`.
- repo3 stops hand-rolling: declares `loader = "llext-hex"` and calls `host.load`/`host.unload`.

## Non-goals

- No progress for the device-side relocation/link — LLEXT emits no incremental signal, so the bar
  measures only the wire push (then sits at 100% during the load). No spinner fallback.
- No load-from-filesystem path (these beds have no FS; `put`+`llext load <path>` is not available).
- No change to `put`/`get`/`EmbeddedFileTransfer` — `load`/`unload` are a separate concern.
- The LLEXT refcount quirk lives in `LlextHexLoader`, not in generic `EmbeddedHost`.

---

## Section A — `BinaryLoader` strategy (`src/otto/host/binary_loader.py`, new)

A pure value object (no session/I/O), mirroring `CommandFrame` / `EmbeddedFileSystem`: it produces
device command strings and reads device output text; the host executes.

```python
class BinaryLoader(ABC):
    type_name: ClassVar[str]                                    # lab-data string, e.g. "llext-hex"

    @abstractmethod
    def load_command(self, name: str, payload: bytes) -> str: ...
    @abstractmethod
    def check_loaded(self, output: str) -> tuple[bool, str]: ...     # (ok, reason-if-not-ok)

    @abstractmethod
    def unload_command(self, name: str) -> str: ...
    @abstractmethod
    def is_fully_unloaded(self, output: str) -> bool: ...            # True => extension no longer resident

    max_unload_rounds: ClassVar[int] = 16
    """Cap on the unload-to-eviction loop the host drives (see unload())."""


class LlextHexLoader(BinaryLoader):
    type_name = "llext-hex"
    def load_command(self, name, payload):  return f"llext load_hex {name} {payload.hex()}"
    def check_loaded(self, output):
        ok = "Successfully loaded extension" in output
        return ok, "" if ok else output.strip()
    def unload_command(self, name):         return f"llext unload {name}"
    def is_fully_unloaded(self, output):    return "No such extension" in output
```

Registry + factory mirror `command_frame` exactly: `_LOADER_CLASSES`, `register_binary_loader(type_name, cls)`
(validates `cls.type_name == type_name`), `build_binary_loader(type_name) -> BinaryLoader` (raises
`ValueError` listing known names on a miss). A project registers custom loaders from a `.otto` init
module.

**Why `is_fully_unloaded` (not a single `check_unloaded`):** `load_hex` on a resident extension
bumps an LLEXT refcount, so one `unload` may decrement without evicting. "Fully gone" is signalled
only by `No such extension`; `Unloaded extension <name>` means "decremented, maybe more refs." The
host's `unload()` loops on this predicate (below). This keeps the refcount quirk inside the loader.

---

## Section B — write-progress plumbing (`src/otto/host/session.py`)

The only measurable progress is the paced telnet write. Add an optional per-command write-progress
callback, telnet-only:

- `ShellSession.__init__`: `self._write_progress: Callable[[int, int], None] | None = None`.
- `TelnetSession._write`: in the existing `write_chunk_size` chunk loop, after each chunk call
  `self._write_progress(bytes_written_so_far, len(encoded))` when it's set. (When `write_chunk_size`
  is 0 — one write — it fires once at completion; harmless.)
- `ShellSession.run_cmd(..., write_progress: Callable[[int, int], None] | None = None)`: set
  `self._write_progress = write_progress` immediately before the framed `await self._write(framed)`
  inside `_run_cmd_inner`, and reset to `None` immediately after — so handshake/recovery/expect
  writes never report. (Thread `write_progress` into `_run_cmd_inner` alongside `on_output`.)
- `SessionManager.run_cmd(..., write_progress=None)`: pass through to `session.run_cmd`.
- `SshSession`/`LocalSession` `_write` ignore `_write_progress` (no paced write to measure).

`total` is the framed-command wire size (the hex dominates) — the bar reports **actual console
bytes sent**, with the existing `DownloadColumn` / `TransferSpeedColumn` / `TimeRemainingColumn`.
(Rejected: scaling to source-file size — a cosmetic fudge over the real transferred size.)

---

## Section C — `EmbeddedHost.load()` / `unload()` (`src/otto/host/embeddedHost.py`)

New field, coerced from a lab string like `command_frame`:

```python
loader: Optional[BinaryLoader] = None
"""Binary-load strategy for this target's runtime (e.g. LLEXT). Lab data declares it by string in
the ``loader`` field; the storage factory / __post_init__ resolves it. None => this host declares no
loader, and load()/unload() fail loud."""
```

`__post_init__`: if `isinstance(self.loader, str)`, `self.loader = build_binary_loader(self.loader)`.
No construction-time requirement (unlike `command_frame`) — many embedded hosts never load binaries.

```python
async def load(self, file: Path, name: str, *,
               show_progress: bool = False, timeout: float | None = 120.0) -> tuple[Status, str]:
    self._require_loader()                       # ValueError if loader is None (fail-loud config error)
    if isDryRun(): return self._dry_run_transfer("LOAD", [file], Path(name))
    payload = file.read_bytes()
    cmd = self.loader.load_command(name, payload)
    if show_progress:
        async with _acquire_shared_progress() as progress:
            handler = make_rich_progress_handler(progress, self.name)
            wp = lambda done, total: handler(str(file), f"{self.name}:{name}", done, total)
            result = await self._session_mgr.run_cmd(cmd, timeout=timeout, log=False, write_progress=wp)
    else:
        result = await self._session_mgr.run_cmd(cmd, timeout=timeout, log=False)
    ok, reason = self.loader.check_loaded(result.output)
    return (Status.Success, "") if ok else (Status.Error,
            f"load {name} from {file} failed: {reason}")

async def unload(self, name: str, *, timeout: float | None = 20.0) -> tuple[Status, str]:
    self._require_loader()
    if isDryRun(): return self._dry_run_transfer("UNLOAD", [], Path(name))
    cmd = self.loader.unload_command(name)
    for _ in range(self.loader.max_unload_rounds):
        result = await self._session_mgr.run_cmd(cmd, timeout=timeout)   # small command, log normally
        if self.loader.is_fully_unloaded(result.output):
            return Status.Success, ""
    return Status.Error, f"{name} still resident after {self.loader.max_unload_rounds} unload rounds"
```

Behavior summary:
- **Hex hidden**: `load()` runs with `log=False` (the buffered-output + log-flag work already
  shipped). `result.output` is still populated, so the loader can verify.
- **Silent by default**: `show_progress=False` → no bar; success → `(Status.Success, "")`. Matches
  `put`/`get`. The caller logs context (repo3 keeps its "Loaded … on <host>" line).
- **Progress (opt-in)**: `show_progress=True` wires the write-progress callback to a shared Rich
  task labelled `<file.name> → <host>:<name>`; fills as the paced chunks go out, holds at 100%
  during relocation.
- **unload drains**: loops `unload_command` until `is_fully_unloaded` (≤ `max_unload_rounds`),
  fully replacing repo3's `_drain_unload`. `unload` of a not-loaded extension returns success on the
  first round (`No such extension`). Unload commands log normally (small, clean — and now
  prompt/`retval`-free thanks to the buffered-frame fix).
- **Errors**: device-level failure → `(Status.Error, msg)`. Missing loader → `ValueError`
  (fail-loud config error, like a frame-less host).

`ZephyrHost` declares **no** default loader (LLEXT isn't universal to Zephyr); the `loader` is
profile/lab-declared, exactly like `transfer="console"`.

---

## Section D — repo3 wiring

- [.otto/settings.toml](../../../tests/repo3/.otto/settings.toml): add `loader = "llext-hex"` to the
  `[os_profiles."zephyr-3.7"]` and `[os_profiles."zephyr-4.4"]` tables. This flows to the host
  automatically once `loader` is an `EmbeddedHost` field: [os_profile.py](../../../src/otto/host/os_profile.py)
  validates profile keys against the host class's fields and merges them beneath the host's own,
  and `__post_init__`'s string→instance coercion (Section C) turns `"llext-hex"` into an
  `LlextHexLoader` — the same path `command_frame`/`filesystem` already use. The plan must confirm
  no separate key allowlist needs the `loader` addition.
- [test_embedded_coverage.py](../../../tests/repo3/tests/test_embedded_coverage.py):
  - `_load_extension`: replace the `_drain_unload` + `oneshot("llext load_hex … ", log=False)` +
    `"Successfully loaded extension"` hand-check with:
    ```python
    await host.unload(ext)
    status, err = await host.load(_extension_path_for(host), name=ext)
    if not status.is_ok:
        raise RuntimeError(f"load_hex did not load {ext} on {host.id}: {err}")
    ```
    where `_extension_path_for` returns the `.llext` Path (the existing `_extension_hex_from` body
    minus the `binascii.hexlify`). Drop `_extension_hex_from` and the `host_hex` dict.
  - The teardown `oneshot("llext unload {ext}")` → `await host.unload(ext)`.
  - `_drain_unload` is deleted (its loop is now `unload()`'s).

---

## Testing strategy

TDD, fakes/`tmp_path` only — never destructive in the dev repo.

- **`binary_loader.py` (pure, unit):** `LlextHexLoader.load_command` formats `llext load_hex <name>
  <hex>` (assert hex of known bytes); `check_loaded` True on "Successfully loaded extension", False +
  reason otherwise; `unload_command`; `is_fully_unloaded` True only on "No such extension", False on
  "Unloaded extension <name>". Registry: `build_binary_loader("llext-hex")` is `LlextHexLoader`;
  unknown raises with the known list; `register_binary_loader` round-trips and rejects name mismatch.
- **write-progress (unit, `session.py`):** a fake `TelnetSession` writer with `write_chunk_size>0`
  records `_write_progress` calls — assert one per chunk with monotonic `done` and constant `total`,
  and that `done` ends at `total`. Assert `run_cmd(write_progress=…)` scopes the callback to the
  framed write only (handshake/recovery don't fire it). Assert SSH/local `_write` ignore it.
- **`EmbeddedHost.load()/unload()` (unit, fake `_session_mgr` AsyncMock):** `load` formats the
  loader command, passes `log=False`, returns `(Success,'')` when the stubbed output contains the
  success marker and `(Error, …)` otherwise; `show_progress=True` invokes the progress factory
  (contract test); `load`/`unload` raise `ValueError` when `loader is None`; `unload` loops until
  `is_fully_unloaded` (stub returns "Unloaded…" then "No such extension" → 2 rounds; "No such
  extension" immediately → 1 round; never → `(Error, …)` at `max_unload_rounds`). Lab-string
  coercion: an `EmbeddedHost(..., loader="llext-hex")` resolves to an `LlextHexLoader` instance.
- **repo3 wiring:** settings parse; the test imports and calls `host.load`/`host.unload`.
- **Live:** rerun `TestEmbeddedCoverage` — install still works via `load()`, log stays hex-free; with
  `show_progress=True` in an interactive/`otto run` the bar animates over the ~4 s push (under
  `otto test` output is captured, so the contract test is the wiring proof). Bed-unreachable must
  FAIL with a host-named error, never skip.

## Risks & mitigations

- **Write-progress fires on small expect/recovery writes** → scoped to the single framed write in
  `_run_cmd_inner` (set immediately before, reset immediately after); unit-tested.
- **`unload` infinite-loop if firmware never prints "No such extension"** → bounded by
  `max_unload_rounds`, returns `(Error, …)`.
- **Bar invisible under `otto test`** → expected (pytest capture); documented, wiring proven by the
  contract test; the bar is for interactive/`otto run`.
- **`load_hex` hex doubles payload size on the wire** → the bar measures wire bytes (honest); the
  `total` is the framed write size, not the file size.
