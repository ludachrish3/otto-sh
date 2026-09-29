# otto_kgcov on kernels 2.6.32 to 6.17 and newer — the compatibility ladder, one consumer linker script, and per-kernel build proofs

**Date:** 2026-09-28
**Status:** approved in brainstorming (Chris, 2026-09-28: gcc 4.7 and newer now, gcc 4.4 to 4.6 later as #492; the 2.6.32 proof environment is a self-provisioned tree with the gcc 9 cross package; the ladder is macro defaults proven per arm); this document is the written form. The first of three specs under `todo/clang-kernel-coverage.md`; the clang source-based runtime and otto's `.profraw` pipeline follow it and build on the linker script and the compatibility header it introduces.
**Depends on:** `2026-09-18-kgcov-toolchains-and-cross-compiling-design.md` (the sentinel walk, the `make kgcov` lane, the cross-build proof), `2026-09-18-kgcov-compatibility-matrix-design.md` (the artifact, its rules, `rewrite_matrix_axes`), `2026-09-19-kgcov-distribution-design.md` (the shipped file list, `kgcov_local.h`, the interface number).

## 1. Problem

The kernel-modules page says a stock kernel is the target and `otto init --kgcov` hands a user the library to build against their own kernel. Measured 2026-09-28, the shipped library compiles only against kernels 6.3 to 6.14: `kgcov_mkdir()` calls `mnt_idmap()`, which exists from 6.3, and assigns `vfs_mkdir()`'s result to an `int`, which 6.15 turned into a `struct dentry *`. Against 2.6.32.71 eleven calls fail (`kernel_write`, `simple_open`, `noop_llseek`, `kern_path_create`, `done_path_create`, `d_inode`, `mnt_idmap`, `vfs_mkdir`, `kvmalloc`, `kvfree`, `within_module`); against 4.4 four fail outright and one, `kernel_write`, compiles with a *warning* while passing a pointer where that kernel takes the offset by value — a silent miscompile. The 5.10, 5.15 and 6.1 LTS kernels are in the broken range too. The `todo/clang-kernel-coverage.md` ask — list helpers and `within_module()` so a 2.6.32 tree builds the library — is two of those calls.

A second gap sits under the registration mechanism. The sentinel bracket relies on the module linker script laying out `*(SORT(.init_array.*)) *(.init_array)`, which `scripts/module-common.lds` only does from kernel 4.0 (`.ctors` from 5.15); below that a `.ko` keeps `.init_array.0`, `.init_array.00100` and `.init_array` as separate orphan sections that the loader lays out independently, and the walk between the sentinels covers whatever fell between. And the section a gcc puts its constructor in is fixed when that gcc was configured, not by its version: Ubuntu 24.04's `x86_64-linux-gnu-gcc-9` cross package emits `.ctors.65435` where its native `gcc-9` emits `.init_array.00100`. A gcc 4.7+ user with a vendor cross toolchain can hit `.ctors` today, and the library brackets nothing for them.

The compat header and the linker script this spec adds are also what the clang source-based runtime (the next spec) needs: its counters live in per-function section groups that the same intermediate-link script must gather, and its raw-profile writer reuses the file I/O the ladder makes portable.

## 2. Goals and non-goals

### Goals

- The shipped library builds, with no warnings in its own files, against every kernel from 2.6.32 to 6.17 and newer, with any gcc from 4.7 up in either constructor convention (`.init_array` or `.ctors`), and with clang where kbuild takes clang (§4.1, §4.2).
- Every version arm is proven against a real kernel on each side of its boundary; an arm no kernel in the set exercises is an `#error` naming the override, never a guess (§4.1, §4.5).
- Consumers change nothing: the linker script travels in `consumer.mk`; sentinels, macros, debugfs layout and `KGCOV_INTERFACE` are unchanged (§4.2, §4.3).
- The proof is build-only per kernel, from a pinned, provisioned kernel set, and folds into the compatibility matrix as build columns (§4.5).
- Docs: the supported range, the arm table with its proof column, the 2.6.32 recipe, the `.ctors` fact, and the override for kernels whose version number lies (§4.6).

### Non-goals

- gcc 4.4 to 4.6's `gcov_info` layout (`gcc_3_4.c`): #492, which this spec narrows to that layout alone, since the `.ctors` half is done here.
- Kernels 3.1 to 3.5: `kern_path_create()` exists there without `done_path_create()`, no kernel in the set can prove that arm, and the ladder refuses the window loudly (§4.1). A 3.4 source column can be added later with the 2.6.32 recipe if a target appears.
- Functional (load, dump, report) proof on any kernel but the beds' 6.8: no bed runs another kernel; the docs say plainly which arm is exercised live and which are build-proven.
- Anything in otto proper beyond the shipped-file list and the matrix tooling: no change to capture, fetch, merge or report.
- clang source-based coverage and `.profraw` (the next two specs).

## 3. Decisions and rules

1. **Every kernel-facing call goes through a `KGCOV_` name.** The ladder supplies each name's default by `LINUX_VERSION_CODE`; `kgcov_local.h` overrides a name and wins, because Kbuild force-includes it before anything else. A distro kernel with backported APIs, whose version number lies, is handled by the override, not by sniffing.
2. **An arm is claimed only where a kernel proved it.** The kernel set (§4.5) has a kernel on each side of every boundary in the table. A window with no kernel is an `#error` that names the `KGCOV_` name to define in `kgcov_local.h`.
3. **The library is warning-free in its own files on every column.** A `warning:` whose location is under the library's build directory fails the column. Warnings from kernel headers (located under the tree) and from the demo (a user's module) do not count.
4. **One consumer linker script, always.** `consumer.mk` adds it for every consumer. It is a no-op where the module linker script already orders `.init_array`, and the bracket where it does not or where the toolchain emits `.ctors`.
5. **A missing tree fails the lane naming the provisioning command; a pin that rots is a failure to re-pin, never a skip.** The same rule the cross build applies to its 6.8 tree.
6. **`KGCOV_INTERFACE` stays 1.** The interface is the debugfs layout, the `gcov_dir` parameter and the consumer macros, and none of them change. A consumer built from an older vendored copy still works; a re-export adds two files, which `otto cov kgcov check` reports as drift the way it reports any other.

