# otto ext_svc — base-side service helpers for LLEXT extensions

An out-of-tree Zephyr module compiled into the ARM loader bases (`cov`,
`cov44`) via `-DEXTRA_ZEPHYR_MODULES` (see the `zephyr-qemu-cov-build` step in
the repo `Vagrantfile`). Gated by `CONFIG_OTTO_EXT_SVC`, set only in
`configs/cov_an385/overlay.conf` — configs that don't set it (`no_fs_arm`,
every x86 config) compile nothing from here.

Added for the basecamp product request (`todo/testbed-request.md`): an LLEXT
extension cannot define its own thread stack (`K_THREAD_STACK_DEFINE` has
alignment/section requirements a loaded extension's sections can't satisfy),
so a background service inside an extension needs the base to own the stack.

## Exported surface

| Symbol | Contract |
|---|---|
| `k_tid_t ext_svc_spawn(k_thread_entry_t entry)` | Run `entry` on the base-owned stack (`CONFIG_OTTO_EXT_SVC_STACK_SIZE`, default 2048 B) at `K_PRIO_PREEMPT(CONFIG_OTTO_EXT_SVC_THREAD_PRIO)`. Single-flight: returns `NULL` if the service thread is already alive (or `entry` is `NULL`). The slot frees when the entry returns or is aborted. |
| `void ext_svc_abort(k_tid_t tid)` | Abort the service thread. Ignores a `tid` that isn't the one this module owns. |
| `const struct device *ext_svc_uart_pipe(void)` | The `zephyr,uart-pipe` chosen device — uart1 (`uart@40005000`) on the mps2 boards, which QEMU bridges to the raw-TCP protocol port (`pport` column of the Vagrantfile's `ARM_INSTANCES`). The console/shell stays on uart0. |

Typical extension shape: `call_fn <ext> start` → the extension's `start` calls
`ext_svc_spawn(loop)`; the loop polls `ext_svc_uart_pipe()` with the inline
`uart_poll_in/out` API and `k_sleep()`s between polls; `call_fn <ext> stop`
clears a flag and/or calls `ext_svc_abort()`.

## Deliberately NOT here

- **Any gcov/coverage runtime.** Policy (Chris, 2026-07-20): otto applications
  that want on-target coverage ship the gcov instrumentation *and* emission
  runtime inside their own extension. `tests/repo3/product/` is the working
  example (single-TU `#include` of the patched embedded-gcov, Approach A in
  `tests/repo3/docs/feasibility.md`); the base stays product-agnostic.
- **Shell verbs.** Extensions can't register shell commands (link-time
  iterable sections), and the `llext` shell's `call_fn` already covers
  start/stop semantics.
