# Testbed requirements: LLEXT base image + instance changes for basecamp

**To:** otto team (owners of the Zephyr testbed)
**From:** basecamp
**Status:** requirements — basecamp's Zephyr product is blocked on these
**Summary:** 4 symbol exports, 1 base-side thread helper, a second QEMU serial per
instance, and (for coverage) the patched embedded-gcov runtime in the base.

## Context

basecamp is a binary command/control protocol — byte-stuffed framing, CRC-16, and
a GPIO device model — implemented as a freestanding C core. It already ships as a
Linux TCP server with a CLI client and is unit-tested on the host (39 tests). We
want to ship the same core as an **LLEXT product** loaded into your loader base
images (`cov_base`, `cov_base_v4_4`), so it runs on real Cortex-M3.

Target behaviour: `llext load_hex` the extension, `call_fn bc_start` to start the
service, a host client speaks the basecamp protocol to the target over a serial
port and receives framed responses, and `call_fn bc_stop` stops it.

Everything below is what that requires. We've verified each item against the
deployed base (`samples/subsys/llext/shell_loader`, Zephyr 3.7) rather than
guessing, and noted the evidence so you can check our reasoning.

---

## 1. Symbol exports (required)

An LLEXT may only call symbols the base exports. The deployed base exports 18:
`printk`, `vprintk`, `memcpy`, `memset`, `memcmp`, the `str*` family, assert
hooks, ztest hooks, and `k_is_in_isr`. basecamp's core needs `memcpy`/`memset`
(already there); the gaps are below.

Add to the base application (any TU compiled into it):

```c
#include <zephyr/device.h>
#include <zephyr/kernel.h>
#include <zephyr/llext/symbol.h>

EXPORT_SYMBOL(z_impl_device_get_binding);  /* look up a UART at runtime      */
EXPORT_SYMBOL(z_impl_k_uptime_ticks);      /* uptime reporting               */
EXPORT_SYMBOL(z_impl_k_sleep);             /* yield in the service loop      */
EXPORT_SYMBOL(z_impl_k_yield);             /* lighter yield (nice-to-have)   */
```

Why these exact symbols:

| Need | Symbol | Evidence |
|---|---|---|
| Get the UART device pointer | `z_impl_device_get_binding` | Real function in `kernel/device.c`; `device_get_binding` is a `__syscall` |
| Uptime for our `INFO` command | `z_impl_k_uptime_ticks` | `k_uptime_get_32()` → `k_uptime_get()` → `k_uptime_ticks()`; only the last is a syscall |
| Yield in the poll loop | `z_impl_k_sleep` | `k_msleep()` is `static inline` over `k_sleep()` |

**Not needed — please don't add:** `uart_poll_in`/`uart_poll_out` exports. Their
`z_impl_*` bodies are `static inline` in `drivers/uart.h` and dispatch through
`dev->api->poll_in()`, so they compile *into* the extension and reference no
external symbol. Exporting the device lookup is sufficient for full serial I/O.

### Device name

We look up the UART the boards choose as `zephyr,uart-pipe` (uart1 on both
`qemu_x86` and `mps2/an385`), keeping the protocol off the console/shell on uart0.
`uart1` on `mps2_an385` carries no `label`, so the binding name should be the node
name (`uart@40005000`) — **please confirm** what the base reports.

If you'd rather not have us depend on the name, an exported accessor works equally
well and we'll use it instead:

```c
static const struct device *const proto_uart =
        DEVICE_DT_GET(DT_CHOSEN(zephyr_uart_pipe));
const struct device *bc_proto_uart(void) { return proto_uart; }
EXPORT_SYMBOL(bc_proto_uart);
```

## 2. Thread helper for the background service (required for start/stop)

We want `bc_start` to return immediately and the service to run in the background,
so `bc_stop` can stop it. Extensions cannot allocate their own thread stacks —
`K_THREAD_STACK_DEFINE` has alignment and section requirements that aren't
satisfiable from inside a loaded extension — so the stack must come from the base.

Please add a small helper to the base and export it:

```c
#include <zephyr/kernel.h>
#include <zephyr/llext/symbol.h>

#define EXT_SVC_STACK_SIZE 2048
K_THREAD_STACK_DEFINE(ext_svc_stack, EXT_SVC_STACK_SIZE);
static struct k_thread ext_svc_thread;

/* Run an extension-provided entry point on a base-owned stack. */
k_tid_t ext_svc_spawn(k_thread_entry_t entry)
{
    return k_thread_create(&ext_svc_thread, ext_svc_stack,
                           K_THREAD_STACK_SIZEOF(ext_svc_stack),
                           entry, NULL, NULL, NULL,
                           K_PRIO_PREEMPT(5), 0, K_NO_WAIT);
}
EXPORT_SYMBOL(ext_svc_spawn);

void ext_svc_abort(k_tid_t tid) { k_thread_abort(tid); }
EXPORT_SYMBOL(ext_svc_abort);
```

(If you'd prefer, exporting `z_impl_k_thread_create` + `z_impl_k_thread_abort` and
a `K_THREAD_STACK_DEFINE`'d stack symbol achieves the same thing; the helper is
just the tidier interface.)

