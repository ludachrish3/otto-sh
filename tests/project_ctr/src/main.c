#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ops.h"

int main(int argc, char *argv[]) {
    if (argc != 4) {
        fprintf(stderr, "Usage: %s add|sub <a> <b>\n", argv[0]);
        return 1;
    }
    int a = atoi(argv[2]);
    int b = atoi(argv[3]);
    if (strcmp(argv[1], "add") == 0) {
        printf("%d\n", add(a, b));
    } else if (strcmp(argv[1], "sub") == 0) {
        printf("%d\n", subtract(a, b));
    } else {
        fprintf(stderr, "Unknown operation: %s\n", argv[1]);
        return 1;
    }
    return 0;
}
