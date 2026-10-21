/*
 * Drives otto_kmodcov's own kmodcov.c and backend as a user-space program:
 * kshim.h's definitions, then one "module" (unit.c, instrumented) registered
 * and unregistered the way KMODCOV_INIT()/KMODCOV_EXIT() do, its sysfs
 * dump/reset stores called directly.
 *
 *   harness <cov_dir> <op>...
 *
 * ops, run in order: load, unload, run:<n> (unit.c's loop, n iterations),
 * dump, reset, fail-read:<errno> (every file read returns -errno from now
 * on; 0 stops it). Each op prints one line to stdout: "<op> <result>".
 * Library log lines go to stderr as "kmodcov[<level>]: <line>".
 */
#include <sys/stat.h>

#include "kmodcov.h"

/* ---- kshim.h's out-of-line half ----------------------------------------- */

struct task_struct kshim_task;
int kshim_read_error;

void kshim_log(const char *level, const char *fmt, ...)
{
	va_list ap;

	fprintf(stderr, "kmodcov[%s]: ", level);
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
}

char *kasprintf(int gfp, const char *fmt, ...)
{
	va_list ap;
	char *s = NULL;

	(void)gfp;
	va_start(ap, fmt);
	if (vasprintf(&s, fmt, ap) < 0)
		s = NULL;
	va_end(ap);
	return s;
}

struct file *filp_open(const char *path, int flags, int mode)
{
	struct file *f;
	int fd = open(path, flags, mode);

	if (fd < 0)
		return ERR_PTR(-errno);
	f = malloc(sizeof(*f));
	f->fd = fd;
	return f;
}

int filp_close(struct file *f, void *id)
{
	(void)id;
	close(f->fd);
	free(f);
	return 0;
}

ssize_t kernel_write(struct file *f, const void *buf, size_t len, loff_t *pos)
{
	ssize_t w = pwrite(f->fd, buf, len, *pos);

	if (w < 0)
		return -errno;
	*pos += w;
	return w;
}

ssize_t kernel_read(struct file *f, void *buf, size_t len, loff_t *pos)
{
	ssize_t r;

	if (kshim_read_error)
		return kshim_read_error;
	r = pread(f->fd, buf, len, *pos);
	if (r < 0)
		return -errno;
	*pos += r;
	return r;
}

int kshim_mkdir(const char *path)
{
	return mkdir(path, 0755) ? -errno : 0;
}

static struct kobject kshim_dir = { "kmodcov" };
static const struct attribute_group *kshim_group;

struct kobject *kshim_sysfs_dir(const char *name, struct kobject *parent)
{
	(void)name;
	(void)parent;
	return &kshim_dir;
}

int kshim_sysfs_group(struct kobject *kobj, const struct attribute_group *group)
{
	(void)kobj;
	kshim_group = group;
	return 0;
}

/* ---- the harness ---------------------------------------------------------- */

int kc_loop(int n); /* unit.c */

static struct module kshim_mod = { .name = "kshim_demo" };

static long store(const char *name)
{
	struct attribute **a;

	if (!kshim_group)
		return -ENODEV;
	for (a = kshim_group->attrs; *a; a++) {
		struct kobj_attribute *ka = container_of(*a, struct kobj_attribute, attr);

		if (!strcmp((*a)->name, name))
			return ka->store(&kshim_dir, ka, "1\n", 2);
	}
	return -ENOENT;
}

int main(int argc, char **argv)
{
	const char *dir;
	int i;

	if (argc < 2) {
		fprintf(stderr, "usage: harness <cov_dir> <op>...\n");
		return 2;
	}
	dir = argv[1];
	for (i = 2; i < argc; i++) {
		const char *op = argv[i];
		long r;

		if (!strcmp(op, "load")) {
			r = kmodcov_register(&kshim_mod, &__kmodcov_ctors_begin + 1, &__kmodcov_ctors_end,
					   dir);
		} else if (!strcmp(op, "unload")) {
			kmodcov_unregister(&kshim_mod);
			kshim_group = NULL;
			r = 0;
		} else if (!strncmp(op, "run:", 4)) {
			r = kc_loop(atoi(op + 4));
		} else if (!strcmp(op, "dump") || !strcmp(op, "reset")) {
			r = store(op);
		} else if (!strncmp(op, "fail-read:", 10)) {
			kshim_read_error = -atoi(op + 10);
			r = 0;
		} else {
			fprintf(stderr, "unknown op %s\n", op);
			return 2;
		}
		printf("%s %ld\n", op, r);
		fflush(stdout);
	}
	return 0;
}
