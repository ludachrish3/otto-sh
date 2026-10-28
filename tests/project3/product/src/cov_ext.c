/*
 * project3 embedded coverage product -- the LLEXT analogue of project1's math_ops.
 *
 * Approach A (self-contained): the instrumented product *and* the embedded-gcov
 * runtime are compiled into one translation unit, so the coverage host's base
 * image stays stock (it provides only the generic LLEXT loader + `printk`). The
 * base carries NO coverage code.
 *
 * The runtime and the cov_init/cov_dump/cov_reset entry points come from the
 * starter otto ships (cov_entry.c, on the include path -- see CMakeLists.txt),
 * which #includes the embedded-gcov sources from the patched submodule. gcov
 * serial output is routed to printk by that patch.
 *
 * Lifecycle (mirrors project1's Unix product): load (`llext load_hex`) ->
 * initialise (`call_fn cov_init`) -> exercise (`call_fn op_*`) -> collect
 * (`call_fn cov_dump`, which prints the .gcda as a serial hexdump) -> unload.
 * `otto cov clean` zeroes the counters in place with `call_fn cov_reset`.
 */
#include "cov_entry.c"

static volatile int g_acc;

int math_clamp(int v, int lo, int hi)
{
	if (v < lo) {
		return lo;
	}
	if (v > hi) {
		return hi;
	}
	return v;
}

int math_div(int a, int b)
{
	if (b == 0) {
		return -1;
	}
	return a / b;
}

/* One exported entry point per code path to exercise, invoked via
 * `llext call_fn cov_ext <op>` (prototype void fn(void)). */
void op_clamp_lo(void) { g_acc += math_clamp(-5, 0, 10); }
void op_clamp_in(void) { g_acc += math_clamp(7, 0, 10); }
void op_div_ok(void)   { g_acc += math_div(10, 2); }
void op_div_zero(void) { g_acc += math_div(10, 0); }

LL_EXTENSION_SYMBOL(op_clamp_lo);
LL_EXTENSION_SYMBOL(op_clamp_in);
LL_EXTENSION_SYMBOL(op_div_ok);
LL_EXTENSION_SYMBOL(op_div_zero);
