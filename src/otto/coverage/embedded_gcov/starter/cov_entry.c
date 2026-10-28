/*
 * Starter for an embedded coverage extension: the embedded-gcov runtime and the
 * three entry points otto drives, compiled into one translation unit.
 *
 * Put the code to measure in this same translation unit (below the entry
 * points, or in a file that #includes this one), and compile it with
 * -fprofile-arcs -ftest-coverage. The runtime sources are #included from the
 * embedded-gcov checkout, so its `code/` directory, patched with
 * `otto cov embedded-gcov export`, must be on the include path.
 *
 * Lifecycle: load the extension -> `cov_init` -> exercise your own exported
 * functions -> `cov_dump` (prints the .gcda as a serial hexdump) -> unload.
 * `cov_reset` zeroes the counters in place.
 */
#include <stdint.h>
#include <zephyr/llext/symbol.h>
#include <zephyr/sys/printk.h>

#include "gcov_public.c"
#include "gcov_gcc.c"
#include "gcov_printf.c"

// doc: begin entry-points
/* Dump this extension's gcov counters as a .gcda hexdump over the console. */
void cov_dump(void) { __gcov_exit(); }

/* Zero this extension's gcov counters in place. */
void cov_reset(void) { __gcov_clear(); }

/* Run gcc's gcov constructor for this translation unit, which registers its
 * gcov_info with the runtime. The loader does not run .init_array and the
 * constructor is a local symbol, so alias it and call it from this exported
 * entry. */
extern void gcov_ctor(void) __asm__("_sub_I_00100_0");
void cov_init(void) { gcov_ctor(); }

LL_EXTENSION_SYMBOL(cov_init);
LL_EXTENSION_SYMBOL(cov_dump);
LL_EXTENSION_SYMBOL(cov_reset);
// doc: end entry-points
