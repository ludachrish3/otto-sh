// SPDX-License-Identifier: GPL-2.0
/*
 * otto_kmodcov: the runtime behind kmodcov.h. One client per registered module,
 * filled by the consumer's own gcov constructors, which kmodcov_register()
 * runs; each client owns an ACCUMULATOR per instrumented object (a private
 * gcov_info copy). A dump adds the live counters into the accumulator,
 * zeroes the live ones and writes the accumulator out, so two dumps of one
 * load never double count.
 *
 * Counts also accumulate across loads: each accumulator is seeded once per
 * load from the object's .gcda on disk, at the object's first dump and
 * before its write, so the file holds the total of every load since the
 * last clean. An object is PENDING at load, SEEDED once its file is
 * imported (or there is none) and SKIPPED after a reset or a file that
 * does not match it; only a pending object reads its file. The backend
 * checks the whole file against the accumulator before adding any of it
 * (gcov_info_import()); a file that does not match is warned about once
 * and overwritten. A file that cannot be read (an I/O or allocation
 * failure) is left as it is by a sysfs dump, which keeps the counts in
 * memory and fails; the unload's dump retries the read once and, if it
 * still fails, writes this load's counts and warns that the earlier loads'
 * counts are lost. A reset zeroes the live counters and the accumulators
 * and skips every object, so a reset followed by deleting the files never
 * reads them back, whatever dump runs between the two.
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
#include <linux/kobject.h>
#include <linux/list.h>
#include <linux/module.h>
#include <linux/sched.h>
#include <linux/string.h>
#include <linux/sysfs.h>

#include "kmodcov.h"
#include "kmodcov_gcov.h"
#include "kmodcov_version.h"

/* Where an object's accumulator stands with its file on disk, this load. */
enum kmodcov_seed {
	KMODCOV_PENDING, /* not read yet: the next dump imports the file first */
	KMODCOV_SEEDED,  /* imported, or there was no file */
	KMODCOV_SKIPPED, /* reset, or the file did not match: never read */
};

struct kmodcov_object {
	struct gcov_info *live; /* the consumer's own, in its .data/.bss */
	struct gcov_info *acc;  /* private total since register / reset, plus the seed */
	enum kmodcov_seed seed;
	bool read_warned; /* a sysfs dump has said the file could not be read */
};

struct kmodcov_client {
	struct list_head node;
	struct module *mod;
	char *dir;
	size_t n_objs;
	struct kmodcov_object *objs;
	size_t cap; /* objs' capacity: one per constructor between the sentinels */
	int err; /* first failure while the constructors ran */
	struct kobject *kobj; /* /sys/module/<mod>/kmodcov; set under kmodcov_lock when published */
};

static LIST_HEAD(kmodcov_clients);
KMODCOV_DEFINE_LOCK(kmodcov_lock);
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

/*
 * Read at most CAP bytes of PATH into BUF, *LEN being how many it held: 0,
 * or the negative errno opening or reading failed with (-ENOENT: no file).
 */
static int kmodcov_read_file(const char *path, char *buf, size_t cap, size_t *len)
{
	struct file *f;
	loff_t pos = 0;
	int err = 0;

	*len = 0;
	f = filp_open(path, O_RDONLY, 0);
	if (IS_ERR(f))
		return PTR_ERR(f);
	while (*len < cap) {
		ssize_t r = KMODCOV_FILE_READ(f, buf + *len, cap - *len, &pos);

		if (r < 0) {
			err = r;
			break;
		}
		if (!r)
			break;
		*len += r;
	}
	filp_close(f, NULL);
	return err;
}

/* ---- dump / reset (caller holds kmodcov_lock) ----------------------------- */

/*
 * Seed a pending object's accumulator from PATH. 0 once the object is
 * seeded (imported, or no file) or skipped (the file does not match it);
 * the negative errno of an operational failure leaves it pending.
 */
