# Kbuild fragment for an otto_kmodcov consumer. The consumer's Makefile passes
# KMODCOV=<the library's build tree> and KBUILD_EXTRA_SYMBOLS=$(KMODCOV)/Module.symvers
# (CONFIG_MODVERSIONS needs the library's symbol versions at modpost time).
# Plain gcov instrumentation, the same flags for gcc and clang: the
# constructor each compiler emits is what otto_kmodcov runs at KMODCOV_INIT().
ccflags-y += -I$(KMODCOV)
KMODCOV_CFLAGS := -fprofile-arcs -ftest-coverage

# One linker script for every consumer, at the intermediate link: folds
# .init_array.*, .ctors.* and .ctors into the one .init_array the sentinels
# bracket, on kernels whose module linker script orders none of them
# (before 4.0; .ctors before 5.15) and for toolchains that emit .ctors.
# Harmless for an uninstrumented module in the same directory.
ldflags-y += -T $(KMODCOV)/kmodcov.lds
