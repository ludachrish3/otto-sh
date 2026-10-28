# embedded-gcov patch and starter

`embedded-gcov-zephyr-gcc12plus.patch` makes NASA's
[embedded-gcov](https://github.com/nasa-jpl/embedded-gcov) runtime build for Zephyr
under GCC 12 through 14. It applies to upstream commit
`7cc670697d0b9fa23f7d22ed5f6776164d7732e2`.

Export it with `otto cov embedded-gcov export DIRECTORY`; the reasoning and the
apply steps are in the
[embedded coverage guide](https://otto-sh.readthedocs.io/en/stable/cli/cov/instrumenting/embedded.html).

`starter/cov_entry.c` holds the coverage entry points (`cov_dump`, `cov_reset`,
`cov_init`) that an extension compiles in with the runtime.
