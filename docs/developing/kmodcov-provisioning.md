# Kernel-module coverage: provisioning kernels, ISAs and compilers

This page is for otto's maintainers: how otto proves `otto_kmodcov` on the
kernels, ISAs and compilers of the {ref}`compatibility matrix <kmodcov-matrix>`.
Building and instrumenting your own module, and the rules every build must
follow, are on {doc}`../cli/cov/instrumenting/kernel-modules`.

## The cross build otto's proof runs

otto's own proof runs this recipe — it also builds a copy of the
demo module the same way — on an arm64 machine building x86_64 modules (any
other pair changes only the `ARCH` name and the `CROSS_COMPILE` prefix):

```bash
tar -xJf linux-6.8.tar.xz
make -C linux-6.8 ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu- defconfig modules_prepare
KDIR=$PWD/linux-6.8 ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu- \
    KMAKEFLAGS=KBUILD_MODPOST_WARN=1 \
    src/otto/kmodcov/build.sh build
```

That `KMAKEFLAGS=KBUILD_MODPOST_WARN=1` is there because a prepared source
tree like this one has no `Module.symvers`: `make … defconfig
modules_prepare` never produces one (the kernel's own kbuild docs say a
full build is needed), so without it modpost fails outright on the
core-kernel exports (`memcpy`, `kfree`, `_printk`, …) it cannot resolve;
with it, those become warnings instead. A module meant to be *loaded*, as
opposed to checked, is built against a fully built tree or the target's
own headers package — either ships a real `Module.symvers` — never a bare
`modules_prepare` tree.

The release to check `vermagic` against is the tree's own
(`cat linux-6.8/include/config/kernel.release`), which is what the script
reads when `KDIR` is set and no release is given.

## Each kernel with its own era's compiler

**Build an old kernel's modules with that kernel's own era of compiler.** A
kernel tree is coupled to the compilers of its time in both directions: its
headers dispatch on `__GNUC__` (kernels before 4.2 include a
`compiler-gcc<major>.h` that exists only for the majors they knew), its
kbuild assumes that compiler's defaults (2.6.32 and 3.13 refuse a
PIE-by-default gcc, 2.6.32's host tools a `-fno-common` one), and
distributions backport compiler fixes into their long-term kernels
precisely because a newer gcc is unsupported there. A container image of
the kernel's own distribution release is the cheap way to have that
compiler; this is how `make kmodcov` builds for every provisioned kernel it
proves. Its oldest column, 2.6.32 in Ubuntu 12.04 with gcc 4.7 (the oldest
gcc the library's `gcov_info` layouts cover), runs exactly this, inside
the image:

```bash
tar -xJf linux-2.6.32.71.tar.xz
make -C linux-2.6.32.71 CC=gcc-4.7 HOSTCC=gcc-4.7 defconfig modules_prepare
KDIR=$PWD/linux-2.6.32.71 CC=gcc-4.7 KMAKEFLAGS="HOSTCC=gcc-4.7 KBUILD_MODPOST_WARN=1" \
    src/otto/kmodcov/build.sh build
```

The images and the prepared trees come from
`scripts/provision_kmodcov_kernels.sh`, whose header is the one home of that
mechanism.

A newer gcc *can* be shimmed onto such a tree — a `compiler-gcc<major>.h`
copied from `compiler-gcc4.h`, `KCFLAGS=-fno-PIE`, `HOSTCFLAGS=-fcommon` —
but that certifies an environment no user of that kernel has, and
`make kmodcov` does not certify it.

## What `make kmodcov` proves

What is proven, and where: `make kmodcov` (which `make release` runs) rebuilds
the fixture with gcc 9, 10, 11, 12, 13 and 14 and with clang 18 and runs
the kernel-module coverage suite on the bed for each, then builds it for
every kernel of the set under [Kernel versions](../cli/cov/instrumenting/kernel-modules.md#kernel-versions)
(`x86_64-cross`, `2.6.32`, `3.13`, `4.4`, `5.4`, `5.15`, `6.17`): each
provisioned kernel inside the image of its own era described above, and
the x86_64 cross build on the host with the VM's `x86_64-linux-gnu-gcc`
13. Every gcc from 4.7 on is in the format table, and every clang from 11
on shares one format.

Where a gcc puts its constructor is decided when that gcc was configured,
not by its version: Ubuntu's `x86_64-linux-gnu-gcc-9` cross package, a
compiler `make kmodcov` does not build with, emits `.ctors.65435` where the
native `gcc-9` emits `.init_array.00100`. No compiler `make kmodcov` builds
with emits `.ctors`, which is why the matrix carries a synthetic
`.ctors`-convention row. The library's own linker script, `kmodcov.lds`, which
`consumer.mk` applies at the consumer's intermediate link, folds
`.init_array.*`, `.ctors.*` and `.ctors` into the one `.init_array` the
sentinels bracket, so both conventions work on every kernel — including
kernels before 4.0, whose module linker script orders none of these sections
itself. What stays unsupported is older than either convention: the gcc 4.4
to 4.6 counter layout (issue #492).

On the two columns whose kbuild compiles every object as `.tmp_<unit>.o`
before deciding whether to relink or rename it (3.13 and 4.4,
`CONFIG_MODVERSIONS=y`), the `.gcno` gcc writes keeps that name —
`.tmp_<unit>.gcno` beside the object — and a module's `.gcda` files carry
the same prefix. gcc 4.7, 4.8 and 5 put their constructor,
`_GLOBAL__sub_I_65535_0_<first public symbol, or the file name>`, in plain
`.init_array`; gcc 9 and later put `_sub_I_00100_0` in `.init_array.00100`.
No compiler `make kmodcov` builds with emits `.ctors`, so that convention is
proven instead on a rewritten object — the matrix row "build: a
.ctors-convention demo is bracketed too". Collection of those files is
tracked as #530.
