#include <gtest/gtest.h>

#include <cstring>

#include "wordcount.h"

namespace {

int count(const char* text) { return qs_count_words(text, std::strlen(text)); }

}  // namespace

TEST(CountWords, CountsRunsOfNonSpace) { EXPECT_EQ(count("one  two\tthree\n"), 3); }

TEST(CountWords, EmptyLineHasNone) { EXPECT_EQ(count(""), 0); }
