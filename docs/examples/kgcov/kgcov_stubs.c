// SPDX-License-Identifier: GPL-2.0
/*
 * The symbols an instrumented object references. gcc's libgcov merges
 * profiles with these; here they are the same empty bodies the in-kernel
 * gcov (kernel/gcov/gcc_base.c) exports — merging is otto_kgcov's own work.
 */
#include <linux/module.h>
#include "kgcov_gcc.h"

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

void __gcov_init(struct gcov_info *info) { }
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
