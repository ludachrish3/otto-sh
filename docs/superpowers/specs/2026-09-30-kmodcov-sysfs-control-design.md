# otto_kmodcov's control files move from debugfs to sysfs — dump and reset on any kernel that loads modules

**Date:** 2026-09-30. **Status:** approved in conversation, awaiting review of this text.
**Follows:** `2026-09-30-kmodcov-rename-design.md` (landed as origin/main c61c7baa).
**Precedes:** spec 2 of `todo/clang-kernel-coverage.md` (the clang source-based backend), which adds a second output format behind the control files this spec defines.

## 1. Problem

The library's data path never touched debugfs: counters are written as files under the `cov_dir` module parameter by `kernel_write`. Its control path did. `kmodcov_register` created `/sys/kernel/debug/otto_kmodcov/<module>/dump` and `.../reset`, so an on-demand dump — what otto's `prepare_coverage` issues while the module is loaded — needed a kernel built with `CONFIG_DEBUG_FS`, with debugfs mounted, and (since 5.10) not booted with `debugfs=off`. Production and embedded product kernels turn debugfs off routinely; it is a debugging option. A module whose coverage could only be read at `rmmod` on such a kernel is a weaker promise than the library makes everywhere else.

Chris asked (2026-09-30) that on-demand dump and reset work without debugfs, and chose to replace the debugfs path rather than add a second one.

## 2. Goals and non-goals

### Goals

- On every kernel the ladder supports (2.6.32 through 6.17), a loaded consumer has two write-only control files, `dump` and `reset`, with exactly the semantics the debugfs files had, and the library references no debugfs symbol at all.
- The new path adds **no kernel-version arm**: the sysfs primitives it uses have one signature across the whole ladder, and five override names with two arms leave `kmodcov_compat.h`.
- otto drives the new path from the one place it drove the old one, with the same hooks, the same dry-run behaviour and the same error shapes.

### Non-goals

- No second control path. debugfs is gone from the library; a copy vendored at interface 1 is refused by the existing interface check with the existing re-export remedy.
- No change to the data path: `cov_dir`, the file layout under it, `KMODCOV_MKDIR`, `KMODCOV_FILE_WRITE`, the accumulator and the exit dump are untouched.
- No change to the `kernel` coverage method of the `kmod` kind. That method reads the kernel's own gcov tree under `/sys/kernel/debug/gcov/` and needs debugfs by its nature; its docs paragraph stays.
- No change to the demo consumer's own debugfs control file (`tests/repo5/kmod/demo/demo_main.c`). The demo is the product under test; its interface is its own business. The bed hosts keep debugfs for it.
- No bed without debugfs. None exists in the lab; the proof that debugfs is no longer needed is structural (§4.6), not a run on such a host.
- No read-side files. The one value a reader might want, the module's `cov_dir`, is already readable at `/sys/module/<module>/parameters/cov_dir` (mode 0444), which the loader creates from `KMODCOV_DECLARE()`'s `module_param`.

## 3. Decisions and rules