**Note on shell verbs:** we are *not* asking you to add `start`/`stop` shell
commands. Extensions cannot register shell commands — `SHELL_CMD_REGISTER` uses
link-time iterable sections the base scans, which an extension's sections don't
join. With the thread helper above, `call_fn bc_start` / `call_fn bc_stop` through
the existing `llext` shell gives exactly the semantics we want.

## 3. Second QEMU serial per instance (required — the protocol is unreachable without it)

The running instances use a single serial:

```
qemu-system-arm -machine mps2-an385 ... -serial telnet:192.0.2.33:2323,server,nowait
```

That's uart0 (console/shell). **uart1 exists in devicetree but is wired to nothing**,
so no amount of symbol exporting makes the protocol reachable. Please add a second
`-serial` — QEMU maps serials to UARTs in order, so the second becomes uart1:

```
-serial telnet:192.0.2.33:2323,server,nowait      # uart0: console + shell (unchanged)
-serial tcp:192.0.2.33:2400,server=on,wait=off    # uart1: basecamp protocol
```

**Please use raw `tcp:`, not `telnet:`, for the protocol serial.** This one matters:
telnet escapes `0xFF` as IAC, and our frames carry arbitrary binary — a CRC byte of
`0xFF` would be corrupted in transit and the frame silently dropped. `tcp:` is a
clean 8-bit path. (uart0 can stay `telnet:` — it's text.)

Also please confirm `uart1` is `status = "okay"` in the base's build for each board.

## 4. Coverage: patched embedded-gcov in the base (wanted)

We want on-target line/branch coverage of the basecamp core, reusing the flow your
feasibility work already proved.

The deployed `cov_base` contains **no gcov runtime** (`nm` shows no `__gcov*`
symbols), so today our only option is a self-contained extension carrying the
runtime — which inflates the extension and puts the dumper's own files in our
coverage report as noise.

Preferred: put the runtime in the base ("Approach C" from your feasibility notes)
and export its entry points:

```c
EXPORT_SYMBOL(__gcov_init);
EXPORT_SYMBOL(__gcov_exit);
EXPORT_SYMBOL(__gcov_merge_add);
EXPORT_SYMBOL(__gcov_clear);
```

**The gcc-12 patch is mandatory** for the SDK 0.16.8 toolchain (gcc 12.2), per your
own findings — without it you get a bus fault or a silent 0%:

1. `struct gcov_info` gained a `checksum` field after `stamp` in gcc 12; it must be
   added *and* written into the `.gcda` header, or the runtime reads `filename` 4
   bytes early and faults.
2. gcov record length fields changed from 32-bit words to **bytes**:
   `GCOV_TAG_FUNCTION_LENGTH` 3 → 12, `GCOV_TAG_COUNTER_LENGTH(N)` `N*2` → `N*2*4`.
   Without it `gcov` reports "record size mismatch" and 0% despite a valid dump.
3. `gcov_printf.c` output routed to `printk`.

If you'd rather not carry gcov in the base, tell us and we'll self-contain it in
the extension — it works, it's just larger and noisier.

## 5. Sizing (please confirm / raise)

The current base has `CONFIG_LLEXT_SHELL_MAX_SIZE=32768` and
`CONFIG_LLEXT_HEAP_SIZE=64`. Your feasibility notes needed
`CONFIG_SHELL_CMD_BUFF_SIZE=49152` (+ RX ring 4096) to carry a 16 KB extension's
`load_hex` line. Our extension is the basecamp core plus the service loop; we'll
report exact size once it builds, but please size the shell command buffer and
`LLEXT_SHELL_MAX_SIZE` for **at least a 32 KB extension** so we aren't re-asking.

## Summary

| # | Item | Priority | Change |
|---|---|---|---|
| 1 | Export `z_impl_device_get_binding`, `z_impl_k_uptime_ticks`, `z_impl_k_sleep`, `z_impl_k_yield` | **required** | 4 lines in base |
| 2 | `ext_svc_spawn` / `ext_svc_abort` helper + stack | **required** for start/stop | ~15 lines in base |
| 3 | Second QEMU `-serial`, raw `tcp:`, mapped to uart1 | **required** | instance launch args |
| 4 | Patched embedded-gcov in base + `__gcov_*` exports | wanted | base build + patch |
| 5 | Shell/LLEXT buffers sized for a ≥32 KB extension | confirm | Kconfig |
| — | UART API exports | **not needed** | inline; do not add |

### Verifying items 1–2

```sh
NM=~/zephyr-sdk-0.16.8/arm-zephyr-eabi/bin/arm-zephyr-eabi-nm
$NM build/<base>/zephyr/zephyr.elf | grep -E "device_get_binding|k_uptime_ticks|k_sleep|ext_svc_"
# exported-symbol area should grow by the number of EXPORT_SYMBOLs added:
$NM build/<base>/zephyr/zephyr.elf | grep _llext_const_symbol_list
```

We'll confirm from our side by loading an extension that resolves each symbol and
`printk`s the result.
