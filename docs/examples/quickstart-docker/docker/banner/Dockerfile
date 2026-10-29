# The singleton image: banner, built on the build machine as a static binary
# (the context is its build directory), on the same base. Its long-running
# process is this CMD; the tests start banner inside it with `docker exec`.
FROM alpine:3.20
COPY banner /opt/quickstart/banner
CMD ["sleep", "infinity"]
