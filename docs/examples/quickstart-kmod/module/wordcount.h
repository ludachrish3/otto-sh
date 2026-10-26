/* SPDX-License-Identifier: GPL-2.0 */
/*
 * The module's logic, freestanding: this header and wordcount.c include no
 * kernel header and no C library header, so the same source builds into the
 * module (Kbuild) and into the unit tests on the build machine (CMake).
 */
#ifndef QS_WORDCOUNT_H
#define QS_WORDCOUNT_H

#ifdef __cplusplus
extern "C" {
#endif

/* The number of runs of non-space characters in the first `len` bytes of `text`. */
int qs_count_words(const char *text, unsigned long len);

#ifdef __cplusplus
}
#endif

#endif /* QS_WORDCOUNT_H */
