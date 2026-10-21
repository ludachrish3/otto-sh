#include <gtest/gtest.h>

#include "core.h"

TEST(CoreScale, TriplesItsArgument)
{
    EXPECT_EQ(core_scale(2), 6);
}

TEST(CoreScale, KeepsZero)
{
    EXPECT_EQ(core_scale(0), 0);
}
