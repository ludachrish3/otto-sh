// SPDX-License-Identifier: GPL-2.0
/*
 * otto_kgcov: the runtime behind kgcov.h. One client per registered module,
 * filled by the consumer's own gcov constructors, which kgcov_register()
 * runs; each client owns an ACCUMULATOR per instrumented object (a private
 * gcov_info copy). A dump adds the live counters into the accumulator,
 * zeroes the live ones and writes the accumulator out, so the file on disk
 * is always the module's total since register (or the last reset) and two
 * dumps never double count — no .gcda is ever parsed.
 */
/*
 * Kbuild force-includes a kgcov_local.h before this file when one exists, and
 * such a header pulls kernel headers of its own, so printk.h's own pr_fmt may
 * already be defined here. Undefine it first: without one, this redefinition
 * warns.
 */
#undef pr_fmt
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/fs.h>
#include <linux/list.h>
#include <linux/module.h>
#include <linux/namei.h>
#include <linux/sched.h>
#include <linux/string.h>
#include <linux/uaccess.h>

#include "kgcov.h"
#include "kgcov_gcov.h"
#include "kgcov_version.h"

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
	size_t cap; /* objs' capacity: one per constructor between the sentinels */
	int err; /* first failure while the constructors ran */
	struct dentry *dent;
};

static LIST_HEAD(kgcov_clients);
KGCOV_DEFINE_LOCK(kgcov_lock);
static struct dentry *kgcov_root;
static unsigned int kgcov_version; /* gcov format of the first consumer; 0 = none yet */

/* The registration in progress and the thread running it: set under kgcov_lock for the walk's duration. */
static struct kgcov_client *kgcov_registering;
static struct task_struct *kgcov_registering_task; /* the thread running the walk; a constructor from any other thread registers nothing */

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

	buf = KGCOV_STRDUP(file_path);
	if (!buf)
		return -ENOMEM;
	for (p = buf + 1; *p && !err; p++) {
		if (*p != '/')
			continue;
		*p = '\0';
		err = kgcov_mkdir(buf);
		*p = '/';
	}
	KGCOV_FREE(buf);
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
	buf = KGCOV_BIG_ALLOC(size);
	if (!buf)
		return -ENOMEM;
	convert_to_gcda(buf, o->acc);
	/* gcov_info_filename() is the absolute .gcda path the compiler baked in. */
	path = KGCOV_ASPRINTF("%s%s%s", c->dir,
			      gcov_info_filename(o->acc)[0] == '/' ? "" : "/",
			      gcov_info_filename(o->acc));
	if (!path) {
		KGCOV_BIG_FREE(buf);
		return -ENOMEM;
	}
	err = kgcov_mkdir_parents(path);
	if (!err)
		err = kgcov_write_file(path, buf, size);
	if (err)
		pr_err("%s: writing %s failed: %d\n", c->mod->name, path, err);
	KGCOV_FREE(path);
	KGCOV_BIG_FREE(buf);
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

	for (i = 0; c->objs && i < c->n_objs; i++) {
		if (c->objs[i].acc)
			gcov_info_free(c->objs[i].acc);
		gcov_info_forget(c->objs[i].live);
	}
	KGCOV_FREE(c->objs);
	KGCOV_FREE(c->dir);
	KGCOV_FREE(c);
}

/* ---- debugfs: /sys/kernel/debug/otto_kgcov/<module>/{dump,reset} -------- */

static ssize_t kgcov_dump_write(struct file *f, const char __user *ubuf, size_t n,
				loff_t *off)
{
	struct kgcov_client *c = f->private_data;
	int err;

	KGCOV_LOCK(&kgcov_lock);
	err = kgcov_dump_client(c);
	KGCOV_UNLOCK(&kgcov_lock);
	return err ? err : n;
}

