# Embedded (LLEXT) Products

Embedded RTOS targets (Zephyr) have no filesystem that otto can `scp` or
`sftp` from, so the standard `.gcda`-over-SSH path does not apply.  Instead,
otto uses a separate embedded fetcher that pulls coverage data over the
console. This page covers both halves: how the console pipeline works, and —
the larger part — how to set up a product so it can emit coverage at all.

## How it works

A coverage-instrumented LLEXT extension built against NASA's
[embedded-gcov](https://github.com/nasa-jpl/embedded-gcov) library dumps its
counters as an ASCII hexdump over the serial console when the `cov_dump`
function is called (via `llext call_fn <extension> cov_dump` →
`__gcov_exit`).  Otto captures that output, decodes the hexdump blocks back to
binary `.gcda` files, and stages them under the same per-host directory
structure used by the remote fetcher:

```text
<staging_root>/
    <host_id>/
        *.gcda
```

This means the downstream merge and report pipeline (`lcov --capture`, path
mapping, HTML render) is reused without modification — the embedded and Unix
code paths converge at the same `.gcda` file tree, and `otto cov get` produces
a `capture.json` for an embedded board exactly as it does for a Unix one.
`otto cov clean` does not reach embedded boards — see
{ref}`coverage-tier-kinds` on the main page.

## Setting up a product

The design rule that shapes everything below: **the coverage runtime ships
inside your extension, not in the base image**. Base images stay
product-agnostic; an instrumented extension is self-contained — it carries
its product code, the embedded-gcov runtime, and the entry points otto
calls. The complete working example in the otto repo is
`tests/repo3/product/` (build script, CMake, and extension source), driven
live by `tests/e2e/cov/test_embedded_coverage_e2e.py`.

### One extension, one translation unit

On Cortex-M targets Zephyr's LLEXT type is `ELF_OBJECT`: an extension is a
single relocatable object, which in practice means a single translation
unit. The pattern that follows from this is `#include`-composition — the
product sources and the embedded-gcov runtime sources are included into one
`.c` file:

```c
#include <stdint.h>
#include <zephyr/llext/symbol.h>
#include <zephyr/sys/printk.h>

#include "gcov_public.c"   /* embedded-gcov runtime … */
#include "gcov_gcc.c"
#include "gcov_printf.c"

/* … your product code … */
```

### Vendoring embedded-gcov, and the modern-GCC patch

Vendor embedded-gcov (a git submodule works well) — and note that upstream
was written for GCC ≤ 11. **Zephyr SDK toolchains from 0.16.x onward ship
GCC 12+, and the stock runtime miscompiles against them in two ways**:

1. GCC 12 added a `checksum` field to `struct gcov_info` (between `stamp`
   and `filename`). Without it the runtime reads `filename` four bytes
   early — a bus fault the moment it touches the name.
2. GCC 12 changed gcov record *length* fields from 32-bit words to bytes
   (`GCOV_TAG_FUNCTION_LENGTH` 3 → 12, `GCOV_TAG_COUNTER_LENGTH(N)`
   `N*2` → `N*2*4`). Without it `gcov` reports *"record size mismatch"*
   and 0% coverage despite a valid dump.

The otto repo ships a patch covering both (plus routing the runtime's
serial output through `printk`) that supports GCC 12 through 14:
`tests/repo3/third_party/patches/embedded-gcov-zephyr-gcc12plus.patch`.
Apply it to your vendored copy:

```bash
git -C third_party/embedded-gcov apply embedded-gcov-zephyr-gcc12plus.patch
```

Build the extension and run the report's cross-`gcov` with the **same** GCC
(i.e. the same Zephyr SDK) — the runtime hard-codes gcc's internal structs,
so mixing compiler versions across those steps reintroduces exactly the
failures the patch fixes.

### Entry points: `cov_init` and `cov_dump`

GCC registers each instrumented TU by emitting an `.init_array` constructor
that calls `__gcov_init` — but Zephyr 3.7's LLEXT loader never runs an
extension's constructors, and the constructor is a *local* symbol that
`llext call_fn` cannot reach by name. The fix (possible because the
extension is a single TU) is an exported wrapper that calls the constructor
through an assembler alias:

```c
/* Verify the generated ctor name with:  nm <ext>.llext | grep _sub_I  */
extern void gcov_ctor(void) __asm__("_sub_I_00100_0");
void cov_init(void) { gcov_ctor(); }        /* registers this TU */

void cov_dump(void) { __gcov_exit(); }      /* hexdump over the console */

LL_EXTENSION_SYMBOL(cov_init);
LL_EXTENSION_SYMBOL(cov_dump);
```

The runtime lifecycle over the console is then:
`llext load_hex` → `call_fn <ext> cov_init` → exercise the product →
`call_fn <ext> cov_dump` (otto issues this one itself — see configuration
below).

### Build: instrument only the extension

With Zephyr's `add_llext_target()`, coverage flags attach to the extension's
library target — the (throwaway) host app stays uninstrumented:

```cmake
add_llext_target(my_product_cov
  OUTPUT  ${ZEPHYR_BINARY_DIR}/my_product_cov.llext
  SOURCES ${PROJECT_SOURCE_DIR}/src/my_product_cov.c)

target_include_directories(my_product_cov_llext_lib PRIVATE
  ${PROJECT_SOURCE_DIR}/../third_party/embedded-gcov/code)

target_compile_options(my_product_cov_llext_lib PRIVATE
  -fprofile-arcs -ftest-coverage -O0)

add_dependencies(app my_product_cov)
```

(`-O0` keeps the line table predictable; without the `add_dependencies` a
plain `west build` never produces the `.llext`.)

### Post-build: strip the constructor sections

The instrumented object carries the `.init_array` entry for the gcov ctor,
and **Zephyr 3.7's loader refuses any extension containing one**
(`-ENOEXEC` on `.rel.init_array`). Strip it after the build — the ctor
*function* stays in `.text`, where `cov_init`'s alias reaches it:

```bash
arm-zephyr-eabi-objcopy \
    --remove-section='.init_array*' --remove-section='.fini_array*' \
    --remove-section='.rel.init_array*' --remove-section='.rel.fini_array*' \
    --strip-debug \
    my_product_cov.llext my_product_cov.stripped.llext
```

(coverage-embedded-stamp-guard)=
### The `.gcno` stamp guard

Every compilation stamps a random 32-bit value into both the `.gcno` notes
file and the `gcov_info` struct baked into the object. The report step
refuses a `.gcda` whose stamp doesn't match the `.gcno` (*"stamp mismatch
with notes file"*) — and the classic way to hit that is a build graph that
recompiles the source (fresh `.gcno`, fresh stamp) but re-ships a **stale**
`.llext`. The failure surfaces minutes later, at report time, as a confusing
0%.

Guard against it *in the build*: after producing the stripped extension,
check that the `.gcno`'s stamp actually appears in the shipped binary. The
stamp sits at byte offset 8 of the `.gcno`; in the extension it follows the
4-byte gcov format-version marker (the `.gcno`'s bytes 4–8) inside each
`gcov_info`:

```python
import glob, struct, sys, pathlib
build = pathlib.Path(sys.argv[1])
gcno = pathlib.Path(glob.glob(str(build / "**" / "*.gcno"), recursive=True)[0]).read_bytes()
ver, stamp = gcno[4:8], struct.unpack_from("<I", gcno, 8)[0]
blob = (build / "zephyr" / "my_product_cov.stripped.llext").read_bytes()
stamps = [struct.unpack_from("<I", blob, i + 8)[0]
          for i in range(len(blob) - 12) if blob[i:i + 4] == ver]
if stamp not in stamps:
    sys.exit(f"stamp guard: .gcno {stamp:#x} not in extension {list(map(hex, stamps))} "
             "— the shipped .llext is stale relative to the notes file")
```

`tests/repo3/product/build.sh` runs exactly this check on every build; copy
it. It converts a silent wrong-coverage report into an immediate,
actionable build failure.

The same guard exists for Unix targets, where the ship step is a deploy
rather than `llext load_hex` — see the
{ref}`GCC page <coverage-gcc-stamp-guard>` (note the stamp offset inside
`gcov_info` differs with pointer width: +8 here on 32-bit Cortex-M, +16
on 64-bit Unix hosts).

## Embedded coverage configuration

Declare the extension name in `.otto/settings.toml` under `[coverage.embedded]`:

```toml
[coverage.embedded]
extension = "my_product_cov"
```

When `extension` is set, otto issues `llext call_fn my_product_cov cov_dump` on
every embedded host in the lab that matches the optional `[coverage].hosts`
selector.  Non-embedded hosts (Unix, Docker) are skipped automatically.

The `dump_command` timeout is generous (120 s) because the hexdump is emitted
one `printk` character at a time and can take several seconds for large binaries.

## Toolchain for embedded coverage

Embedded hosts that need a cross-`gcov` binary for the report step can declare
a `toolchain` block in `lab.json` pointing to the cross toolchain's `gcov`:

```json
{
    "element": "board_cov",
    "toolchain": {
        "sysroot": "/home/vagrant/zephyr-sdk-0.16.8/arm-zephyr-eabi",
        "gcov": "bin/arm-zephyr-eabi-gcov",
        "lcov": "/usr/bin/lcov"
    }
}
```

Note that `lcov` is a host-side Perl orchestrator and is **not** part of the
cross toolchain — point it at the host's `lcov` binary (e.g. `/usr/bin/lcov`),
not a path under the sysroot.

## Operational notes

- **The base image needs headroom, not coverage code.** The loader base must
  size its shell command buffer for the extension's `load_hex` line — the
  hex encoding is 2× the stripped extension size, plus command overhead —
  and `CONFIG_LLEXT_SHELL_MAX_SIZE` / `CONFIG_LLEXT_HEAP_SIZE` to match.
- **Pace bulk console writes.** A multi-KB `load_hex` line written in one
  burst overruns the shell's UART RX ring; otto's transports chunk writes
  (e.g. 64 bytes / 15 ms) for exactly this reason.
- **One console session.** The Zephyr shell serves a single session;
  coordinate anything else driving the same console.

See {doc}`hosts/embedded` for embedded host setup and {doc}`setup/lab-config`
for the full `lab.json` schema.
