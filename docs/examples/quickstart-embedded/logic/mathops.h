#ifndef MATHOPS_H
#define MATHOPS_H

/* Clamp v into [lo, hi]. */
int mathops_clamp(int v, int lo, int hi);

/* a / b into *out; returns 0, or -1 (leaving *out alone) when b is 0. */
int mathops_div(int a, int b, int *out);

#endif
