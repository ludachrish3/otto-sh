/*
 * The extension: one translation unit holding the embedded-gcov runtime, the
 * coverage entry points (cov_init, cov_dump, cov_reset) and the logic under
 * test. cov_entry.c is the starter `otto init --embedded-gcov` writes, copied
 * beside this file; the logic is the same source the unit tests build.
 */
#include <zephyr/sys/printk.h>

#include "cov_entry.c"
#include "../../logic/mathops.c"

static volatile int g_acc;

/* One exported entry point per code path to exercise, called as
 * `llext call_fn mathops <op>`; each prints its result for the test to read. */
void op_clamp_lo(void) { g_acc = mathops_clamp(-5, 0, 10); printk("op_clamp_lo=%d\n", g_acc); }
void op_clamp_in(void) { g_acc = mathops_clamp(7, 0, 10); printk("op_clamp_in=%d\n", g_acc); }
void op_div_ok(void)
{
	int q = 0;
	int rc = mathops_div(10, 2, &q);
	printk("op_div_ok=%d,%d\n", rc, q);
}
void op_div_zero(void)
{
	int q = 0;
	int rc = mathops_div(10, 0, &q);
	printk("op_div_zero=%d,%d\n", rc, q);
}

LL_EXTENSION_SYMBOL(op_clamp_lo);
LL_EXTENSION_SYMBOL(op_clamp_in);
LL_EXTENSION_SYMBOL(op_div_ok);
LL_EXTENSION_SYMBOL(op_div_zero);
