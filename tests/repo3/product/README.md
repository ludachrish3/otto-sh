# repo3 LLEXT coverage product

The embedded analogue of repo1's `math_ops`, built as a Zephyr **LLEXT
extension** rather than a host binary. It is the *product* inserted into the
otherwise-stock coverage host; the host's base image carries no coverage code.

```
product/
  CMakeLists.txt   add_llext_target(cov_ext) + -fprofile-arcs -ftest-coverage
  prj.conf         CONFIG_LLEXT=y
  src/
    main.c         throwaway host app (the .llext is the real artifact)
    cov_ext.c      math ops + cov_init/cov_dump + the #included gcov runtime
```

**Approach A (self-contained):** `cov_ext.c` `#include`s the embedded-gcov
runtime, so the product *and* the on-device `.gcda` dumper compile into one
instrumented translation unit. The base resolves only `printk`. Exported entry
points (`llext call_fn cov_ext <sym>`): `op_clamp_lo`, `op_clamp_in`,
`op_div_ok`, `op_div_zero` (exercise code paths), `cov_init` (run the gcov
constructor), `cov_dump` (print the `.gcda` as a serial hexdump).

`.gcno` land in the extension's build dir
(`build/cov_ext_app/CMakeFiles/cov_ext_llext_lib.dir/src/`) — that path is the
report step's `source_root` (`[coverage.embedded].build_dir`).

## One-time setup — embedded-gcov submodule + gcc 12–14 patch

The gcov runtime is NASA's [embedded-gcov](../third_party/embedded-gcov)
(vendored as a git submodule — otto does not own it). It targets gcc ≤ 11, so a
patch adds gcc 12–14 support. Apply it over the pristine submodule before building:

```bash
git submodule update --init tests/repo3/third_party/embedded-gcov
git -C tests/repo3/third_party/embedded-gcov apply \
    ../patches/embedded-gcov-zephyr-gcc12plus.patch
```

**GCC coupling:** the `.gcda`/`.gcno` format and `struct gcov_info` are
GCC-internal, so the patch is version-gated and **must** be kept in step with
the cross-gcc. Two version-sensitive things, both handled in the patch:
- **gcc ≥ 12** changed record-length fields to bytes and added a `checksum`
  field to `struct gcov_info` (the `#if __GNUC__ >= 12` hunks).
- **gcc 14** added a 9th gcov counter (`GCOV_COUNTER_CONDS`, condition
  coverage), growing `struct gcov_info.merge[]` from 8 to 9. The runtime's
  `GCOV_COUNTERS` must match (`#if __GNUC__ >= 14` ⇒ 9, else 8 for gcc 10–13),
  or `n_functions` is read one slot early and the device dumps an **empty**
  `.gcda` (it reads as 0 functions — a silent 0% with no error). Confirmed
  live: gcc 12.2 emits a 60-byte `gcov_info`, gcc 14.3 a 64-byte one.

The product and the report `gcov` must be the **same** GCC — each coverage host
declares its cross-gcov as a per-host `toolchain` in lab data (e.g. Zephyr SDK
0.16.8 → `arm-zephyr-eabi-gcov` 12.2 for 3.7; SDK 1.0.1 → 14.3 for 4.4), matching
the toolchain that built its extension. A future GCC that changes the format or
counter set again needs another patch branch — cross-check `gcc/gcov-counter.def`
and `gcc/gcov-io.h` for that release.

## Build

`build.sh` is the one command that builds this extension, and the
`TestEmbeddedCoverage` suite runs it automatically before loading — so the
loaded `.llext` always matches the current source (the embedded analogue of
repo1 recompiling its binary each run). To build by hand into the configured
`[coverage.embedded].build_dir`:

```bash
tests/repo3/product/build.sh ~/build/cov_ext_app
```

It is **idempotent**: it initializes the embedded-gcov submodule and applies the
gcc 12–14 patch if not already done (making the One-time setup above optional), runs
an incremental `west build` for `mps2_an385` (falling back to a pristine rebuild
if the build dir was previously initialized for a different source tree), then
strips the sections LLEXT 3.7 cannot relocate. It runs on the machine executing
the suite (the dev VM, where `build_dir` lives); toolchain paths default to the
Vagrant-provisioned locations and are overridable via `ZEPHYR_VENV` /
`ZEPHYR_WORKSPACE` / `ZEPHYR_SDK_INSTALL_DIR`.

The equivalent manual steps (what `build.sh` runs):

```bash
source ~/zephyr-venv-v3_7/bin/activate
cd ~/zephyrproject-v3_7 && source zephyr/zephyr-env.sh

west build -b mps2_an385 -d ~/build/cov_ext_app /path/to/tests/repo3/product

# Strip the sections LLEXT 3.7 cannot relocate (its loader -ENOEXECs on
# .init_array/.fini_array relocations; cov_init calls the ctor explicitly).
arm-zephyr-eabi-objcopy \
    --remove-section='.init_array*' --remove-section='.fini_array*' \
    --remove-section='.rel.init_array*' --remove-section='.rel.fini_array*' \
    --strip-debug \
    ~/build/cov_ext_app/zephyr/cov_ext.llext ~/build/cov_ext_app/zephyr/cov_ext.stripped.llext
```

`cov_ext.stripped.llext` (~15 KB) is what otto sends via `llext load_hex`. See
[`../docs/feasibility.md`](../docs/feasibility.md) for why these specific
sections are stripped and the full bring-up record.
