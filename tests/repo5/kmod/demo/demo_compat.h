/* SPDX-License-Identifier: GPL-2.0 */
/*
 * What a module of 2.6.32's age carries itself: the three kernel names the
 * demo uses that are younger than that kernel, each under its own version
 * guard. Nothing here is an executable line, so the instrumented units and
 * every coverage expectation are the same on every kernel.
 */
#ifndef OTTO_KMOD_DEMO_COMPAT_H
#define OTTO_KMOD_DEMO_COMPAT_H

#include <linux/version.h>
#include <linux/fs.h>
#include <linux/kernel.h>
#include <linux/string.h>

#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 5, 0)
static inline int demo_simple_open(struct inode *inode, struct file *file)
{
	if (inode->i_private)
		file->private_data = inode->i_private;
	return 0;
}
#define simple_open demo_simple_open
#endif

#if LINUX_VERSION_CODE < KERNEL_VERSION(2, 6, 39)
#define kstrtol(s, base, res) strict_strtol((s), (base), (res))
#endif

#if LINUX_VERSION_CODE < KERNEL_VERSION(2, 6, 33)
#define strim(s) strstrip(s)
#endif

#endif /* OTTO_KMOD_DEMO_COMPAT_H */
