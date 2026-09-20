# otto_kgcov

A GPL companion kernel module that gives out-of-tree modules gcov coverage
on a stock kernel built without `CONFIG_GCOV_KERNEL` or
`CONFIG_CONSTRUCTORS`. It runs the gcov constructors the kernel never runs,
provides the runtime symbols those constructors and objects reference (gcc's
`__gcov_*` or clang's `llvm_gcov_*`/`llvm_gcda_*`, whichever compiler built
the library), keeps an accumulator per instrumented object (a dump never
double counts, and two dumps of the same run add correctly), and exposes a
debugfs control file per registered module. Any gcc from 4.7 to 15, or
clang 11 and newer; the library and its consumers must be built by the same
compiler family, and for gcc by the same major.

This directory reaches a user's repo one of two ways: `otto init --kgcov`
vendors it (default `third_party/otto_kgcov`) alongside a commented
`[[dev_tools]]` entry and a consumer starter, or `otto cov kgcov export
<dir>` vendors just the sources with none of the rest of that scaffolding.
`otto cov kgcov check <dir>` compares a vendored copy with the library this
otto ships (exit 0 current, 1 differs, 2 absent) and `otto init` reports
the same drift as a warning. Every build reports a `MODULE_VERSION` of
`<otto version>+kgcov<n>`, `n` being the interface number
(`KGCOV_INTERFACE` in `kgcov.h`) this build implements — the debugfs
layout, the `gcov_dir` parameter and the consumer macros below all move
together with it, and otto refuses to load a `.ko` whose `n` does not match
its own. A `kgcov_local.h` beside the sources, never exported nor compared,
lets a build replace the kernel-facing allocation, lock and debugfs names
`kgcov_gcov.h` isolates for it, one name at a time. See
[the kernel-modules guide page](../../../docs/cli/cov/instrumenting/kernel-modules.md#getting-the-library)
for all of the above in more detail.

`build.sh <build-dir> [<release>]` builds it out of tree; `KDIR`, `ARCH`,
`CROSS_COMPILE`, `LLVM`, `CC` and `KMAKEFLAGS` pass through to kbuild — see
the kernel-modules guide page
(`docs/cli/cov/instrumenting/kernel-modules.md`, "Another kernel, ISA
or compiler").

A consumer becomes coverage-instrumented in three steps:

1. **Sentinels.** Link two tiny objects — one built from a file containing
   only `KGCOV_SENTINEL_BEGIN;` and one from a file containing only
   `KGCOV_SENTINEL_END;` — first and last in the consumer's object list, so
   they bracket the gcov constructors every other object contributes to
   `.init_array`.
2. **Flags.** Compile every instrumented object with `$(KGCOV_CFLAGS)` from
   `consumer.mk`, which also adds `-I$(KGCOV)` via `ccflags-y` so `kgcov.h`
   is on the include path — nothing else to pass.
3. **Macros.** Call `KGCOV_DECLARE()` at file scope, `KGCOV_INIT()` as the
   first statement of the module's init routine, and `KGCOV_EXIT()` as the
   last statement of its exit routine.

Once registered, a write to
`/sys/kernel/debug/otto_kgcov/<module>/dump` writes each instrumented
object's `.gcda` file under the module's `gcov_dir` parameter; a write to
`.../reset` zeroes the accumulated counters. The exit routine's own
coverage is captured by the dump `KGCOV_EXIT()` performs on unregister.

A worked consumer following these three steps lives at
[`tests/repo5/kmod/demo/`](../../../tests/repo5/kmod/demo/) — a bounded
queue driven from debugfs, built in place by `tests/repo5/kmod/build.sh`
against the library this directory's own `build.sh` builds out of tree. It
lives in the SUT repo rather than here because otto's coverage capture
anchors every measured file to a committed git blob under the repo that
declares the `[[products]]` entry.
