// SPDX-License-Identifier: GPL-2.0
/*
 * otto_kmodcov: the runtime behind kmodcov.h. One client per registered module,
 * filled by the consumer's own gcov constructors, which kmodcov_register()
 * runs; each client owns an ACCUMULATOR per instrumented object (a private
 * gcov_info copy). A dump adds the live counters into the accumulator,
 * zeroes the live ones and writes the accumulator out, so the file on disk
 * is always the module's total since register (or the last reset) and two
 * dumps never double count — no .gcda is ever parsed.
 */
/*
 * Kbuild force-includes a kmodcov_local.h before this file when one exists, and
 * such a header pulls kernel headers of its own, so printk.h's own pr_fmt may
 * already be defined here. Undefine it first: without one, this redefinition
 * warns.
 */
#undef pr_fmt
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/fs.h>
#include <linux/list.h>
#include <linux/module.h>
#include <linux/sched.h>
#include <linux/string.h>

#include "kmodcov.h"
#include "kmodcov_gcov.h"
#include "kmodcov_version.h"

struct kmodcov_object {
	struct gcov_info *live; /* the consumer's own, in its .data/.bss */
	struct gcov_info *acc;  /* private total since register / reset */
};

struct kmodcov_client {
	struct list_head node;
	struct module *mod;
	char *dir;
	size_t n_objs;
	struct kmodcov_object *objs;
	size_t cap; /* objs' capacity: one per constructor between the sentinels */
	int err; /* first failure while the constructors ran */
	struct dentry *dent;
};

static LIST_HEAD(kmodcov_clients);
KMODCOV_DEFINE_LOCK(kmodcov_lock);
static struct dentry *kmodcov_root;
static unsigned int kmodcov_version; /* gcov format of the first consumer; 0 = none yet */

/* The registration in progress and the thread running it: set under kmodcov_lock for the walk's duration. */
static struct kmodcov_client *kmodcov_registering;
static struct task_struct *kmodcov_registering_task; /* the thread running the walk; a constructor from any other thread registers nothing */

/* ---- file I/O ---------------------------------------------------------- */

static int kmodcov_mkdir(const char *path)
{
	int err = KMODCOV_MKDIR(path);

	return err == -EEXIST ? 0 : err;
}

/* mkdir -p for every directory component of an absolute file path. */
static int kmodcov_mkdir_parents(const char *file_path)
{
	char *buf, *p;
	int err = 0;

	buf = KMODCOV_STRDUP(file_path);
	if (!buf)
		return -ENOMEM;
	for (p = buf + 1; *p && !err; p++) {
		if (*p != '/')
			continue;
		*p = '\0';
		err = kmodcov_mkdir(buf);
		*p = '/';
	}
	KMODCOV_FREE(buf);
	return err;
}

