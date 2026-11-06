FROM alpine:3.20
# No GCOV_PREFIX or GCOV_PREFIX_STRIP here: otto sets both on the `docker run`
# it owns, the strip counted from the product's build_root (.otto/settings.toml),
# and its `-e` would override an ENV baked in here anyway.
# A static build: alpine has no glibc, and the binary was linked on the dev VM.
COPY product /opt/product
# The product itself is PID 1, waiting in `serve`, so that `docker stop`
# (SIGTERM) makes it return from main and gcov's exit-time dump flush its
# counters. As PID 1 an unhandled SIGTERM is ignored, and docker would SIGKILL
# it after 10 s with nothing flushed; `serve` installs the handler for that.
CMD ["/opt/product", "serve"]
