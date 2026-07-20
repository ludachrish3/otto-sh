# basecamp testbed enablement — design

**Date:** 2026-07-20
**Input:** `todo/testbed-request.md` (basecamp's requirements for running their
binary-protocol product as an LLEXT on the `cov`/`cov44` bases)
**Directive:** comply with the request if at all possible (Chris, 2026-07-20),
**except item 4 (gcov runtime in the base), which Chris explicitly DENIED**:
otto applications are required to ship their own gcov instrumentation and
emission inside their extension if they want coverage (Approach A stays the
policy; the base carries no coverage code).

## Findings that reshape the request

Every claim in the request was re-verified against the live bed (basil,
`10.10.200.14`) and both local Zephyr trees before designing. Two of the five
items shrink to zero or near-zero:

1. **Item 1 (symbol exports) needs no change.** The request's survey of "18
   exports" is stale/wrong. The deployed 3.7 `cov_base` exports **161** symbols:
   Zephyr 3.7's syscall generator emits `EXPORT_SYMBOL(z_impl_*)` for every
   syscall in the build when `CONFIG_LLEXT=y`. All four requested symbols
   (`z_impl_device_get_binding`, `z_impl_k_uptime_ticks`, `z_impl_k_sleep`,
   `z_impl_k_yield`) are already exported — as are `z_impl_k_thread_create`,
   `z_impl_k_thread_abort`, and the whole `z_impl_uart_*` family. The 4.4 base
   exports the same surface via `CONFIG_LLEXT_EXPORT_SYMBOL_GROUP_SYSCALL=y`
   (verified in the ELF's export strtab).
2. **The device name question is settled.** Both built devicetrees carry
   `uart1: uart@40005000` with no `label` and implicit `status = "okay"`, and
   both boards choose `zephyr,uart-pipe = &uart1`. `CONFIG_UART_CMSDK_APB=y` is
   set in both `.config`s and the string `uart@40005000` appears in both ELFs'
   device tables — `device_get_binding("uart@40005000")` will resolve on 3.7
   *and* 4.4.

What genuinely needs base-side code is item 2 (the thread-stack helper). Item
3 (second serial) and item 5 (sizing) are launch-arg and Kconfig changes.
Item 4 is denied per the directive above — basecamp uses their own fallback
(self-contained runtime, exactly what repo3's product does today; proven,
documented in the feasibility notes, including the strip-`.init_array`
recipe).

## Approach

**One otto-authored Zephyr module, `tests/firmware/zephyr/ext_svc/`,** wired
into the ARM cov builds via `-DEXTRA_ZEPHYR_MODULES`, following the existing
`snmp_agent` module precedent. No Zephyr source patch (the patch mechanism is
for fixing Zephyr itself; this is additive base-app code). The `shell_loader`
sample stays stock; the module is Kconfig-gated so only configs that opt in
(`cov_an385`) compile any of it. Alternatives rejected:

- *Zephyr source patch:* wrong tool — this isn't a Zephyr defect, and patches
  couple us to two Zephyr trees.
- *Forking the sample into an otto app:* loses the "canonical loader" property
  that fixed the silent shell-stack-overflow bug (feasibility notes); a module
  keeps the sample authoritative.

### Module contents

`src/ext_svc.c` (gated by new `CONFIG_OTTO_EXT_SVC`):

- `k_tid_t ext_svc_spawn(k_thread_entry_t entry)` — runs `entry` on a
  base-owned 2 KB stack at `K_PRIO_PREEMPT(5)`, per the request's sketch, with
  two deliberate deviations: a **busy-guard** (returns `NULL` if the service
  thread is already live — a double `bc_start` must not corrupt a live
  `struct k_thread` on a shared bed) and a **trampoline** that clears the busy
  flag when the entry returns, so a service that exits on its own can be
  respawned.
- `void ext_svc_abort(k_tid_t tid)` — `k_thread_abort` + busy-flag clear.
- `const struct device *ext_svc_uart_pipe(void)` — returns
  `DEVICE_DT_GET(DT_CHOSEN(zephyr_uart_pipe))` (uart1). The request offered to
  use an accessor instead of name lookup; we provide it under a bed-generic
  name (not `bc_proto_uart` — the base is product-agnostic).

No gcov code of any kind (item 4 denied — see the directive). The module's
Kconfig header records the policy so the next requester finds the answer
where they'd look first.

### Vagrantfile changes (zephyr VM provisioners)

- `ARM_INSTANCES` gains a `pport` column (protocol serial port, `-` = none):
  `cov → 2423`, `cov44 → 2424`, `no_fs_arm → -`. Chosen to mirror the console
  ports 2323/2324 rather than the request's example 2400.
- **Build step:** add to every row's `west build`:
  `-DEXTRA_ZEPHYR_MODULES=/vagrant/tests/firmware/zephyr/ext_svc`.
  Uniform flags; the `cov_an385` overlay decides what compiles (`no_fs_arm`
  keeps the Kconfig off).
- **Unit step:** rows with a `pport` get a second serial after the console one
  (QEMU maps `-serial` args to CMSDK UARTs in order → uart1):
  `-serial tcp:<addr>:<pport>,server=on,wait=off` — raw `tcp:` per the request
  (telnet IAC-escapes 0xFF; the protocol frames are binary). Console stays
  `telnet:`.

### Config changes (`configs/cov_an385/overlay.conf`)

- `CONFIG_OTTO_EXT_SVC=y`.
- Sizing for a ≥32 KB extension (item 5): `CONFIG_SHELL_CMD_BUFF_SIZE=81920`
  (64 KB hex + command overhead), `CONFIG_LLEXT_SHELL_MAX_SIZE=65536`,
  `CONFIG_LLEXT_HEAP_SIZE=128`. RX ring stays 8192 (writes are chunked).
  mps2_an385 has 4 MB SRAM; the deltas are noise.

## Testing / verification

1. Build both cov configs with the module on the dev VM first (same workspaces
   + SDKs) to shake out CMake/Kconfig errors before touching basil.
2. Deploy on basil by hand (staged module + overlay, rebuild, regenerate unit
   files exactly as the new provisioner would, restart `zephyr-qemu-cov{,44}`);
   `vagrant provision zephyr` later converges to the same state.
3. Verify: `nm` shows `ext_svc_*` in the export area (and still no `__gcov_*`);
   both tcp protocol ports accept connections; console shell still answers.
4. **End-to-end probe** (stands in for basecamp's own verification): a small
   LLEXT that references `ext_svc_spawn/abort`, `ext_svc_uart_pipe`,
   `device_get_binding("uart@40005000")`, `k_uptime_ticks`, and `k_sleep` —
   load it over the console, `call_fn probe_start`, then check bytes flow both
   ways on the uart1 tcp port (echo+1 loop), `call_fn probe_stop`. Proves
   items 1–3 in one pass on the 3.7 instance; 4.4 verified at the nm/listener
   level (same module, same overlay).

## Out of scope

- otto lab-data changes (the protocol port is basecamp's client-side concern).
- The x86 beds and `no_fs_arm` (untouched).
- basecamp's own extension build (they own it; the reply documents the
  single-TU/ELF_OBJECT constraint and the strip-`.init_array` recipe).