static int kmodcov_seed_object(struct kmodcov_client *c, struct kmodcov_object *o,
			       const char *path)
{
	char why[96];
	char *buf;
	size_t cap, len;
	int err;

	/* One byte past this object's own image: a longer file cannot be one. */
	cap = convert_to_gcda(NULL, o->acc) + 1;
	buf = KMODCOV_BIG_ALLOC(cap);
	if (!buf)
		return -ENOMEM;
	err = kmodcov_read_file(path, buf, cap, &len);
	if (err == -ENOENT) {
		o->seed = KMODCOV_SEEDED;
		err = 0;
	} else if (!err) {
		if (len == cap) {
			snprintf(why, sizeof(why), "longer than this object's %zu bytes", cap - 1);
			err = -EINVAL;
		} else {
			err = gcov_info_import(o->acc, buf, len, why, sizeof(why));
		}
		if (err)
			pr_warn("%s: %s does not match this object (%s); not adding its counts, and this dump overwrites it\n",
				c->mod->name, path, why);
		o->seed = err ? KMODCOV_SKIPPED : KMODCOV_SEEDED;
		err = 0;
	}
	KMODCOV_BIG_FREE(buf);
	return err;
}

/*
 * FINAL is the unload's dump, the last chance: a file that still cannot be
 * read is overwritten with this load's counts. Any other dump leaves it be.
 */
static int kmodcov_dump_object(struct kmodcov_client *c, struct kmodcov_object *o, bool final)
{
	char *path, *buf;
	size_t size;
	int err;

	/* gcov_info_filename() is the absolute .gcda path the compiler baked in. */
	path = KMODCOV_ASPRINTF("%s%s%s", c->dir,
			      gcov_info_filename(o->acc)[0] == '/' ? "" : "/",
			      gcov_info_filename(o->acc));
	if (!path)
		return -ENOMEM;
	err = o->seed == KMODCOV_PENDING ? kmodcov_seed_object(c, o, path) : 0;
	gcov_info_add(o->acc, o->live);
	gcov_info_reset(o->live);
	if (err && !final) {
		if (!o->read_warned)
			pr_warn("%s: cannot read %s (%d); its counts stay in memory, and the file is left as it is until a dump can read it\n",
				c->mod->name, path, err);
		o->read_warned = true;
		KMODCOV_FREE(path);
		return err;
	}
	if (err)
		pr_warn("%s: cannot read %s (%d) at unload; writing this load's counts, so the earlier loads' counts in that file are lost\n",
			c->mod->name, path, err);
	size = convert_to_gcda(NULL, o->acc);
	buf = KMODCOV_BIG_ALLOC(size);
	if (!buf) {
		KMODCOV_FREE(path);
		return -ENOMEM;
	}
	convert_to_gcda(buf, o->acc);
	err = kmodcov_mkdir_parents(path);
	if (!err)
		err = kmodcov_write_file(path, buf, size);
	if (err)
		pr_err("%s: writing %s failed: %d\n", c->mod->name, path, err);
	KMODCOV_FREE(path);
	KMODCOV_BIG_FREE(buf);
	return err;
}

