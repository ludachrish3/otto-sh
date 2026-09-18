// SPDX-License-Identifier: GPL-2.0
/*
 * otto_kgcov: the runtime behind kgcov.h. One client per registered module;
 * each client owns an ACCUMULATOR per instrumented object (a private
 * gcov_info copy). A dump adds the live counters into the accumulator,
 * zeroes the live ones and writes the accumulator out, so the file on disk
 * is always the module's total since register (or the last reset) and two
 * dumps never double count — no .gcda is ever parsed.
 */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/debugfs.h>
#include <linux/fs.h>
#include <linux/list.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/namei.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>

#include "kgcov.h"
#include "kgcov_gcc.h"

struct kgcov_object {
	struct gcov_info *live; /* the consumer's own, in its .data/.bss */
	struct gcov_info *acc;  /* private total since register / reset */
};

struct kgcov_client {
	struct list_head node;
	struct module *mod;
	char *dir;
	size_t n_objs;
	struct kgcov_object *objs;
	struct dentry *dent;
};

static LIST_HEAD(kgcov_clients);
static DEFINE_MUTEX(kgcov_lock);
static struct dentry *kgcov_root;
static unsigned int kgcov_version; /* gcov format of the first consumer; 0 = none yet */

/* ---- file I/O ---------------------------------------------------------- */

static int kgcov_mkdir(const char *path)
{
	struct path parent;
	struct dentry *d;
	int err;

	d = kern_path_create(AT_FDCWD, path, &parent, LOOKUP_DIRECTORY);
	if (IS_ERR(d))
		return PTR_ERR(d) == -EEXIST ? 0 : PTR_ERR(d);
	err = vfs_mkdir(mnt_idmap(parent.mnt), d_inode(parent.dentry), d, 0755);
	done_path_create(&parent, d);
	return err == -EEXIST ? 0 : err;
}

