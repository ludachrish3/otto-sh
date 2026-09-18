# otto_kgcov: any gcc, clang, and other kernels or ISAs — design

> Captured 2026-09-18 from a brainstorm with Chris, after the product-kinds
> branch (spec `2026-09-17-product-kinds-shell-kmod-docker-image-design.md`)
> landed on main. Approved section by section in that conversation.

## 1. Context and motivation

`otto_kgcov` (`docs/examples/kgcov/`) is the companion module that lets a
kernel module report gcov coverage on a stock kernel: the instrumented module
links against it, and the library dumps `.gcda` files under the product's
`cov_dir`. It is a test-enabling example, not part of otto: otto owns no
product build, the `kmod` kind only transfers a `.ko`. Two limits of the
first version close the loop poorly for real users:

- It is gcc-only, and gcc 11 to 13 at that. Registration relies on
  `-fprofile-info-section` (gcc 11+), and the vendored gcov format table
  stops at gcc 13 (`#error` above). Some projects compile their modules with
  clang, whose gcov runtime is a different dialect.
- Its build script assumes the build machine's own kernel headers under
  `/lib/modules/<release>/build`. A user whose target kernel or ISA differs
  from the build machine has no documented path.

Two facts fix the shape of the answer. Stock kernels are the target — Chris:
assume `CONFIG_CONSTRUCTORS` is unset — so a module's constructors, which is
where both gcc (`__gcov_init`) and clang (`llvm_gcov_init`) register their
counters, never run at load. And the net must be cast wide: every gcc the
kernel's own gcov format supports, old and new, and clang 11 and newer.

## 2. Goals and non-goals

### Goals

- One registration mechanism for every compiler: the library runs the
  consumer's own constructors, bracketed by the two sentinel objects the
  consumer already links first and last (§3).
- Two vendored backends behind one interface: the kernel's `gcc_4_7.c` with
  its full version table (gcc 4.7 through the newest arm in mainline), and
  the kernel's `clang.c` (§4).
- `consumer.mk` is plain `-fprofile-arcs -ftest-coverage` for both compilers;
  the consumer contract, the debugfs files, otto's `kmod` kind and the
  coverage hooks are unchanged (§5).
- The build scripts accept a kernel tree anywhere and pass every kbuild knob
  through unchanged (§6).
- Proof: a live toolchain matrix on the beds and a build-only cross build
  for another ISA from a kernel source tree (§8).
- Docs: how to build the library for another kernel, ISA or compiler, and
  the one rule that binds users (§9).

### Non-goals

- Anything in otto proper. Neither `otto.host.kmod_kind` nor the coverage
  hooks change; otto never builds a module.
- `.ctors`-only targets (toolchains that predate `.init_array`); documented
  as unsupported.
- Kernels whose gcov formats predate the vendored backends (the kernel
  itself requires gcc 5.1; the format floor is gcc 4.7).
- A wrapper verb (`otto kgcov build …`); rejected: a project's build system
  already knows its toolchain and tree, and a wrapper would only chase
  kbuild.

## 3. Registration: the library runs the consumer's constructors

On a stock kernel the module loader links a module's `.init_array` (the
module linker script keeps `.init_array` and `.ctors`) but runs it only
under `CONFIG_CONSTRUCTORS`. gcc places its gcov constructor in
`.init_array.00100`, clang in `.init_array.0`; the script emits
`SORT(.init_array.*)` and then plain `.init_array`.

The consumer keeps two sentinel objects, first and last in its `Kbuild`
object list as today, and their source files keep their one-line bodies
(`KGCOV_SENTINEL_BEGIN;` and `KGCOV_SENTINEL_END;`); the macros in
`kgcov.h` now expand to one function pointer each, to a static no-op of
its own (the walk never calls the sentinels themselves):

