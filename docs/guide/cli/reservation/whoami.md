# otto reservation whoami

```bash
otto reservation whoami
```

Prints the resolved identity, its source (`--as-user` or `$USER`), the
configured backend name, and the lab named on the command line (if any).
Needs no lab at all — identity and backend come from repo settings —
and never contacts a host.

`whoami` is **lab-free**: it takes no `--lab`, loads no lab data, and is the
fastest way to confirm which backend a repo is actually configured against.
See {doc}`identity` for how the identity itself is resolved and overridden.
