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
module's own request. `otto_kgcov` is that runtime.

## The library

`otto_kgcov` (`src/otto/kgcov/` in otto's tree; shipped in the wheel) is a
small GPL kernel module every instrumented consumer links against — a
"library" in kernel space is just another loadable module that exports
symbols. It vendors both of the kernel's own gcov backends — `gcc_4_7.c`
for gcc (every format from gcc 4.7 to 15) and `clang.c` for clang (11 and
newer) — and builds whichever matches the compiler building it, exporting
that family's runtime symbols (`__gcov_init` and the `__gcov_merge_*` set,
or `llvm_gcov_init` and the `llvm_gcda_*` callbacks) so an instrumented
object links, plus the two calls a consumer actually drives:

```{literalinclude} ../../../../src/otto/kgcov/kgcov.h
:language: c
:start-at: "int kgcov_register"
:end-at: "void kgcov_unregister(struct module *mod);"
```

Registering creates `/sys/kernel/debug/otto_kgcov/<module>/dump` and
`.../reset`, and gives the module a private **accumulator** — a copy of
each instrumented object's counters. Writing to `dump` adds the live
counters into the accumulator, zeroes the live ones, and writes the
accumulator out as `<gcov_dir>/<absolute object path>.gcda`; because the
accumulator holds the running total and the live counters start over every
time, two dumps in one run add correctly instead of double-counting.
Writing to `reset` zeroes both. `otto_kgcov` never parses a `.gcda` itself
— it only ever writes one.

## Getting the library

- **In a repo otto scaffolds**: `otto init --kgcov` vendors the sources into
  `third_party/otto_kgcov` (`--kgcov-dir` names another directory), appends a
  commented `[[dev_tools]]` entry of kind `kgcov` to fill in, and writes a
  consumer starter beside it — the two sentinel files, a `Kbuild.example`,
  and a `README.md`. Re-run `otto init --kgcov` after upgrading otto to
  refresh the vendored library. See {doc}`../../init` for the area's full
  detect/validate/scaffold contract.
- **Just the sources**: `otto cov kgcov export <dir>` writes the same
  sources with none of the rest of that scaffolding; commit the directory. A
  machine that only builds the module needs no otto at all. See
  {doc}`../index`.
- **Staying current**: `otto cov kgcov check <dir>` exits 0 when the
  directory is current, 1 when it differs (naming the files and the otto
  that exported them), or 2 when it is absent; run it in CI. `otto init`
  reports the same drift as a warning for every declared kgcov `source`.
  Neither command notices a file that a later otto no longer ships — both
  walk only the list of files this otto currently ships — so a re-export
  after an upgrade can leave such a file behind, for the repo's own diff to
  catch.
