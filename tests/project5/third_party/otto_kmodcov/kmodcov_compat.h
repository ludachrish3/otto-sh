/* SPDX-License-Identifier: GPL-2.0 */
/*
 * otto_kmodcov's kernel compatibility ladder, and the whole override surface.
 *
 * Every kernel-facing name the library uses is a KMODCOV_ macro, defined here
 * only when nothing defined it first. Kbuild force-includes a kmodcov_local.h
 * beside the sources when one exists, ahead of everything else, so a build
 * replaces a name by defining it there, one name at a time — and an
 * overridden name compiles NONE of its default below, which is what lets a
 * distribution kernel with backported APIs (a version number that lies)
 * build against the arm its headers actually have. Each default is chosen
 * by LINUX_VERSION_CODE, one arm per kernel API generation; every arm is
 * either proven by a kernel on each side of its boundary in `make kmodcov`'s
 * kernel set or, for the one window no kernel there covers (2.6.39 to 3.5),
 * marked UNTESTED beside its code, so a user there builds and reads the
 * caveat where it applies, and a kmodcov_local.h override replaces an arm
 * that misbehaves. The supported range, the arm table and the column that
 * proves each arm are on the kernel-modules docs page ("Kernel versions");
 * this header does not restate them.
 *
 * Three requirements the library relies on and never checks, so an override
 * that breaks one shows it only on a rare error path, on the target kernel.
 * KMODCOV_ALLOC and KMODCOV_ALLOC_ARRAY must return ZEROED memory: the client
 * struct's error and object fields are read before anything writes them,
 * and the unwind that frees a half-built object array tests its entries for
 * NULL. KMODCOV_FREE and KMODCOV_BIG_FREE must accept NULL, because those
 * failure paths free structures whose members were never allocated. And
 * whatever KMODCOV_ALLOC, KMODCOV_ALLOC_ARRAY, KMODCOV_STRDUP, KMODCOV_MEMDUP and
 * KMODCOV_ASPRINTF return must be releasable by KMODCOV_FREE, since that is the
 * only thing that frees them — replacing an allocator without its matching
 * free is a wrong-pool free, which corrupts rather than fails.
 */
#ifndef OTTO_KMODCOV_COMPAT_H
#define OTTO_KMODCOV_COMPAT_H

#include <linux/version.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/err.h>
#include <linux/fs.h>
#include <linux/namei.h>
#include <linux/mount.h>
#include <linux/dcache.h>
#include <linux/uaccess.h>
#include <linux/slab.h>
#include <linux/vmalloc.h>
/*
 * kvmalloc()/kvfree() live here up to the kernel that moved them into
 * slab.h (already included above); mm.h keeps declaring both either way.
 */
#include <linux/mm.h>
#include <linux/mutex.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>
#include <linux/list.h>

/* ---- allocation, locks, sysfs: one arm each, except the big pair ------- */

#ifndef KMODCOV_ALLOC
#define KMODCOV_ALLOC(size) kzalloc((size), GFP_KERNEL)
#endif
#ifndef KMODCOV_ALLOC_ARRAY
#define KMODCOV_ALLOC_ARRAY(n, size) kcalloc((n), (size), GFP_KERNEL)
#endif
#ifndef KMODCOV_STRDUP
#define KMODCOV_STRDUP(s) kstrdup((s), GFP_KERNEL)
#endif
#ifndef KMODCOV_MEMDUP
#define KMODCOV_MEMDUP(p, size) kmemdup((p), (size), GFP_KERNEL)
#endif
#ifndef KMODCOV_ASPRINTF
#define KMODCOV_ASPRINTF(...) kasprintf(GFP_KERNEL, __VA_ARGS__)
#endif
#ifndef KMODCOV_FREE
#define KMODCOV_FREE(p) kfree(p)
#endif
/* kvmalloc arrived in 4.12 (kvfree alone in 3.15, and a pair must match). */
#ifndef KMODCOV_BIG_ALLOC
#if LINUX_VERSION_CODE < KERNEL_VERSION(4, 12, 0)
#define KMODCOV_BIG_ALLOC(size) vmalloc(size)
#else
#define KMODCOV_BIG_ALLOC(size) kvmalloc((size), GFP_KERNEL)
#endif
#endif
#ifndef KMODCOV_BIG_FREE
#if LINUX_VERSION_CODE < KERNEL_VERSION(4, 12, 0)
#define KMODCOV_BIG_FREE(p) vfree(p)
#else
#define KMODCOV_BIG_FREE(p) kvfree(p)
#endif
#endif
#ifndef KMODCOV_DEFINE_LOCK
#define KMODCOV_DEFINE_LOCK(name) static DEFINE_MUTEX(name)
#endif
#ifndef KMODCOV_LOCK
#define KMODCOV_LOCK(l) mutex_lock(l)
#endif
#ifndef KMODCOV_UNLOCK
#define KMODCOV_UNLOCK(l) mutex_unlock(l)
#endif
/*
 * The control files: /sys/module/<module>/kmodcov/{dump,reset}, a child of
 * the consumer's own module kobject. These four signatures are the same from
 * 2.6.32 through 6.17, so no arm; the names exist for the override contract.
 * KMODCOV_SYSFS_DIR returns NULL on failure; KMODCOV_SYSFS_GROUP returns 0
 * or a negative errno; KMODCOV_SYSFS_GROUP_REMOVE must wait for a store in
 * flight before it returns, as the default does.
 */
