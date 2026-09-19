# Build otto_kmod_demo.ko against KDIR, linking against the library in KGCOV.
# The demo lives in the SUT repo (tests/repo5) alongside its built library
# (tests/repo5/build/lib, git-ignored); KGCOV's default reaches it from here.
# ARCH, CROSS_COMPILE and LLVM reach kbuild from the environment untouched;
# CC only counts when the caller set it (kbuild assigns its own CC and only a
# command-line value overrides that); KMAKEFLAGS is appended verbatim.
KDIR ?= /lib/modules/$(shell uname -r)/build
KGCOV ?= $(abspath $(CURDIR)/../../build/lib)
KBUILD_ARGS := $(KMAKEFLAGS)
ifneq ($(filter environment command line,$(origin CC)),)
KBUILD_ARGS += CC=$(CC)
endif

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) KGCOV=$(KGCOV) KBUILD_EXTRA_SYMBOLS=$(KGCOV)/Module.symvers $(KBUILD_ARGS) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) KGCOV=$(KGCOV) $(KBUILD_ARGS) clean