1. **sysfs, as a child directory of the consumer's own module directory.** The files are `/sys/module/<module>/kmodcov/dump` and `/sys/module/<module>/kmodcov/reset`, mode 0200, owned by root. `<module>` is the consumer's name as `lsmod` prints it, the same spelling the debugfs layout used. The directory is created by `kmodcov_register` and removed by `kmodcov_unregister`; it never outlives the registration.
2. **Why sysfs and not procfs.** Measured against the six kernel trees of the ladder: `kobject_create_and_add`, `sysfs_create_group`, `sysfs_remove_group`, `kobject_put` and `struct kobj_attribute` have the same signatures from 2.6.32 through 6.17; the procfs entry API broke three times in the same span (`create_proc_entry` removed by 3.10, `file_operations` became `proc_ops` in 5.6, `PDE_DATA` became `pde_data` in 5.17) and a proc file would also keep the `open`/`llseek` helpers the debugfs files needed. Both filesystems are `default y` behind `CONFIG_EXPERT`, so procfs buys no availability. `/sys/module/<module>/` already exists for every loaded module and already carries the `cov_dir` parameter; the control files sit beside the parameter that configures them.
3. **The interface number becomes 2.** `KMODCOV_INTERFACE` in `kmodcov.h` and `otto.kmodcov.library.INTERFACE` both read 2; the modinfo suffix becomes `+kmodcov2`. The debugfs layout was part of the interface by its own definition ("the debugfs layout, the cov_dir parameter and the macros"); it changed, so the number changes. The interface's definition is reworded to "the sysfs layout, the `cov_dir` parameter and the consumer macros".
4. **Every sysfs call goes through a `KMODCOV_` name**, like every other kernel-facing call, even though none has an arm: the override surface is the rule, not the arms. Four names enter the "one arm each" block of `kmodcov_compat.h`; five leave (§4.3).
5. **A control-file failure fails the registration.** If the kobject or the attribute group cannot be created, `kmodcov_register` logs `pr_err("%s: cannot create /sys/module/%s/kmodcov (%d)\n", ...)`, unwinds, and returns the errno, so the consumer's `insmod` fails the way it fails today for a missing `cov_dir` or no instrumented objects. A coverage build whose control files cannot exist is a broken build; otto would fail at the first dump regardless, with less to say. On a kernel without `CONFIG_SYSFS` the creation calls are inline successes and the files simply do not exist; otto's dump write then reports the missing path with the existing message.
6. **Registering a module twice is refused up front** with `-EBUSY`, under the lock, before any kobject work. Today a second registration quietly got a second debugfs directory that failed to create; with sysfs a duplicate kobject name produces the kernel's `kobject_add_internal failed` warning with a stack dump, which a refusal avoids.
7. **Removal stays outside the lock.** `kmodcov_unregister` does the exit dump and the list removal under `kmodcov_lock`, releases it, then removes the group and puts the kobject. sysfs waits for in-flight store callbacks before `sysfs_remove_group` and `kobject_put` return, and a store blocked on the lock must be able to take it and finish; holding the lock across the removal would deadlock against exactly that writer.
8. **A store that finds no client returns `-ENODEV`.** The store callback looks the client up by kobject under the lock. In the window between unregister's list removal and its group removal the lookup fails; the write fails with `-ENODEV`, never touches freed memory, and never dumps a module that has already dumped at exit.
9. **The matrix is re-measured.** Every cell builds the library against its kernel's headers, which is the compile proof of rule 2 per kernel; the artifact is re-folded and committed, as the rename did.
10. **Frozen history keeps the old layout**: earlier specs under `docs/superpowers/specs/` and `CHANGELOG.md` are not edited.

## 4. Design

### 4.1 The files

| Path | Mode | Write | Returns |
|---|---|---|---|
| `/sys/module/<module>/kmodcov/dump` | 0200 | any bytes; content ignored | the count, or the dump's negative errno (`-ENOMEM`, the file write's errno, `-ENODEV` per rule 8) |
| `/sys/module/<module>/kmodcov/reset` | 0200 | any bytes; content ignored | the count, or `-ENODEV` per rule 8 |

`dump` adds the live counters into the accumulator, zeroes the live ones, and writes the accumulator out under `cov_dir`, exactly as before; two dumps in one run add, never double count. `reset` zeroes both. The exit dump at `KMODCOV_EXIT()` is unchanged. otto writes `1` to each, as it did to the debugfs files.

### 4.2 The mechanism in `kmodcov.c`