static ssize_t kgcov_reset_write(struct file *f, const char __user *ubuf, size_t n,
				 loff_t *off)
{
	struct kgcov_client *c = f->private_data;

	KGCOV_LOCK(&kgcov_lock);
	kgcov_reset_client(c);
	KGCOV_UNLOCK(&kgcov_lock);
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

void kgcov_ctor_info(struct gcov_info *info)
{
	struct kgcov_client *c = kgcov_registering_task == current ? kgcov_registering : NULL;
	struct gcov_info *acc;
	size_t i;
	int have, want;

	if (!c || c->err) {
		/*
		 * No registration in progress on THIS thread — the kernel's own
		 * constructor pass on a CONFIG_CONSTRUCTORS kernel, possibly for
		 * another module while a walk is in flight elsewhere — or one that
		 * already failed: not ours to keep.
		 */
		gcov_info_forget(info);
		return;
	}
	for (i = 0; i < c->n_objs; i++)
		if (c->objs[i].live == info)
			return; /* the same unit twice in one walk */
	if (!gcov_info_built_by_this_compiler(info, c->mod->name, &have, &want)) {
		/*
		 * The unit's own filename pointer is NOT read here: this is the
		 * path where the backend decided it cannot parse *info, so every
		 * field past the version word may sit at another offset than this
		 * build expects. The raw version word and the module name are all
		 * that can be trusted, and they are enough to name the culprit.
		 */
		pr_err("%s: a unit with gcov version word %#x was compiled by gcc %d, otto_kgcov by gcc %d; build both with one compiler major\n",
		       c->mod->name, gcov_info_version(info), have, want);
		c->err = -EPROTO;
		gcov_info_forget(info);
		return;
	}
	if (kgcov_version && gcov_info_version(info) != kgcov_version) {
		pr_err("%s: gcov format %#x differs from the format already registered (%#x); build every consumer and the library with one compiler\n",
		       c->mod->name, gcov_info_version(info), kgcov_version);
		c->err = -EPROTO;
		gcov_info_forget(info);
		return;
	}
	if (c->n_objs == c->cap) {
		pr_err("%s: more gcov units than constructors between the sentinels\n",
		       c->mod->name);
		c->err = -EOVERFLOW;
		gcov_info_forget(info);
		return;
	}
	acc = gcov_info_dup(info);
	if (!acc) {
		c->err = -ENOMEM;
		gcov_info_forget(info);
		return;
	}
	gcov_info_reset(acc);
	c->objs[c->n_objs].live = info;
	c->objs[c->n_objs].acc = acc;
	c->n_objs++;
}

int kgcov_register(struct module *mod, const kgcov_ctor_fn *begin,
		   const kgcov_ctor_fn *end, const char *dir)
{
	struct kgcov_client *c;
	const kgcov_ctor_fn *p;
	int err;

	if (!dir || dir[0] != '/') {
		pr_err("%s: the gcov_dir module parameter must be an absolute path\n",
		       mod->name);
		return -EINVAL;
	}
	if (end <= begin) {
		pr_err("%s: nothing between the kgcov sentinels — are kgcov_begin.o and kgcov_end.o first and last in the link?\n",
		       mod->name);
		return -ENOENT;
	}
	c = KGCOV_ALLOC(sizeof(*c));
	if (!c)
		return -ENOMEM;
	c->mod = mod;
	c->cap = end - begin;
	c->objs = KGCOV_ALLOC_ARRAY(c->cap, sizeof(*c->objs));
	c->dir = KGCOV_STRDUP(dir);
	if (!c->objs || !c->dir) {
		err = -ENOMEM;
		goto fail;
	}
	KGCOV_LOCK(&kgcov_lock);
	kgcov_registering = c;
	kgcov_registering_task = current;
	for (p = begin; p < end; p++)
		if (*p)
			(*p)();
	kgcov_registering_task = NULL;
	kgcov_registering = NULL;
	err = c->err;
	if (!err && !c->n_objs) {
		pr_err("%s: the constructors registered no gcov data — built without $(KGCOV_CFLAGS)?\n",
		       mod->name);
		err = -ENOENT;
	}
	if (err) {
		KGCOV_UNLOCK(&kgcov_lock);
		goto fail;
	}
	if (!kgcov_version)
		kgcov_version = gcov_info_version(c->objs[0].live);
	list_add(&c->node, &kgcov_clients);
	KGCOV_UNLOCK(&kgcov_lock);

	c->dent = KGCOV_DEBUGFS_DIR(mod->name, kgcov_root);
	KGCOV_DEBUGFS_FILE("dump", 0200, c->dent, c, &kgcov_dump_fops);
	KGCOV_DEBUGFS_FILE("reset", 0200, c->dent, c, &kgcov_reset_fops);
	pr_info("%s: %zu instrumented object(s), .gcda under %s\n", mod->name, c->n_objs, dir);
	return 0;
fail:
	kgcov_free_client(c);
	return err;
}
EXPORT_SYMBOL_GPL(kgcov_register);

void kgcov_unregister(struct module *mod)
{
	struct kgcov_client *c, *found = NULL;

	KGCOV_LOCK(&kgcov_lock);
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
	KGCOV_UNLOCK(&kgcov_lock);
	if (!found)
		return;
	/* Outside the lock: a writer blocked on the lock must be able to finish. */
	KGCOV_DEBUGFS_REMOVE(found->dent);
	kgcov_free_client(found);
}
EXPORT_SYMBOL_GPL(kgcov_unregister);

static int __init kgcov_init(void)
{
	kgcov_root = KGCOV_DEBUGFS_DIR("otto_kgcov", NULL);
	return 0;
}

static void __exit kgcov_exit(void)
{
	/* No client can be left: every consumer holds a reference to this module. */
	KGCOV_DEBUGFS_REMOVE(kgcov_root);
}

module_init(kgcov_init);
module_exit(kgcov_exit);
#define KGCOV_STR_(x) #x
#define KGCOV_STR(x) KGCOV_STR_(x)
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("gcov runtime for out-of-tree modules on a kernel without CONFIG_GCOV_KERNEL");
MODULE_AUTHOR("otto");
MODULE_VERSION(KGCOV_OTTO_VERSION "+kgcov" KGCOV_STR(KGCOV_INTERFACE));
