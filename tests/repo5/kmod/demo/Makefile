# Build otto_kmod_demo.ko against KDIR, linking against the library in KGCOV.
# The demo lives in the SUT repo (tests/repo5) alongside its built library
# (tests/repo5/build/lib, git-ignored); KGCOV's default reaches it from here.
KDIR ?= /lib/modules/$(shell uname -r)/build
KGCOV ?= $(abspath $(CURDIR)/../../build/lib)

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) KGCOV=$(KGCOV) KBUILD_EXTRA_SYMBOLS=$(KGCOV)/Module.symvers modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) KGCOV=$(KGCOV) clean
