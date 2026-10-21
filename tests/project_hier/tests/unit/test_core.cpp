#include <cstdio>

#include "core.h"

int main()
{
    if (core_scale(2) != 6) {
        std::puts("FAIL: core_scale(2) != 6");
        return 1;
    }
    std::puts("ok");
    return 0;
}
