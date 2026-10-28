#include "mathops.h"

int mathops_clamp(int v, int lo, int hi)
{
	if (v < lo) {
		return lo; // qs: clamp-lo
	}
	if (v > hi) {
		return hi;
	}
	return v; // qs: clamp-in
}

int mathops_div(int a, int b, int *out)
{
	if (b == 0) {
		return -1;
	}
	*out = a / b; // qs: divide
	return 0;
}
