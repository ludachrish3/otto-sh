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

## Framework status — done

Host-side framework implemented and unit-tested (`tests/unit/cov/test_embedded_collector.py`):
- `decode_cov_dump` (serial hexdump → `.gcda`, validated against the live capture),
  `EmbeddedGcdaCollector.collect_all`, `collect_embedded_coverage` (config-driven).
- `_run_coverage` (`src/otto/cli/test.py`) routes Unix and/or embedded collection;
  embedded `.otto_cov_meta.json` cross-`gcov` via `discover_toolchain_from_gcno`.
- Config schema: `[coverage.embedded].extension` (→ `llext call_fn <ext> cov_dump`)
  and `.build_dir`.

## Lab integration: mps2_an385 networking — PROVEN ✅

The plan's flagged "main risk" (Cortex-M networking) is cleared. `mps2_an385` runs
in-guest Zephyr networking + the telnet shell, reachable like the existing x86
instances (`192.0.2.x:23` via the basil hop). Non-obvious config it needs:

- **Driver:** the mps2_an385 DTS already has `eth@40200000` (`smsc,lan9220`);
  enable `CONFIG_ETH_SMSC911X` (the LAN9118/9220 driver) — *not* x86's
  `ETH_E1000`/`PCIE`.
- **Entropy:** mps2 has no entropy device, so the net stack won't link
  (undefined `z_impl_sys_rand_get`) — add `CONFIG_TEST_RANDOM_GENERATOR=y`.
- **Telnet line buffer:** `CONFIG_SHELL_TELNET_LINE_BUF_SIZE` defaults to **80
  bytes** and truncates the `load_hex` line — bump to ≥ the hex length (16384).
- **LLEXT log:** `shell_loader` ships `CONFIG_LLEXT_LOG_LEVEL_DBG`, which floods
  the telnet link with relocation debug during load — set to `WRN`.
