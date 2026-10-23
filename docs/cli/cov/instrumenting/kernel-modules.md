# Kernel-module products

A `kind = "kmod"` product is a Linux kernel module, loaded and unloaded with
`insmod`/`rmmod` instead of staged and run. Its coverage story is different
from every other kind on this page's siblings, because a kernel module has
no libc, no process exit, and — on most distribution kernels — no gcov
runtime to hand counters to in the first place.

The `.ko` still crosses to the host before the `insmod`, and where it lands
is the entry's `stage_dir` — one field, one rule, on every kind that places
a file: see {doc}`../../../configuration/declared-products-tools`. The staged
copy is removed once the module is resident.

## What a module lacks

A user-space product needs nothing beyond `--coverage`: gcc's own
constructor registers each translation unit's counters before `main` runs,
and `__gcov_exit` flushes them when the process exits. Neither half of that
exists for an out-of-tree module on a stock kernel. `CONFIG_CONSTRUCTORS`
is off, so the kernel never runs a module's constructors and plain
`-fprofile-arcs` registers nothing with anyone. `CONFIG_GCOV_KERNEL` is
off too, so there is no in-kernel gcov and no
`/sys/kernel/debug/gcov/` tree to fall back on.

The way through is to run those constructors ourselves. Plain
`-fprofile-arcs` still emits one per translation unit — gcc's calls
`__gcov_init`, clang's `llvm_gcov_init` — into the module's `.init_array`;
the kernel keeps that section, it just never walks it. Two tiny sentinel
objects, linked first and last in the module's object list, bracket the
section so a runtime can call every constructor between them at the
module's own request. `otto_kmodcov` is that runtime.

## The library

`otto_kmodcov` (`src/otto/kmodcov/` in otto's tree; shipped in the wheel) is a
small GPL kernel module every instrumented consumer links against — a
"library" in kernel space is just another loadable module that exports
symbols. It vendors both of the kernel's own gcov backends — `gcc_4_7.c`
for gcc (every format from gcc 4.7 to 15) and `clang.c` for clang (11 and
newer) — and builds whichever matches the compiler building it, exporting
that family's runtime symbols (`__gcov_init` and the `__gcov_merge_*` set,
or `llvm_gcov_init` and the `llvm_gcda_*` callbacks) so an instrumented
object links, plus the two calls a consumer actually drives:

```{literalinclude} ../../../../src/otto/kmodcov/kmodcov.h
:language: c
:start-at: "int kmodcov_register"
:end-at: "void kmodcov_unregister(struct module *mod);"
```

Registering creates `/sys/module/<module>/kmodcov/dump` and `.../reset` —
two write-only sysfs files beside the `parameters/` directory the loader
gives every module, so the module method needs no `CONFIG_DEBUG_FS` — and
gives the module a private **accumulator** — a copy of each instrumented
object's counters. Writing to `dump` adds the live counters into the
accumulator, zeroes the live ones, and writes the accumulator out as
`<cov_dir>/<absolute object path>.gcda`; because the accumulator holds the
running total and the live counters start over every time, two dumps in
one load add correctly instead of double-counting. Writing to `reset` zeroes
both. A module registered twice without an unregister between is refused
(`-EBUSY`), and a control file that cannot be created fails the
registration, so an `insmod` that succeeds has both files.

### Counts across loads

A module's counts accumulate across loads, the way a user-space program's
do across runs: load, exercise, unload, load again, exercise, unload, and
the `.gcda` files hold both loads' hits, until a clean deletes them. Each
load seeds an object's accumulator once, from the object's `.gcda` already
on disk, at that object's first dump and before the dump writes. Until
then the object is *pending*; afterwards it is *seeded* (the file was added
in, or there was none) or *skipped*, and neither reads the file again
during that load.

- **Reset stops a seed.** Writing to `reset` zeroes the live counters and
  the accumulator and skips every object, so a reset before the first dump
  means the file is never read. otto's clean ({doc}`../clean`, which
  `--cov-clean` runs too) resets a loaded module, then deletes the files:
  whatever dump runs between the two writes zeroed counters and cannot
  bring the old counts back. The `reset` file itself never deletes one.
