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
`cov_dir`; a {doc}`kernel-module product <instrumenting/kernel-modules>`
writes its sysfs `reset` file; an embedded board calls its exported
`reset_fn` over the console (see {ref}`coverage-embedded-reset` for what
that export looks like and the two rules the real boards forced on it). A
host with no instrumented product at all is printed, not silently skipped —
`test1: no instrumented products`.

The command prints one line per host and product it reset — `test1/myapp:
counters cleared` — and exits `1` when any reset failed, naming the host,
the product, and the fix. A coverage directory that does not exist yet (gcov
creates it when the product first exits, and a reboot empties `/tmp`) has
nothing to clear and counts as cleared; one that exists but cannot be
cleared is a failure. No `[coverage]` section, or a selector matching no
coverage host at all, is also an exit-`1` refusal. Under `--dry-run`, a
declined reset is printed as not run rather than counted as cleared or
failed, and the command exits `0`.
