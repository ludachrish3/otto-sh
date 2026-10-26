# Build qs_wordcount.ko in place against the kernel tree KDIR, linking the
# otto_kmodcov library built under build/kmodcov. ARCH, CROSS_COMPILE and LLVM
# reach kbuild from the environment; CC counts only when the caller set it
# (kbuild assigns its own, and only a command-line value overrides that);
# KMAKEFLAGS is appended verbatim.
KDIR ?= /lib/modules/$(shell uname -r)/build
KMODCOV ?= $(abspath $(CURDIR)/../build/kmodcov/lib)
KBUILD_ARGS := $(KMAKEFLAGS)
ifneq ($(filter environment command line,$(origin CC)),)
KBUILD_ARGS += CC=$(CC)
endif

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) KMODCOV=$(KMODCOV) KBUILD_EXTRA_SYMBOLS=$(KMODCOV)/Module.symvers $(KBUILD_ARGS) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) KMODCOV=$(KMODCOV) $(KBUILD_ARGS) clean