#ifndef KMODCOV_SYSFS_DIR
#define KMODCOV_SYSFS_DIR(name, parent) kobject_create_and_add((name), (parent))
#endif
#ifndef KMODCOV_SYSFS_DIR_PUT
#define KMODCOV_SYSFS_DIR_PUT(kobj) kobject_put(kobj)
#endif
#ifndef KMODCOV_SYSFS_GROUP
#define KMODCOV_SYSFS_GROUP(kobj, group) sysfs_create_group((kobj), (group))
#endif
#ifndef KMODCOV_SYSFS_GROUP_REMOVE
#define KMODCOV_SYSFS_GROUP_REMOVE(kobj, group) sysfs_remove_group((kobj), (group))
#endif

/* ---- the version ladder ------------------------------------------------ */

/*
 * KMODCOV_MKDIR(path): create the last component of the absolute PATH, whose
 * parents exist. 0 on success, -EEXIST when it already exists (the caller
 * treats that as success), another negative errno otherwise.
 */
#ifndef KMODCOV_MKDIR
#define KMODCOV_MKDIR(path) kmodcov_compat_mkdir(path)
#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 1, 0)
/* Exported GPL by fs/namei.c; declared by dcache.h (included above) on this era too. */
static inline int kmodcov_compat_mkdir(const char *path)
{
	struct nameidata nd;
	struct dentry *d;
	int err;

#if LINUX_VERSION_CODE < KERNEL_VERSION(2, 6, 39)
	err = path_lookup(path, LOOKUP_PARENT, &nd);
#else
	/*
	 * UNTESTED: no kernel in make kmodcov's set lies in 2.6.39 to 3.0. This
	 * is the line above under its 2.6.39 name — path_lookup() with
	 * LOOKUP_PARENT became kern_path_parent() — and every other line of
	 * this arm is proven on 2.6.32. A 3.0 headers column would prove it;
	 * until then, a kmodcov_local.h that defines KMODCOV_MKDIR replaces this
	 * arm outright.
	 */
	err = kern_path_parent(path, &nd);
#endif
	if (err)
		return err;
	/* Takes the parent's i_mutex and holds it even on an error pointer, as mkdirat does. */
	d = lookup_create(&nd, 1);
	if (IS_ERR(d)) {
		err = PTR_ERR(d);
	} else {
		err = mnt_want_write(nd.path.mnt);
		if (!err) {
			err = vfs_mkdir(nd.path.dentry->d_inode, d, 0755);
			mnt_drop_write(nd.path.mnt);
		}
		dput(d);
	}
	mutex_unlock(&nd.path.dentry->d_inode->i_mutex);
	path_put(&nd.path);
	return err;
}
#else
static inline int kmodcov_compat_mkdir(const char *path)
{
	struct path parent;
	struct dentry *d;
	int err;

	d = kern_path_create(AT_FDCWD, path, &parent, LOOKUP_DIRECTORY);
	if (IS_ERR(d))
		return PTR_ERR(d);
#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 6, 0)
	/*
	 * UNTESTED: no kernel in make kmodcov's set lies in 3.1 to 3.5. On these
	 * kernels kern_path_create() takes no write reference and there is no
	 * done_path_create() (both arrive in 3.6), so this block takes the
	 * reference itself and releases what done_path_create() would, exactly
	 * as sys_mkdirat() did then; the vfs_mkdir() call is the 3.6 to 4.0 line,
	 * proven on 3.13. A 3.2 headers column would prove it; until then, a
	 * kmodcov_local.h that defines KMODCOV_MKDIR replaces this arm outright.
	 */
	err = mnt_want_write(parent.mnt);
	if (!err) {
		err = vfs_mkdir(parent.dentry->d_inode, d, 0755);
		mnt_drop_write(parent.mnt);
	}
	dput(d);
	mutex_unlock(&parent.dentry->d_inode->i_mutex);
	path_put(&parent);
#else
#if LINUX_VERSION_CODE < KERNEL_VERSION(4, 1, 0)
	err = vfs_mkdir(parent.dentry->d_inode, d, 0755);
#elif LINUX_VERSION_CODE < KERNEL_VERSION(5, 12, 0)
	err = vfs_mkdir(d_inode(parent.dentry), d, 0755);
#elif LINUX_VERSION_CODE < KERNEL_VERSION(6, 3, 0)
	err = vfs_mkdir(mnt_user_ns(parent.mnt), d_inode(parent.dentry), d, 0755);
#elif LINUX_VERSION_CODE < KERNEL_VERSION(6, 15, 0)
	err = vfs_mkdir(mnt_idmap(parent.mnt), d_inode(parent.dentry), d, 0755);
#else
	/*
	 * vfs_mkdir() returns the dentry now and has already dropped the one
	 * passed in on error; done_path_create() takes the error pointer, the
	 * kernel's own do_mkdirat() pattern.
	 */
	d = vfs_mkdir(mnt_idmap(parent.mnt), d_inode(parent.dentry), d, 0755);
	err = IS_ERR(d) ? PTR_ERR(d) : 0;
#endif
	done_path_create(&parent, d);
#endif
	return err;
}
#endif
#endif

