/* SPDX-License-Identifier: GPL-2.0 */
/*
 * otto_kmodcov — gcov for out-of-tree modules on a kernel built without
 * CONFIG_GCOV_KERNEL or CONFIG_CONSTRUCTORS. A consumer compiles its
 * instrumented objects with -fprofile-arcs -ftest-coverage (gcc or clang),
 * brackets its .init_array with the two sentinels below, and calls
 * KMODCOV_INIT() first in its init routine and KMODCOV_EXIT() last in its exit
 * routine. KMODCOV_INIT() runs the constructors the kernel never ran — gcc's
 * call __gcov_init(), clang's llvm_gcov_init(), both exported by this
 * module — which is how each translation unit's counters reach the
 * library. Counters are written as .gcda files under the `cov_dir` module
 * parameter on a write to /sys/kernel/debug/otto_kmodcov/<module>/dump, and
 * once more at KMODCOV_EXIT() — so the exit routine's own coverage is kept.
 */
#ifndef OTTO_KMODCOV_H
#define OTTO_KMODCOV_H

#include <linux/module.h>
#include <linux/moduleparam.h>

/*
 * The library interface otto drives: the debugfs layout, the cov_dir
 * parameter and the macros below. otto reads it back off a built module's
 * MODULE_VERSION ("<otto version>+kmodcov<n>") and refuses a library whose
 * number is not the one it expects. Bump it only when one of those changes.
 */
#define KMODCOV_INTERFACE 1

typedef void (*kmodcov_ctor_fn)(void);

int kmodcov_register(struct module *mod, const kmodcov_ctor_fn *begin,
		   const kmodcov_ctor_fn *end, const char *dir);
void kmodcov_unregister(struct module *mod);

/*
 * Section sentinels. Each instrumented object carries one constructor in
 * .init_array: gcc's in .init_array.00100, clang's in .init_array.0. The
 * module linker script (scripts/module.lds) lays the section out as
 * *(SORT(.init_array.*)) then *(.init_array), and ld -r keeps input order
 * within one name. So a pointer in .init_array.0 from the FIRST object of
 * the consumer's link precedes every constructor, and a pointer in plain
 * .init_array from the LAST object follows every one; kmodcov_register()
 * calls what lies strictly between the two. The kernel itself runs
 * .init_array only under CONFIG_CONSTRUCTORS, and a constructor that runs
 * outside kmodcov_register() registers nothing, so the walk is the one
 * registration either way. The sentinels point at a no-op each so that a
 * CONFIG_CONSTRUCTORS kernel's own pass has something harmless to call,
 * and each is aligned like the pointer it is rather than to a fixed 8: an
 * .init_array entry is four bytes on an ILP32 target, where over-aligning
 * would let the linker insert a zero word for that pass to call.
 */
#define KMODCOV_SENTINEL(name, sect, marker)                                     \
	static void marker(void)                                               \
	{                                                                      \
	}                                                                      \
	const kmodcov_ctor_fn name                                               \
		__attribute__((section(sect), used,                            \
			       aligned(sizeof(kmodcov_ctor_fn)))) = marker
#define KMODCOV_SENTINEL_BEGIN                                                   \
	KMODCOV_SENTINEL(__kmodcov_ctors_begin, ".init_array.0", __kmodcov_begin_marker)
#define KMODCOV_SENTINEL_END                                                     \
	KMODCOV_SENTINEL(__kmodcov_ctors_end, ".init_array", __kmodcov_end_marker)

extern const kmodcov_ctor_fn __kmodcov_ctors_begin;
extern const kmodcov_ctor_fn __kmodcov_ctors_end;

/* File scope, once per consumer: the cov_dir parameter otto passes to insmod. */
#define KMODCOV_DECLARE()                                                        \
	static char *cov_dir;                                                 \
	module_param(cov_dir, charp, 0444);                                   \
	MODULE_PARM_DESC(cov_dir, "absolute directory the module's coverage files are written under")

/* First statement of the init routine: 0, or a negative errno to return. */
#define KMODCOV_INIT()                                                           \
	kmodcov_register(THIS_MODULE, &__kmodcov_ctors_begin + 1, &__kmodcov_ctors_end, cov_dir)

/* Last statement of the exit routine. */
#define KMODCOV_EXIT() kmodcov_unregister(THIS_MODULE)

#endif /* OTTO_KMODCOV_H */
