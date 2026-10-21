/* The instrumented "module" the harness registers: one loop, one counted line. */
int kc_loop(int n);
int kc_never(int n);

int kc_loop(int n)
{
	int i, sum = 0;

	for (i = 0; i < n; i++)
		sum += i; /* KC_HIT */
	return sum;
}

int kc_never(int n)
{
	return n * 2;
}
