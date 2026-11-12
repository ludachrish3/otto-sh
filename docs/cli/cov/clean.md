# otto cov clean

`otto cov clean` zeroes counters on every one of the lab's coverage hosts
without fetching anything — useful ahead of a manual session when the
previous capture has already been retrieved:

```bash
otto cov clean
```

It calls {func}`~otto.coverage.collect.clean_coverage`, which resets every
instrumented product on every host `[coverage].hosts` matches — containers
included, the otto runner itself excluded — one product at a time, so a
product that is not a coverage build is left alone. Each product kind resets
its own way: a Unix or container product deletes the counters under its own
`cov_dir`, then the directories that removal left empty, then the `cov_dir`
itself if nothing is left in it; a {doc}`kernel-module product <instrumenting/kernel-modules>`
writes its sysfs `reset` file; an embedded board calls its exported
`reset_fn` over the console (see {ref}`coverage-embedded-reset` for what
that export looks like and the two rules the real boards forced on it). A
host with no instrumented product at all is printed, not silently skipped —
`test1: no instrumented products`.

(coverage-clean-directories)=
Once the counters are gone, the clean tries to remove every directory below
the `cov_dir` that is an ancestor of a counter it removed: deleting
`<cov_dir>/a/b/c.gcda` makes it try `a/b`, then `a`. It goes deepest first,
and a directory that still holds anything stays: a directory holding a file
of your own is kept, and so is every directory above it. A directory below
the `cov_dir` that was already empty, or that held no counter, is not the
clean's to remove. Last, the clean removes the `cov_dir` itself when it is
empty, whatever it held before, and keeps it when anything at all is left in
it: a file of your own, or an empty directory the clean did not empty. Nothing
above the `cov_dir` is ever touched, and a `cov_dir` that is a symbolic link
stays, as does the directory it points to. The next run creates the `cov_dir`
again when it writes its first counters (gcov, `otto_kmodcov` and the kernel
copy all create the directories they write into), and the next `otto test`
does not mistake a `cov_dir` the clean removed for lost coverage. One kind is
the exception: a `docker_image` product's `cov_dir` is the source of its
running container's bind mount, and removing that directory would leave the
container writing into a deleted directory, so its emptied `cov_dir` is
always kept.

The removal runs with the privilege that wrote the counters, so a kernel
module's root-owned tree (written with sudo) is removed with sudo too. Removing a directory is best effort: one the clean
cannot remove (a permission error, say) is left in place and does not fail
the clean, since the counters are already gone. `otto run cleanup` goes
further and removes every empty directory under the `cov_dir`, the `cov_dir`
included ({ref}`the coverage lifecycle <cov-dir-lifecycle>`).

The trade-off: a `cov_dir` you created in advance, with an owner, mode or ACL
a product relies on to write there (a daemon running as another user, say),
is removed once the clean empties it, and the next write must create it again
with the writer's own owner and umask, which fails when the writer may not
create it. Keep any file in such a `cov_dir` (a `.keep`, say) and the clean
always leaves it in place.

(coverage-clean-running-process)=
Deleting `.gcda` files does not zero a process that is still running. It
holds its counts in memory and writes all of them when it exits
({ref}`how it must exit <coverage-process-exit>`), so a daemon or a
container's `CMD` that ran before the clean still reports that work. Zero a
long-running Unix or container product in this order:

1. Stop its process, so it writes what it holds.
2. Clean: `otto cov clean`, or the clean `otto test` runs before its first
   test ({doc}`during-tests`).
3. Start the process again.

Restarting it after the clean instead (`docker restart`, say) does not work:
the stop writes the old counts into the directory the clean just emptied. A
process that exits while the clean runs can also lose its counts outright: if
the clean removes a directory between gcov recreating it and opening the
counter inside it, that counter is never written.
Around `otto test`, stop the process before the run and start it in a
fixture's setup, which runs after the run's clean. On a compose container
host the clean runs inside the container, so stop the product's process
there, not the container: a stopped container fails the clean. A kernel
module's or a board's reset zeroes the counters in memory and needs no
restart.

The command prints one line per host and product it reset — `test1/myapp:
counters cleared` — and exits `1` when any reset failed, naming the host,
the product, and the fix. A coverage directory that does not exist yet (gcov
creates it when the product first exits, and a reboot empties `/tmp`) has
nothing to clear and counts as cleared; one that exists but cannot be
cleared is a failure. No `[coverage]` section, or a selector matching no
coverage host at all, is also an exit-`1` refusal. Under `--dry-run`, a
declined reset is printed as not run rather than counted as cleared or
failed, and the command exits `0`.