/*
 * KMODCOV_FILE_WRITE(file, buf, len, ppos): write LEN bytes of a kernel buffer
 * at *PPOS, advancing it; the bytes written or a negative errno — the
 * contract kernel_write() has had since 4.14.
 */
#ifndef KMODCOV_FILE_WRITE
#define KMODCOV_FILE_WRITE(file, buf, len, ppos) \
	kmodcov_compat_file_write((file), (buf), (len), (ppos))
#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 9, 0)
static inline ssize_t kmodcov_compat_file_write(struct file *file, const char *buf, size_t len,
					      loff_t *ppos)
{
	mm_segment_t old_fs = get_fs();
	ssize_t w;

	set_fs(KERNEL_DS);
	w = vfs_write(file, (const char __user __force *)buf, len, ppos);
	set_fs(old_fs);
	return w;
}
#elif LINUX_VERSION_CODE < KERNEL_VERSION(4, 14, 0)
static inline ssize_t kmodcov_compat_file_write(struct file *file, const char *buf, size_t len,
					      loff_t *ppos)
{
	ssize_t w = kernel_write(file, buf, len, *ppos); /* the offset by value */

	if (w > 0)
		*ppos += w;
	return w;
}
#else
static inline ssize_t kmodcov_compat_file_write(struct file *file, const char *buf, size_t len,
					      loff_t *ppos)
{
	return kernel_write(file, buf, len, ppos);
}
#endif
#endif

/* KMODCOV_WITHIN_MODULE(addr, mod): true when ADDR lies in MOD's core or init range. */
#ifndef KMODCOV_WITHIN_MODULE
#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 17, 0)
#define KMODCOV_WITHIN_MODULE(addr, mod)                                         \
	(within_module_core((addr), (mod)) || within_module_init((addr), (mod)))
#else
#define KMODCOV_WITHIN_MODULE(addr, mod) within_module((addr), (mod))
#endif
#endif

/* ---- list helpers the vendored clang backend uses ---------------------- */

#ifndef list_first_entry_or_null /* 3.10 */
#define list_first_entry_or_null(ptr, type, member)                            \
	(!list_empty(ptr) ? list_first_entry(ptr, type, member) : NULL)
#endif
#ifndef list_last_entry /* 3.13 */
#define list_last_entry(ptr, type, member) list_entry((ptr)->prev, type, member)
#endif
#ifndef list_next_entry /* 3.13 */
#define list_next_entry(pos, member) list_entry((pos)->member.next, typeof(*(pos)), member)
#endif
#if LINUX_VERSION_CODE < KERNEL_VERSION(2, 6, 38)
static inline void __list_del_entry(struct list_head *entry)
{
	__list_del(entry->prev, entry->next);
}
#endif

#endif /* OTTO_KMODCOV_COMPAT_H */
