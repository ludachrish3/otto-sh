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

## Not yet done

- The instrumented `math_ops`+embedded-gcov extension and `cov_dump`.
- `src/otto/coverage/fetcher/embedded.py` (`EmbeddedGcdaCollector`) + the
  `remote.py` routing change.
- The standalone `mps2_an385` coverage lab instance + the repo3 `OttoSuite`.
