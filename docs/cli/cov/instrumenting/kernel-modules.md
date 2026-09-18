# Kernel-module products

A `kind = "kmod"` product is a Linux kernel module, loaded and unloaded with
`insmod`/`rmmod` instead of staged and run. Its coverage story is different
from every other kind on this page's siblings, because a kernel module has
no libc, no process exit, and — on most distribution kernels — no gcov
runtime to hand counters to in the first place.

## What a module lacks

A user-space product needs nothing beyond `--coverage`: gcc's own
constructor registers each translation unit's counters before `main` runs,
and `__gcov_exit` flushes them when the process exits. Neither half of that
exists for an out-of-tree module on a stock kernel. `CONFIG_CONSTRUCTORS`
is off, so the kernel never runs a module's constructors and plain
`-fprofile-arcs` registers nothing with anyone. `CONFIG_GCOV_KERNEL` is
off too, so there is no in-kernel gcov and no
`/sys/kernel/debug/gcov/` tree to fall back on.

The way through is `-fprofile-info-section`: instead of a constructor call,
it records each translation unit's `gcov_info` pointer in a `.gcov_info`
linker section. Two tiny sentinel objects, linked first and last in the
module's object list, bound that section so a runtime can walk it end to
end. `otto_kgcov` is that runtime.

## The library

`otto_kgcov` (`docs/examples/kgcov/`) is a small GPL kernel module every
instrumented consumer links against — a "library" in kernel space is just
another loadable module that exports symbols. It exports empty stand-ins
for the same `__gcov_*` symbols the in-kernel gcov exports, so any
`--coverage`-compiled object links against *something*, plus the two calls
a consumer actually drives:

```{literalinclude} ../../../examples/kgcov/kgcov.h
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

## Instrumenting a module

The worked example throughout this page is `tests/repo5/kmod/demo/`, a
small bounded queue driven from a debugfs control file, living inside the
repo that declares it as a product. A repo that reports coverage for a
module must own that module's sources and build them in place: coverage
capture anchors every measured file to a committed git blob under the SUT
repo. The library's own
`docs/examples/kgcov/README.md` documents the same three steps that follow,
in more general terms, for any consumer.

A consumer becomes coverage-instrumented in three steps:

**1. Sentinels.** Link two tiny objects — one built from a file containing
only `KGCOV_SENTINEL_BEGIN;`, one from a file containing only
`KGCOV_SENTINEL_END;` — first and last in the object list, so `ld -r`'s
input-order guarantee keeps them, and only them, outside the
`.gcov_info` range every other object contributes to. The same Kbuild also
applies the flags step below to each instrumented object:

```{literalinclude} ../../../../tests/repo5/kmod/demo/Kbuild
:language: make
```

**2. Flags.** Compile every instrumented object with `$(KGCOV_CFLAGS)`,
defined by the library's own Kbuild fragment:

```{literalinclude} ../../../examples/kgcov/consumer.mk
:language: make
```

**3. Macros.** Call `KGCOV_DECLARE()` at file scope, `KGCOV_INIT()` first in
the module's init routine, and `KGCOV_EXIT()` last in its exit routine:

```{literalinclude} ../../../examples/kgcov/kgcov.h
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

`docs/examples/kgcov/build.sh <build-dir> [<release>]` takes that release
as an argument, defaulting to the build machine's own running kernel when
none is given, and checks the resulting `.ko`'s `vermagic` against it. The
library builds **out of tree**: it is never itself a measured product, so
`build.sh` copies its sources into a scratch directory and builds them
there against `/lib/modules/<release>/build`. The consumer builds **in place**, next to
its own committed sources, for the reason given above. `tests/repo5/build.sh`
does both, library first, and checks the resulting `vermagic` before calling
either build a success:

```{literalinclude} ../../../../tests/repo5/build.sh
:language: bash
```

A product repo's own module follows the same shape: build `otto_kgcov` out
of tree once, then build the module against it in place, checking
`vermagic` the same way before it ships anywhere.

## Declaring the products

The library and the consumer are each their own `[[products]]` entry, the
library declared first — products install in declaration order, so the
consumer's `insmod` always finds the library already resident. That
ordering is otto's own guarantee; unloading is not reversed for you — a
repo whose products have a load-order dependency, like this one, unloads in
reverse in its own teardown, the way `tests/repo5/tests/test_kmod_demo.py`'s
suite does:

```{literalinclude} ../../../../tests/repo5/.otto/settings.toml
:language: toml
:start-at: "[[products]]"
:end-before: "# The container-image products"
```

`otto_kgcov` declares `instrumented = false` — overriding the artifact
scan, for the reason its own entry comment gives. `otto_kmod_demo` sets
`coverage = "module"` and a `cov_dir` for `otto_kgcov` to write under. A
module built with the sentinel/macro snippet above refuses to load without
that `gcov_dir=` argument — `KGCOV_INIT()` returns `-EINVAL` — so
`coverage = "none"` on such a module, or a manual `insmod`, fails with a
message only `dmesg --sudo` shows. See
{doc}`../../../configuration/declared-products-tools` for every parameter
this kind takes — this page only walks the build the params point at.

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
