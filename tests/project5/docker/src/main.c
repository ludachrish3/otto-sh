#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "math_ops.h"

static void usage(const char *prog) {
    fprintf(stderr, "Usage: %s <op> <a> <b>\n", prog);
    fprintf(stderr, "  ops: add, sub, mul, div, clamp\n");
    fprintf(stderr, "       %s serve   (wait for SIGTERM/SIGINT, then exit 0)\n",
            prog);
}

static volatile sig_atomic_t stop_requested;

static void on_stop(int sig) {
    (void)sig;
    stop_requested = 1;
}

static int setup_failed(const char *call) {
    fprintf(stderr, "serve: %s: %s\n", call, strerror(errno));
    return 2;
}

/*
 * serve: wait for SIGTERM or SIGINT, then return 0 so exit() flushes the
 * gcov counters. The stop signals stay blocked except inside sigsuspend(),
 * which unblocks and waits in one atomic step, so a stop that lands at any
 * moment -- before the wait, or between the flag check and the wait -- is
 * held pending and ends the wait instead of being lost. A plain
 * `while (!stop_requested) pause();` loses a stop that lands between the
 * check and pause() and then sleeps forever.
 *
 * OTTO_SERVE_RAISE_BEFORE_WAIT raises SIGTERM once inside that window, so
 * a test can prove the design rather than hope to hit the race.
 */
static int serve(void) {
    sigset_t stop, old, unblocked;
    if (sigemptyset(&stop) != 0) return setup_failed("sigemptyset");
    if (sigaddset(&stop, SIGTERM) != 0) return setup_failed("sigaddset");
    if (sigaddset(&stop, SIGINT) != 0) return setup_failed("sigaddset");
    if (sigprocmask(SIG_BLOCK, &stop, &old) != 0) {
        return setup_failed("sigprocmask");
    }
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_stop;
    if (sigemptyset(&sa.sa_mask) != 0) return setup_failed("sigemptyset");
    if (sigaction(SIGTERM, &sa, NULL) != 0) return setup_failed("sigaction");
    if (sigaction(SIGINT, &sa, NULL) != 0) return setup_failed("sigaction");
    unblocked = old;
    if (sigdelset(&unblocked, SIGTERM) != 0) return setup_failed("sigdelset");
    if (sigdelset(&unblocked, SIGINT) != 0) return setup_failed("sigdelset");
    int raise_once = getenv("OTTO_SERVE_RAISE_BEFORE_WAIT") != NULL;
    while (!stop_requested) {
        if (raise_once) {
            raise_once = 0;
            raise(SIGTERM);
        }
        sigsuspend(&unblocked);
    }
    return 0;
}

int main(int argc, char *argv[]) {
    if (argc == 2 && strcmp(argv[1], "serve") == 0) {
        return serve();
    }
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
