// SPDX-License-Identifier: GPL-2.0
/* The queue: three full-queue policies, a drain loop with a stop marker. */
#include <linux/errno.h>
#include <linux/slab.h>

#include "demo.h"

int demo_queue_init(struct demo_queue *q, size_t cap)
{
	q->items = kcalloc(cap, sizeof(int), GFP_KERNEL);
	if (!q->items)
		return -ENOMEM;
	q->cap = cap;
	q->len = 0;
	q->head = 0;
	q->policy = DEMO_FIFO;
	q->enqueued = 0;
	q->dropped = 0;
	q->drained = 0;
	return 0;
}

void demo_queue_free(struct demo_queue *q)
{
	kfree(q->items);
	q->items = NULL;
	q->cap = 0;
	q->len = 0;
}

int demo_enqueue(struct demo_queue *q, int value)
{
	if (q->len == q->cap) {
		switch (q->policy) {
		case DEMO_FIFO:
		case DEMO_LIFO:
			q->dropped++;
			return -ENOSPC;
		case DEMO_DROP_OLDEST:
			q->head = (q->head + 1) % q->cap;
			q->len--;
			q->dropped++;
			break;
		default:
			/* Unreachable: the parser admits exactly three policies. */
			return -EINVAL;
		}
	}
	q->items[(q->head + q->len) % q->cap] = value;
	q->len++;
	q->enqueued++;
	return 0;
}

size_t demo_drain(struct demo_queue *q, long *sum)
{
	size_t n = 0;

	*sum = 0;
	while (q->len) {
		int v;

		if (q->policy == DEMO_LIFO) {
			v = q->items[(q->head + q->len - 1) % q->cap];
		} else {
			v = q->items[q->head];
			q->head = (q->head + 1) % q->cap;
		}
		q->len--;
		*sum += v;
		n++;
		if (v < 0)
			break; /* a negative item is a stop marker: the rest stays queued */
	}
	q->drained += n;
	return n;
}

int demo_set_limit(struct demo_queue *q, size_t cap)
{
	int *items;
	size_t i;

	if (cap < q->len)
		return -EBUSY; /* deliberately never exercised by the suite */
	items = kcalloc(cap, sizeof(int), GFP_KERNEL);
	if (!items)
		return -ENOMEM;
	for (i = 0; i < q->len; i++)
		items[i] = q->items[(q->head + i) % q->cap];
	kfree(q->items);
	q->items = items;
	q->cap = cap;
	q->head = 0;
	return 0;
}
