# Skipping and disabling the check

```bash
otto -R run some_instruction
otto --skip-reservation-check test TestSmoke
```

`-R` / `--skip-reservation-check` bypasses the check entirely.  It is
intentionally aggressive:

- A bold-red WARNING is printed naming the user, lab, and required
  resources.  This is deliberate friction — the option should feel
  scary to reach for.
- A WARNING-level log line records the same details, so after-the-fact
  log review can find the runs that skipped.

`-R` exists for two realistic situations:

1. **Reservation-system outage** — scheduler is down, you need to keep
   working.
2. **Data mistakes** — your name is spelled wrong in the booking tool,
   or the entry got dropped, and you can't wait for it to be fixed.

It is *not* a normal path.  If your team runs with `-R` routinely, the
check is miscalibrated — fix the data instead.

## Why error messages don't mention `-R`

When the reservation check fails because you don't hold something, the
error message lists the missing resources and their current holders and
stops there.  It *deliberately* doesn't advertise `--skip-reservation-check`,
even though a suggestion would be friendly — the flag gets abused the
moment a user assumes it's a normal workaround.

The one exception is backend-unreachable errors (network down, file
corrupt).  There, `-R` is shown as a suggestion because the user
otherwise has no way to proceed.

## Disabling the check team-wide

Teams that don't have a scheduler yet, or who run against isolated
sandbox labs, can disable the check entirely:

```toml
[reservations]
backend = "none"
```

This is the default when no `[reservations]` section exists, so a repo
with no reservations config behaves the same as `backend = "none"`.
The `NullReservationBackend` short-circuits the check to a no-op — no
banner, no warning, no error.

Omit `[reservations]` or set `backend = "none"` for labs that nobody
else is using, while keeping `backend = "json"` (or your custom
backend) on the production labs.  There is currently one
`[reservations]` section per repo, so the backend cannot be varied by
lab.