static int kmodcov_dump_client(struct kmodcov_client *c, bool final)
{
	size_t i;
	int err = 0;

	for (i = 0; i < c->n_objs; i++) {
		int e = kmodcov_dump_object(c, &c->objs[i], final);

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
		c->objs[i].seed = KMODCOV_SKIPPED;
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

/* ---- sysfs: /sys/module/<module>/kmodcov/{dump,reset} -------------------- */

/*
 * The client whose directory is KOBJ, or NULL once unregister has unlisted
 * it. Caller holds kmodcov_lock.
 */
static struct kmodcov_client *kmodcov_client_of(struct kobject *kobj)
{
	struct kmodcov_client *c;

	list_for_each_entry(c, &kmodcov_clients, node)
		if (c->kobj == kobj)
			return c;
	return NULL;
}

static ssize_t kmodcov_dump_store(struct kobject *kobj, struct kobj_attribute *attr,
				  const char *buf, size_t count)
{
	struct kmodcov_client *c;
	int err;

	KMODCOV_LOCK(&kmodcov_lock);
	c = kmodcov_client_of(kobj);
	err = c ? kmodcov_dump_client(c, false) : -ENODEV;
	KMODCOV_UNLOCK(&kmodcov_lock);
	return err ? err : count;
}

static ssize_t kmodcov_reset_store(struct kobject *kobj, struct kobj_attribute *attr,
				   const char *buf, size_t count)
{
	struct kmodcov_client *c;

	KMODCOV_LOCK(&kmodcov_lock);
	c = kmodcov_client_of(kobj);
	if (c)
		kmodcov_reset_client(c);
	KMODCOV_UNLOCK(&kmodcov_lock);
	return c ? count : -ENODEV;
}

/* __ATTR rather than __ATTR_WO: 2.6.32 has only the former. */
static struct kobj_attribute kmodcov_dump_attr = __ATTR(dump, 0200, NULL, kmodcov_dump_store);
static struct kobj_attribute kmodcov_reset_attr = __ATTR(reset, 0200, NULL, kmodcov_reset_store);

static struct attribute *kmodcov_attrs[] = {
	&kmodcov_dump_attr.attr,
	&kmodcov_reset_attr.attr,
	NULL,
};

static const struct attribute_group kmodcov_group = {
	.attrs = kmodcov_attrs,
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
	c->objs[c->n_objs].seed = KMODCOV_PENDING;
	c->n_objs++;
}

int kmodcov_register(struct module *mod, const kmodcov_ctor_fn *begin,
		   const kmodcov_ctor_fn *end, const char *dir)
{
	struct kmodcov_client *c, *other;
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
	list_for_each_entry(other, &kmodcov_clients, node) {
		if (other->mod == mod) {
			KMODCOV_UNLOCK(&kmodcov_lock);
			pr_err("%s: already registered — KMODCOV_INIT() runs once per load\n",
			       mod->name);
			err = -EBUSY;
			goto fail;
		}
	}
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
	/*
	 * Published and given its files under the lock, so a store finds a
	 * fully built client or none. The module kobject exists: the loader
	 * sets it up before the init routine that brought us here.
	 */
	c->kobj = KMODCOV_SYSFS_DIR("kmodcov", &mod->mkobj.kobj);
	err = c->kobj ? KMODCOV_SYSFS_GROUP(c->kobj, &kmodcov_group) : -ENOMEM;
	if (err) {
		list_del(&c->node);
		KMODCOV_UNLOCK(&kmodcov_lock);
		pr_err("%s: cannot create /sys/module/%s/kmodcov (%d)\n", mod->name, mod->name,
		       err);
		if (c->kobj)
			KMODCOV_SYSFS_DIR_PUT(c->kobj);
		goto fail;
	}
	KMODCOV_UNLOCK(&kmodcov_lock);
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
		kmodcov_dump_client(found, true);
		list_del(&found->node);
	}
	KMODCOV_UNLOCK(&kmodcov_lock);
	if (!found)
		return;
	/*
	 * Outside the lock: sysfs waits here for a store in flight, and that
	 * store may be waiting for the lock. It then finds no client (-ENODEV).
	 */
	KMODCOV_SYSFS_GROUP_REMOVE(found->kobj, &kmodcov_group);
	KMODCOV_SYSFS_DIR_PUT(found->kobj);
	kmodcov_free_client(found);
}
EXPORT_SYMBOL_GPL(kmodcov_unregister);

#define KMODCOV_STR_(x) #x
#define KMODCOV_STR(x) KMODCOV_STR_(x)
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("gcov runtime for out-of-tree modules on a kernel without CONFIG_GCOV_KERNEL");
MODULE_AUTHOR("otto");
MODULE_VERSION(KMODCOV_OTTO_VERSION "+kmodcov" KMODCOV_STR(KMODCOV_INTERFACE));
