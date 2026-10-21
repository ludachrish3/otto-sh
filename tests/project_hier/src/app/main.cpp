#include <cstdio>

#include "core.h"

#if STALE
static int stale_offset = 1;
#else
static int stale_offset = 0;
#endif

int main()
{
    std::printf("app %d\n", core_scale(2) + stale_offset); // hier: app-main
    return 0;
}
