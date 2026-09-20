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
- **Kernel differences**: a `kgcov_local.h` beside the sources takes effect
  with no flag — `Kbuild` force-includes it with `-include` whenever it
  exists, ahead of `kgcov_gcov.h`'s own `#ifndef` guards, so a rebuild is
  all that is needed. It replaces one kernel-facing name at a time —
  `KGCOV_ALLOC`, `KGCOV_ALLOC_ARRAY`, `KGCOV_STRDUP`, `KGCOV_MEMDUP`,
  `KGCOV_ASPRINTF`, `KGCOV_FREE`, `KGCOV_BIG_ALLOC`, `KGCOV_BIG_FREE`,
  `KGCOV_DEFINE_LOCK`, `KGCOV_LOCK`, `KGCOV_UNLOCK`, `KGCOV_DEBUGFS_DIR`,
  `KGCOV_DEBUGFS_FILE`, and `KGCOV_DEBUGFS_REMOVE` — under a contract the
  library relies on and never checks: `KGCOV_ALLOC`/`KGCOV_ALLOC_ARRAY`
  must return zeroed memory, `KGCOV_FREE`/`KGCOV_BIG_FREE` must accept
  `NULL`, and whatever an allocator returns must be releasable by its
  matching `KGCOV_FREE` — a mismatched pair corrupts rather than fails.
  `check` ignores the file and `export` never overwrites it. Proving a
  customised library against the matrix's own contracts is issue #406.
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

## Building

Build against the headers of the kernel release the module will **load
into** — a `.ko`'s `vermagic` string must match that release exactly, and a
mismatch fails `insmod` with a message that only the kernel log explains:

```bash
otto host test1 run --sudo dmesg
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
the kernel-module coverage suite on the bed for each, then cross-builds it
for x86_64 from a 6.8 source tree; every gcc from 4.7 on is in the format
table, and every clang from 11 on shares one format. `.ctors`-only
toolchains, which predate `.init_array`, are not supported.

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
down in; a suite that drives the products itself and never runs `cleanup`
unloads it in its own teardown, the way
`tests/repo5/tests/test_kmod_demo.py`'s suite does.

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
show real hits rather than zero, because the suite leaves the queue non-empty
before teardown unloads the module and the exit dump captures what the exit
routine did. Three paths stay uncovered: sending `drain` with an argument
(`demo_parse.c`'s `if (arg)` guard), lowering `limit` below the queue's
current length (`demo_policy.c`'s `if (cap < q->len)` guard), and the policy
switch's `default:` arm (`demo_policy.c`'s `switch (q->policy)`), unreachable
since the parser only ever admits the three named policies.
