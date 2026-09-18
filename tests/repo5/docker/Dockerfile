FROM alpine:3.20
# GCOV_PREFIX_STRIP is baked in at build time (docker/build.sh computes it
# from its own absolute build directory, which differs between a worktree
# and the main checkout) so a product's run_args need carry only GCOV_PREFIX.
ARG GCOV_PREFIX_STRIP
# A hand `docker build` without --build-arg GCOV_PREFIX_STRIP would bake in
# an empty value (read as 0 — no stripping, counters land nested under the
# full absolute build path instead of flat) with no error; refuse instead.
RUN test -n "$GCOV_PREFIX_STRIP"
ENV GCOV_PREFIX_STRIP=$GCOV_PREFIX_STRIP
# A static build: alpine has no glibc, and the binary was linked on the dev VM.
COPY product /opt/product
CMD ["sleep", "infinity"]