## 4. Design

### 4.1 The ladder: `kgcov_compat.h`

A new shipped header. `kgcov_gcov.h` includes it right after its kernel includes, and every `#ifndef KGCOV_…` default that lives at the end of `kgcov_gcov.h` today moves into it, so one file holds both the version ladder and the whole override surface; `kgcov_gcov.h` keeps the vendored gcov interface alone. The header includes `<linux/version.h>` and the headers its arms need (`fs.h`, `namei.h`, `mount.h`, `uaccess.h`, `vmalloc.h`, `list.h`, `module.h`, `dcache.h`).

Each arm's helper is a `static inline` function defined **inside** its name's `#ifndef` block, so a user who overrides the name compiles none of the default's code — on a backported kernel the default arm may not even compile, and that must not matter once it is overridden.

| name | contract | arms by `LINUX_VERSION_CODE` |
|---|---|---|
| `KGCOV_MKDIR(path)` | create the last component of the absolute `path`, whose parents exist; `0` on success, `-EEXIST` when it exists (the caller in `kgcov.c` treats that as success, as today), another negative errno otherwise | **below 3.1:** `path_lookup(path, LOOKUP_PARENT, &nd)`, `lookup_create(&nd, 1)` (exported GPL but declared in no 2.6.32 header, so the arm carries a local prototype; it takes the parent's `i_mutex` and holds it even when it returns an error pointer, the way the kernel's own `mkdirat` handles it), `mnt_want_write`, `vfs_mkdir(nd.path.dentry->d_inode, d, 0755)`, `mnt_drop_write`, `dput`, `mutex_unlock(&…->i_mutex)`, `path_put`. **3.1 to 3.5:** `#error "kernels 3.1 to 3.5 are not in otto_kgcov's proven range: define KGCOV_MKDIR in kgcov_local.h"`. **3.6 to 4.0:** `kern_path_create`, `vfs_mkdir(parent.dentry->d_inode, d, 0755)`, `done_path_create`. **4.1 to 5.11:** the same with `d_inode(parent.dentry)`. **5.12 to 6.2:** `vfs_mkdir(mnt_user_ns(parent.mnt), …)`. **6.3 to 6.14:** `vfs_mkdir(mnt_idmap(parent.mnt), …)`, today's code. **6.15 and up:** `d = vfs_mkdir(…)` returns the dentry; `err = IS_ERR(d) ? PTR_ERR(d) : 0`; `done_path_create(&parent, d)` is called with `d` even when it is an error pointer — that is the kernel's own `do_mkdirat` pattern, and `vfs_mkdir` has already dropped the passed dentry on error. |
| `KGCOV_FILE_WRITE(file, buf, len, ppos)` | write `len` bytes from a kernel buffer at `*ppos`, advancing it; bytes written or a negative errno, like `kernel_write` from 4.14 | **below 3.9:** `vfs_write` under `set_fs(KERNEL_DS)` restored afterwards, the buffer cast through `__force`. **3.9 to 4.13:** `kernel_write(file, buf, len, *ppos)` with the offset by value, `*ppos` advanced by the return. **4.14 and up:** `kernel_write(file, buf, len, ppos)`. |
| `KGCOV_BIG_ALLOC(size)` / `KGCOV_BIG_FREE(p)` | as today | **below 4.12:** `vmalloc` / `vfree` (`kvfree` alone arrived in 3.15, and a pair must match). **4.12 and up:** `kvmalloc(size, GFP_KERNEL)` / `kvfree`. |
| `KGCOV_FOPS_OPEN` | a `file_operations.open` that stores the inode's `i_private` in `file->private_data` | **below 3.5:** a local open doing exactly that. **3.5 and up:** `simple_open`. |
| `KGCOV_LLSEEK` | a `file_operations.llseek` for a write-only control file | **below 2.6.37:** `no_llseek`. **2.6.37 and up:** `noop_llseek`. |
| `KGCOV_WITHIN_MODULE(addr, mod)` | true when `addr` lies in the module's core or init range | **below 3.17:** `within_module_core(addr, mod) \|\| within_module_init(addr, mod)`. **3.17 and up:** `within_module`. |
| list helpers | the four names the vendored `clang.c` uses | `list_first_entry_or_null` (3.10), `list_last_entry` and `list_next_entry` (3.13) are macros upstream, so each is provided under `#ifndef`; `__list_del_entry` is a function from 2.6.39, provided below that as a static inline over `__list_del(entry->prev, entry->next)`. |

