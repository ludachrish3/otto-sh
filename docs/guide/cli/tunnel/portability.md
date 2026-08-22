# Portability & host-down behavior

`otto tunnel` is best-effort and transparent about failure, never silently
wrong:

- **`list` / discovery** shows tunnels found on every host it could reach,
  marks a tunnel's status uncertain (a trailing `?`) when a chain host
  couldn't be scanned, and names each unreachable host. It never silently
  drops a host from the picture.
- **`remove`** kills tunnels on every host it could reach, names the hosts
  it couldn't, reports any process still alive after the kill as a
  survivor, and **exits non-zero** whenever any of that happened — so a
  script checking the exit code learns the reap was incomplete instead of
  being told it succeeded while a stray `socat` may still be running.

## Old-OS portability

Tunnel processes launch detached and owner-agnostic so they outlive the
`otto tunnel add` invocation and the SSH session that ran it — see
{doc}`../../../architecture/subsystems/network` for the launch mechanism
(`systemd-run --user` versus a `setsid` fallback on hosts without a user systemd
manager, including inside Docker containers). The `socat` address forms, the
`exec -a` argv-tagging trick, and the
discovery `ps` command all stay within an old-stable portability floor
(pre-`etimes`, procps/socat compatible back to Linux 2.6.32-era
userland). The docker-endpoint e2e suite exercises this floor against a
`centos:7` (arm64) container — no systemd, so the `setsid` launch path,
old-procps `etime` parsing, and old-bash `exec -a` are what actually run
there. True CentOS-6/2.6.32 validation remains a documented manual check.

