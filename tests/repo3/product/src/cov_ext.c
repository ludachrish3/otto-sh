/*
 * repo3 embedded coverage product — the LLEXT analogue of repo1's math_ops.
 *
 * Approach A (self-contained): the instrumented product *and* the embedded-gcov
 * runtime are compiled into one translation unit, so the coverage host's base
 * image stays stock (it provides only the generic LLEXT loader + `printk`). The
 * base carries NO coverage code.
 *
 * The embedded-gcov sources are #included from the patched submodule
 * (../third_party/embedded-gcov/code, on the include path — see CMakeLists.txt
 * and the README for the one-time submodule init + gcc-12 patch). gcov serial
 * output is routed to printk by that patch.
 *
 * Lifecycle (mirrors repo1's Unix product): load (`llext load_hex`) ->
 * initialise (`call_fn cov_init`) -> exercise (`call_fn op_*`) -> collect
 * (`call_fn cov_dump`, which prints the .gcda as a serial hexdump) -> unload.
 */
#include <stdint.h>
#include <zephyr/llext/symbol.h>
#include <zephyr/sys/printk.h>

#include "gcov_public.c"
#include "gcov_gcc.c"
#include "gcov_printf.c"

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

/* Dump this extension's gcov counters as a .gcda hexdump over the console. */
void cov_dump(void) { __gcov_exit(); }

/* Run gcc's gcov constructor for this TU (registers the gcov_info with the
 * embedded-gcov runtime). LLEXT 3.7 does not run .init_array, and the ctor is a
 * local symbol, so alias it and call it explicitly via this exported entry. */
extern void gcov_ctor(void) __asm__("_sub_I_00100_0");
void cov_init(void) { gcov_ctor(); }

LL_EXTENSION_SYMBOL(op_clamp_lo);
LL_EXTENSION_SYMBOL(op_clamp_in);
LL_EXTENSION_SYMBOL(op_div_ok);
LL_EXTENSION_SYMBOL(op_div_zero);
LL_EXTENSION_SYMBOL(cov_init);
LL_EXTENSION_SYMBOL(cov_dump);
