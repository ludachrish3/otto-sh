/*
 * A user-space stand-in for the kernel headers otto_kmodcov includes, so its
 * own C files (kmodcov.c and one backend) compile and run as an ordinary
 * program. Every <linux/...> header the library names resolves to a file in
 * linux/ beside this one that includes this header and nothing else.
 *
 * Only what the library touches is here. File I/O is real (open/pread/
 * pwrite under the path the library asks for), with one injection point:
 * kshim_read_error, when non-zero, is what the next reads return.
 */
#ifndef KSHIM_H
#define KSHIM_H

#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

typedef uint32_t u32;
typedef uint64_t u64;

#define BITS_PER_LONG (8 * __SIZEOF_LONG__)
#define KERNEL_VERSION(a, b, c) (((a) << 16) + ((b) << 8) + (c))
#define LINUX_VERSION_CODE KERNEL_VERSION(6, 8, 0)

#define __user
#define __force
#define EXPORT_SYMBOL(sym)
#define EXPORT_SYMBOL_GPL(sym)
#define MODULE_LICENSE(x)
#define MODULE_DESCRIPTION(x)
#define MODULE_AUTHOR(x)
#define MODULE_VERSION(x)
#define MODULE_PARM_DESC(x, y)
#define module_param(name, type, perm)

#define container_of(ptr, type, member) ((type *)((char *)(ptr)-offsetof(type, member)))

/* ---- errors ------------------------------------------------------------- */

#define MAX_ERRNO 4095
#define IS_ERR_VALUE(x) ((unsigned long)(void *)(x) >= (unsigned long)-MAX_ERRNO)
static inline void *ERR_PTR(long error) { return (void *)error; }
static inline long PTR_ERR(const void *ptr) { return (long)ptr; }
static inline bool IS_ERR(const void *ptr) { return IS_ERR_VALUE((unsigned long)ptr); }

/* ---- logging: every line to stderr, tagged with its level --------------- */

#ifndef pr_fmt
#define pr_fmt(fmt) fmt
#endif
void kshim_log(const char *level, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
#define pr_err(fmt, ...) kshim_log("err", pr_fmt(fmt), ##__VA_ARGS__)
#define pr_warn(fmt, ...) kshim_log("warn", pr_fmt(fmt), ##__VA_ARGS__)
#define pr_info(fmt, ...) kshim_log("info", pr_fmt(fmt), ##__VA_ARGS__)

/* ---- allocation ---------------------------------------------------------- */

#define GFP_KERNEL 0
static inline void *kzalloc(size_t size, int gfp) { (void)gfp; return calloc(1, size ? size : 1); }
static inline void *kcalloc(size_t n, size_t size, int gfp) { (void)gfp; return calloc(n ? n : 1, size ? size : 1); }
static inline char *kstrdup(const char *s, int gfp) { (void)gfp; return s ? strdup(s) : NULL; }
static inline void *kmemdup(const void *p, size_t size, int gfp)
{
	void *d = malloc(size ? size : 1);

	(void)gfp;
	if (d)
		memcpy(d, p, size);
	return d;
}
char *kasprintf(int gfp, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static inline void kfree(const void *p) { free((void *)p); }
static inline void *kvmalloc(size_t size, int gfp) { (void)gfp; return malloc(size ? size : 1); }
static inline void kvfree(const void *p) { free((void *)p); }

/* ---- lists: the subset the library and the clang backend use ------------ */

struct list_head {
	struct list_head *next, *prev;
};
#define LIST_HEAD_INIT(name) { &(name), &(name) }
#define LIST_HEAD(name) struct list_head name = LIST_HEAD_INIT(name)
static inline void INIT_LIST_HEAD(struct list_head *l) { l->next = l->prev = l; }
static inline void __list_add(struct list_head *n, struct list_head *prev, struct list_head *next)
{
	next->prev = n;
	n->next = next;
	n->prev = prev;
	prev->next = n;
}
static inline void list_add(struct list_head *n, struct list_head *head) { __list_add(n, head, head->next); }
static inline void list_add_tail(struct list_head *n, struct list_head *head) { __list_add(n, head->prev, head); }
static inline void __list_del(struct list_head *prev, struct list_head *next)
{
	next->prev = prev;
	prev->next = next;
}
static inline void __list_del_entry(struct list_head *e) { __list_del(e->prev, e->next); }
static inline void list_del(struct list_head *e)
{
	__list_del_entry(e);
	e->next = e->prev = NULL;
}
static inline int list_empty(const struct list_head *h) { return h->next == h; }
static inline int list_is_last(const struct list_head *l, const struct list_head *h) { return l->next == h; }
#define list_entry(ptr, type, member) container_of(ptr, type, member)
#define list_first_entry(ptr, type, member) list_entry((ptr)->next, type, member)
#define list_last_entry(ptr, type, member) list_entry((ptr)->prev, type, member)
#define list_first_entry_or_null(ptr, type, member) \
	(!list_empty(ptr) ? list_first_entry(ptr, type, member) : NULL)
#define list_next_entry(pos, member) list_entry((pos)->member.next, __typeof__(*(pos)), member)
#define list_for_each_entry(pos, head, member)                                   \
	for (pos = list_first_entry(head, __typeof__(*pos), member); &pos->member != (head); \
	     pos = list_next_entry(pos, member))
#define list_for_each_entry_safe(pos, n, head, member)                           \
	for (pos = list_first_entry(head, __typeof__(*pos), member),             \
	    n = list_next_entry(pos, member);                                    \
	     &pos->member != (head); pos = n, n = list_next_entry(n, member))

/* ---- locks: one thread, so a lock is a no-op ----------------------------- */

struct mutex {
	int unused;
};
#define DEFINE_MUTEX(name) struct mutex name = { 0 }
static inline void mutex_lock(struct mutex *m) { (void)m; }
static inline void mutex_unlock(struct mutex *m) { (void)m; }

/* ---- module, task, sysfs ------------------------------------------------- */

struct kobject {
	const char *name;
};
struct module_kobject {
	struct kobject kobj;
};
struct module {
	char name[64];
	struct module_kobject mkobj;
};
struct task_struct {
	int unused;
};
extern struct task_struct kshim_task;
#define current (&kshim_task)
static inline bool within_module(unsigned long addr, const struct module *mod)
{
	(void)addr;
	(void)mod;
	return false;
}

struct attribute {
	const char *name;
	unsigned short mode;
};
struct kobj_attribute {
	struct attribute attr;
	ssize_t (*show)(struct kobject *kobj, struct kobj_attribute *attr, char *buf);
	ssize_t (*store)(struct kobject *kobj, struct kobj_attribute *attr, const char *buf,
			 size_t count);
};
#define __ATTR(_name, _mode, _show, _store)                                      \
	{ .attr = { .name = #_name, .mode = _mode }, .show = _show, .store = _store }
struct attribute_group {
	struct attribute **attrs;
};

/* ---- files ------------------------------------------------------------- */

struct file {
	int fd;
};
extern int kshim_read_error; /* non-zero: every read returns it */
struct file *filp_open(const char *path, int flags, int mode);
int filp_close(struct file *f, void *id);
ssize_t kernel_write(struct file *f, const void *buf, size_t len, loff_t *pos);
ssize_t kernel_read(struct file *f, void *buf, size_t len, loff_t *pos);

#endif /* KSHIM_H */
