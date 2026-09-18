// SPDX-License-Identifier: GPL-2.0
/*
 * otto_kmod_demo: a bounded queue driven from debugfs, instrumented with
 * otto_kgcov. Write a command to /sys/kernel/debug/otto_kmod_demo/ctl and
 * read the counters back; a rejected command is reported in `err=`, not as
 * a failed write.
 */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/debugfs.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/uaccess.h>

#include "demo.h"
#include "kgcov.h"

KGCOV_DECLARE();

static struct demo_queue queue;
static DEFINE_MUTEX(demo_lock);
static struct dentry *demo_dir;
static int last_error;
static long last_sum;

static ssize_t ctl_write(struct file *f, const char __user *ubuf, size_t n, loff_t *off)
{
	char line[32];
	struct demo_request req = {};
	int err;

	if (n >= sizeof(line))
		return -E2BIG;
	if (copy_from_user(line, ubuf, n))
		return -EFAULT;
	err = demo_parse(line, n, &req);
	mutex_lock(&demo_lock);
	if (!err) {
		switch (req.cmd) {
		case DEMO_CMD_ENQUEUE:
			err = demo_enqueue(&queue, (int)req.arg);
			break;
		case DEMO_CMD_DRAIN:
			demo_drain(&queue, &last_sum);
			break;
		case DEMO_CMD_POLICY:
			queue.policy = req.policy;
			break;
		case DEMO_CMD_LIMIT:
			err = demo_set_limit(&queue, (size_t)req.arg);
			break;
		}
	}
	last_error = err;
	mutex_unlock(&demo_lock);
	return n;
}

static ssize_t ctl_read(struct file *f, char __user *ubuf, size_t n, loff_t *off)
{
	char out[160];
	int len;

	mutex_lock(&demo_lock);
	len = scnprintf(out, sizeof(out),
			"len=%zu cap=%zu policy=%d enqueued=%lu dropped=%lu drained=%lu sum=%ld err=%d\n",
			queue.len, queue.cap, queue.policy, queue.enqueued, queue.dropped,
			queue.drained, last_sum, last_error);
	mutex_unlock(&demo_lock);
	return simple_read_from_buffer(ubuf, n, off, out, len);
}

static const struct file_operations ctl_fops = {
	.owner = THIS_MODULE,
	.open = simple_open,
	.read = ctl_read,
	.write = ctl_write,
	.llseek = default_llseek,
};

static int __init demo_init(void)
{
	int err = KGCOV_INIT();

	if (err)
		return err;
	err = demo_queue_init(&queue, 8);
	if (err) {
		KGCOV_EXIT();
		return err;
	}
	demo_dir = debugfs_create_dir("otto_kmod_demo", NULL);
	debugfs_create_file("ctl", 0600, demo_dir, NULL, &ctl_fops);
	pr_info("loaded, queue capacity %zu\n", queue.cap);
	return 0;
}

static void __exit demo_exit(void)
{
	long sum;

	debugfs_remove_recursive(demo_dir);
	if (queue.len)
		pr_info("draining %zu item(s) left at exit\n", demo_drain(&queue, &sum));
	demo_queue_free(&queue);
	pr_info("unloaded after %lu enqueue(s), %lu dropped\n", queue.enqueued, queue.dropped);
	KGCOV_EXIT();
}

module_init(demo_init);
module_exit(demo_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("otto coverage demo: a bounded queue with three eviction policies");
MODULE_AUTHOR("otto");
