# Kbuild fragment for an otto_kgcov consumer. The consumer's Makefile passes
# KGCOV=<the library's build tree> and KBUILD_EXTRA_SYMBOLS=$(KGCOV)/Module.symvers
# (CONFIG_MODVERSIONS needs the library's symbol versions at modpost time).
ccflags-y += -I$(KGCOV)
KGCOV_CFLAGS := -fprofile-arcs -ftest-coverage -fprofile-info-section