- `struct kmodcov_client` loses `struct dentry *dent` and gains `struct kobject *kobj`.
- Two `kobj_attribute`s, `__ATTR(dump, 0200, NULL, kmodcov_dump_store)` and `__ATTR(reset, 0200, NULL, kmodcov_reset_store)`, collected in one `struct attribute_group kmodcov_group`. `__ATTR` is used rather than `__ATTR_WO`, which 2.6.32 lacks.
- A store callback has the stable `kobj_attribute` shape, `ssize_t (*)(struct kobject *, struct kobj_attribute *, const char *, size_t)`. It takes `kmodcov_lock`, walks `kmodcov_clients` for the client whose `kobj` is the callback's `kobj`, runs `kmodcov_dump_client` or `kmodcov_reset_client`, releases the lock, and returns the count or the error (rule 8 when nothing matched).
- `kmodcov_register`, under the lock: refuse a module already on the list (rule 6) before the constructor pass; after the pass succeeds, `list_add`, then `c->kobj = KMODCOV_SYSFS_DIR("kmodcov", &mod->mkobj.kobj)` and `KMODCOV_SYSFS_GROUP(c->kobj, &kmodcov_group)`. Creation happens under the lock so a store finds a fully built client or none. On failure (a NULL kobject is `-ENOMEM`; the group's errno otherwise): `list_del`, unlock, `pr_err` per rule 5, put the kobject if it exists, free the client, return the errno. The success `pr_info` is unchanged.
- `kmodcov_unregister`: under the lock, find, exit dump, `list_del`; unlock; `KMODCOV_SYSFS_GROUP_REMOVE(c->kobj, &kmodcov_group)`; `KMODCOV_SYSFS_DIR_PUT(c->kobj)`; free (rule 7).
- `kmodcov_root`, `kmodcov_init`, `kmodcov_exit`, the two `file_operations` and the debugfs write handlers are deleted. The library module has no init or exit routine of its own; the `module_init`/`module_exit` lines go with them. `MODULE_LICENSE`, `MODULE_VERSION` and the exports stay.
- The header comment of `kmodcov.c` and of `kmodcov.h` describe the sysfs path.

### 4.3 The override surface in `kmodcov_compat.h`

Leave, with their arms: `KMODCOV_DEBUGFS_DIR`, `KMODCOV_DEBUGFS_FILE`, `KMODCOV_DEBUGFS_REMOVE` (no arm), `KMODCOV_FOPS_OPEN` (arm at 3.5, `simple_open`), `KMODCOV_LLSEEK` (arm at 2.6.35, `noop_llseek`), and the `kmodcov_compat_simple_open` helper. `#include <linux/debugfs.h>` leaves.

Enter, in the "one arm each" block, with `#include <linux/kobject.h>` and `#include <linux/sysfs.h>`:

| name | default | what it does |
|---|---|---|
| `KMODCOV_SYSFS_DIR(name, parent)` | `kobject_create_and_add((name), (parent))` | the consumer's `kmodcov/` directory; `NULL` on failure |
| `KMODCOV_SYSFS_DIR_PUT(kobj)` | `kobject_put(kobj)` | drop it; must accept the kobject `KMODCOV_SYSFS_DIR` returned |
| `KMODCOV_SYSFS_GROUP(kobj, group)` | `sysfs_create_group((kobj), (group))` | the two files; `0` or a negative errno |
| `KMODCOV_SYSFS_GROUP_REMOVE(kobj, group)` | `sysfs_remove_group((kobj), (group))` | remove them; must wait for in-flight stores, as the default does |

The header's opening comment keeps its ladder description; the `KMODCOV_FILE_WRITE` block, the mkdir arms and the rest are untouched. The 2.6.39 to 3.5 UNTESTED window is unchanged in substance: the two untested arms are `KMODCOV_MKDIR`'s, and `KMODCOV_FOPS_OPEN`'s 3.5 boundary leaving the header removes nothing that was proven there.

### 4.4 Interface 2

`kmodcov.h`: `#define KMODCOV_INTERFACE 2`, and the comment above it names the sysfs layout. `library.py`: `INTERFACE = 2`, docstring likewise. `tests/unit/test_kmodcov_export.py`: the guard list `BARE_KERNEL_CALLS` drops `debugfs_create_dir(`, `debugfs_create_file(`, `debugfs_remove_recursive(`, `simple_open`, `noop_llseek` and gains `kobject_create_and_add(`, `kobject_put(`, `sysfs_create_group(`, `sysfs_remove_group(`; the macro-shape test's name list drops the five and gains the four; fixtures that assert this otto's own suffix read `+kmodcov2` (the parser cases `+kmodcov1`, `+kmodcov12` and the bare `kmodcov1` are parsing tests and stay). The vendored copy under `tests/repo5/third_party/otto_kmodcov/` is regenerated by `otto cov kmodcov export`, never hand-edited; the identity test proves it.

### 4.5 otto

`src/otto/host/kmod_kind.py`: `KMODCOV_DEBUGFS` becomes `KMODCOV_SYSFS_ROOT = "/sys/module"` with a docstring naming the layout, `_kmodcov_file` returns `f"{KMODCOV_SYSFS_ROOT}/{self.module_name}/kmodcov/{name}"`, and the module docstring, the comments and the error texts say sysfs where they said debugfs. The hooks' control flow, the dry-run decline handling, `_kmodcov_context`, `_ensure_library`, the `kernel` method and `kmod_tool_kind.py` are unchanged.

`tests/unit/host/test_kmod_kind.py` and `tests/unit/cov/test_fetcher.py`: every assertion on the control path moves to the new one; the two tests named after debugfs are renamed after sysfs; one new test pins the exact path for a module name (`/sys/module/otto_kmod_demo/kmodcov/dump`) so a later edit to the root or the directory name is a visible change. `tests/repo5/tests/test_kmod_demo.py`: `DUMP` moves; `CTL` (the demo's own debugfs file) stays.

### 4.6 Proof

- **The grep is empty**: `git grep -n -i debugfs -- src/otto/kmodcov src/otto/host/kmod_kind.py tests/unit/host/test_kmod_kind.py tests/unit/cov/test_fetcher.py tests/repo5/third_party` prints nothing, and `git grep -n 'kernel/debug/otto_kmodcov' -- . ':!docs/superpowers/specs' ':!CHANGELOG.md'` prints nothing.
- **The built library imports no debugfs symbol**: the kmod coverage e2e, which builds `otto_kmodcov.ko` for the beds' kernel, asserts that `nm -u` on the `.ko` lists no `debugfs_` symbol. This is the structural proof that `CONFIG_DEBUG_FS` is no longer a requirement of the module method, since no lab host lacks it.
- **Compile proof per kernel**: the full `make kmodcov` run, then `make kmodcov-matrix`, whose fold is committed as the re-measured artifact (rule 9).
- **Runtime proof**: `tests/e2e/cov/test_kmod_coverage_e2e.py` and `tests/repo5/tests/test_kmod_demo.py` (the mid-suite dump that must not double count against the exit dump) on the beds' 6.8 kernel, through the new files.
- **The otto side**: the unit files of §4.5 and §4.4, the export identity test, `make docs` (`-W`), `make typecheck`, `make lint-arch`, `nox -s tests_hostless-3.14`, `make coverage`, `make gate-fresh`.

## 5. Files

Library: `src/otto/kmodcov/kmodcov.c`, `kmodcov_compat.h`, `kmodcov.h`, `README.md`, `library.py`, and the vendored copy of the first four under `tests/repo5/third_party/otto_kmodcov/` by export. otto: `src/otto/host/kmod_kind.py`. Tests: `tests/unit/test_kmodcov_export.py`, `tests/unit/host/test_kmod_kind.py`, `tests/unit/cov/test_fetcher.py`, `tests/repo5/tests/test_kmod_demo.py`, `tests/e2e/cov/test_kmod_coverage_e2e.py` (the `nm -u` assertion). Docs: `docs/cli/cov/instrumenting/kernel-modules.md` (the library section describes the sysfs layout and states that the module method needs no `CONFIG_DEBUG_FS`; the arm table loses the `KMODCOV_FOPS_OPEN` and `KMODCOV_LLSEEK` rows and its last row's three debugfs names, and gains the four sysfs names in that row), `docs/getting-started/coverage.md` (one sentence). Artifact: `schemas/kmodcov_matrix.json` re-measured. New files: none. Deleted files: none.

## 6. Compatibility

None kept, by rule. A consumer tree vendored at interface 1 builds and loads as a module on its own, but otto refuses to load that library with the existing message (`reports <version> (interface kmodcov1), but this otto drives interface kmodcov2 — re-export ...`), which is the intended signal; after `otto cov kmodcov export` and a rebuild it works. The bed hosts carry no state under the old path.

## 7. Follow-ups

- Spec 2 (the clang source-based backend) writes its second format under the same `cov_dir` on the same `dump`; it adds nothing to the control files.
- #531 (proving the 2.6.39 to 3.5 arms) is unaffected: the remaining untested arms are `KMODCOV_MKDIR`'s.

## 8. Gates

Per commit on this branch, the targeted set: the unit files the commit touches plus `ruff check` on every changed Python file, `make docs` for docs commits. Once, before the squash, the full set in §4.6. The branch is squashed onto main.
