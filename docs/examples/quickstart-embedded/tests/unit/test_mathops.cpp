#include <gtest/gtest.h>

extern "C" {
#include "mathops.h"
}

TEST(Clamp, BelowRangeGivesTheLowBound) { EXPECT_EQ(mathops_clamp(-5, 0, 10), 0); }
TEST(Clamp, InsideRangeIsUnchanged) { EXPECT_EQ(mathops_clamp(7, 0, 10), 7); }
TEST(Clamp, AboveRangeGivesTheHighBound) { EXPECT_EQ(mathops_clamp(42, 0, 10), 10); }

TEST(Div, DividesWhenTheDivisorIsNotZero)
{
	int out = 0;
	EXPECT_EQ(mathops_div(10, 2, &out), 0);
	EXPECT_EQ(out, 5);
}

TEST(Div, RefusesZeroAndLeavesTheOutputAlone)
{
	int out = 99;
	EXPECT_EQ(mathops_div(10, 0, &out), -1);
	EXPECT_EQ(out, 99);
}
