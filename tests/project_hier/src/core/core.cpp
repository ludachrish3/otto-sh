#include "core.h"

#if STALE
// Only the stale tree compiles this, so its notes describe another object.
int core_unused(int x)
{
    return x - 1;
}
#endif

int core_scale(int x)
{
    return x * 3; // hier: core-entry
}
