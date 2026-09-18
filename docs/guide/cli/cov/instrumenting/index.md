# Instrumenting your product

The collection workflow in {doc}`../index` is the same for every product;
what differs is how the product itself is built and instrumented. How the
product is compiled decides which page you need:

| If your product is… | Read |
| --- | --- |
| built with GNU GCC on a Unix-like target | {doc}`gcc` |
| built with `clang --coverage` | {doc}`clang` |
| an embedded RTOS (Zephyr) image with no fetchable filesystem | {doc}`embedded` |
| a Linux kernel module (out of tree) | {doc}`kernel-modules` |

Clang-compiled products emit counters in a format GNU `gcov` cannot read;
otto routes them through `llvm-cov` instead. Embedded RTOS targets have no
filesystem otto can fetch `.gcda` files from, so their coverage rides the
serial console via an instrumented LLEXT extension — a different pipeline,
not just different flags. A kernel module has no process to flush counters
at exit, so a companion runtime dumps them from inside the kernel instead —
see {doc}`kernel-modules`.

Which toolchain otto reaches for is resolved per host — see
{ref}`per-host-toolchain` in {doc}`../../../configuration/host-options`.

```{toctree}
:caption: Topics
:hidden:

gcc
clang
embedded
kernel-modules
```