The `d_inode()` accessor difference (4.1) stays internal to the mkdir arms; it needs no override name. `KGCOV_ALLOC`, `KGCOV_ALLOC_ARRAY`, `KGCOV_STRDUP`, `KGCOV_MEMDUP`, `KGCOV_ASPRINTF`, `KGCOV_FREE`, the lock names and the debugfs names move with the block unchanged: nothing in the set predates their defaults.

`kgcov.c` loses its version-specific code: `kgcov_mkdir()` becomes the `-EEXIST` mapping around `KGCOV_MKDIR`, `kgcov_write_file()` calls `KGCOV_FILE_WRITE`, the two `file_operations` name `KGCOV_FOPS_OPEN` and `KGCOV_LLSEEK`, and the two backends' `gcov_info_within_module()` call `KGCOV_WITHIN_MODULE`. `filp_open` and `filp_close` are unchanged since 2.6.32 and stay direct.

### 4.2 One consumer linker script: `kgcov.lds`, shipped in `consumer.mk`

```text
/* kgcov.lds — applied at the consumer's intermediate link (ld -r), before
 * the kernel's own module linker script sees the module. Gathers every
 * constructor convention into one .init_array so the two sentinels bracket
 * the gcov constructors on every kernel and toolchain: the begin sentinel
 * (.init_array.0) sorts first, then gcc's or clang's .init_array.NNNNN,
 * then a .ctors toolchain's .ctors.NNNNN and .ctors, then the end sentinel
 * in plain .init_array. */
SECTIONS {
    .init_array : { *(SORT(.init_array.*)) *(SORT(.ctors.*)) *(.ctors) *(.init_array) }
}
```

