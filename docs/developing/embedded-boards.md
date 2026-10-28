# Embedded coverage: the example on the bed

This page is for otto's maintainers: how the embedded track's example is built
and run on the dev VM. The reader's walkthrough is
{doc}`../setup/tracks/embedded`, and the instrumentation it relies on is
{doc}`../cli/cov/instrumenting/embedded`.

The bed boards and their provisioning are not described here. The embedded
coverage instance is in {ref}`the contributing guide's bed section
<embedded-coverage-bed>`, and the boards' firmware is built from
[`tests/firmware/zephyr/README.md`](https://github.com/ludachrish3/otto-sh/blob/main/tests/firmware/zephyr/README.md).
otto's own embedded product is
[`tests/project3/product/README.md`](https://github.com/ludachrish3/otto-sh/blob/main/tests/project3/product/README.md):
its **GCC coupling** notes pair each Zephyr version the bed runs with its
SDK and GCC, and ties the report's `gcov` to the build's. The track page's
captures were made on the Zephyr 3.7 pair. The tests that drive the product are
[`tests/project3/tests/test_embedded_coverage.py`](https://github.com/ludachrish3/otto-sh/blob/main/tests/project3/tests/test_embedded_coverage.py).

## Building the example on the dev VM

The track page tells a reader to set `ZEPHYR_WORKSPACE`,
`ZEPHYR_SDK_INSTALL_DIR` and `EMBEDDED_GCOV_DIR` and run `ext/build.sh`. The
capture runner does the same through the bed overlay's
`docs/examples/bed/quickstart-embedded/ext/bed-build.sh`, which fills the first
two in from the dev VM's Zephyr 3.7 workspace and SDK and takes the patched
embedded-gcov checkout as its argument. It passes the otto under test as
`OTTO`, so the build's `cov embedded-gcov finish` step is that otto's.

## An uninitialized embedded-gcov submodule

otto's own project keeps embedded-gcov as a git submodule under
`tests/project3/third_party/`, and the capture set clones it from there at the
pinned commit. Where the submodule was never initialized, `tests/project3`'s
`product/build.sh` names it (see its [README](https://github.com/ludachrish3/otto-sh/blob/main/tests/project3/product/README.md)), but the
capture set's runtime clone fails with git's own "repository does not exist"
message. The fix is the same for both:
`git submodule update --init tests/project3/third_party/embedded-gcov`.

The upstream commit the track page, the patch's README and the capture set name
is pinned to that submodule's commit by a unit test.