```c
/* KGCOV_SENTINEL_BEGIN — first object in the consumer's link */
static void __kgcov_begin_marker(void) {}
const kgcov_ctor_fn __kgcov_ctors_begin
	__attribute__((section(".init_array.0"), used, aligned(8))) = __kgcov_begin_marker;

/* KGCOV_SENTINEL_END — last object in the consumer's link */
static void __kgcov_end_marker(void) {}
const kgcov_ctor_fn __kgcov_ctors_end
	__attribute__((section(".init_array"), used, aligned(8))) = __kgcov_end_marker;
```

`.init_array.0` sorts before every other entry, and within it the begin
sentinel precedes clang's entries by link order; plain `.init_array` comes
after every sorted entry. Proven on 2026-09-18 with a throwaway module built
both ways against the bed kernel: the final link places the begin sentinel,
the compiler's constructor and the end sentinel at offsets 0, 8 and 16 for
gcc 13 and clang 18 alike.

`KGCOV_INIT()` (unchanged in spelling) expands to
`kgcov_register(THIS_MODULE, &__kgcov_ctors_begin + 1, &__kgcov_ctors_end,
gcov_dir)`. The library walks the pointers strictly between the two
sentinels and calls each: gcc's constructor calls `__gcov_init(info)`,
clang's calls `llvm_gcov_init(writeout, flush)`, both exported by the
library, and each backend hands the resulting `gcov_info` to the
registration in progress, so every translation unit lands in the same
per-module accumulator as today. A constructor that runs while no
registration is in progress (the kernel's own pass on a
`CONFIG_CONSTRUCTORS` kernel) registers nothing.
`-fprofile-info-section` and the `.gcov_info` sentinels are removed.

A coverage build's only constructors are gcov's. A consumer's own
`__attribute__((constructor))` functions run too, which is what a
`CONFIG_CONSTRUCTORS` kernel would have done; the docs say so.

**Idempotence.** On a kernel that does have `CONFIG_CONSTRUCTORS`, the
loader has already run the constructors before `KGCOV_INIT` walks them
again. The first pass registers nothing (no registration was in
progress), the walk registers each unit once, and a gcc `gcov_info` seen
twice within one walk is skipped by pointer. Registration is never
doubled.

## 4. Two vendored backends behind one interface

The library keeps one internal interface (`gcov_info` construction,
`gcov_info_add`, `gcov_info_reset`, serialisation to a `.gcda` buffer,
`filename`/`version` accessors) and vendors the kernel's two
implementations of it:

- `kgcov_gcc.c`: `kernel/gcov/gcc_4_7.c` (v6.8) plus `gcc_base.c`'s
  exported stubs, as today, with the version table completed from mainline:
  gcc 4.7 to 5.0 (9 counters), 5.1 to 6 (10), 7 to 9 (9), 10 to 13 (8),
  14 (9), 15 (mainline's arm). The gcc 14 `#error` goes; an unknown newer
  gcc fails the build with a message naming the file to extend.
- `kgcov_clang.c`: `kernel/gcov/clang.c` (v6.8). It builds a `gcov_info`
  from the `llvm_gcda_*` callbacks that `llvm_gcov_init`'s writeout runs
  at registration, keeps pointers to the live counters, and serialises with
  its own writer. The kernel's backend does not branch on the clang version;
  clang 11 and newer, which is what kbuild accepts, are covered.

The library's `Kbuild` selects the backend by the compiler building it
(`__clang__`), exports gcc's `__gcov_*` symbols and clang's `llvm_gcov_init`
and `llvm_gcda_*` symbols (all under the existing GPL exports).

The one rule that binds users is now checked instead of merely stated: the
library must be built by the same compiler family as the consumer, and for
gcc by the same major version, because `gcc_4_7.c` lays out `gcov_info` by
`__GNUC__` at compile time. For clang the format word is fixed (`408*`,
the gcc 4.8 layout, from clang 11 on, probed on 2026-09-18 with clang 18)
and the callback ABI has been stable since clang 11, so the family and the
kbuild floor are the whole rule.

At registration the gcc backend decodes the major from each translation
unit's `info->version` word (probed on 2026-09-18: gcc 9 writes `A95*`, gcc
10 to 14 write `B05*`, `B15*`, `B24*`, `B33*`, `B42*`; so `'4'` means gcc
4, `'A'` + digit means 5 to 9, `'B'` + digit means 10 to 19) and compares it
with the `__GNUC__` the library was built with. A mismatch is refused with
both versions in the kernel log rather than parsed into garbage: the
module still loads, the unit is not registered, and the product reports
no coverage for it. An encoding the decoder does not know (a first byte
after `'B'`) skips the major check with one `pr_info`, so a future gcc is
not refused by a decoder that predates it. A family mismatch needs no
code: each build of the library exports only its own family's entry
points, so a gcc consumer against a clang-built library (or the reverse)
fails to load with `Unknown symbol __gcov_init` (or `llvm_gcov_init`) in
the kernel log; the docs name that message.

