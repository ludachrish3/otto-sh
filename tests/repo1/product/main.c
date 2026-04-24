#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "math_ops.h"

static void usage(const char *prog) {
    fprintf(stderr, "Usage: %s <op> <a> <b>\n", prog);
    fprintf(stderr, "  ops: add, sub, mul, div, clamp\n");
}

int main(int argc, char *argv[]) {
    if (argc < 4) {
        usage(argv[0]);
        return 1;
    }

    const char *op = argv[1];
    int a = atoi(argv[2]);
    int b = atoi(argv[3]);

    if (strcmp(op, "add") == 0) {
        printf("%d\n", add(a, b));
    } else if (strcmp(op, "sub") == 0) {
        printf("%d\n", subtract(a, b));
    } else if (strcmp(op, "mul") == 0) {
        printf("%d\n", multiply(a, b));
    } else if (strcmp(op, "div") == 0) {
        int result;
        int rc = divide(a, b, &result);
        if (rc != 0) {
            fprintf(stderr, "Division by zero\n");
            return 1;
        }
        printf("%d\n", result);
    } else if (strcmp(op, "clamp") == 0) {
        if (argc < 5) {
            fprintf(stderr, "clamp requires: <value> <lo> <hi>\n");
            return 1;
        }
        int hi = atoi(argv[4]);
        printf("%d\n", clamp(a, b, hi));
    } else {
        fprintf(stderr, "Unknown operation: %s\n", op);
        usage(argv[0]);
        return 1;
    }

    return 0;
}
