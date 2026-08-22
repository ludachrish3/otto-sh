# Identity and overrides

By default otto queries the backend using `getpass.getuser()` — i.e.
your shell login.  Pass `--as-user` on the top-level callback to query
as someone else:

```bash
otto --as-user alice test TestSmoke
otto --as-user alice host router1 run "uname -a"
```

When `--as-user` is on the command line, otto prints a bold-magenta
banner before the command runs:

```text
[reservations] acting as alice (--as-user)
```

The banner fires only in that one case.  On a normal run (no
`--as-user`) there is no banner — you already know who you are.

## Username tab-completion

If your backend can enumerate its users, otto offers them as `--as-user`
tab-completion values. A backend opts in by implementing the optional
[`SupportsUsernameCompletion`](../../../api/reservations.rst) capability — a single
`list_usernames() -> list[str]` method. Otto detects it structurally; backends
that can't list users simply omit it and `--as-user` still accepts free-form
input.

The values are cached with the same policy as host ids (otto's completion cache,
invalidated by the settings fingerprint and `--clear-autocomplete-cache`), because
enumerating users can be slow and the list changes rarely. The fingerprint is a
stat over files, and a reservation backend's user list lives outside any of
them — so a repo that configures one falls back to a short cache lifetime
(minutes, not a day) rather than waiting for a file to change. A cold cache yields
no suggestions and refreshes on the next normal run — completion never blocks on
the backend.

Real situations where `--as-user` is the right tool:

- A teammate has a shared rack booked under their name; you need to run
  a one-off `otto host` command against it without rebooking.
- Oncall takes over from someone else mid-incident; the booking is in
  the original person's name.
- A CI job needs to run against a rack booked under a service account.

If your process never hits these, you can just leave the flag alone —
otto will always operate as `$USER`.
