// SPDX-License-Identifier: GPL-2.0
/*
 * qs_wordcount: the kernel-module track's example. Write a line to the
 * `line` parameter and read its word count back from `words`.
 */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/string.h>

#include "kmodcov.h"
#include "wordcount.h"

KMODCOV_DECLARE();

static int words;
module_param(words, int, 0444);
MODULE_PARM_DESC(words, "How many words the last line written to `line` held");

static int line_set(const char *val, const struct kernel_param *kp)
{
	words = qs_count_words(val, strlen(val));
	return 0; // qs: line-set
}

static const struct kernel_param_ops line_ops = {
	.set = line_set,
};
module_param_cb(line, &line_ops, NULL, 0200);
MODULE_PARM_DESC(line, "Write a line here; `words` then holds its word count");

static int __init qs_init(void)
{
	int err = KMODCOV_INIT();

	if (err)
		return err;
	pr_info("loaded\n"); // qs: init
	return 0;
}

static void __exit qs_exit(void)
{
	pr_info("unloaded; the last line held %d word(s)\n", words); // qs: exit
	KMODCOV_EXIT();
}

module_init(qs_init);
module_exit(qs_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("otto kernel-module track example: a word counter");