`consumer.mk` gains one line beside the include path: `ldflags-y += -T $(KGCOV)/kgcov.lds`. kbuild passes `ldflags-y` to the `ld -r` that combines a module's objects in every kernel of the set (2.6.32 already has `ld_flags = $(LDFLAGS) $(ldflags-y)`), and the line reaches every module in that Kbuild directory, which is harmless for an uninstrumented one. A consumer is already a multi-object module (the sentinels are objects), which is what makes the intermediate link exist; the docs say so.

Measured 2026-09-28 with a three-object probe: with the script, on 2.6.32 with the `.ctors`-emitting cross gcc 9 and on 6.8 with gcc 13 (`ld.bfd`) and with clang 18 (`LLVM=1`, `ld.lld`), the `.ko` carries one `.init_array` of exactly three entries, begin marker, constructor, end marker, in that order; on 4.4 the same bracket held with the kernel's own module linker script alone, which is the case the script must leave undisturbed. Without the script on 2.6.32, the constructor sat in its own `.ctors.65435` section and the two sentinels in two `.init_array` sections of one entry each. Folding PROGBITS `.ctors` input into the INIT_ARRAY output is accepted by both linkers, and a module's `.ctors` holds plain function pointers (the `-1`/`0` markers come from `crtbegin.o`, which no module links). From kernel 4.0 the module linker script's own `.init_array` line then places the already-merged section; from 5.15 its `.ctors` line never sees one. The bed matrix's `build-init-array-bracket` row is the standing proof that the script does not disturb the modern case.

`KGCOV_SENTINEL_BEGIN`, `KGCOV_SENTINEL_END`, `KGCOV_DECLARE()`, `KGCOV_INIT()` and `KGCOV_EXIT()` are unchanged. The consumer starter `otto init --kgcov` writes is unchanged too: its `Kbuild.example` already includes `consumer.mk`.

### 4.3 Distribution

- `SHIPPED_FILES` gains `kgcov_compat.h` and `kgcov.lds`; `build.sh` copies both into the build tree (the consumer references `$(KGCOV)/kgcov.lds` there); `check` compares both. An older vendored copy reads `differs` naming the two missing files, the advisory the distribution spec designed.
- `tests/repo5/third_party/otto_kgcov/` is re-exported in the same change: the guard that holds it `current` requires it.
- `README.md` names the two files and points at the docs page for the override names and the supported range (one home; §4.6).
- `KGCOV_INTERFACE` stays 1 (rule 6).

### 4.4 The demo

`tests/repo5/kmod/demo/` gains `demo_compat.h`, included by `demo_main.c` and `demo_parse.c`: `simple_open` below 3.5 (the same local open) and `kstrtol` below 2.6.39 (`strict_strtol`). Those are the demo's only calls younger than 2.6.32, and they are what a module of that age carries itself; the kernel-modules page says so beside the demo. The three instrumented units and every coverage expectation are unchanged.

### 4.5 Proof: build columns from a pinned kernel set

**Provisioning.** `scripts/provision_kgcov_kernels.sh [<id>…]` (default: every id) fetches and prepares each kernel under `${OTTO_KGCOV_KERNELS_DIR:-/home/vagrant/build/kgcov-kernels}/<id>/`, idempotently (a tree whose `include/config/kernel.release` exists is left alone), verifying every download against a sha256 recorded in the script's pin table. It refuses to start an id whose tools are missing, naming the package. `--list` prints the pin table without touching the network, one line per id with the id as the first field, which is the form the unit test of §4.7 reads.

