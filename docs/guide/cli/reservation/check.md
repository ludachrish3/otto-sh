# otto reservation check

```bash
otto --lab tech1 reservation check
```

Runs the check standalone and prints a human-readable report: required
resources, whether everything is covered, and if not, what's missing
and who holds it.  Useful as a pre-flight before kicking off a long
`otto test` run — you find out in one second instead of twenty minutes.
`check` is the one reservation subcommand that needs `--lab`: the lab
defines the required-resource list.  It reads lab *data* only — no host
is contacted.

`check` runs the same gate every hardware-touching command runs in its
preamble — see {doc}`index` for what that gate covers and
{doc}`skipping` for the break-glass override.