- **A file that does not match is overwritten.** The whole file is checked
  against the loaded object before any of it is added: the gcov magic and
  version, the object's stamp, every function's checksums, and every
  record's length against the file's size. A file from another build —
  the module was rebuilt without a clean — adds nothing; the kernel log
  says once which file and why (`<module>: <file> does not match this
  object (<reason>); not adding its counts, and this dump overwrites it`)
  and that dump replaces it with this load's counts.
- **A file that cannot be read is kept.** An I/O or allocation failure
  leaves the object pending: a sysfs `dump` keeps the counts in memory,
  does not touch the file, fails with the error, and warns once in the
  kernel log. Each later dump retries. The unload's dump is the last
  chance: it retries once, and if it still fails it writes this load's
  counts and warns that the earlier loads' counts in that file are lost.

The `KMODCOV_INTERFACE` number does not change with this behaviour — a
consumer needs no rebuild — but the library itself does: it comes with an
otto release, and a lab picks it up by re-exporting the sources
(`otto cov kmodcov export`, or `otto init --kmodcov`) and rebuilding the
`otto_kmodcov` dev tool.

## Getting the library

- **In a project otto scaffolds**: `otto init --kmodcov` vendors the sources into
  `third_party/otto_kmodcov` (`--kmodcov-dir` names another directory), appends a
  commented `[[dev_tools]]` entry of kind `kmodcov` to fill in, and writes a
  consumer starter beside it — the two sentinel files, a `Kbuild.example`,
  and a `README.md`. Re-run `otto init --kmodcov` after upgrading otto to
  refresh the vendored library. See {doc}`../../init` for the area's full
  detect/validate/scaffold contract.
- **Just the sources**: `otto cov kmodcov export <dir>` writes the same
  sources with none of the rest of that scaffolding; commit the directory. A
  machine that only builds the module needs no otto at all. See
  {doc}`../index`.
- **Staying current**: `otto cov kmodcov check <dir>` exits 0 when the
  directory is current, 1 when it differs (naming the files and the otto
  that exported them), or 2 when it is absent; run it in CI. `otto init`
  reports the same drift as a warning for every declared kmodcov `source`.
  Neither command notices a file that a later otto no longer ships — both
  walk only the list of files this otto currently ships — so a re-export
  after an upgrade can leave such a file behind, for the project's own diff to
  catch.