| id | source (pinned in the script) | preparation | ISA | compiler for the column |
|---|---|---|---|---|
| `2.6.32` | kernel.org `linux-2.6.32.71.tar.xz` | `cp include/linux/compiler-gcc4.h include/linux/compiler-gcc9.h` (the tree includes `compiler-gcc<__GNUC__>.h`), then `KCFLAGS=-fno-PIE make ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu- CC=x86_64-linux-gnu-gcc-9 HOSTCFLAGS="-Wall -O2 -fomit-frame-pointer -fcommon" defconfig modules_prepare` — the tree predates PIE-by-default compilers and `-fno-common` host compilers | x86_64 | `x86_64-linux-gnu-gcc-9` (apt `gcc-9-x86-64-linux-gnu`; a `.ctors` toolchain) |
| `3.13` | ports.ubuntu.com `linux-headers-3.13.0-170-generic_3.13.0-170.220_arm64.deb` + `linux-headers-3.13.0-170_3.13.0-170.220_all.deb` | `dpkg -x` both into `<id>/root`; the tree is `root/usr/src/linux-headers-3.13.0-170-generic` (its cross-links are relative, its host tools are arm64 binaries) | arm64 | `gcc-9` |
| `4.4` | `linux-headers-4.4.0-210-generic_4.4.0-210.242_arm64.deb` + `…-210_4.4.0-210.242_all.deb` | as above | arm64 | `gcc-9` |
| `5.4` | `linux-headers-5.4.0-218-generic_5.4.0-218.238_arm64.deb` + `…-218_5.4.0-218.238_all.deb` | as above | arm64 | `gcc-9` |
| `5.15` | `linux-headers-5.15.0-198-generic_5.15.0-198.208_arm64.deb` + `…-198_5.15.0-198.208_all.deb` | as above | arm64 | `gcc-11` |
| `6.17` | `linux-headers-6.17.0-41-generic_6.17.0-41.41_arm64.deb` + `…-41_6.17.0-41.41_all.deb` | as above | arm64 | `gcc-14` |

The compilers are those of each kernel's own era where the VM has one, so no column depends on the older-gcc config overrides; where a kernel was configured for a newer gcc than the column's (6.17), the build helper's existing derivation from `.config` and the compiler's version applies unchanged. Every arm in §4.1's table has a kernel below and above its boundary: 2.6.32 below everything; 3.13 for `kernel_write` by value, `kern_path_create` with `->d_inode`, no `within_module`, no `d_inode()`, and a pre-4.0 module linker script; 4.4 for `d_inode()` with `vfs_mkdir` at three arguments and no `kvmalloc`; 5.4 for `kernel_write` by pointer and `kvmalloc`; 5.15 for `mnt_user_ns`; the beds' 6.8 for `mnt_idmap`; 6.17 for the returned dentry. Disk: about 1.3 GB in all, of which the 2.6.32 tree is already in place from the spike.

The headers packages ship `Module.symvers`, so those columns build with full symbol resolution; the two source trees (`x86_64-cross` and `2.6.32`) keep `KBUILD_MODPOST_WARN=1` with the `depends` and symvers checks that prove the link there. The 2.6.32 column also sets `KCFLAGS=-fno-PIE` for the module builds, through the same environment the tree was prepared with.