/* mkdir -p for every directory component of an absolute file path. */
static int kgcov_mkdir_parents(const char *file_path)
{
	char *buf, *p;
	int err = 0;

	buf = kstrdup(file_path, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;
	for (p = buf + 1; *p && !err; p++) {
		if (*p != '/')
			continue;
		*p = '\0';
		err = kgcov_mkdir(buf);
		*p = '/';
	}
	kfree(buf);
	return err;
}

static int kgcov_write_file(const char *path, const char *buf, size_t len)
{
	struct file *f;
	loff_t pos = 0;
	int err = 0;

	f = filp_open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
	if (IS_ERR(f))
		return PTR_ERR(f);
	while (len) {
		ssize_t w = kernel_write(f, buf, len, &pos);

		if (w < 0) {
			err = w;
			break;
		}
		buf += w;
		len -= w;
	}
	filp_close(f, NULL);
	return err;
}

/* ---- dump / reset (caller holds kgcov_lock) ----------------------------- */

static int kgcov_dump_object(struct kgcov_client *c, struct kgcov_object *o)
{
	char *path, *buf;
	size_t size;
	int err;

	gcov_info_add(o->acc, o->live);
	gcov_info_reset(o->live);
	size = convert_to_gcda(NULL, o->acc);
	buf = vmalloc(size);
	if (!buf)
		return -ENOMEM;
	convert_to_gcda(buf, o->acc);
	/* gcov_info_filename() is the absolute .gcda path the compiler baked in. */
	path = kasprintf(GFP_KERNEL, "%s%s", c->dir, gcov_info_filename(o->acc));
	if (!path) {
		vfree(buf);
		return -ENOMEM;
	}
	err = kgcov_mkdir_parents(path);
	if (!err)
		err = kgcov_write_file(path, buf, size);
	if (err)
		pr_err("%s: writing %s failed: %d\n", c->mod->name, path, err);
	kfree(path);
	vfree(buf);
	return err;
}

static int kgcov_dump_client(struct kgcov_client *c)
{
	size_t i;
	int err = 0;

	for (i = 0; i < c->n_objs; i++) {
		int e = kgcov_dump_object(c, &c->objs[i]);

		if (e && !err)
			err = e;
	}
	return err;
}

static void kgcov_reset_client(struct kgcov_client *c)
{
	size_t i;

	for (i = 0; i < c->n_objs; i++) {
		gcov_info_reset(c->objs[i].live);
		gcov_info_reset(c->objs[i].acc);
	}
}

static void kgcov_free_client(struct kgcov_client *c)
{
	size_t i;

	for (i = 0; c->objs && i < c->n_objs; i++)
		if (c->objs[i].acc)
			gcov_info_free(c->objs[i].acc);
	kfree(c->objs);
	kfree(c->dir);
	kfree(c);
}

/* ---- debugfs: /sys/kernel/debug/otto_kgcov/<module>/{dump,reset} -------- */

static ssize_t kgcov_dump_write(struct file *f, const char __user *ubuf, size_t n,
				loff_t *off)
{
	struct kgcov_client *c = f->private_data;
	int err;

	mutex_lock(&kgcov_lock);
	err = kgcov_dump_client(c);
	mutex_unlock(&kgcov_lock);
	return err ? err : n;
}

static ssize_t kgcov_reset_write(struct file *f, const char __user *ubuf, size_t n,
				 loff_t *off)
{
	struct kgcov_client *c = f->private_data;

	mutex_lock(&kgcov_lock);
	kgcov_reset_client(c);
	mutex_unlock(&kgcov_lock);
	return n;
}

static const struct file_operations kgcov_dump_fops = {
	.owner = THIS_MODULE,
	.open = simple_open,
	.write = kgcov_dump_write,
	.llseek = noop_llseek,
};

static const struct file_operations kgcov_reset_fops = {
	.owner = THIS_MODULE,
	.open = simple_open,
	.write = kgcov_reset_write,
	.llseek = noop_llseek,
};

/* ---- the API ------------------------------------------------------------ */

int kgcov_register(struct module *mod, const struct gcov_info *const *begin,
		   const struct gcov_info *const *end, const char *dir)
{
	struct kgcov_client *c;
	const struct gcov_info *const *p;
	size_t n = 0, i = 0;
	int err;

	if (!dir || dir[0] != '/') {
		pr_err("%s: the gcov_dir module parameter must be an absolute path\n",
		       mod->name);
		return -EINVAL;
	}
	for (p = begin; p < end; p++)
		if (*p)
			n++;
	if (!n) {
		pr_err("%s: no .gcov_info entries — built without -fprofile-info-section?\n",
		       mod->name);
		return -ENOENT;
	}
	c = kzalloc(sizeof(*c), GFP_KERNEL);
	if (!c)
		return -ENOMEM;
	c->mod = mod;
	c->objs = kcalloc(n, sizeof(*c->objs), GFP_KERNEL);
	c->dir = kstrdup(dir, GFP_KERNEL);
	if (!c->objs || !c->dir) {
		err = -ENOMEM;
		goto fail;
	}
	mutex_lock(&kgcov_lock);
	for (p = begin; p < end; p++) {
		struct gcov_info *info = (struct gcov_info *)*p;

		if (!info)
			continue;
		if (kgcov_version && gcov_info_version(info) != kgcov_version) {
			pr_err("%s: gcov format %#x differs from the format already registered (%#x); build every consumer and the library with one compiler\n",
			       mod->name, gcov_info_version(info), kgcov_version);
			mutex_unlock(&kgcov_lock);
			err = -EPROTO;
			goto fail;
		}
		c->objs[i].live = info;
		c->objs[i].acc = gcov_info_dup(info);
		if (!c->objs[i].acc) {
			mutex_unlock(&kgcov_lock);
			err = -ENOMEM;
			goto fail;
		}
		gcov_info_reset(c->objs[i].acc);
		i++;
	}
	c->n_objs = n;
	if (!kgcov_version)
		kgcov_version = gcov_info_version(c->objs[0].live);
	list_add(&c->node, &kgcov_clients);
	mutex_unlock(&kgcov_lock);

	c->dent = debugfs_create_dir(mod->name, kgcov_root);
	debugfs_create_file("dump", 0200, c->dent, c, &kgcov_dump_fops);
	debugfs_create_file("reset", 0200, c->dent, c, &kgcov_reset_fops);
	pr_info("%s: %zu instrumented object(s), .gcda under %s\n", mod->name, n, dir);
	return 0;
fail:
	c->n_objs = i;
	kgcov_free_client(c);
	return err;
}
EXPORT_SYMBOL_GPL(kgcov_register);

void kgcov_unregister(struct module *mod)
{
	struct kgcov_client *c, *found = NULL;

	mutex_lock(&kgcov_lock);
	list_for_each_entry(c, &kgcov_clients, node) {
		if (c->mod == mod) {
			found = c;
			break;
		}
	}
	if (found) {
		/* The exit dump: everything the exit routine ran before this call. */
		kgcov_dump_client(found);
		list_del(&found->node);
	}
	mutex_unlock(&kgcov_lock);
	if (!found)
		return;
	/* Outside the lock: a writer blocked on the lock must be able to finish. */
	debugfs_remove_recursive(found->dent);
	kgcov_free_client(found);
}
EXPORT_SYMBOL_GPL(kgcov_unregister);

static int __init kgcov_init(void)
{
	kgcov_root = debugfs_create_dir("otto_kgcov", NULL);
	return 0;
}

static void __exit kgcov_exit(void)
{
	/* No client can be left: every consumer holds a reference to this module. */
	debugfs_remove_recursive(kgcov_root);
}

module_init(kgcov_init);
module_exit(kgcov_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("gcov runtime for out-of-tree modules on a kernel without CONFIG_GCOV_KERNEL");
MODULE_AUTHOR("otto");