## 5. Consumer contract

- `consumer.mk`: `KGCOV_CFLAGS := -fprofile-arcs -ftest-coverage` for both
  compilers; the include path as today.
- The consumer's `Kbuild` lists `kgcov_begin.o` first and `kgcov_end.o`
  last, as today; the two files are copied from the library as today.
- `KGCOV_DECLARE()` / `KGCOV_INIT()` / `KGCOV_EXIT()`, the debugfs
  `/sys/kernel/debug/otto_kgcov/<module>/{dump,reset}` files, the
  `gcov_dir=` module parameter, dump-adds-then-zeroes, exit dump, and the
  `.gcda` layout are unchanged. otto's `kmod` kind and the coverage hooks
  do not change; the existing unit tests and the kmod e2e stay valid.
- The demo `otto_kmod_demo` gains nothing but the new sentinel files and
  compiles clean under clang's warnings.

## 6. Build knobs and cross-compiling

`docs/examples/kgcov/build.sh <build-dir> [<release>]`:

- `KDIR` from the environment names the kernel tree (source or headers,
  anywhere); default stays `/lib/modules/<release>/build`.
- `ARCH`, `CROSS_COMPILE`, `LLVM`, `CC` and `KMAKEFLAGS` (extra `make`
  arguments, verbatim) pass through to kbuild unchanged; nothing is
  inferred. The library's and the demo's `Makefile` do the same.
- The vermagic check stays: `modinfo` reads the ELF, so it works for any
  ISA; `<release>` is what it compares against (for a source tree,
  `make kernelrelease` gives it).
- The fixture's kernel half moves into its own script,
  `tests/repo5/kmod/build.sh [<release>]`, with the same passthrough, so
  the e2es and the matrix build it with any installed toolchain without
  also building the container image; `tests/repo5/build.sh` becomes the
  human entry point that runs that script and then `docker/build.sh`.

