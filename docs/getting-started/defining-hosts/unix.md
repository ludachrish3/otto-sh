# Unix hosts

There is little to say, which is the point. A Unix host needs an address, at
least one credential, and `os_type: "unix"` — the default. `valid_terms` and
`valid_transfers` are menus otto chooses from (`ssh` and `scp` first, by
default); everything else otto works out for itself on first connect.

Is `probe` worth running on one? On a current Debian, Ubuntu or Fedora box,
no: the probe finds exactly the GNU answers otto's transfer and command
paths are built around, so it confirms rather than discovers — compare the
`test1` capture on {doc}`index`, where every row's source is `probed` and
what it found is the modern-GNU answer on every row. The command earns its
keep on the *other* Unix: an old release, a stripped image, a vendor build
with `busybox` behind `/bin/sh`. There, one probe replaces a connection that
fails halfway through a transfer for a reason nobody can see.

`hop` is the field this family uses most: the BusyBox guests on the next page
are reached through `test1`, and a Zephyr target through `test4`. otto opens
the jump session itself.
