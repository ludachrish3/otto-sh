// SPDX-License-Identifier: GPL-2.0
/*
 * The gcc side of otto_kgcov: the symbols a gcc-instrumented object
 * references, and the two backend hooks kgcov_gcov.h declares.
 *
 * gcc's -fprofile-arcs emits one constructor per translation unit that
 * calls __gcov_init(&this_unit's_gcov_info). A kernel without
 * CONFIG_CONSTRUCTORS never runs it; kgcov_register() does, and
 * __gcov_init() hands the unit to that registration. The merge functions
 * are the same empty bodies the in-kernel gcov (kernel/gcov/gcc_base.c)
 * exports — merging is otto_kgcov's own work.
 */
#include <linux/module.h>
#include "kgcov_gcov.h"

/*
 * No kernel header declares these — gcc's own libgcov ABI supplies them
 * out-of-band — so without local prototypes -Wmissing-prototypes fires on
 * every definition below.
 */
void __gcov_init(struct gcov_info *info);
void __gcov_flush(void);
void __gcov_exit(void);
void __gcov_merge_add(gcov_type *counters, unsigned int n_counters);
void __gcov_merge_single(gcov_type *counters, unsigned int n_counters);
void __gcov_merge_delta(gcov_type *counters, unsigned int n_counters);
void __gcov_merge_ior(gcov_type *counters, unsigned int n_counters);
void __gcov_merge_time_profile(gcov_type *counters, unsigned int n_counters);
void __gcov_merge_icall_topn(gcov_type *counters, unsigned int n_counters);
void __gcov_merge_topn(gcov_type *counters, unsigned int n_counters);

void __gcov_init(struct gcov_info *info)
{
	kgcov_ctor_info(info);
}
EXPORT_SYMBOL(__gcov_init);
void __gcov_flush(void) { }
EXPORT_SYMBOL(__gcov_flush);
void __gcov_exit(void) { }
EXPORT_SYMBOL(__gcov_exit);
void __gcov_merge_add(gcov_type *counters, unsigned int n_counters) { }
EXPORT_SYMBOL(__gcov_merge_add);
void __gcov_merge_single(gcov_type *counters, unsigned int n_counters) { }
EXPORT_SYMBOL(__gcov_merge_single);
void __gcov_merge_delta(gcov_type *counters, unsigned int n_counters) { }
EXPORT_SYMBOL(__gcov_merge_delta);
void __gcov_merge_ior(gcov_type *counters, unsigned int n_counters) { }
EXPORT_SYMBOL(__gcov_merge_ior);
void __gcov_merge_time_profile(gcov_type *counters, unsigned int n_counters) { }
EXPORT_SYMBOL(__gcov_merge_time_profile);
void __gcov_merge_icall_topn(gcov_type *counters, unsigned int n_counters) { }
EXPORT_SYMBOL(__gcov_merge_icall_topn);
/* gcc has used both `topn` and `icall_topn` as the merge-function name across
 * versions; a consumer built by either toolchain must link, so both are
 * exported.
 */
void __gcov_merge_topn(gcov_type *counters, unsigned int n_counters) { }
EXPORT_SYMBOL(__gcov_merge_topn);

/*
 * gcc's version word: four bytes, most significant first — '4' '0' <minor>
 * for gcc 4.x, 'A' <major> <minor> for 5 to 9, 'B' <major - 10> <minor> for
 * 10 to 19, then '*' (measured 2026-09-18: gcc 9.5 writes "A95*", gcc 14.2
 * "B42*"). struct gcov_info is laid out by __GNUC__ at compile time, so a
 * unit from another major cannot be parsed; this decodes the major and
 * compares. An encoding this decoder does not know — a lead byte past 'B',
 * or a version byte that is not a digit — is a gcc it predates: the major
 * check is skipped with one pr_info, so a newer table arm is not undone by
 * a stale check here. Only the version word is read; a unit this function
 * rejects may have every other field at an offset this build does not know.
 */
bool gcov_info_built_by_this_compiler(struct gcov_info *info, const char *mod,
				      int *have, int *want)
{
	unsigned int v = gcov_info_version(info);
	unsigned char lead = v >> 24, digit = (v >> 16) & 0xff;
	bool is_digit = digit >= '0' && digit <= '9';

	*want = __GNUC__;
	if (lead == '4')
		*have = 4;
	else if (lead == 'A' && is_digit)
		*have = digit - '0';
	else if (lead == 'B' && is_digit)
		*have = 10 + (digit - '0');
	else
		*have = -1;
	if (*have < 0) {
		pr_info("%s: gcov version word %#x is an encoding otto_kgcov's gcc %d does not know; taking the unit as it comes\n",
			mod, v, __GNUC__);
		return true;
	}
	return *have == *want;
}

/* A gcc unit's gcov_info is the consumer's own static data: nothing to free. */
void gcov_info_forget(struct gcov_info *info)
{
}
