# repo3 embedded coverage — LLEXT feasibility gate (results)

Status as of 2026-06-01: the **bare LLEXT lifecycle is proven on Cortex-M3**; the
coverage dump path is fully designed and de-risked but not yet implemented. This
records the recipe so it can be reproduced and so the collector/product work can
proceed from a known-good base. Design lives in
[`../../../todo/embedded_coverage.md`](../../../todo/embedded_coverage.md).

## Proven

On the lab `zephyr` VM (basil, `10.10.200.14`), Zephyr 3.7:

- `llext load_hex <name> <hex>` → `call_fn <name> <fn>` → `unload` works over the
  console on Cortex-M3, using the stock `samples/subsys/llext/shell_loader` base
  image (already `CONFIG_LLEXT=y` + `CONFIG_LLEXT_SHELL=y`). An otto-authored,
  ztest-free extension's exported `void test_entry(void)` executed and `unload`
  cleaned up.

## Toolchain / infra added to basil (additive, reversible)

- `apt install qemu-system-arm` (QEMU 8.2.2).
- `arm-zephyr-eabi` toolchain added to the Zephyr SDK 0.16.8 via
  `~/zephyr-sdk-0.16.8/setup.sh -t arm-zephyr-eabi -c` (gcc 12.2.0).
- `pip install pexpect` in `~/zephyr-venv-v3_7` (console driver).
- Spike scaffolding under `~/gate/` on basil (not in git).

## Target board: `mps2_an385` (not `qemu_cortex_m3`)

Both are Cortex-M3 / Zephyr 3.7, but the coverage runtime doesn't fit the
LM3S6965:

