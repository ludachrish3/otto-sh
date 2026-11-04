/*
 * project5's unit test of the container product's math_ops.c, built by
 * docker/build.sh into docker/build-unit/ (git-ignored), the unit view's
 * directory for cov_container_ref (.otto/settings.toml). Its exact calls are
 * what the stopped-container report e2e pins: add twice, multiply once,
 * subtract never.
 */
#include <stdio.h>

#include "../src/math_ops.h"

static int check(const char *what, int got, int want) {
    if (got != want) {
        fprintf(stderr, "%s: got %d, want %d\n", what, got, want);
        return 1;
    }
    return 0;
}

int main(void) {
    int failed = 0;
    failed |= check("add(1, 1)", add(1, 1), 2);
    failed |= check("add(1, 1) again", add(1, 1), 2);
    failed |= check("multiply(2, 3)", multiply(2, 3), 6);
    return failed;
}
