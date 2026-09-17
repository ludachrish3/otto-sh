# otto cov clean

`otto cov clean` zeroes `.gcda` counters on the lab's coverage hosts
without fetching anything — useful ahead of a manual session when the
previous capture has already been retrieved:

```bash
otto cov clean
```

It deletes the counters under each instrumented product's own `cov_dir`,
one product at a time, so a product that is not a coverage build is left
alone.  It targets the same host selection `otto cov get` fetches from, but
**Unix hosts only**.  Embedded targets expose no counter-reset hook; when
the lab has any embedded coverage hosts, the command logs a note and
exits `0` rather than failing.  A lab with *only* embedded coverage hosts
is likewise not an error — on embedded hosts it is simply a no-op.