- `qemu_cortex_m3` = TI LM3S6965, **64 KB SRAM** (hardwired in
  `dts/arm/ti/lm3s6965.dtsi` and QEMU's `lm3s6965evb` machine; `-m` is ignored).
  embedded-gcov statically needs ~32 KB (`gcov_buf[8192]` u32), plus the shell
  command buffer for `load_hex` and the LLEXT heap — too much for 64 KB.
- `mps2_an385` = Cortex-M3, **4 MB SRAM** (`CONFIG_SRAM_SIZE=4096`; base image
  uses 47 KB). Build with `CONFIG_ARM_MPU=n` (MPU blocks loading executable code
  into RAM). Also carries the SMSC LAN9118 NIC for later telnet integration.

## Build recipe

```bash
# base image (mps2_an385, MPU off)
west build -p auto -b mps2_an385 zephyr/samples/subsys/llext/shell_loader \
    -d ~/build/llext_an385 -- -DEXTRA_CONF_FILE=<conf-with CONFIG_ARM_MPU=n>

# an extension (.llext): add_llext_target builds a normal CMake target
# `<ext>_llext_lib`, so coverage flags attach via
# set_source_files_properties(... COMPILE_OPTIONS "-fprofile-arcs;-ftest-coverage").
# On mps2_an385 the LLEXT type is ELF_OBJECT → a single source file per extension.
```

## Console flow control (mandatory)

Dumping a multi-KB `load_hex` line in one write overruns the shell UART RX ring
(`shell_uart: RX ring buffer full`) and corrupts the command. Send the hex in
~16-byte chunks with a ~30 ms gap (otto's `EmbeddedFileTransfer` already paces;
the pexpect driver does too).

## Coverage init: the constructor problem and the fix

gcc registers each instrumented TU's coverage via a `.init_array` constructor
that calls `__gcov_init`. **Zephyr 3.7 LLEXT does not run an extension's
constructors** (no `llext_bringup`), and `llext_call_fn` resolves **only exported
symbols**, so the gcc constructor (a *local* symbol, e.g. `_sub_I_00100_0`)
neither auto-runs nor can be called by name.

Fix (works because `ELF_OBJECT` makes the extension a single TU): export a
`cov_init()` that calls the local constructor through an `__asm__` alias, e.g.

```c
extern void gcov_ctor(void) __asm__("_sub_I_00100_0");  /* verify name via nm */
void cov_init(void) { gcov_ctor(); }       /* runs __gcov_init for this TU */
LL_EXTENSION_SYMBOL(cov_init);
```

Sequence: `load_hex` → `call_fn cov_init` → `call_fn <op>` … → `call_fn cov_dump`
(`__gcov_exit`). (Alternatives if we move off 3.7: `llext_bringup()` on Zephyr 4.x
runs ctors natively. On a single ELF_OBJECT TU the embedded-gcov runtime is
co-instrumented with the product; `ELF_RELOCATABLE` + a base rebuild would
separate them — deferred past the gate.)

## Dump format → host decode

`__gcov_exit()` with `GCOV_OPT_OUTPUT_SERIAL_HEXDUMP` emits, per file:
`Emitting N bytes for <path>` → an `xxd`-style hexdump (`%08x: ` + `%02x `) →
a bare `<path>.gcda` line → `Gcov End`. embedded-gcov's `scripts/serial_split.awk`
+ `xxd -r` reconstruct binary `.gcda`; the host `EmbeddedGcdaCollector` decoder
must replicate exactly that, then `arm-zephyr-eabi-gcov` + the build `.gcno`
produce coverage. gcc-12 compatible: `GCOV_COUNTERS=8` for `__GNUC__ >= 10`, and
the `.gcda` version word is taken from the gcc-populated `info->version`.

## Gate results (continued) — coverage path PROVEN end-to-end ✅

Implemented and ran on `mps2_an385` (Approach C: runtime in base, product-only extension):

- **Base image** = `shell_loader` + the embedded-gcov runtime (`gcov_public.c`,
  `gcov_gcc.c`, `gcov_printf.c`, output routed to `printk`) with
  `EXPORT_SYMBOL(__gcov_init/__gcov_exit/__gcov_merge_add/__gcov_clear)` so an
  instrumented extension resolves them. Needed `CONFIG_SHELL_CMD_BUFF_SIZE=16384`
  (+ RX ring 4096) to carry the extension's `load_hex` hex.
- **Extension** = a single instrumented TU (`math_clamp`/`math_div` + `op_*` +
  `cov_init`/`cov_dump`), ~4 KB. Build wrinkle: gcc emits an `.init_array` entry
  for the gcov ctor; **LLEXT 3.7 fails to load any extension with `.init_array`**
  (`-ENOEXEC` on `.rel.init_array`), so strip it post-build
  (`objcopy --remove-section .init_array* --remove-section .fini_array* --strip-debug`).
  The ctor *function* `_sub_I_00100_0` stays in `.text` and `cov_init` reaches it
  via the `__asm__` alias.
- **Two gcc-12 ports of embedded-gcov were required** (it was written for gcc ≤11;
  both guarded `#if __GNUC__ >= 12`, old behavior preserved in `#else`):
  1. gcc 12 added a `checksum` field to `struct gcov_info` (after `stamp`, before
     `filename`). Without it the runtime reads `filename` 4 bytes early → **bus
     fault** the instant it prints the file name. Fix: add the field *and* write
     the matching `checksum` word into the `.gcda` header after `stamp`.
  2. gcc 12 changed gcov record **length fields from 32-bit words to bytes**.
     `GCOV_TAG_FUNCTION_LENGTH` (3 → 12) and `GCOV_TAG_COUNTER_LENGTH(NUM)`
     (`NUM*2` → `NUM*2*4`). Without it `gcov` reads a 3-byte function record where
     12 is expected → *"record size mismatch, 9 bytes overread"* → counters
     unreadable → **0% coverage** despite a valid dump.

- **PROVEN end-to-end:** `load_hex → cov_init (__gcov_init fires) → call_fn op_*
  → cov_dump` emits the serial hexdump; host decode (`xxd -r`) → 376-byte `.gcda`;
  `arm-zephyr-eabi-gcov` reports **`Lines executed: 100.00% of 13`,
  `Branches executed: 100.00% of 6`, `Taken at least once: 83.33%`** — and
  `math_clamp` shows *blocks executed 83%* (the `v > hi` clamp branch never run),
  precisely the gap a second instance would fill (cross-instance merge proof).

> The embedded-gcov patches above live in the basil spike copy only. In repo3 they
> must be applied as a **patch over the submodule** (don't edit the vendored
> sources), e.g. `tests/repo3/third_party/patches/embedded-gcov-gcc12.patch` applied
> by the product's CMake, so the submodule stays pristine.

## Compiler-version bounds & clang support (future work)

The on-target `.gcda` writer hard-codes gcc's internal gcov structs and record
format, so it is **compiler-version-sensitive**. Current status and bounds:

- **gcc 12.x:** working (proven on `arm-zephyr-eabi-gcc 12.2.0`, gcov version
  `B22*`). The gcov *version word* itself is taken from the live `info->version`,
  so it auto-matches the compiler; only the struct/record format is hand-coded.
- **gcc ≤11:** the `#else` branches restore the historical layout, but this is
  **untested** in this bed — verify before relying on it.
- **gcc 13/14+:** **unverified.** The native reference here was gcc 13.3 (version
  `B24*`) and parsed compatibly, but newer gcc may shift the format again. Also
  embedded-gcov omits the `OBJECT_SUMMARY` record (gcc writes it; gcov tolerated
  its absence here — `runs` shows 0 — but `lcov`/newer gcov may want it). Work
  items: test gcc 13/14, add `OBJECT_SUMMARY`, and treat `struct gcov_info` /
  `gcov_fn_info` / `gcov_ctr_info` as version-gated.
- **clang:** not supported as-is. Two separate paths, each its own work item:
  - *clang `--coverage` (gcov-compatible):* emits gcno/gcda but with clang's own
    gcov version + struct layout; needs `__clang_*`-gated struct/format variants
    distinct from gcc's.
  - *LLVM-native (`-fprofile-instr-generate -fcoverage-mapping` → `.profraw` +
    `llvm-cov`):* a completely different runtime (`__llvm_profile_*`) and on-wire
    format — a separate in-target dumper and host `llvm-cov` path, not embedded-gcov.
- **Recommended architecture to contain this churn:** keep the on-target side as
  dumb as possible (dump raw counters + minimal identity) and do the
  format-specific serialization **on the host** in `EmbeddedGcdaCollector`, keyed
  off the host toolchain's gcov/llvm-cov version. That decouples the firmware-side
  runtime from compiler-format drift and is where clang/gcc/version handling
  should ultimately live.

## Still to build (framework)

- Finish the gcc-12 `.gcda` serialization (above) so `gcov`/`lcov` report real
  coverage; then prove cross-instance merge raises coverage.
- `src/otto/coverage/fetcher/embedded.py` (`EmbeddedGcdaCollector`): drive
  `cov_dump`, decode the hexdump → `.gcda` under `staging_root/<host>/`, write
  `.otto_cov_meta.json` with the Zephyr cross-`gcov`; route `EmbeddedHost` in
  `remote.py` (currently skipped) to it.
- A standalone `mps2_an385` coverage lab instance + the repo3 `OttoSuite`
  (mirroring `TestCoverageProduct`).
