/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Vendored from Linux v6.8 kernel/gcov/ for otto_kgcov; unchanged apart from
 * the include name, the otto_kgcov block at the end, and the KGCOV_ macro set
 * that closes the file — every kernel-facing allocation, lock and debugfs name
 * the library uses goes through one of those, so a build may replace it.
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
#include <linux/debugfs.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/vmalloc.h>

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

/*
 * Kernel-facing names a build may replace. Every allocation, lock and
 * debugfs call the library makes goes through one of these, each defined
 * here only when nothing defined it first — and Kbuild force-includes a
 * kgcov_local.h beside the sources when one exists, so a kernel whose
 * allocator, lock or debugfs API differs is adapted in that one file,
 * which otto never exports and never compares. Replace one name at a
 * time; the defaults are the kernel's ordinary calls.
 *
 * Three requirements the library relies on and never checks, so an override
 * that breaks one shows it only on a rare error path, on the target kernel.
 * KGCOV_ALLOC and KGCOV_ALLOC_ARRAY must return ZEROED memory: the client
 * struct's error and object fields are read before anything writes them,
 * and the unwind that frees a half-built object array tests its entries for
 * NULL. KGCOV_FREE and KGCOV_BIG_FREE must accept NULL, because those
 * failure paths free structures whose members were never allocated. And
 * whatever KGCOV_ALLOC, KGCOV_ALLOC_ARRAY, KGCOV_STRDUP, KGCOV_MEMDUP and
 * KGCOV_ASPRINTF return must be releasable by KGCOV_FREE, since that is the
 * only thing that frees them — replacing an allocator without its matching
 * free is a wrong-pool free, which corrupts rather than fails.
 */
#ifndef KGCOV_ALLOC
#define KGCOV_ALLOC(size) kzalloc((size), GFP_KERNEL)
#endif
#ifndef KGCOV_ALLOC_ARRAY
#define KGCOV_ALLOC_ARRAY(n, size) kcalloc((n), (size), GFP_KERNEL)
#endif
#ifndef KGCOV_STRDUP
#define KGCOV_STRDUP(s) kstrdup((s), GFP_KERNEL)
#endif
#ifndef KGCOV_MEMDUP
#define KGCOV_MEMDUP(p, size) kmemdup((p), (size), GFP_KERNEL)
#endif
#ifndef KGCOV_ASPRINTF
#define KGCOV_ASPRINTF(...) kasprintf(GFP_KERNEL, __VA_ARGS__)
#endif
#ifndef KGCOV_FREE
#define KGCOV_FREE(p) kfree(p)
#endif
#ifndef KGCOV_BIG_ALLOC
#define KGCOV_BIG_ALLOC(size) kvmalloc((size), GFP_KERNEL)
#endif
#ifndef KGCOV_BIG_FREE
#define KGCOV_BIG_FREE(p) kvfree(p)
#endif
#ifndef KGCOV_DEFINE_LOCK
#define KGCOV_DEFINE_LOCK(name) static DEFINE_MUTEX(name)
#endif
#ifndef KGCOV_LOCK
#define KGCOV_LOCK(l) mutex_lock(l)
#endif
#ifndef KGCOV_UNLOCK
#define KGCOV_UNLOCK(l) mutex_unlock(l)
#endif
#ifndef KGCOV_DEBUGFS_DIR
#define KGCOV_DEBUGFS_DIR(name, parent) debugfs_create_dir((name), (parent))
#endif
#ifndef KGCOV_DEBUGFS_FILE
#define KGCOV_DEBUGFS_FILE(name, mode, parent, data, fops)                     \
	debugfs_create_file((name), (mode), (parent), (data), (fops))
#endif
#ifndef KGCOV_DEBUGFS_REMOVE
#define KGCOV_DEBUGFS_REMOVE(d) debugfs_remove_recursive(d)
#endif

#endif /* GCOV_H */
