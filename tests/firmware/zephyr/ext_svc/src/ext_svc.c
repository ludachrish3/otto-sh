/*
 * Base-side service helpers exported to LLEXT extensions.
 *
 * Extensions cannot define their own thread stacks: K_THREAD_STACK_DEFINE
 * carries alignment and section-placement requirements that a loaded
 * extension's sections don't satisfy. So the stack (and the k_thread object)
 * live here in the base, and extensions drive them through two exported
 * calls:
 *
 *   tid = ext_svc_spawn(entry);   start `entry` on the base-owned stack
 *   ext_svc_abort(tid);           stop it
 *
 * Single-flight by design: one base stack, one service thread at a time.
 * A second spawn while the first thread is alive returns NULL instead of
 * re-creating over a live k_thread — on a shared bed a misbehaving extension
 * must not be able to corrupt kernel state. The busy flag clears when the
 * entry returns on its own or when ext_svc_abort() kills it.
 */
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>
#include <zephyr/llext/symbol.h>
#include <zephyr/sys/atomic.h>

K_THREAD_STACK_DEFINE(ext_svc_stack, CONFIG_OTTO_EXT_SVC_STACK_SIZE);
static struct k_thread ext_svc_thread;
static atomic_t ext_svc_busy;
static k_thread_entry_t ext_svc_entry;

/* Runs the extension's entry, then clears the busy flag so a service loop
 * that exits by itself (e.g. a stop-flag poll) can be respawned without an
 * explicit abort. */
static void ext_svc_trampoline(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	ext_svc_entry(NULL, NULL, NULL);
	atomic_set(&ext_svc_busy, 0);
}

/* Run an extension-provided entry point on the base-owned stack.
 * Returns NULL if a service thread is already running (or entry is NULL). */
k_tid_t ext_svc_spawn(k_thread_entry_t entry)
{
	if (entry == NULL || !atomic_cas(&ext_svc_busy, 0, 1)) {
		return NULL;
	}
	ext_svc_entry = entry;
	return k_thread_create(&ext_svc_thread, ext_svc_stack,
			       K_THREAD_STACK_SIZEOF(ext_svc_stack),
			       ext_svc_trampoline, NULL, NULL, NULL,
			       K_PRIO_PREEMPT(CONFIG_OTTO_EXT_SVC_THREAD_PRIO),
			       0, K_NO_WAIT);
}
EXPORT_SYMBOL(ext_svc_spawn);

void ext_svc_abort(k_tid_t tid)
{
	/* Only the thread this module owns; a garbage tid from a confused
	 * extension is ignored rather than handed to k_thread_abort(). */
	if (tid != &ext_svc_thread || atomic_get(&ext_svc_busy) == 0) {
		return;
	}
	k_thread_abort(tid);
	atomic_set(&ext_svc_busy, 0);
}
EXPORT_SYMBOL(ext_svc_abort);

/* The protocol UART: whatever the board chose as zephyr,uart-pipe (uart1 on
 * the mps2 boards — the console/shell stays on uart0). Exported so
 * extensions don't have to hard-code the devicetree node name, which is
 * version-drift-prone (device_get_binding("uart@40005000") also works on the
 * current 3.7 and 4.4 bases). */
#if DT_HAS_CHOSEN(zephyr_uart_pipe)
static const struct device *const ext_svc_uart_pipe_dev =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_uart_pipe));
#endif

const struct device *ext_svc_uart_pipe(void)
{
#if DT_HAS_CHOSEN(zephyr_uart_pipe)
	return ext_svc_uart_pipe_dev;
#else
	return NULL;
#endif
}
EXPORT_SYMBOL(ext_svc_uart_pipe);
