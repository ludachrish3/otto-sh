/*
 * The user-space harness's kmodcov_local.h: force-included ahead of every
 * library file, as Kbuild does with a real one, replacing the kernel-facing
 * names whose defaults reach too far into the VFS or sysfs for kshim.h to
 * stand in. Everything else — the file read and write arms among them —
 * builds from kmodcov_compat.h's own defaults for LINUX_VERSION_CODE 6.8.
 */
struct kobject;
struct attribute_group;
int kshim_mkdir(const char *path);
struct kobject *kshim_sysfs_dir(const char *name, struct kobject *parent);
int kshim_sysfs_group(struct kobject *kobj, const struct attribute_group *group);

#define KMODCOV_MKDIR(path) kshim_mkdir(path)
#define KMODCOV_SYSFS_DIR(name, parent) kshim_sysfs_dir((name), (parent))
#define KMODCOV_SYSFS_DIR_PUT(kobj) ((void)(kobj))
#define KMODCOV_SYSFS_GROUP(kobj, group) kshim_sysfs_group((kobj), (group))
#define KMODCOV_SYSFS_GROUP_REMOVE(kobj, group) ((void)(kobj), (void)(group))
