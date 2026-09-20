# Build otto_kgcov.ko against KDIR (default: the running kernel's headers).
# ARCH, CROSS_COMPILE and LLVM reach kbuild from the environment untouched.
# CC only counts when the caller set it — kbuild assigns its own CC and only
# a command-line value overrides that — and KMAKEFLAGS is appended verbatim.
KDIR ?= /lib/modules/$(shell uname -r)/build
KBUILD_ARGS := $(KMAKEFLAGS)
ifneq ($(filter environment command line,$(origin CC)),)
KBUILD_ARGS += CC=$(CC)
endif

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) clean
