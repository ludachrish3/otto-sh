// SPDX-License-Identifier: GPL-2.0
/* One control line -> one request. Errors are the reader's: -EINVAL for a
 * command or argument that does not parse, -ERANGE for a limit outside 1..64. */
#include <linux/errno.h>
#include <linux/kernel.h>
#include <linux/string.h>

#include "demo.h"

static int parse_policy(const char *word, enum demo_policy *out)
{
	if (!strcmp(word, "fifo")) {
		*out = DEMO_FIFO;
		return 0;
	}
	if (!strcmp(word, "lifo")) {
		*out = DEMO_LIFO;
		return 0;
	}
	if (!strcmp(word, "drop-oldest")) {
		*out = DEMO_DROP_OLDEST;
		return 0;
	}
	return -EINVAL;
}

int demo_parse(const char *line, size_t len, struct demo_request *req)
{
	char buf[32];
	char *cmd, *arg, *rest;

	if (len == 0 || len >= sizeof(buf))
		return -EINVAL;
	memcpy(buf, line, len);
	buf[len] = '\0';
	rest = strim(buf);
	cmd = strsep(&rest, " ");
	arg = rest ? strim(rest) : NULL;
	if (arg && !*arg)
		arg = NULL;

	if (!strcmp(cmd, "drain")) {
		if (arg)
			return -EINVAL; /* deliberately never exercised by the suite */
		req->cmd = DEMO_CMD_DRAIN;
		return 0;
	}
	if (!strcmp(cmd, "enqueue") || !strcmp(cmd, "limit")) {
		long v;

		if (!arg || kstrtol(arg, 10, &v))
			return -EINVAL;
		if (!strcmp(cmd, "limit") && (v < 1 || v > 64))
			return -ERANGE;
		req->cmd = !strcmp(cmd, "limit") ? DEMO_CMD_LIMIT : DEMO_CMD_ENQUEUE;
		req->arg = v;
		return 0;
	}
	if (!strcmp(cmd, "policy")) {
		if (!arg)
			return -EINVAL;
		req->cmd = DEMO_CMD_POLICY;
		return parse_policy(arg, &req->policy);
	}
	return -EINVAL;
}
