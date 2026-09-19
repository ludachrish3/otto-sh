/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Vendored from Linux v6.8 kernel/gcov/ for otto_kgcov; unchanged apart from the include name and the otto_kgcov block at the end.
 *
 *  Profiling infrastructure declarations.
 *
 *  This file is based on gcc-internal definitions. Data structures are
 *  defined to be compatible with gcc counterparts. For a better
 *  understanding, refer to gcc source: gcc/gcov-io.h.
 *
 *    Copyright IBM Corp. 2009
 *    Author(s): Peter Oberparleiter <oberpar@linux.vnet.ibm.com>
 *
 *    Uses gcc-internal data definitions.
 */

#ifndef GCOV_H
#define GCOV_H GCOV_H

#include <linux/module.h>
#include <linux/types.h>

/*
 * Profiling data types used for gcc 3.4 and above - these are defined by
 * gcc and need to be kept as close to the original definition as possible to
 * remain compatible.
 */
#define GCOV_DATA_MAGIC		((unsigned int) 0x67636461)
#define GCOV_TAG_FUNCTION	((unsigned int) 0x01000000)
#define GCOV_TAG_COUNTER_BASE	((unsigned int) 0x01a10000)
#define GCOV_TAG_FOR_COUNTER(count)					\
	(GCOV_TAG_COUNTER_BASE + ((unsigned int) (count) << 17))

#if BITS_PER_LONG >= 64
typedef long gcov_type;
#else
typedef long long gcov_type;
#endif

/* Opaque gcov_info. The gcov structures can change as for example in gcc 4.7 so
 * we cannot use full definition here and they need to be placed in gcc specific
 * implementation of gcov. This also means no direct access to the members in
 * generic code and usage of the interface below.*/
struct gcov_info;

/* Interface to access gcov_info data  */
const char *gcov_info_filename(struct gcov_info *info);
unsigned int gcov_info_version(struct gcov_info *info);
struct gcov_info *gcov_info_next(struct gcov_info *info);
void gcov_info_link(struct gcov_info *info);
void gcov_info_unlink(struct gcov_info *prev, struct gcov_info *info);
bool gcov_info_within_module(struct gcov_info *info, struct module *mod);
size_t convert_to_gcda(char *buffer, struct gcov_info *info);

/* Base interface. */
enum gcov_action {
	GCOV_ADD,
	GCOV_REMOVE,
};

void gcov_event(enum gcov_action action, struct gcov_info *info);
void gcov_enable_events(void);

/* writing helpers */
size_t store_gcov_u32(void *buffer, size_t off, u32 v);
size_t store_gcov_u64(void *buffer, size_t off, u64 v);

/* gcov_info control. */
void gcov_info_reset(struct gcov_info *info);
int gcov_info_is_compatible(struct gcov_info *info1, struct gcov_info *info2);
void gcov_info_add(struct gcov_info *dest, struct gcov_info *source);
struct gcov_info *gcov_info_dup(struct gcov_info *info);
void gcov_info_free(struct gcov_info *info);

struct gcov_link {
	enum {
		OBJ_TREE,
		SRC_TREE,
	} dir;
	const char *ext;
};
extern const struct gcov_link gcov_link[];

extern int gcov_events_enabled;
extern struct mutex gcov_lock;

/*
 * otto_kgcov's additions to the vendored interface: the one call a backend
 * makes into kgcov.c, and the two hooks each backend implements for it.
 */

/*
 * Hand a translation unit's gcov_info to the registration in progress. A
 * backend's constructor entry point (__gcov_init, llvm_gcov_init) calls it
 * from the constructor kgcov_register() is running, on that thread, with
 * kgcov_register() holding the library's lock — so it takes no lock itself.
 * Outside the registering thread's own registration it keeps nothing.
 */
void kgcov_ctor_info(struct gcov_info *info);

/*
 * Whether this build of the library can parse *info. On false, *have and
 * *want name the consumer's and the library's compiler major for the log.
 * *mod is the consumer module's name, for a backend that logs on its own.
 * Only *info's version word may be read: nothing else is known to be where
 * this build expects it until the answer is true.
 */
bool gcov_info_built_by_this_compiler(struct gcov_info *info, const char *mod,
				      int *have, int *want);

/*
 * Release whatever the backend allocated to present a LIVE unit — never its
 * counters or filename, which are the consumer's own. A no-op where the
 * live gcov_info is the consumer's static data (gcc).
 */
void gcov_info_forget(struct gcov_info *info);

#endif /* GCOV_H */