- **Kernel differences**: the library builds from 2.6.32 on; the 2.6.39 to
  3.5 window builds from arms no kernel in the proof set has run, marked
  untested in the header, and a `kmodcov_local.h` beside the sources replaces
  one kernel-facing name at a time. `Kbuild` force-includes that file with
  `-include` whenever it exists, ahead of `kmodcov_compat.h`'s own `#ifndef`
  defaults, so a rebuild is all that is needed. See
  [Kernel versions](#kernel-versions) for the range, the names and the
  contract. `check` ignores the file and `export` never overwrites it.
  Proving a customised library against the matrix's own contracts is issue
  #406.
- **What otto checks**: the built `.ko`'s `MODULE_VERSION`
  (`<otto version>+kmodcov<n>`, read directly off the file, no host tool)
  must carry this otto's interface number — checked at lab load when the
  file already exists, and checked again, unconditionally, at every
  `install` — the load driven by the `[[dev_tools]]` entry of
  [Declaring the module and its library](#declaring-the-module-and-its-library)
  below — before the library is loaded. `vermagic` and the compiler are
  what the module itself already reports: see [Building](#building) for the
  `vermagic` rule and
  [Another kernel, ISA or compiler](#another-kernel-isa-or-compiler) for the
  compiler-family one.

## Instrumenting a module

The worked example throughout this page is `tests/project5/kmod/demo/`, a
small bounded queue driven from a debugfs control file, living inside the
project that declares it as a product. A project that reports coverage for a
module must own that module's sources and build them in place: coverage
capture anchors every measured file to a committed git blob under the
project root. The library's own `README.md` — the copy you hold, e.g.
`third_party/otto_kmodcov/README.md` (`src/otto/kmodcov/README.md` in otto's
own source tree) — documents the same three steps that follow, in more
general terms, for any consumer.

A consumer becomes coverage-instrumented in three steps:

**1. Sentinels.** Link two tiny objects — one built from a file containing
only `KMODCOV_SENTINEL_BEGIN;`, one from a file containing only
`KMODCOV_SENTINEL_END;` — first and last in the object list, so `ld -r`'s
input-order guarantee (and the module linker script's `SORT(.init_array.*)`
then `.init_array` layout) keeps them, and only them, outside the
constructors every instrumented object contributes — `KMODCOV_INIT()` calls
what lies between. That is every constructor, not only gcov's: a module of
your own with `__attribute__((constructor))` functions linked between the
sentinels has those run by `KMODCOV_INIT()` too, which is what a
`CONFIG_CONSTRUCTORS` kernel would have done for them. The same Kbuild also
applies the flags step below to each instrumented object:

```{literalinclude} ../../../../tests/project5/kmod/demo/Kbuild
:language: make
```

The module's `Makefile` hands kbuild the library's build directory as
`KMODCOV` and its `Module.symvers` as `KBUILD_EXTRA_SYMBOLS`, so the
module's references to the library's exports resolve:

```{literalinclude} ../../../../tests/project5/kmod/demo/Makefile
:language: make
```

**2. Flags.** Compile every instrumented object with `$(KMODCOV_CFLAGS)`,
defined by the library's own Kbuild fragment:

```{literalinclude} ../../../../src/otto/kmodcov/consumer.mk
:language: make
```

**3. Macros.** Call `KMODCOV_DECLARE()` at file scope, `KMODCOV_INIT()` first in
the module's init routine, and `KMODCOV_EXIT()` last in its exit routine — and
first on every error return of the init routine after `KMODCOV_INIT()` has
succeeded, the way the demo's `demo_init` does below. The registration holds
the module's own sysfs directory; a module that leaves it held cannot be
freed, and `insmod` or `rmmod` waits forever:

```{literalinclude} ../../../../src/otto/kmodcov/kmodcov.h
:language: c
:start-at: "/* File scope, once per consumer"
:end-at: "#define KMODCOV_EXIT() kmodcov_unregister(THIS_MODULE)"
```

The demo applies all three at the call sites they describe. Declaring the
parameter, at file scope:

```{literalinclude} ../../../../tests/project5/kmod/demo/demo_main.c
:language: c
:start-at: "KMODCOV_DECLARE();"
:end-at: "KMODCOV_DECLARE();"
```

Registering, first in the init routine, failing the module load if it
fails:

```{literalinclude} ../../../../tests/project5/kmod/demo/demo_main.c
:language: c
:start-at: "int err = KMODCOV_INIT();"
:end-at: "return err;"
```

And unregistering, as the last statement of the exit routine — so the exit
dump `kmodcov_unregister` performs sees everything the exit routine did
before it:

```{literalinclude} ../../../../tests/project5/kmod/demo/demo_main.c
:language: c
:start-at: "demo_queue_free(&queue);"
:end-at: "KMODCOV_EXIT();"
```

The demo also carries a `demo_compat.h`, three definitions of what a
module of 2.6.32's age carries itself: `simple_open` below 3.5, `kstrtol`
below 2.6.39 and `strim` below 2.6.33, each under its own version guard.
It is the demo's business, not the library's — a module
written for one kernel needs none of it — and it is what lets `make kmodcov`
build the same three instrumented units, with the same coverage
expectations, on every kernel of the set.

## Building

Build against the headers of the kernel release the module will **load
into** — a `.ko`'s `vermagic` string must match that release exactly, and a
mismatch fails `insmod` with a message that only the kernel log explains:

```bash
otto host test1 exec --sudo dmesg
```

The vendored copy's own `build.sh <build-dir> [<release>]`
(`third_party/otto_kmodcov/build.sh` in a user's project; `src/otto/kmodcov/build.sh`
in otto's own tree — the same script, copied verbatim by `export`) takes
the release as its second, optional argument, defaulting to the build
machine's own running kernel when none is given, and checks the resulting
`.ko`'s `vermagic` against it — `KDIR` names a different
kernel tree to build against instead, and the release then comes from that
tree (see below). The library builds **out of tree**: it is never itself a
measured product, so `build.sh` copies its sources into a scratch directory
and builds them there against `/lib/modules/<release>/build`. The consumer
builds **in place**, next to its own committed sources, for the reason
given above. `tests/project5/kmod/build.sh` does both, library first, and
checks the resulting `vermagic` before calling either build a success:

```{literalinclude} ../../../../tests/project5/kmod/build.sh
:language: bash
```

`tests/project5/build.sh` is the human entry point: it runs that script and
then the container image's own `docker/build.sh`.

A product project's own module follows the same shape: build `otto_kmodcov` out
of tree once, then build the module against it in place, checking
`vermagic` the same way before it ships anywhere. Only the library's `.ko`
carries `modinfo -F version`: `<otto version>+kmodcov<n>`, naming the otto
that exported the sources and the interface they implement — the
consumer's own `.ko` carries no such stamp, and otto never looks for one
there, only on the kmodcov dev tool's own artifact (see
[Getting the library](#getting-the-library) above for what otto checks).

## Another kernel, ISA or compiler

`otto_kmodcov` parses what the consumer's compiler emitted, which fixes the
one rule that binds every build: **the library and its consumers are built
by the same compiler family, and for gcc by the same major version.**
gcc's `gcov_info` layout is chosen by `__GNUC__` when the library is
compiled, so a consumer from another gcc major is refused at `KMODCOV_INIT()`,
loudly: `dmesg` names both majors, `KMODCOV_INIT()` returns `-EPROTO`, and a
consumer that returns that error from its init routine — the pattern above —
does not load at all. Nothing quietly reports less coverage than the build
asked for. A consumer from the other family never gets that far: the library
exports only its own family's runtime symbols, so `insmod` fails with
`Unknown symbol __gcov_init` (a gcc consumer on a clang-built library) or
`Unknown symbol llvm_gcov_init` (the reverse).
clang's format does not change across clang versions, so for clang the rule
is just the family and kbuild's own floor of clang 11. What each installed
compiler was last measured to do, contract by contract, is the
{ref}`compatibility matrix <kmodcov-matrix>`.

Both build scripts — the library's `build.sh` and the fixture's
`kmod/build.sh` — take the kernel tree and the toolchain from the
environment exactly as your own module's build would, and pass them to
kbuild unchanged:

| Variable | Meaning |
|---|---|
| `KDIR` | the kernel tree: a distro headers package or a prepared source tree, anywhere (default `/lib/modules/<release>/build`) |
| `ARCH`, `CROSS_COMPILE` | the target architecture and the cross toolchain prefix, as kbuild takes them |
| `LLVM=1` | build with clang, lld and the LLVM binutils |
| `CC` | one specific compiler, such as `gcc-12` (passed on kbuild's command line, where it overrides the kernel's own choice) |
| `KMAKEFLAGS` | extra `make` arguments, verbatim: the place for `CONFIG_*` overrides |

A distro headers package ships host tools for its own architecture and
cannot be used from a different build machine; a foreign target needs a
source tree prepared for it (and `modules_prepare` builds the kernel's own
host tools, so the build machine needs `flex`, `bison`, `libelf-dev` and
`libssl-dev` — the Debian and Ubuntu package names — beside the cross
compiler). A module meant to be *loaded* is built against a fully built kernel tree or
the target's own headers package — either ships a real `Module.symvers` —
never a bare `modules_prepare` tree. The cross-build recipe otto's own proof
runs is on {doc}`../../../developing/kmodcov-provisioning`.

**Build an old kernel's modules with a compiler of that kernel's own era.**
An old kernel's headers and kbuild assume the compilers of their time, and a
newer gcc is unsupported there. How otto provisions those kernels and
compilers, and what `make kmodcov` proves on each, is maintainer material on
{doc}`../../../developing/kmodcov-provisioning`.

A stock kernel's config was written for the compiler that built it, and
kbuild applies that config's compiler-specific flags to every external
module. Two cases need `KMAKEFLAGS`, both proven on Ubuntu's
`6.8.0-86-generic`, configured for gcc 13:

- **clang against a gcc-built kernel**: tell kbuild the compiler is clang
  and replace the two gcc-only flags it rejects —
  `LLVM=1 KMAKEFLAGS="CONFIG_CC_IS_GCC= CONFIG_GCC_VERSION=0 CONFIG_CC_IS_CLANG=y CONFIG_CLANG_VERSION=180103 CONFIG_CC_IMPLICIT_FALLTHROUGH=-Wimplicit-fallthrough CONFIG_UBSAN_BOUNDS_STRICT= CONFIG_UBSAN_ARRAY_BOUNDS=y"`
  (`CONFIG_CLANG_VERSION` is `clang -dumpversion` as one number). A
  clang-built kernel needs none of this.
- **a gcc older than the kernel's**: tell kbuild the real version (it gates
  flags on `CONFIG_GCC_VERSION`) and turn off the options whose flags your
  gcc names in its error — for gcc 9, 10 and 11 against that kernel,
  `CC=gcc-11 KMAKEFLAGS="CONFIG_GCC_VERSION=110500 CONFIG_SHADOW_CALL_STACK= CONFIG_INIT_STACK_ALL_ZERO= CONFIG_ZERO_CALL_USED_REGS="`.
  gcc 12 and newer need nothing there.

Getting a module to load is not the same as getting its counters read
back. The gcov that reads them is chosen per product from the data's own
stamp — the system `gcov`, `gcov-<major>` for a module built by another
gcc, `llvm-cov` for clang — unless the bed host's `toolchain.gcov` names
one; the order, and the failure naming the package when a tool is missing,
are on the {ref}`main coverage page <coverage-gcov-resolution>`. What
naming `llvm-cov` there means for otto is [clang's own page](clang.md).

A clang build records the inlined kernel-header records differently:
clang's `.gcno` carries no compilation directory (gcc 9+ records it), so
those records are relative to the kernel tree, and the reporting machine
usually has no such file. That no longer fails the capture: otto runs every
capture with `--ignore-errors source`, so a missing source file is never an
error. Each header path otto cannot place in the project is logged as a
warning and left out of the report; those records are kernel headers,
never the module's own files. If you have the kernel tree on the reporting
machine, `["--base-directory", "<kernel tree>"]` in the bed host's
`toolchain.lcov_args` ({doc}`../../../configuration/lab/coverage-toolchain`'s toolchain
table) lets lcov resolve them instead.

## Kernel versions

The library builds against every kernel from 2.6.32 on, proven through 6.17,
each compiled with a gcc of its kernel's own era, 4.7 for 2.6.32 up to 15
for 6.17. Every kernel-facing call goes through a `KMODCOV_` name whose
default `kmodcov_compat.h` chooses by `LINUX_VERSION_CODE`, and a
`kmodcov_local.h` beside the sources defines a name first to replace its
default outright (see [Getting the library](#getting-the-library)). The
2.6.39 to 3.5 window is the one stretch no kernel in otto's set has run:
from 2.6.39 to 3.0 upstream had renamed `path_lookup()` to
`kern_path_parent()`, and from 3.1 to 3.5 `kern_path_create()` existed
without `done_path_create()`. The header carries an arm for each half,
marked UNTESTED in a comment beside the code that names the headers column
which would prove it; a module on those kernels builds, and the caveat sits
where the reader is. Proving them is issue #531. A kernel whose version
number lies about its APIs (a distribution kernel with backports) gets the
same remedy, a `kmodcov_local.h` that defines the name. On the beds' 6.8
kernel the arm each name selects for 6.8 runs live, which is the newest for
every name but `KMODCOV_MKDIR` (its 6.3 to 6.14 arm); every other proven arm
is build-proven (compiled against that kernel's tree, never loaded) on the
kernel columns of the {ref}`compatibility matrix <kmodcov-matrix>`, and the
two untested arms are proven by nothing yet.

| name | what it does | arms by kernel version | proven by |
|---|---|---|---|
| `KMODCOV_MKDIR(path)` | create the last component of an absolute path whose parents exist; `-EEXIST` counts as success | below 2.6.39 `path_lookup` + `lookup_create`; 2.6.39–3.0 the same with `kern_path_parent` (untested); 3.1–3.5 `kern_path_create` with `done_path_create` written out (untested); 3.6–4.0 `kern_path_create` with `->d_inode`; 4.1–5.11 `d_inode()`; 5.12–6.2 `mnt_user_ns`; 6.3–6.14 `mnt_idmap`; 6.15+ `vfs_mkdir` returns the dentry | `2.6.32`, `3.13`, `4.4`, `5.15`, the beds' 6.8 live, and the 6.8 tree (`x86_64-cross`), `6.17`, the untested arms: none |
| `KMODCOV_FILE_WRITE(file, buf, len, ppos)` | write a kernel buffer at `*ppos`, advancing it | below 3.9 `vfs_write` under `set_fs`; 3.9–4.13 `kernel_write` with the offset by value; 4.14+ `kernel_write` by pointer | `2.6.32`, `3.13`, `5.4` |
| `KMODCOV_FILE_READ(file, buf, len, ppos)` | read into a kernel buffer from `*ppos`, advancing it — the seed of [counts across loads](#counts-across-loads) | below 4.14 `kernel_read` with the offset by value; 4.14+ `kernel_read` by pointer | `2.6.32`, `3.13`, `4.4`, `5.4` |
| `KMODCOV_BIG_ALLOC` / `KMODCOV_BIG_FREE` | the `.gcda` image buffer | below 4.12 `vmalloc`/`vfree`; 4.12+ `kvmalloc`/`kvfree` | `4.4`, `5.4` |
| `KMODCOV_WITHIN_MODULE(addr, mod)` | whether an address is the module's | below 3.17 `within_module_core` or `within_module_init`; 3.17+ `within_module` | `3.13`, `4.4` |
| `list_first_entry_or_null`, `list_last_entry`, `list_next_entry`, `__list_del_entry` | the list helpers the clang backend uses | provided below 3.10, 3.13, 3.13 and 2.6.38 | `2.6.32`, `3.13` (the "build: the clang backend's unit compiles with the column's gcc" row) |
| `pr_warn` | the kernel-log warnings of [counts across loads](#counts-across-loads) | `pr_warning` where the kernel has no `pr_warn` (below 2.6.37) | `2.6.32` |
| `KMODCOV_ALLOC`, `KMODCOV_ALLOC_ARRAY`, `KMODCOV_STRDUP`, `KMODCOV_MEMDUP`, `KMODCOV_ASPRINTF`, `KMODCOV_FREE`, `KMODCOV_DEFINE_LOCK`, `KMODCOV_LOCK`, `KMODCOV_UNLOCK`, `KMODCOV_SYSFS_DIR`, `KMODCOV_SYSFS_DIR_PUT`, `KMODCOV_SYSFS_GROUP`, `KMODCOV_SYSFS_GROUP_REMOVE` | allocation, the lock, the sysfs control files | one default each, unchanged since 2.6.32 | every column |

The contract an override must keep: `KMODCOV_ALLOC` and `KMODCOV_ALLOC_ARRAY`
return zeroed memory; `KMODCOV_FREE` and `KMODCOV_BIG_FREE` accept `NULL`;
whatever `KMODCOV_ALLOC`, `KMODCOV_ALLOC_ARRAY`, `KMODCOV_STRDUP`, `KMODCOV_MEMDUP`
and `KMODCOV_ASPRINTF` return is releasable by `KMODCOV_FREE`, and
`KMODCOV_BIG_ALLOC`'s by `KMODCOV_BIG_FREE` — a mismatched pair corrupts rather
than fails. `KMODCOV_MKDIR` returns `0`, `-EEXIST` or another negative errno;
`KMODCOV_FILE_WRITE` returns the bytes written or a negative errno, and
`KMODCOV_FILE_READ` the bytes read (`0` at the end of the file) or a
negative errno.

A reader writes one of these for a distribution kernel whose version number
lies about its APIs, or to replace an untested arm that misbehaves:

```c
/* kmodcov_local.h */
#define KMODCOV_MKDIR(path) my_mkdir(path)
```

## Declaring the module and its library

The two live in different seams. The library is a `[[dev_tools]]` entry of
kind `kmodcov` — project tooling nothing measures, one entry per kernel, pointed
at the hosts running it by its `match` table — and the consumer is an
ordinary `kmod` product with `coverage = "module"`:

```{literalinclude} ../../../../tests/project5/.otto/settings.toml
:language: toml
:start-at: "[[dev_tools]]"
:end-before: "# The container-image products"
```

otto loads the library on demand, at the consumer's own `install`, when it
is not already resident — so an `install-tools` before the run costs
nothing, and nothing depends on declaration order. Both halves of the
binding are checked at lab load: a host carrying a `coverage = "module"`
product with no matching `kmodcov` entry is refused, naming the products, the
host and the kind, and so is a host matching two of them. `cleanup` unloads
the library after the products, which is the order dev tools always come
down in; a test module that drives the products itself and never runs
`cleanup` unloads it in its own teardown, the way
`tests/project5/tests/test_kmod_demo.py` does. Such a teardown leaves the lab
uninstalled, module and library both, so the next run puts them back:
`otto run install --ensure` ({doc}`../../run/defaults`) installs them again,
and a test marked `@pytest.mark.ensure("installed")`
([Declaring lab state](../../../setup/writing-tests.md#declaring-lab-state-the-ensure-marker))
converges the lab the same way before it runs. Either one loads the library
on demand, at the module's own `install`.

`otto_kmod_demo` sets `coverage = "module"` and a `cov_dir` for `otto_kmodcov`
to write under. A module built with the sentinel/macro snippet above refuses
to load without that `cov_dir=` argument — `KMODCOV_INIT()` returns `-EINVAL`
— so `coverage = "none"` on such a module, or a manual `insmod`, fails with
a message only the kernel log shows: `otto host <id> exec --sudo dmesg`
({doc}`../../host/exec`). See
{doc}`../../../configuration/declared-products-tools` for every parameter
these kinds take — this page only walks the build the params point at.

## The `kernel` method

When the kernel itself has `CONFIG_GCOV_KERNEL`, none of the above is
needed: the kernel maintains counters for every compiled-in and loaded
object under its own `/sys/kernel/debug/gcov/` tree, keyed by the absolute
path the module was built at. `coverage = "kernel"` points a product at its
own subtree of that tree (`gcov_path`) instead of loading `otto_kmodcov`, and
otto copies the `.gcda` entries under it into `cov_dir` — debugfs entries
report size 0, so `scp` cannot fetch them directly, and this copy is the
materialize step. Resetting writes each entry under `gcov_path`
individually, never the tree's own global reset, which would zero every
other loaded module's counters along with this one's. See
{doc}`../../../configuration/declared-products-tools` for the parameter
table.

## What the report shows

If `otto cov report` fails outright instead of showing anything below — a
gcov version mismatch, or clang's unreadable kernel-header records — see
the remedies under
[Another kernel, ISA or compiler](kernel-modules.md#another-kernel-isa-or-compiler)
above.

A run of `TestKmodDemo` produces exactly three tracked files per host —
`demo_main.c`, `demo_parse.c`, `demo_policy.c`, one per translation unit
compiled with `$(KMODCOV_CFLAGS)`; the two sentinel objects carry no code of
their own and contribute nothing to the report. The demo's own `ctl` read-back
reports a `dropped` counter alongside `enqueued` and `drained`: under the
`fifo`/`lifo` policies it counts rejected enqueues into a full queue, but
under `drop-oldest` — which never rejects — it counts evicted items instead.
The exit routine's lines — `demo_exit`'s cleanup and its `pr_info` calls —
show real hits rather than zero, because the tests leave the queue non-empty
before teardown unloads the module and the exit dump captures what the exit
routine did. Three paths stay uncovered: sending `drain` with an argument
(`demo_parse.c`'s `if (arg)` guard), lowering `limit` below the queue's
current length (`demo_policy.c`'s `if (cap < q->len)` guard), and the policy
switch's `default:` arm (`demo_policy.c`'s `switch (q->policy)`), unreachable
since the parser only ever admits the three named policies.