**The lane.** `test_kgcov_cross_build.py` becomes `tests/e2e/cov/test_kgcov_kernel_builds.py`, its `cross_build` fixture becomes `kernel_build`, parametrised over the kernel set: `OTTO_KGCOV_KERNELS` (from the Makefile's `KGCOV_KERNELS ?= x86_64-cross,2.6.32,3.13,4.4,5.4,5.15,6.17`) and `OTTO_KGCOV_KERNELS_DIR` (from `KGCOV_KERNELS_DIR`). `x86_64-cross` is the first member: the existing 6.8 source tree at `KGCOV_CROSS_KDIR`, `x86_64-linux-gnu-gcc`, the same id, so its column and provenance carry on. The set lives in `tests/e2e/cov/_repo5_build.py` as a table of `KernelColumn` values (id, tree path, ISA, `CROSS_COMPILE`, `CC`, extra environment, whether modpost may warn), and the provisioning script's `--list` must name the same ids (§4.7). For each column the fixture builds the library through its own `build.sh` into a temporary directory and the demo from a copy of its sources, exactly as the cross build does today, records the column's compiler version and kernel release through `note_toolchain`, and the tests assert:

| row id (title) | assertion |
|---|---|
| `build-release` (build: both modules carry the tree's release) | `modinfo -F vermagic` of both `.ko` starts with the tree's `kernel.release` |
| `build-target-isa` (build: both modules are objects for the column's ISA) | `readelf -h` names the column's machine, `X86-64` or `AArch64` |
| `build-compiler` (build: the column's compiler built them) | the `.comment` of both `.ko` names it |
| `build-instrumented-bracketed` (build: the demo is instrumented and bracketed) | a `.gcno` beside each instrumented unit, and `init_array_symbols(demo.ko)` is the begin marker, one gcov constructor per unit, the end marker — on `2.6.32` this is the `.ctors` fold's proof, on `3.13` the pre-4.0 proof |
| `build-fixture-untouched` (build: the in-place fixture build was not touched) | as today |
| `build-linked-against-library` (build: the demo linked against the library) | `depends` names `otto_kgcov`, the library's `Module.symvers` lists its two exports, and on a `KBUILD_MODPOST_WARN=1` column no `undefined!` line names a library symbol |
| `build-library-warning-free` (build: the library compiled without a warning of its own) — new | no `warning:` line in the library build's output whose path lies under the library's build directory |
| `build-clang-backend-compiles` (build: the clang backend's unit compiles with the column's gcc) — new | `make -C <tree> M=<library build dir> kgcov_clang.o` with the column's environment exits 0 and is warning-free under the same rule: the vendored list helpers expand against that kernel, which is the todo's ask and the only proof clang's backend can have on a kernel clang cannot build for |

The first six are today's rows under new ids: `cross-release` → `build-release`, `cross-x86_64-objects` → `build-target-isa`, `cross-compiler-built` → `build-compiler`, `cross-instrumented-bracketed` → `build-instrumented-bracketed`, `cross-fixture-untouched` → `build-fixture-untouched`, `cross-linked-against-library` → `build-linked-against-library`. `rewrite_matrix_axes()` gains a rename map applied once in this change, carrying the `x86_64-cross` column's verdicts and provenance to the new ids so no verdict is lost even transiently; the new columns' and the new rows' cells start `untested`, and the first `make kgcov` fills them. The rename and the rewritten artifact land in one commit, so `check_matrix_downgrades.py` at the next release compares like with like. `bed_profile_ids` gains a build sibling that reads `KGCOV_KERNELS`'s default from the Makefile rather than copying it, the way the bed columns read `KGCOV_TOOLCHAINS`.

The matrix page's build grid becomes seven columns; its provenance table already carries compiler version, kernel release and date per column, which is what distinguishes them; the legend's "a build-only column proves what a build can prove" stands. `make kgcov` grows by about a minute per column. A column whose tree is absent fails naming `scripts/provision_kgcov_kernels.sh <id>` (rule 5); `docs/contributing.md` lists provisioning once as a dev-VM prerequisite of the lane, beside the 6.8 tree.

### 4.6 Documentation

- `docs/cli/cov/instrumenting/kernel-modules.md`: a new section "Kernel versions" after "Another kernel, ISA or compiler": the supported range (2.6.32 to 6.17 and newer, kernels 3.1 to 3.5 excluded and why), §4.1's table with a column naming the matrix column that proves each arm, the sentence that only the 6.3 to 6.14 arm is exercised live on the beds and every other is build-proven, the `#error` and `kgcov_local.h` remedy for the 3.1 to 3.5 window and for a kernel whose version lies, and the override-name list, moved here from "Getting the library" with the new names. In "Another kernel, ISA or compiler": the 2.6.32 recipe (the header shim, `KCFLAGS=-fno-PIE`, `HOSTCFLAGS` with `-fcommon`, `KBUILD_MODPOST_WARN=1`) as a second recipe beside the 6.8 cross one, and the `.ctors` paragraph in place of "`.ctors`-only toolchains … are not supported": where a gcc's constructor lands is decided when that gcc was configured, the library's linker script handles both conventions, and what stays unsupported is the gcc 4.4 to 4.6 counter layout (#492). Beside the demo's three steps, one sentence on `demo_compat.h`.
- `src/otto/kgcov/README.md`: the two new files, and a link to the page for the range and the override names.
- `docs/contributing.md`: the `make kgcov` row names the kernel-set prerequisite and the provisioning script.
- The matrix page is generated; nothing to write by hand.

One home per topic: the arm table lives on the kernel-modules page only; the README and the header's own comments link to it.

### 4.7 Tests

- **Unit**: `test_kgcov_export.py` covers the two new shipped files through export and check (a copy missing them reads `differs` naming them); the repo5-copy guard stays green after the re-export; `test_kgcov_matrix.py` asserts the build profiles equal the Makefile's `KGCOV_KERNELS` default both ways, the renamed and the two new rows equal the discovered contracts both ways, and every renamed row carried its cell; `test_collate_kgcov_matrix.py` places a record for a new build column; a new test runs `scripts/provision_kgcov_kernels.sh --list` and asserts its ids equal the Makefile default minus `x86_64-cross`, so the script and the lane cannot drift apart.
- **Build-only (`kgcov` marker, `make kgcov`)**: `test_kgcov_kernel_builds.py` over the set, §4.5's eight rows per column.
- **Live**: the routine kmod e2e and the bed matrix, unchanged in shape, now with the linker script in every consumer build; one `make kgcov` on the dev VM after provisioning fills the build grid, and `schemas/kgcov_matrix.json` is committed with the branch as the first measured state.

## 5. Files

- New: `src/otto/kgcov/kgcov_compat.h`, `src/otto/kgcov/kgcov.lds`, `scripts/provision_kgcov_kernels.sh`, `tests/repo5/kmod/demo/demo_compat.h`, `tests/e2e/cov/test_kgcov_kernel_builds.py` (from `test_kgcov_cross_build.py`), `tests/unit/test_provision_kgcov_kernels.py`.
- Modified: `src/otto/kgcov/kgcov.c`, `kgcov_gcov.h`, `kgcov_gcc.c`, `kgcov_clang.c`, `consumer.mk`, `build.sh`, `README.md`, `src/otto/kgcov/library.py` (`SHIPPED_FILES`), `tests/repo5/third_party/otto_kgcov/` (re-export), `tests/repo5/kmod/demo/demo_main.c`, `demo_parse.c`, `tests/e2e/cov/_repo5_build.py` (`KernelColumn`, the set), `tests/_fixtures/kgcov_matrix.py` (rows, build profiles, the rename map in `rewrite_matrix_axes`), `schemas/kgcov_matrix.json` (rewritten axes), `scripts/render_kgcov_matrix.py` if the build grid needs it, `Makefile` (`KGCOV_KERNELS`, `KGCOV_KERNELS_DIR`, the lane's environment), `tests/unit/test_kgcov_export.py`, `test_kgcov_matrix.py`, `test_collate_kgcov_matrix.py`, the three docs pages of §4.6.
- Deleted: `tests/e2e/cov/test_kgcov_cross_build.py` (renamed).

## 6. Compatibility

Nothing a consumer or otto sees changes shape: the same macros, the same debugfs files, the same `gcov_dir` parameter, interface 1, the same `[[dev_tools]]` and `[[products]]` entries. A vendored copy from an earlier otto keeps building and loading against the same kernels it did; re-exporting it adds two files and widens the range. The matrix artifact's `x86_64-cross` column keeps its id and its verdicts under renamed rows.

## 7. Follow-ups

- #492 is re-scoped to the gcc 4.4 to 4.6 `gcov_info` layout: its `.ctors` half is delivered here.
- A 3.4 source column with the 2.6.32 recipe, if a 3.1 to 3.5 target appears; it turns the `#error` into a proven arm.
- A functional proof of one old kernel under QEMU (boot, insmod both modules, dump, read the `.gcda` back), when a target justifies it.
- The clang source-based runtime spec: it adds its own gathering to `kgcov.lds` and its writer to the ladder's file I/O.

## 8. Gates

Per task: the named selections and `ruff check` on the changed files; `make docs-html` where docs change; the tier-marker invariant tests whenever a selector or marker changes. Before the squash: the unit tier, `make docs`, `make gate-fresh`, the kmod e2e (default toolchain) on the bed, and `make kgcov` once with the kernel set provisioned — matrix, bed columns and all seven build columns green, the folded artifact reviewed and committed. Bed selections run alone and sequentially; kernel modules are never loaded on the dev VM.