static int kmodcov_write_file(const char *path, const char *buf, size_t len)
{
	struct file *f;
	loff_t pos = 0;
	int err = 0;

	f = filp_open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
	if (IS_ERR(f))
		return PTR_ERR(f);
	while (len) {
		ssize_t w = KMODCOV_FILE_WRITE(f, buf, len, &pos);

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

/* ---- dump / reset (caller holds kmodcov_lock) ----------------------------- */

static int kmodcov_dump_object(struct kmodcov_client *c, struct kmodcov_object *o)
{
	char *path, *buf;
	size_t size;
	int err;

	gcov_info_add(o->acc, o->live);
	gcov_info_reset(o->live);
	size = convert_to_gcda(NULL, o->acc);
	buf = KMODCOV_BIG_ALLOC(size);
	if (!buf)
		return -ENOMEM;
	convert_to_gcda(buf, o->acc);
	/* gcov_info_filename() is the absolute .gcda path the compiler baked in. */
	path = KMODCOV_ASPRINTF("%s%s%s", c->dir,
			      gcov_info_filename(o->acc)[0] == '/' ? "" : "/",
			      gcov_info_filename(o->acc));
	if (!path) {
		KMODCOV_BIG_FREE(buf);
		return -ENOMEM;
	}
	err = kmodcov_mkdir_parents(path);
	if (!err)
		err = kmodcov_write_file(path, buf, size);
	if (err)
		pr_err("%s: writing %s failed: %d\n", c->mod->name, path, err);
	KMODCOV_FREE(path);
	KMODCOV_BIG_FREE(buf);
	return err;
}

static int kmodcov_dump_client(struct kmodcov_client *c)
{
	size_t i;
	int err = 0;

	for (i = 0; i < c->n_objs; i++) {
		int e = kmodcov_dump_object(c, &c->objs[i]);

		if (e && !err)
			err = e;
	}
	return err;
}

static void kmodcov_reset_client(struct kmodcov_client *c)
{
	size_t i;

	for (i = 0; i < c->n_objs; i++) {
		gcov_info_reset(c->objs[i].live);
		gcov_info_reset(c->objs[i].acc);
	}
}

static void kmodcov_free_client(struct kmodcov_client *c)
{
	size_t i;

	for (i = 0; c->objs && i < c->n_objs; i++) {
		if (c->objs[i].acc)
			gcov_info_free(c->objs[i].acc);
		gcov_info_forget(c->objs[i].live);
	}
	KMODCOV_FREE(c->objs);
	KMODCOV_FREE(c->dir);
	KMODCOV_FREE(c);
}

/* ---- debugfs: /sys/kernel/debug/otto_kmodcov/<module>/{dump,reset} -------- */

static ssize_t kmodcov_dump_write(struct file *f, const char __user *ubuf, size_t n,
				loff_t *off)
{
	struct kmodcov_client *c = f->private_data;
	int err;

	KMODCOV_LOCK(&kmodcov_lock);
	err = kmodcov_dump_client(c);
	KMODCOV_UNLOCK(&kmodcov_lock);
	return err ? err : n;
}

static ssize_t kmodcov_reset_write(struct file *f, const char __user *ubuf, size_t n,
				 loff_t *off)
{
	struct kmodcov_client *c = f->private_data;

	KMODCOV_LOCK(&kmodcov_lock);
	kmodcov_reset_client(c);
	KMODCOV_UNLOCK(&kmodcov_lock);
	return n;
}

static const struct file_operations kmodcov_dump_fops = {
	.owner = THIS_MODULE,
	.open = KMODCOV_FOPS_OPEN,
	.write = kmodcov_dump_write,
	.llseek = KMODCOV_LLSEEK,
};

static const struct file_operations kmodcov_reset_fops = {
	.owner = THIS_MODULE,
	.open = KMODCOV_FOPS_OPEN,
	.write = kmodcov_reset_write,
	.llseek = KMODCOV_LLSEEK,
};

/* ---- the API ------------------------------------------------------------ */

void kmodcov_ctor_info(struct gcov_info *info)
{
	struct kmodcov_client *c = kmodcov_registering_task == current ? kmodcov_registering : NULL;
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
		pr_err("%s: a unit with gcov version word %#x was compiled by gcc %d, otto_kmodcov by gcc %d; build both with one compiler major\n",
		       c->mod->name, gcov_info_version(info), have, want);
		c->err = -EPROTO;
		gcov_info_forget(info);
		return;
	}
	if (kmodcov_version && gcov_info_version(info) != kmodcov_version) {
		pr_err("%s: gcov format %#x differs from the format already registered (%#x); build every consumer and the library with one compiler\n",
		       c->mod->name, gcov_info_version(info), kmodcov_version);
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

int kmodcov_register(struct module *mod, const kmodcov_ctor_fn *begin,
		   const kmodcov_ctor_fn *end, const char *dir)
{
	struct kmodcov_client *c;
	const kmodcov_ctor_fn *p;
	int err;

	if (!dir || dir[0] != '/') {
		pr_err("%s: the cov_dir module parameter must be an absolute path\n",
		       mod->name);
		return -EINVAL;
	}
	if (end <= begin) {
		pr_err("%s: nothing between the kmodcov sentinels — are kmodcov_begin.o and kmodcov_end.o first and last in the link?\n",
		       mod->name);
		return -ENOENT;
	}
	c = KMODCOV_ALLOC(sizeof(*c));
	if (!c)
		return -ENOMEM;
	c->mod = mod;
	c->cap = end - begin;
	c->objs = KMODCOV_ALLOC_ARRAY(c->cap, sizeof(*c->objs));
	c->dir = KMODCOV_STRDUP(dir);
	if (!c->objs || !c->dir) {
		err = -ENOMEM;
		goto fail;
	}
	KMODCOV_LOCK(&kmodcov_lock);
	kmodcov_registering = c;
	kmodcov_registering_task = current;
	for (p = begin; p < end; p++)
		if (*p)
			(*p)();
	kmodcov_registering_task = NULL;
	kmodcov_registering = NULL;
	err = c->err;
	if (!err && !c->n_objs) {
		pr_err("%s: the constructors registered no gcov data — built without $(KMODCOV_CFLAGS)?\n",
		       mod->name);
		err = -ENOENT;
	}
	if (err) {
		KMODCOV_UNLOCK(&kmodcov_lock);
		goto fail;
	}
	if (!kmodcov_version)
		kmodcov_version = gcov_info_version(c->objs[0].live);
	list_add(&c->node, &kmodcov_clients);
	KMODCOV_UNLOCK(&kmodcov_lock);

	c->dent = KMODCOV_DEBUGFS_DIR(mod->name, kmodcov_root);
	KMODCOV_DEBUGFS_FILE("dump", 0200, c->dent, c, &kmodcov_dump_fops);
	KMODCOV_DEBUGFS_FILE("reset", 0200, c->dent, c, &kmodcov_reset_fops);
	pr_info("%s: %zu instrumented object(s), .gcda under %s\n", mod->name, c->n_objs, dir);
	return 0;
fail:
	kmodcov_free_client(c);
	return err;
}
EXPORT_SYMBOL_GPL(kmodcov_register);

void kmodcov_unregister(struct module *mod)
{
	struct kmodcov_client *c, *found = NULL;

	KMODCOV_LOCK(&kmodcov_lock);
	list_for_each_entry(c, &kmodcov_clients, node) {
		if (c->mod == mod) {
			found = c;
			break;
		}
	}
	if (found) {
		/* The exit dump: everything the exit routine ran before this call. */
		kmodcov_dump_client(found);
		list_del(&found->node);
	}
	KMODCOV_UNLOCK(&kmodcov_lock);
	if (!found)
		return;
	/* Outside the lock: a writer blocked on the lock must be able to finish. */
	KMODCOV_DEBUGFS_REMOVE(found->dent);
	kmodcov_free_client(found);
}
EXPORT_SYMBOL_GPL(kmodcov_unregister);

static int __init kmodcov_init(void)
{
	kmodcov_root = KMODCOV_DEBUGFS_DIR("otto_kmodcov", NULL);
	return 0;
}

static void __exit kmodcov_exit(void)
{
	/* No client can be left: every consumer holds a reference to this module. */
	KMODCOV_DEBUGFS_REMOVE(kmodcov_root);
}

module_init(kmodcov_init);
module_exit(kmodcov_exit);
#define KMODCOV_STR_(x) #x
#define KMODCOV_STR(x) KMODCOV_STR_(x)
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("gcov runtime for out-of-tree modules on a kernel without CONFIG_GCOV_KERNEL");
MODULE_AUTHOR("otto");
MODULE_VERSION(KMODCOV_OTTO_VERSION "+kmodcov" KMODCOV_STR(KMODCOV_INTERFACE));
