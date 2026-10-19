/* SPDX-License-Identifier: GPL-2.0 */
#ifndef OTTO_KMOD_DEMO_H
#define OTTO_KMOD_DEMO_H

#include <linux/types.h>

enum demo_policy { DEMO_FIFO, DEMO_LIFO, DEMO_DROP_OLDEST };

/* A bounded ring buffer of ints with a selectable full-queue policy. */
struct demo_queue {
	int *items;
	size_t cap, len, head;
	enum demo_policy policy;
	unsigned long enqueued, dropped, drained;
};

enum demo_cmd { DEMO_CMD_ENQUEUE, DEMO_CMD_DRAIN, DEMO_CMD_POLICY, DEMO_CMD_LIMIT };

struct demo_request {
	enum demo_cmd cmd;
	long arg;
	enum demo_policy policy;
};

/* demo_parse.c */
int demo_parse(const char *line, size_t len, struct demo_request *req);

/* demo_policy.c */
int demo_queue_init(struct demo_queue *q, size_t cap);
void demo_queue_free(struct demo_queue *q);
int demo_enqueue(struct demo_queue *q, int value);
size_t demo_drain(struct demo_queue *q, long *sum);
int demo_set_limit(struct demo_queue *q, size_t cap);

#endif