- **Kernel differences**: the library builds from 2.6.32 on; the 2.6.39 to
  3.5 window builds from arms no kernel in the proof set has run, marked
  untested in the header, and a `kgcov_local.h` beside the sources replaces
  one kernel-facing name at a time. `Kbuild` force-includes that file with
  `-include` whenever it exists, ahead of `kgcov_compat.h`'s own `#ifndef`
  defaults, so a rebuild is all that is needed. See
  [Kernel versions](#kernel-versions) for the range, the names and the
  contract. `check` ignores the file and `export` never overwrites it.
  Proving a customised library against the matrix's own contracts is issue
  #406.
- **What otto checks**: the built `.ko`'s `MODULE_VERSION`
  (`<otto version>+kgcov<n>`, read directly off the file, no host tool)
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

The worked example throughout this page is `tests/repo5/kmod/demo/`, a
small bounded queue driven from a debugfs control file, living inside the
repo that declares it as a product. A repo that reports coverage for a
module must own that module's sources and build them in place: coverage
capture anchors every measured file to a committed git blob under the SUT
repo. The library's own `README.md` — the copy you hold, e.g.
`third_party/otto_kgcov/README.md` (`src/otto/kgcov/README.md` in otto's
own source tree) — documents the same three steps that follow, in more
general terms, for any consumer.

A consumer becomes coverage-instrumented in three steps:

**1. Sentinels.** Link two tiny objects — one built from a file containing
only `KGCOV_SENTINEL_BEGIN;`, one from a file containing only
`KGCOV_SENTINEL_END;` — first and last in the object list, so `ld -r`'s
input-order guarantee (and the module linker script's `SORT(.init_array.*)`
then `.init_array` layout) keeps them, and only them, outside the
constructors every instrumented object contributes — `KGCOV_INIT()` calls
what lies between. That is every constructor, not only gcov's: a module of
your own with `__attribute__((constructor))` functions linked between the
sentinels has those run by `KGCOV_INIT()` too, which is what a
`CONFIG_CONSTRUCTORS` kernel would have done for them. The same Kbuild also
applies the flags step below to each instrumented object:

```{literalinclude} ../../../../tests/repo5/kmod/demo/Kbuild
:language: make
```

**2. Flags.** Compile every instrumented object with `$(KGCOV_CFLAGS)`,
defined by the library's own Kbuild fragment:

```{literalinclude} ../../../../src/otto/kgcov/consumer.mk
:language: make
```

**3. Macros.** Call `KGCOV_DECLARE()` at file scope, `KGCOV_INIT()` first in
the module's init routine, and `KGCOV_EXIT()` last in its exit routine:

```{literalinclude} ../../../../src/otto/kgcov/kgcov.h
:language: c
:start-at: "/* File scope, once per consumer"
:end-at: "#define KGCOV_EXIT() kgcov_unregister(THIS_MODULE)"
```

The demo applies all three at the call sites they describe. Declaring the
parameter, at file scope:

```{literalinclude} ../../../../tests/repo5/kmod/demo/demo_main.c
:language: c
:start-at: "KGCOV_DECLARE();"
:end-at: "KGCOV_DECLARE();"
```

Registering, first in the init routine, failing the module load if it
fails:

```{literalinclude} ../../../../tests/repo5/kmod/demo/demo_main.c
:language: c
:start-at: "int err = KGCOV_INIT();"
:end-at: "return err;"
```

And unregistering, as the last statement of the exit routine — so the exit
dump `kgcov_unregister` performs sees everything the exit routine did
before it:

```{literalinclude} ../../../../tests/repo5/kmod/demo/demo_main.c
:language: c
:start-at: "demo_queue_free(&queue);"
:end-at: "KGCOV_EXIT();"
```

The demo also carries a `demo_compat.h`, three definitions of what a
module of 2.6.32's age carries itself: `simple_open` below 3.5, `kstrtol`
below 2.6.39 and `strim` below 2.6.33, each under its own version guard.
It is the demo's business, not the library's — a module
written for one kernel needs none of it — and it is what lets `make kgcov`
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
(`third_party/otto_kgcov/build.sh` in a user's repo; `src/otto/kgcov/build.sh`
in otto's own tree — the same script, copied verbatim by `export`) takes
the release as its second, optional argument, defaulting to the build
machine's own running kernel when none is given, and checks the resulting
`.ko`'s `vermagic` against it — `KDIR` names a different
kernel tree to build against instead, and the release then comes from that
tree (see below). The library builds **out of tree**: it is never itself a
measured product, so `build.sh` copies its sources into a scratch directory
and builds them there against `/lib/modules/<release>/build`. The consumer
builds **in place**, next to its own committed sources, for the reason
given above. `tests/repo5/kmod/build.sh` does both, library first, and
checks the resulting `vermagic` before calling either build a success:

```{literalinclude} ../../../../tests/repo5/kmod/build.sh
:language: bash
```

`tests/repo5/build.sh` is the human entry point: it runs that script and
then the container image's own `docker/build.sh`.

A product repo's own module follows the same shape: build `otto_kgcov` out
of tree once, then build the module against it in place, checking
`vermagic` the same way before it ships anywhere. Only the library's `.ko`
carries `modinfo -F version`: `<otto version>+kgcov<n>`, naming the otto
that exported the sources and the interface they implement — the
consumer's own `.ko` carries no such stamp, and otto never looks for one
there, only on the kgcov dev tool's own artifact (see
[Getting the library](#getting-the-library) above for what otto checks).

## Another kernel, ISA or compiler

`otto_kgcov` parses what the consumer's compiler emitted, which fixes the
one rule that binds every build: **the library and its consumers are built
by the same compiler family, and for gcc by the same major version.**
gcc's `gcov_info` layout is chosen by `__GNUC__` when the library is
compiled, so a consumer from another gcc major is refused at `KGCOV_INIT()`,
loudly: `dmesg` names both majors, `KGCOV_INIT()` returns `-EPROTO`, and a
consumer that returns that error from its init routine — the pattern above —
does not load at all. Nothing quietly reports less coverage than the build
asked for. A consumer from the other family never gets that far: the library
exports only its own family's runtime symbols, so `insmod` fails with
`Unknown symbol __gcov_init` (a gcc consumer on a clang-built library) or
`Unknown symbol llvm_gcov_init` (the reverse).
clang's format does not change across clang versions, so for clang the rule
is just the family and kbuild's own floor of clang 11. What each installed
compiler was last measured to do, contract by contract, is the
{ref}`compatibility matrix <kgcov-matrix>`.

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
compiler). The recipe otto's own proof runs — it also builds a copy of the
demo module the same way — on an arm64 machine building x86_64 modules (any
other pair changes only the `ARCH` name and the `CROSS_COMPILE` prefix):

```bash
tar -xJf linux-6.8.tar.xz
make -C linux-6.8 ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu- defconfig modules_prepare
KDIR=$PWD/linux-6.8 ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu- \
    KMAKEFLAGS=KBUILD_MODPOST_WARN=1 \
    src/otto/kgcov/build.sh build
```

That `KMAKEFLAGS=KBUILD_MODPOST_WARN=1` is there because a prepared source
tree like this one has no `Module.symvers`: `make … defconfig
modules_prepare` never produces one (the kernel's own kbuild docs say a
full build is needed), so without it modpost fails outright on the
core-kernel exports (`memcpy`, `kfree`, `_printk`, …) it cannot resolve;
with it, those become warnings instead. A module meant to be *loaded*, as
opposed to checked, is built against a fully built tree or the target's
own headers package — either ships a real `Module.symvers` — never a bare
`modules_prepare` tree.

The release to check `vermagic` against is the tree's own
(`cat linux-6.8/include/config/kernel.release`), which is what the script
reads when `KDIR` is set and no release is given.

**Build an old kernel's modules with that kernel's own era of compiler.** A
kernel tree is coupled to the compilers of its time in both directions: its
headers dispatch on `__GNUC__` (kernels before 4.2 include a
`compiler-gcc<major>.h` that exists only for the majors they knew), its
kbuild assumes that compiler's defaults (2.6.32 and 3.13 refuse a
PIE-by-default gcc, 2.6.32's host tools a `-fno-common` one), and
distributions backport compiler fixes into their long-term kernels
precisely because a newer gcc is unsupported there. A container image of
the kernel's own distribution release is the cheap way to have that
compiler; this is how `make kgcov` builds for every provisioned kernel it
proves. Its oldest column, 2.6.32 in Ubuntu 12.04 with gcc 4.7 (the oldest
gcc the library's `gcov_info` layouts cover), runs exactly this, inside
the image:

```bash
tar -xJf linux-2.6.32.71.tar.xz
make -C linux-2.6.32.71 CC=gcc-4.7 HOSTCC=gcc-4.7 defconfig modules_prepare
KDIR=$PWD/linux-2.6.32.71 CC=gcc-4.7 KMAKEFLAGS="HOSTCC=gcc-4.7 KBUILD_MODPOST_WARN=1" \
    src/otto/kgcov/build.sh build
```

The images and the prepared trees come from
`scripts/provision_kgcov_kernels.sh`, whose header is the one home of that
mechanism.

A newer gcc *can* be shimmed onto such a tree — a `compiler-gcc<major>.h`
copied from `compiler-gcc4.h`, `KCFLAGS=-fno-PIE`, `HOSTCFLAGS=-fcommon` —
but that certifies an environment no user of that kernel has, and
`make kgcov` does not certify it.

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

What is proven, and where: `make kgcov` (which `make release` runs) rebuilds
the fixture with gcc 9, 10, 11, 12, 13 and 14 and with clang 18 and runs
the kernel-module coverage suite on the bed for each, then builds it for
every kernel of the set under [Kernel versions](#kernel-versions)
(`x86_64-cross`, `2.6.32`, `3.13`, `4.4`, `5.4`, `5.15`, `6.17`): each
provisioned kernel inside the image of its own era described above, and
the x86_64 cross build on the host with the VM's `x86_64-linux-gnu-gcc`
13. Every gcc from 4.7 on is in the format table, and every clang from 11
on shares one format.

Where a gcc puts its constructor is decided when that gcc was configured,
not by its version: Ubuntu's `x86_64-linux-gnu-gcc-9` cross package, a
compiler `make kgcov` does not build with, emits `.ctors.65435` where the
native `gcc-9` emits `.init_array.00100`. No compiler `make kgcov` builds
with emits `.ctors`, which is why the matrix carries a synthetic
`.ctors`-convention row. The library's own linker script, `kgcov.lds`, which
`consumer.mk` applies at the consumer's intermediate link, folds
`.init_array.*`, `.ctors.*` and `.ctors` into the one `.init_array` the
sentinels bracket, so both conventions work on every kernel — including
kernels before 4.0, whose module linker script orders none of these sections
itself. What stays unsupported is older than either convention: the gcc 4.4
to 4.6 counter layout (issue #492).

Getting a module to load is not the same as getting its counters read
back. The gcov that reads them is chosen per product from the data's own
stamp — the system `gcov`, `gcov-<major>` for a module built by another
gcc, `llvm-cov` for clang — unless the bed host's `toolchain.gcov` names
one; the order, and the failure naming the package when a tool is missing,
are on the {ref}`main coverage page <coverage-gcov-resolution>`. What
naming `llvm-cov` there means for otto is [clang's own page](clang.md).

A clang build needs one more setting for that report to come back clean:
clang's `.gcno` carries no compilation directory (gcc 9+ records it), so
the inlined kernel-header records are relative to the kernel tree and lcov
cannot open them from the fetch directory. Tell the bed host's lcov to
carry on past them:

```json
"toolchain": { "lcov_args": ["--ignore-errors", "source"] }
```

otto passes those arguments to the lcov capture of that host's data; the
field is in {doc}`../../../configuration/lab-config`'s toolchain table. Those records
are kernel headers, never the module's own files, and the report drops them.
otto does not ignore them for you: a blanket `--ignore-errors source` would
also swallow a genuinely missing SUT source — a stale deploy, a wrong source
root — which is the failure the capture exists to surface. If you have the
kernel tree on the reporting machine, `["--base-directory", "<kernel
tree>"]` travels the same way and resolves the records instead of dropping
them.

## Kernel versions

The library builds against every kernel from 2.6.32 on, proven through 6.17,
each compiled with a gcc of its kernel's own era, 4.7 for 2.6.32 up to 15
for 6.17. Every kernel-facing call goes through a `KGCOV_` name whose
default `kgcov_compat.h` chooses by `LINUX_VERSION_CODE`, and a
`kgcov_local.h` beside the sources defines a name first to replace its
default outright (see [Getting the library](#getting-the-library)). The
2.6.39 to 3.5 window is the one stretch no kernel in otto's set has run:
from 2.6.39 to 3.0 upstream had renamed `path_lookup()` to
`kern_path_parent()`, and from 3.1 to 3.5 `kern_path_create()` existed
without `done_path_create()`. The header carries an arm for each half,
marked UNTESTED in a comment beside the code that names the headers column
which would prove it; a module on those kernels builds, and the caveat sits
where the reader is. Proving them is issue #531. A kernel whose version
number lies about its APIs (a distribution kernel with backports) gets the
same remedy, a `kgcov_local.h` that defines the name. On the beds' 6.8
kernel the arm each name selects for 6.8 runs live, which is the newest for
every name but `KGCOV_MKDIR` (its 6.3 to 6.14 arm); every other proven arm
is build-proven (compiled against that kernel's tree, never loaded) on the
kernel columns of the {ref}`compatibility matrix <kgcov-matrix>`, and the
two untested arms are proven by nothing yet.

| name | what it does | arms by kernel version | proven by |
|---|---|---|---|
| `KGCOV_MKDIR(path)` | create the last component of an absolute path whose parents exist; `-EEXIST` counts as success | below 2.6.39 `path_lookup` + `lookup_create`; 2.6.39–3.0 the same with `kern_path_parent` (untested); 3.1–3.5 `kern_path_create` with `done_path_create` written out (untested); 3.6–4.0 `kern_path_create` with `->d_inode`; 4.1–5.11 `d_inode()`; 5.12–6.2 `mnt_user_ns`; 6.3–6.14 `mnt_idmap`; 6.15+ `vfs_mkdir` returns the dentry | `2.6.32`, `3.13`, `4.4`, `5.15`, the beds' 6.8 live, and the 6.8 tree (`x86_64-cross`), `6.17`, the untested arms: none |
| `KGCOV_FILE_WRITE(file, buf, len, ppos)` | write a kernel buffer at `*ppos`, advancing it | below 3.9 `vfs_write` under `set_fs`; 3.9–4.13 `kernel_write` with the offset by value; 4.14+ `kernel_write` by pointer | `2.6.32`, `3.13`, `5.4` |
| `KGCOV_BIG_ALLOC` / `KGCOV_BIG_FREE` | the `.gcda` image buffer | below 4.12 `vmalloc`/`vfree`; 4.12+ `kvmalloc`/`kvfree` | `4.4`, `5.4` |
| `KGCOV_FOPS_OPEN` | the debugfs files' `open` | below 3.5 a local open storing `i_private`; 3.5+ `simple_open` | `2.6.32`, `3.13` |
| `KGCOV_LLSEEK` | the debugfs files' `llseek` | below 2.6.35 `no_llseek`; 2.6.35+ `noop_llseek` | `2.6.32`, `3.13` |
| `KGCOV_WITHIN_MODULE(addr, mod)` | whether an address is the module's | below 3.17 `within_module_core` or `within_module_init`; 3.17+ `within_module` | `3.13`, `4.4` |
| `list_first_entry_or_null`, `list_last_entry`, `list_next_entry`, `__list_del_entry` | the list helpers the clang backend uses | provided below 3.10, 3.13, 3.13 and 2.6.38 | `2.6.32`, `3.13` (the "build: the clang backend's unit compiles with the column's gcc" row) |
| `KGCOV_ALLOC`, `KGCOV_ALLOC_ARRAY`, `KGCOV_STRDUP`, `KGCOV_MEMDUP`, `KGCOV_ASPRINTF`, `KGCOV_FREE`, `KGCOV_DEFINE_LOCK`, `KGCOV_LOCK`, `KGCOV_UNLOCK`, `KGCOV_DEBUGFS_DIR`, `KGCOV_DEBUGFS_FILE`, `KGCOV_DEBUGFS_REMOVE` | allocation, the lock, debugfs | one default each, unchanged since 2.6.32 | every column |

On the two columns whose kbuild compiles every object as `.tmp_<unit>.o`
before deciding whether to relink or rename it (3.13 and 4.4,
`CONFIG_MODVERSIONS=y`), the `.gcno` gcc writes keeps that name —
`.tmp_<unit>.gcno` beside the object — and a module's `.gcda` files carry
the same prefix. gcc 4.7, 4.8 and 5 put their constructor,
`_GLOBAL__sub_I_65535_0_<first public symbol, or the file name>`, in plain
`.init_array`; gcc 9 and later put `_sub_I_00100_0` in `.init_array.00100`.
No compiler `make kgcov` builds with emits `.ctors`, so that convention is
proven instead on a rewritten object — the matrix row "build: a
.ctors-convention demo is bracketed too". Collection of those files is
tracked as #530.

The contract an override must keep: `KGCOV_ALLOC` and `KGCOV_ALLOC_ARRAY`
return zeroed memory; `KGCOV_FREE` and `KGCOV_BIG_FREE` accept `NULL`;
whatever `KGCOV_ALLOC`, `KGCOV_ALLOC_ARRAY`, `KGCOV_STRDUP`, `KGCOV_MEMDUP`
and `KGCOV_ASPRINTF` return is releasable by `KGCOV_FREE`, and
`KGCOV_BIG_ALLOC`'s by `KGCOV_BIG_FREE` — a mismatched pair corrupts rather
than fails. `KGCOV_MKDIR` returns `0`, `-EEXIST` or another negative errno;
`KGCOV_FILE_WRITE` returns the bytes written or a negative errno.

A reader writes one of these for a distribution kernel whose version number
lies about its APIs, or to replace an untested arm that misbehaves:

```c
/* kgcov_local.h */
#define KGCOV_MKDIR(path) my_mkdir(path)
```

## Declaring the module and its library

The two live in different seams. The library is a `[[dev_tools]]` entry of
kind `kgcov` — repo tooling nothing measures, one entry per kernel, pointed
at the hosts running it by its `match` table — and the consumer is an
ordinary `kmod` product with `coverage = "module"`:

```{literalinclude} ../../../../tests/repo5/.otto/settings.toml
:language: toml
:start-at: "[[dev_tools]]"
:end-before: "# The container-image products"
```

otto loads the library on demand, at the consumer's own `install`, when it
is not already resident — so an `install-tools` before the run costs
nothing, and nothing depends on declaration order. Both halves of the
binding are checked at lab load: a host carrying a `coverage = "module"`
product with no matching `kgcov` entry is refused, naming the products, the
host and the kind, and so is a host matching two of them. `cleanup` unloads
the library after the products, which is the order dev tools always come
down in; a test module that drives the products itself and never runs
`cleanup` unloads it in its own teardown, the way
`tests/repo5/tests/test_kmod_demo.py` does.

`otto_kmod_demo` sets `coverage = "module"` and a `cov_dir` for `otto_kgcov`
to write under. A module built with the sentinel/macro snippet above refuses
to load without that `gcov_dir=` argument — `KGCOV_INIT()` returns `-EINVAL`
— so `coverage = "none"` on such a module, or a manual `insmod`, fails with
a message only `dmesg --sudo` shows. See
{doc}`../../../configuration/declared-products-tools` for every parameter
these kinds take — this page only walks the build the params point at.

## The `kernel` method

When the kernel itself has `CONFIG_GCOV_KERNEL`, none of the above is
needed: the kernel maintains counters for every compiled-in and loaded
object under its own `/sys/kernel/debug/gcov/` tree, keyed by the absolute
path the module was built at. `coverage = "kernel"` points a product at its
own subtree of that tree (`gcov_path`) instead of loading `otto_kgcov`, and
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
compiled with `$(KGCOV_CFLAGS)`; the two sentinel objects carry no code of
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