Cross-compiling therefore needs exactly what the user's own module needs: a
prepared target tree (`make ARCH=… CROSS_COMPILE=… defconfig
modules_prepare` on a source tree, or a headers package for the build
machine's own architecture) and the target toolchain. A distro headers
package ships host tools for its own architecture and cannot be used from a
different build machine; the docs say so and show the source-tree recipe.

**clang against a gcc-built kernel.** A stock kernel's config carries
gcc-only flags that kbuild applies to any external module. Proven recipe
(bed kernel, clang 18): override four values on the command line —
`CONFIG_CC_IS_GCC=` `CONFIG_GCC_VERSION=0`
`CONFIG_CC_IMPLICIT_FALLTHROUGH=-Wimplicit-fallthrough`
`CONFIG_UBSAN_BOUNDS_STRICT=` — with `LLVM=1` (plus `CONFIG_CC_IS_CLANG=y
CONFIG_CLANG_VERSION=<n>` for clang-specific flags). That is a kbuild fact,
documented as the `KMAKEFLAGS` example, not a library concern; a clang-built
kernel needs none of it.

## 7. Files

- `docs/examples/kgcov/`: `kgcov.c` (walk, registration context, version
  check), `kgcov.h` (sentinel macros, `KGCOV_INIT`), `kgcov_gcov.h`
  (the vendored kernel `gcov.h` interface, renamed from `kgcov_gcc.h` now
  that two backends implement it, plus the two backend hooks the library
  adds), `kgcov_gcc.c` (full table), `kgcov_gcc_abi.c` (renamed from
  `kgcov_stubs.c`: the real `__gcov_init`, the empty merge functions, the
  gcc side of the two hooks), `kgcov_clang.c` (new: the vendored
  `clang.c`, its `llvm_*` entry points, the clang side of the hooks),
  `Kbuild` (backend by the compiler building it), `consumer.mk`,
  `build.sh`, `Makefile`, `README.md`.
- `tests/repo5/kmod/demo/`: `kgcov_begin.c`, `kgcov_end.c`, `Kbuild` and
  the sources unchanged; `Makefile` gains the passthrough.
- `tests/repo5/kmod/build.sh` (new) and `tests/repo5/build.sh` (§6).
- `tests/e2e/cov/test_kgcov_toolchains_e2e.py` (`integration` +
  `kgcov`), `_repo5_build.py` (toolchain-aware ensure function, the
  toolchain stamp, the clang overrides, the `.init_array` reader) and
  `_kmod_assertions.py` (the assertion bodies the routine kmod e2e and
  the matrix share): the toolchain matrix (§8).
- `tests/e2e/cov/test_kgcov_cross_build.py` (`hostless` + `kgcov`): the
  cross build-only proof (§8); it touches no bed. Both modules satisfy the
  e2e resource-marker rule with exactly one primary marker each.
- `pyproject.toml` (marker), `Makefile` (`kgcov` lane, catch-all
  exclusions, release stage), `noxfile.py` (catch-all exclusions),
  `tests/unit/test_tier_marker_invariants.py` (the `kgcov` rows),
  `docs/contributing.md` (lane row).
- Docs (§9); spec amendment of §6 in the product-kinds spec.

## 8. Proof

**Where it runs: rarely by hand, always before a release.** Chris's
guiding principle (2026-09-18): the toolchain proofs may run rarely, but
a release must bless them. The existing kmod and docker e2es already meet
it: they carry `integration` under `tests/e2e/cov/`, so `make release`
runs them through `make nox` (the full suite on 3.10 and 3.14), and `make
coverage` runs them too. The two new proofs below follow the
`conformance` tier's pattern:

- a `kgcov` marker, declared in `pyproject.toml`, carried by both proofs,
  and excluded from every catch-all selector in the `Makefile` and
  `noxfile.py` (`not kgcov` rides beside `not busybox and not
  conformance`), so neither CI's hostless lane nor `make coverage` nor
  `make nox` ever collects them; the tier-marker invariant tests
  (`tests/unit/test_tier_marker_invariants.py`) gain the same three
  rows the conformance tier has: catch-alls exclude it, one Makefile
  lane positively selects it, the release invokes that lane;
- a `make kgcov` lane (`pytest -m kgcov -n0 --no-cov`, JUnit under
  `reports/junit/kgcov/`) that runs the matrix and the cross build, dev
  VM only, beds required; its knobs are the make variables
  `KGCOV_TOOLCHAINS` (default `gcc-9,gcc-10,gcc-11,gcc-12,gcc-13,gcc-14,clang`)
  and `KGCOV_CROSS_KDIR` (default `/home/vagrant/build/linux-6.8`), which
  the lane hands to the tests as the environment variables
  `OTTO_KGCOV_TOOLCHAINS` and `OTTO_KGCOV_CROSS_KDIR` (the shape
  `OTTO_CONFORMANCE_CELLS` already uses; a pytest option would need a
  root-level conftest hook this tree does not have);
- a `make release` stage that runs `make kgcov` right after
  `release-matrix` (both need the bed; make stages are sequential, so the
  bed is never shared). A missing compiler or tree fails the lane with an
  error naming it and the install or preparation command, so it stops the
  release instead of skipping; that is the blessing.
- `docs/contributing.md`'s regression-category table gains the lane's row.

**Live toolchain matrix (beds test1/test2).** A `kgcov`-marked module,
`tests/e2e/cov/test_kgcov_toolchains_e2e.py`, parametrised over the
compilers in `OTTO_KGCOV_TOOLCHAINS` (unset or empty means the system
default compiler alone, so the module never collects an empty, skipped
parameter set). For each compiler it rebuilds the library and the demo
for the running kernel (`clang` implies `LLVM=1` plus the §6 overrides
whenever the kernel's config says `CONFIG_CC_IS_GCC=y`), checks the
demo's `.init_array` relocations name the begin marker first, one gcov
constructor per instrumented unit, and the end marker last, then runs
the compiler-independent assertions of the routine kmod e2e (fetch tree,
one `.gcda` per unit, the library's scan verdict, exact line hits,
taken-and-untaken branches, exit drain, no double count from the
mid-suite dump) through the shared helpers, unloads, then moves to the
next. The routine e2e keeps, in its own module, the two pins that encode
gcc's branch folding (the four-arc `-ERANGE` compare and its per-arc
count). The kernel half builds into `tests/repo5/build/` and in place as
today; a stamp file `tests/repo5/build/toolchain` names the compiler that
built it, and a stamp naming another compiler makes the artifacts stale,
so the routine e2e and the matrix rebuild for each other correctly. The
routine kmod e2e keeps the system gcc. The gcc 15 arm is compile-checked
only through the table.

**Cross build-only (dev VM).** A `kgcov`-marked module,
`tests/e2e/cov/test_kgcov_cross_build.py`, builds the library and the
demo (a copy of the demo sources under the test's temporary directory,
so the in-place fixture build is untouched) against a kernel 6.8 source
tree (about 2 GB, downloaded once from kernel.org to
`OTTO_KGCOV_CROSS_KDIR`) prepared with `make ARCH=x86_64
CROSS_COMPILE=x86_64-linux-gnu- defconfig modules_prepare`, with the same
environment. It asserts `modinfo -F vermagic` names the tree's release,
`readelf -h` says `X86-64` for both `.ko`, the cross compiler signed the
`.comment` section, the sentinel bracket holds, and the demo's `.gcno`
files exist. Never loaded anywhere; it touches no bed. The dev VM is x86_64, so
this proves the knob passthrough and the source-tree recipe, not a
foreign ISA; the docs show the arm64 form (`ARCH=arm64
CROSS_COMPILE=aarch64-linux-gnu-`) of the same recipe.

**Unit.** The existing 36 `kmod` kind tests stay; nothing in otto changes.
The library has no unit tests (kernel code); its proof is the matrix.

## 9. Docs

- `docs/guide/cli/cov/instrumenting/kernel-modules.md`: a section
  "Another kernel, ISA or compiler" — the same-compiler rule, the knobs,
  the source-tree cross recipe, the clang-on-a-gcc-kernel overrides, and
  the matrix as the evidence of what is supported (gcc 4.7+ in format,
  9 to 14 proven live; clang 11+, 18 proven live). One home; the README
  under `docs/examples/kgcov/` links it.
- `docs/superpowers/specs/2026-09-17-product-kinds-…-design.md` §6:
  amended to the constructor-walk mechanism and the two backends.

## 10. Gates

Per task: the named selections, `make lint`, `make typecheck-python`,
`make docs-html` where docs change, and the tier-marker invariant tests
whenever a selector or marker changes. Before the squash: the unit tier,
`make docs`, gate-fresh, the kmod e2e (default toolchain), the docker e2e
(shares the fixture repo), and `make kgcov` once (matrix plus cross
build). Bed selections run alone and sequentially; kernel modules are
never loaded on the dev VM.
