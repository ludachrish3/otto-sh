#include <gtest/gtest.h>

#include "text.h"

TEST(CountWords, CountsRunsOfNonSpace) { EXPECT_EQ(text::count_words("one  two three"), 3); }

TEST(CountWords, EmptyLineHasNone) { EXPECT_EQ(text::count_words(""), 0); }
