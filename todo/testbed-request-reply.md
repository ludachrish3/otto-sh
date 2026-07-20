# Re: Testbed requirements for basecamp — disposition (2026-07-20)

**To:** basecamp
**From:** otto team
**Status:** items 1–3 and 5 are live on the bed; item 4 is declined (see below,
with a worked alternative). Every claim below was verified against the running
instances, not just the build tree.

| # | Your ask | Disposition |
|---|---|---|
| 1 | 4 symbol exports | **Already satisfied — no change was needed** (see below) |
| 2 | Base-side thread helper | **Done** — `ext_svc_spawn`/`ext_svc_abort`, plus a UART accessor |
| 3 | Second QEMU serial, raw tcp on uart1 | **Done** — `192.0.2.33:2423` (cov/3.7), `192.0.2.34:2424` (cov44/4.4) |
| 4 | Patched embedded-gcov in the base | **Declined by policy** — ship the runtime in your extension; worked example below |
| 5 | Buffers for a ≥32 KB extension | **Done** — `SHELL_CMD_BUFF_SIZE=81920`, `LLEXT_SHELL_MAX_SIZE=65536`, `LLEXT_HEAP_SIZE=128` |

## 1. Symbol exports — your survey was stale; everything you need is exported

The deployed 3.7 base exports **161** symbols, not 18. Zephyr 3.7 generates
`EXPORT_SYMBOL(z_impl_<syscall>)` for every syscall in the image when
`CONFIG_LLEXT=y`; the 4.4 base gets the same surface via
`CONFIG_LLEXT_EXPORT_SYMBOL_GROUP_SYSCALL=y`. Verified present on **both**
instances: `z_impl_device_get_binding`, `z_impl_k_uptime_ticks`,
`z_impl_k_sleep`, `z_impl_k_yield` — and also `z_impl_k_thread_create`,
`z_impl_k_thread_abort`, and the whole `z_impl_uart_*` family (so your
"please don't add" note is moot: they were already there; your inline
`uart_poll_in/out` wrappers still compile into your extension and dispatch
through `dev->api`, as you said).

We loaded a probe extension over the live console that imports
`z_impl_device_get_binding` / `z_impl_k_uptime_ticks` / `z_impl_k_sleep` and
each resolved and ran.

### Device name — confirmed, plus an accessor

`uart1` is `uart@40005000` (no `label`, implicit `status = "okay"`,
`CONFIG_UART_CMSDK_APB=y`) on **both** 3.7 and 4.4, and both boards choose
`zephyr,uart-pipe = &uart1`. So `device_get_binding("uart@40005000")` works
today. Prefer the new accessor, which can't drift with devicetree renames:

```c
extern const struct device *ext_svc_uart_pipe(void);
```

Our probe checked both: they return the same device pointer.

## 2. Thread helper — live, with two deliberate deviations from your sketch

```c
extern k_tid_t ext_svc_spawn(k_thread_entry_t entry);  /* NULL if busy */
extern void ext_svc_abort(k_tid_t tid);
```

Stack 2048 B, `K_PRIO_PREEMPT(5)`, exactly your numbers (both are Kconfig
knobs on our side if you outgrow them). Deviations:

- **Single-flight guard:** `ext_svc_spawn` returns `NULL` while the service
  thread is alive instead of re-creating over a live `struct k_thread` — a
  double `bc_start` must not corrupt kernel state on a shared bed. Check the
  return value.
- **Self-freeing slot:** if your service loop returns on its own (e.g.
  `bc_stop` clears a flag), the slot frees without `ext_svc_abort`. Verified:
  spawn → abort → respawn works in one load.

`ext_svc_abort` ignores a tid that isn't the one it owns. Your
`call_fn bc_start` / `call_fn bc_stop` model works as planned — our probe did
exactly that over the live console.

## 3. Second serial — live, raw tcp, binary-safe

```
cov   (3.7):  console telnet 192.0.2.33:2323   protocol tcp 192.0.2.33:2423
cov44 (4.4):  console telnet 192.0.2.34:2324   protocol tcp 192.0.2.34:2424
```

Ports mirror the console numbering (23xx → 24xx) rather than your example
2400. Raw `tcp:` as you asked. We verified 8-bit cleanliness end-to-end with
an on-target echo+1 loop on uart1: sent `00 41 7E FE FF`, received
`01 42 7F FF 00` — the `0xFF` that telnet would have IAC-mangled came through
intact. Reach them the same way you reach the consoles (SSH hop through the
zephyr VM, then connect on that VM's loopback addresses). Note QEMU's tcp
serial accepts **one client at a time**; reconnecting after a drop is fine.

## 4. gcov in the base — declined; ship the runtime in your extension

Policy decision by the otto testbed owner: **the base images carry no
coverage code.** otto applications that want on-target coverage ship the gcov
instrumentation *and* emission runtime inside their own extension. This is
your fallback option, and it is a proven, working pattern — our own coverage
product uses it in production on these exact instances. Worked example to
copy from, in the otto repo:

- `tests/repo3/product/` — the complete reference:
  - `CMakeLists.txt`: `add_llext_target(...)` + coverage flags
    (`-fprofile-arcs -ftest-coverage -O0`) applied **only** to the extension
    TU, and the include path to the patched embedded-gcov `code/` dir.
  - `src/cov_ext.c`: the single-TU pattern — `#include "gcov_public.c"` /
    `"gcov_gcc.c"` / `"gcov_printf.c"` next to your product code, an exported
    `cov_init` that runs gcc's ctor via an `__asm__("_sub_I_00100_0")` alias
    (LLEXT 3.7 runs no `.init_array`), and `cov_dump` calling `__gcov_exit`.
  - `build.sh`: submodule init + the mandatory gcc-12+ patch
    (`tests/repo3/third_party/patches/embedded-gcov-zephyr-gcc12plus.patch` —
    the same `checksum`-field and byte-length fixes you cited; it covers
    gcc 12–14, so both SDKs), the post-build
    `objcopy --remove-section '.init_array*' …` strip (3.7's loader rejects
    any extension with `.init_array`), and a stamp-coherence guard worth
    copying: it fails the build if the shipped `.llext` and the `.gcno` don't
    share a gcov stamp (a stale link otherwise surfaces as a silent 0%).
- `tests/repo3/docs/feasibility.md` — the why behind each step.

Costs you called out, quantified from our product: the runtime adds ~12 KB to
the extension (ours: 4 KB product-only vs 16 KB self-contained), which the
new 64 KB budget absorbs; the runtime's own files appear in the report's
overall %, but per-file numbers for your product sources are unaffected —
read those.

## 5. Sizing — done, with headroom past your ask

`CONFIG_SHELL_CMD_BUFF_SIZE=81920` (a 32 KB extension is a 64 KB hex line +
command overhead), `CONFIG_LLEXT_SHELL_MAX_SIZE=65536`,
`CONFIG_LLEXT_HEAP_SIZE=128` (KB). Applied to both instances. Two operational
notes from our transports, so you don't rediscover them: pace the `load_hex`
line (we write 64-byte chunks with a 15 ms gap — the UART RX ring is 8 KB and
an unpaced multi-KB line overruns it), and remember the console is a
**single** telnet session — coordinate with anything else driving that shell.

## One constraint you may not have hit yet

On these Cortex-M bases the LLEXT type is **ELF_OBJECT**: an extension is one
relocatable object, i.e. effectively **one translation unit**. Our reference
product `#include`s its pieces into a single TU; plan for the same shape for
the basecamp core + service loop.
