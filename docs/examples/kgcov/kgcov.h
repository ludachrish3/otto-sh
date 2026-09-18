/* SPDX-License-Identifier: GPL-2.0 */
/*
 * otto_kgcov — gcov for out-of-tree modules on a kernel built without
 * CONFIG_GCOV_KERNEL. A consumer compiles its instrumented objects with
 * -fprofile-arcs -ftest-coverage -fprofile-info-section, bounds the
 * resulting .gcov_info section with the two sentinels below, and calls
 * KGCOV_INIT() first in its init routine and KGCOV_EXIT() last in its exit
 * routine. Counters are written as .gcda files under the `gcov_dir` module
 * parameter on a write to /sys/kernel/debug/otto_kgcov/<module>/dump, and
 * once more at KGCOV_EXIT() — so the exit routine's own coverage is kept.
 */
#ifndef OTTO_KGCOV_H
#define OTTO_KGCOV_H

#include <linux/module.h>
#include <linux/moduleparam.h>

struct gcov_info;

int kgcov_register(struct module *mod, const struct gcov_info *const *begin,
		   const struct gcov_info *const *end, const char *dir);
void kgcov_unregister(struct module *mod);

/*
 * Section sentinels. gcc records each instrumented object's gcov_info
 * pointer in ".gcov_info"; the name is not a C identifier, so the linker
 * synthesises no __start_/__stop_ symbols for it. A consumer bounds the
 * section with two one-entry objects placed FIRST and LAST in its object
 * list (ld -r keeps input order): kgcov_begin.c and kgcov_end.c, each three
 * lines with the SPDX line. The arrays are const not to match gcc's
 * section flags — gcc's -fprofile-info-section emits ".gcov_info" as "aw"
 * (writable, since it holds a pointer gcc's own runtime can rewrite), while
 * a const sentinel emits "a"; the assembler and linker union the flags
 * across all contributions to a section, so the mismatch is harmless. What
 * actually makes the bracket [begin+1, end) hold every instrumented entry
 * is POSITION: the consumer's Kbuild lists kgcov_begin.o first and
 * kgcov_end.o last, so `ld -r`'s input-order guarantee keeps the two
 * sentinels — and only them — outside that range. const buys the
 * sentinels one thing: they carry no relocations of their own, so they
 * cannot be mistaken for a live gcov_info entry.
 */
#define KGCOV_SENTINEL(name)                                                   \
	const struct gcov_info *const name[1]                                  \
		__attribute__((section(".gcov_info"), used, aligned(8))) = { NULL }
#define KGCOV_SENTINEL_BEGIN KGCOV_SENTINEL(__kgcov_info_begin)
#define KGCOV_SENTINEL_END KGCOV_SENTINEL(__kgcov_info_end)

extern const struct gcov_info *const __kgcov_info_begin[1];
extern const struct gcov_info *const __kgcov_info_end[1];

/* File scope, once per consumer: the gcov_dir parameter otto passes to insmod. */
#define KGCOV_DECLARE()                                                        \
	static char *gcov_dir;                                                 \
	module_param(gcov_dir, charp, 0444);                                   \
	MODULE_PARM_DESC(gcov_dir, "absolute directory the .gcda files are written under")

/* First statement of the init routine: 0, or a negative errno to return. */
#define KGCOV_INIT()                                                           \
	kgcov_register(THIS_MODULE, __kgcov_info_begin + 1, __kgcov_info_end, gcov_dir)

/* Last statement of the exit routine. */
#define KGCOV_EXIT() kgcov_unregister(THIS_MODULE)

#endif /* OTTO_KGCOV_H */
