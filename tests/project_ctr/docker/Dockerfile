# The runtime image: the same Ubuntu the compiler image (otto-kmodcov-kernel:5.15)
# is built FROM, so the dynamically linked, --coverage binary otto stages into
# the running container finds the glibc it was linked against. Deliberately no
# GCOV_PREFIX_STRIP: the counters keep their full /build/... object paths under
# GCOV_PREFIX, and otto's pairing matches that path's tail to the notes file.
FROM arm64v8/ubuntu:22.04
LABEL org.otto-sh.fixture="project_ctr"
CMD ["sleep", "infinity"]
