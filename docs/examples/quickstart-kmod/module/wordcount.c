// SPDX-License-Identifier: GPL-2.0
#include "wordcount.h"

static int is_space(char c)
{
	return c == ' ' || c == '\t' || c == '\n';
}

int qs_count_words(const char *text, unsigned long len)
{
	int words = 0; // qs: count-words
	int in_word = 0;
	unsigned long i;

	for (i = 0; i < len && text[i]; i++) {
		if (is_space(text[i])) {
			in_word = 0;
		} else if (!in_word) {
			in_word = 1;
			words++;
		}
	}
	return words;
}
