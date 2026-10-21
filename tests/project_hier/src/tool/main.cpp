#include <cstdio>

#if STALE
static const char *banner = "tool (stale)";
#else
static const char *banner = "tool";
#endif

int main()
{
    std::puts(banner); // hier: tool-main
    return 0;
}
