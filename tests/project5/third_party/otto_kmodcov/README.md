# otto_kmodcov

A GPL companion kernel module that gives out-of-tree modules gcov coverage
on a stock kernel built without `CONFIG_GCOV_KERNEL` or
`CONFIG_CONSTRUCTORS`. It runs the gcov constructors the kernel never runs,
provides the runtime symbols those constructors and objects reference (gcc's
`__gcov_*` or clang's `llvm_gcov_*`/`llvm_gcda_*`, whichever compiler built
the library), keeps an accumulator per instrumented object (a dump never
double counts, two dumps of one load add correctly, and each load adds its
counts to the `.gcda` the earlier loads left), and exposes two
sysfs control files, `dump` and `reset`, per registered module. It builds
with any gcc from 4.7 to 15, or clang 11 and newer. It builds against every
kernel from 2.6.32 on, proven through 6.17. The 2.6.39 to 3.5 window builds
from arms no kernel in the proof set has run; `kmodcov_compat.h` marks them
UNTESTED beside the code.
Every kernel-facing call goes through a `KMODCOV_` name whose default
`kmodcov_compat.h` chooses by kernel version, and the library and its
consumers must be built by the same compiler family, and for gcc by the
same major.

This directory reaches a user's project one of two ways: `otto init --kmodcov`
vendors it (default `third_party/otto_kmodcov`) alongside a commented
`[[dev_tools]]` entry and a consumer starter, or `otto cov kmodcov export
<dir>` vendors just the sources with none of the rest of that scaffolding.
`otto cov kmodcov check <dir>` compares a vendored copy with the library this
otto ships (exit 0 current, 1 differs, 2 absent) and `otto init` reports
the same drift as a warning. Every build reports a `MODULE_VERSION` of
`<otto version>+kmodcov<n>`, `n` being the interface number
(`KMODCOV_INTERFACE` in `kmodcov.h`) this build implements — the sysfs
layout, the `cov_dir` parameter and the consumer macros below all move
together with it, and otto refuses to load a `.ko` whose `n` does not match
its own. A `kmodcov_local.h` beside the sources, never exported nor compared,
lets a build replace, one name at a time, any of the kernel-facing names
`kmodcov_compat.h` isolates for it — mkdir, file read and write, the module-address
range, allocation, the lock and the sysfs control files — the same header
that holds the kernel version ladder; the supported range, the full name
list and the override contract are on the kernel-modules guide page's
[Kernel versions](https://otto-sh.readthedocs.io/en/stable/cli/cov/instrumenting/kernel-modules.html#kernel-versions)
section. See
[the kernel-modules guide page](https://otto-sh.readthedocs.io/en/stable/cli/cov/instrumenting/kernel-modules.html#getting-the-library)
for all of the above in more detail.

`build.sh <build-dir> [<release>]` builds it out of tree; `KDIR`, `ARCH`,
`CROSS_COMPILE`, `LLVM`, `CC` and `KMAKEFLAGS` pass through to kbuild — see
the kernel-modules guide page's
[Another kernel, ISA or compiler](https://otto-sh.readthedocs.io/en/stable/cli/cov/instrumenting/kernel-modules.html#another-kernel-isa-or-compiler)
section.

A consumer becomes coverage-instrumented in three steps:

1. **Sentinels.** Link two tiny objects — one built from a file containing
   only `KMODCOV_SENTINEL_BEGIN;` and one from a file containing only
   `KMODCOV_SENTINEL_END;` — first and last in the consumer's object list, so
   they bracket the gcov constructors every other object contributes to
   `.init_array`.
2. **Flags.** Compile every instrumented object with `$(KMODCOV_CFLAGS)` from
   `consumer.mk`, which also adds `-I$(KMODCOV)` via `ccflags-y` so `kmodcov.h`
   is on the include path — nothing else to pass. `consumer.mk` also applies
   `kmodcov.lds` at the module's intermediate link, which is what brackets a
   `.ctors`-convention toolchain's constructors too.
3. **Macros.** Call `KMODCOV_DECLARE()` at file scope, `KMODCOV_INIT()` as the
   first statement of the module's init routine, and `KMODCOV_EXIT()` as the
   last statement of its exit routine. Every error return of the init routine
   after a successful `KMODCOV_INIT()` must call `KMODCOV_EXIT()` first: the
   registration holds the module's own sysfs directory, and a module that
   leaves it held cannot be freed — `insmod` or `rmmod` waits forever.
   The worked consumer below shows the shape.

Once registered, a write to `/sys/module/<module>/kmodcov/dump` writes each
instrumented object's `.gcda` file under the module's `cov_dir` parameter,
first adding in the counts an earlier load left in that file; a write to
`.../reset` zeroes the accumulated counters and stops that seed for the
rest of the load. How a file that does not match, or cannot be read, is
handled is on the kernel-modules guide page's
[Counts across loads](https://otto-sh.readthedocs.io/en/stable/cli/cov/instrumenting/kernel-modules.html#counts-across-loads)
section. The two files are
sysfs, beside the `parameters/` directory the loader gives every module, so
no `CONFIG_DEBUG_FS` is needed. The exit routine's own coverage is captured
by the dump `KMODCOV_EXIT()` performs on unregister.

A worked consumer following these three steps is the demo module in otto's
kernel-module coverage guide, under
[Instrumenting a module](https://otto-sh.readthedocs.io/en/stable/cli/cov/instrumenting/kernel-modules.html#instrumenting-a-module):
a bounded queue driven from a control file of its own, built in place by its
project's `kmod/build.sh` against the library this directory's own `build.sh`
builds out of tree. It lives in the project rather than here because otto's
coverage capture anchors every measured file to a committed git blob under the
project root that declares the `[[products]]` entry.