- **QEMU:** `qemu-system-arm -machine mps2-an385 -nic tap,model=lan9118,ifname=<tap>`,
  using the **SDK** qemu (apt's broke e1000 on x86; SDK is the known-good family).
  Host TAP mirrors the x86 units (`ip tuntap add … 192.0.2.x/30`).
- Verified: `ping` 0% loss; telnet `kernel version` → `Zephyr 3.7.2`; `load_hex`
  transfers and the LLEXT loader parses the ELF over telnet.

Networked image config: `~/gate/an385_cov_net.conf` (basil); instance runs at
`192.0.2.33:23`.

## RESOLVED — in-guest LAN9118 networking can't do bulk `load_hex`; pivot to serial-telnet

The earlier "otto crashes the mps2 instance" was misdiagnosed (the `0x5ff8 =
llext_find_tables` symbol came from a *stale* build — in the live `cov_base_net`
ELF `0x5ff8` is `z_impl_zsock_getsockopt`, a socket fn). Systematic re-diagnosis
found the true root cause, and it is **not** otto-specific, **not** echo, and
**not** LLEXT:

- **Bisected trigger:** otto connect + simple commands (`kernel version`, etc.)
  work fine. The wedge is `load_hex` — specifically its multi-KB hex line. With a
  continuous reader and echo forced off, the instance wedges at **exactly 1460 B =
  one TCP MSS**: `1400+\r` (1 Ethernet frame) survives; `1460+\r` (2 frames) kills
  it. No CPU fault — the **whole net stack hangs** (ping is lost), so it's a
  driver/stack deadlock, not a crash.
- **Ruled out, with evidence:** *echo* (off → still wedges at 1460); *TCP recv
  window / buffers* (window resolves to `(NET_BUF_RX_COUNT·NET_BUF_DATA_SIZE)/3 =
  (128·128)/3 = 5461 B`, yet it dies at frame #2, not at a buffer-sized boundary);
  *host-side pacing* (sub-MSS chunks spaced 1.5 s apart also wedge). otto's
  `ZephyrFrame` does **not** disable echo (unlike `BashFrame`'s `stty -echo`), but
  that is irrelevant to this fault.
- **Root cause:** the mps2 built-in **LAN9118/`smsc911x` NIC** wedges when it must
  receive a **second inbound Ethernet frame with no intervening outbound frame** —
  exactly what a multi-frame `load_hex` line needs. The other otto Zephyr hosts are
  immune because they run **qemu_x86 + E1000** (robust NIC); mps2 was forced only
  because **qemu_x86 has no LLEXT support**. Net-config tuning can't fix a
  driver-level consecutive-frame wedge, and patching the stock driver would break
  the stock-base goal.

**Decision (with Chris): drop in-guest LAN9118 telnet; drive the shell over the
UART via QEMU's native `-serial telnet:<ip>:<port>,server` bridge.** otto's telnet
transport connects unchanged; the firmware reverts to the **stock serial shell
backend** (mps2's default — *more* stock than the net-telnet variant, no net config
at all). Bulk `load_hex` over UART has no MSS/frame limit — this is exactly the path
the feasibility gate already proved (`drive_cov.py`, `west build -t run`,
`uart:~$`, 100 % coverage). All proven LLEXT/gcov work is preserved.

**Sequencing:** serial-telnet now → migrate to `qemu_cortex_a53 + E1000` later, in
lockstep with the fleet's planned x86→ARM move. Cheap churn: otto always speaks
telnet (otto-side code unchanged); the gcc-12 gcov port + `_sub_I_00100_0`
constructor-alias are gcc-version (not arch) specific and transfer to a53; only the
firmware shell-backend Kconfig + the QEMU launch flag are throwaway.

### otto integration — PROVEN end-to-end over serial-telnet ✅

`cov_base` (stock serial shell + LLEXT) launched under
`qemu-system-arm … -serial telnet:192.0.2.33:2323,server,nowait` (listener on
basil's `lo`; port 2323 because 23 is privileged + taken). otto's real
`EmbeddedHost` reaches it via the `basil_seed` hop and runs the full flow:
`kernel version` → `load_hex` (4 KB ext) → `cov_dump` → `EmbeddedGcdaCollector`
decodes a real **376-byte `.gcda`** (matches the gate's `expected.gcda`).

Two general otto fixes were required (both transport-level, no firmware change),
each TDD'd, 812 host/cov/factory unit tests green:

- **`ZephyrSerialFrame`** (`command_frame.py`, `zephyr-serial`): a `ZephyrFrame`
  whose handshake is `shell echo off\r{ready}\n`. The in-guest `SHELL_BACKEND_TELNET`
  honours otto's `IAC DONT ECHO`; the UART shell behind the `-serial telnet:` bridge
  never sees that IAC (QEMU eats it), so it keeps echo on — and the echoed END marker
  matched otto's read loop before the real output, desyncing every command. Mirrors
  repo1's `ZephyrInlineRetcodeFrame` (2.7).
- **`TelnetOptions.write_chunk_size` / `write_chunk_delay`** (default 0 = unchunked):
  `TelnetSession._write` paces large writes (`sprout_cov`: 64 B / 15 ms) so the mps2
  UART RX FIFO doesn't overrun on the bulk `load_hex` line.

`sprout_cov` lab entry: `command_frame: zephyr-serial`,
`telnet_options: {port: 2323, write_chunk_size: 64, write_chunk_delay: 0.015}`.

### Approach A (self-contained ext, stock base) — PROVEN, 100 % coverage ✅

The first end-to-end pass used the 4 KB ext — which is **Approach C** (gcov runtime
exported from the base). Switching to the required **Approach A** (self-contained
16 KB ext, base = stock loader) surfaced two more defects, both now fixed:

1. **Base couldn't load *any* module** (even a 1.1 KB hello-world hung — no fault).
   Root cause: the hand-rolled `cov_base` app lacked `CONFIG_LOG` and an adequate
   `CONFIG_SHELL_STACK_SIZE`, so `llext_load`'s relocation work (run on the shell
   thread) **silently overflowed the shell stack** — and with `CONFIG_ARM_MPU=n`
   the overflow hangs instead of faulting. Fix: build the base from Zephyr's
   canonical `samples/subsys/llext/shell_loader` (the real generic loader) +
   overlay (`CONFIG_SHELL_STACK_SIZE=32768`, `LLEXT_HEAP_SIZE=64`,
   `LLEXT_SHELL_MAX_SIZE=32768`, `SHELL_CMD_BUFF_SIZE=49152`, `ARM_MPU=n`,
   `LLEXT_LOG_LEVEL_WRN`). Build: `west build -p always -b mps2_an385 -d
   build/cov_base zephyr/samples/subsys/llext/shell_loader -- -DEXTRA_CONF_FILE=…`.
2. **`cov_init` bus-faulted** reading `gcov_info->filename` (garbage pointer).
   Root cause: `cov_ext_app` was compiled against an **unpatched** embedded-gcov
   copy, so `struct gcov_info` lacked the **gcc-12 `checksum` field** → `filename`
   read at the wrong offset. Fix: build against the patched embgcov
   (`embedded-gcov-zephyr-gcc12.patch`). Confirmed cause via the fault PC + the
   missing `#if (__GNUC__ >= 12)` branch.

Proven: otto `EmbeddedHost` → `load_hex` (16 KB Approach-A ext) → `cov_init` →
ops → `cov_dump` → `EmbeddedGcdaCollector` → **2132-byte `.gcda`**;
`arm-zephyr-eabi-gcov` reports the product `cov_ext.c` at **100 % lines / 100 %
branches / 100 % calls**. Base carries zero coverage code.

**GCC coupling:** embedded-gcov tracks GCC's internal gcov ABI. Supported floor is
**gcc 4.9** (oldest `struct` branch); the patch adds **gcc ≥ 12** (`checksum` +
byte-length records). Build the product and run the host `gcov` with the *same*
GCC (here Zephyr SDK 0.16.8 = gcc 12.2). otto's decode/collect side is
format-agnostic — the coupling lives in the product's coverage runtime, not otto.

- **Next:** systemd unit (mirror `zephyr-qemu-*.service`) + repo3
  `[coverage.embedded]` config + repo3 `OttoSuite` + `otto test --cov` +
  cross-instance merge. Post-commit cleanup: reconcile the duplicated `embgcov`
  copies; capture the base build recipe into `tests/firmware/zephyr`; user-facing
  doc on fetching/applying the gcc-12 patch.

## Remaining

- **lab_data entry** — `sprout_cov` / `192.0.2.33` / lab `embedded-cov` / hop
  `basil_seed`, added to `tests/lab_data/tech1/hosts.json` (isolated from the
  existing `embedded` lab + `_ZEPHYR_BACKEND_NE` matrix).
- **Validate via otto's real `EmbeddedHost` transport** against the live instance
  — the proper end-to-end check (an ad-hoc telnet driver fights the protocol).
- **systemd unit** + Vagrantfile/firmware wiring for persistence (mirror the
  existing `zephyr-qemu-*` units).
- **repo3 config** (`[coverage.embedded]`) + the repo3 `OttoSuite`
  (mirroring `TestCoverageProduct`), then `otto test --cov` end-to-end.
