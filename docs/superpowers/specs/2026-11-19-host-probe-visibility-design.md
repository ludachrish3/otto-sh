# Make `otto host <id> probe` impossible to miss when setting up a lab

**Status:** approved by Chris, 2026-11-19.

## Intent

Users skip probing their hosts, then complain that commands rescan their
hosts all the time. That rescanning is the cost of an unpinned Unix host.

- **Userland:** otto settles the host's userland capabilities in one probe
  round, the first time a host object needs one. Measured answers are cached
  in memory for that object's lifetime, which is normally one otto command.
  Nothing persists between commands, so the next command that needs one asks
  the device again, and on a BusyBox device that round is slow. Capabilities
  that got no answer may be retried after a cooldown, and option overrides
  build fresh host objects.
- **sudo:** an unset `sudo_form` is measured once per login, then cached.
  After a refused password, later sudo commands try again, and those tries
  count against the account's lockout (`userland.md`).

`otto host <id> probe` measures and prints the settled userland answers as a
`userland_options` pin, plus any protocol menu or port fragments from its
protocol survey, to paste into the host's entry. A declared value is never
measured again, not even by `probe`. Pinning stops the userland probes for
the pinned values only. Other discovery still runs: `$HOME` discovery when
staging without `stage_dir`, and terminal calibration on every session.
Ordinary commands never repeat the protocol survey.

Today only the CLI reference teaches this, in
`docs/cli/host/capabilities/userland.md` and `protocols.md`. A Haiku reader
was given the six setup pages and asked to set up a lab:
- it found no mention of probing;
- it rated the step 1 out of 5;
- it could not say what skipping it costs;
- it would not have run it.

## Goals

- **One home page, `docs/configuration/lab/probing-hosts.md`,** "Probe and
  pin every Unix host", listed with the other lab pages in
  `docs/configuration/lab-config.md`. It covers:
  - the step: `otto --lab <lab> host <id> probe` once per Unix host, then
    pasting what it prints into that host's entry. Only settled answers are
    printed, so a partial pin is normal. The pasted values then read
    `declared`; rows left `assumed` or `unmeasured` stay that way, and are
    never invented by hand.
  - where the pins go:
    - a lab file's host entry;
    - for a host from JSON or NetBox inventory, the lab's host entry beside
      `inventory`, never the inventory record;
    - for a custom host source, the host record it emits.

    Product preferences can override option tables (link `host-options.md`).
  - what you lose without it, in plain terms and no more than the Intent
    says;
  - re-probing after a software or image change: remove the affected
    declarations first, because a declared value is never re-measured; then
    probe and paste again;
  - which hosts don't need a userland pin: embedded, Docker-container and
    `local` hosts have no userland resolver. `probe` still runs its protocol
    survey on them.

  It links to `userland.md` and `protocols.md` for detail and does not
  restate them.
- **Probing is recommended for every Unix host** (Chris, 2026-11-19).
- **Every page that walks someone through setting up a lab config makes it
  the go-to step,** with a visible callout, a one-line consequence and a
  link to the home page:
  - `docs/setup/shared-setup.md`, in "First success", right after the first
    `exec`;
  - `docs/configuration/lab-config.md`, in "A minimal lab";
  - the four track pages, `docs/setup/tracks/*.md`, where each first reaches
    its Unix host: `dut` for shell and kmod, `console` for embedded, and
    `docker-host`, the Unix parent and not its containers, for docker;
  - `docs/use/joining.md`, for someone adding hosts to an existing project;
  - `docs/configuration/host-sources.md`, `docs/configuration/inventory.md`
    and `docs/configuration/lab/inventory-references.md`, where hosts come
    from somewhere other than a lab file;
  - the team setup checklist in `docs/configuration/settings.md`;
  - `docs/cli/host/index.md`, the host verbs.
- **The `otto init` "Next steps" panel** (`src/otto/init/next_steps.py`)
  gains a probe command after `--list-hosts`, whenever the scaffold declares
  its example lab. Tests pin it, and the docs capture that shows the panel
  is re-recorded through `docs/examples/captures.toml`.
- **Proof by a naive reader.** The same two-phase Haiku reading is run on
  the same pages after the change:
  - Phase 1, unprompted: list the setup steps and rate each one.
  - Phase 2: questions about probing.

  The step should rate near 5, and the reader should say what skipping it
  costs, in words that match the source.

## Non-goals

- No product behaviour change beyond the `init` panel line. In particular,
  no warning when a host runs unpinned. That would be a separate issue.
- `docs/overview.md` is not changed.
- The reference pages, `userland.md` and `protocols.md`, keep their content.
  At most they gain a link back to the home page.

## Constraints

- Docs principle: one home per topic, link, never restate. The full
  explanation lives only on `probing-hosts.md`. Each setup page carries one
  callout plus a link.
- Every consequence the docs state must be true of `src/otto`:
  `src/otto/host/userland.py` (the probe round, caching for the host's
  lifetime, declared values skipping the probe) and `userland.md`'s
  `sudo_form` text.
- The docs linters, the API-teaching check and `ruff format` on Markdown
  code blocks (`make check-python`) must pass.
- The scaffold's example lab is a placeholder. The panel line is a command
  to run once the lab is real, like the panel's test commands.

## Approaches

1. **One home page and a callout on every setup path (chosen).**
2. **The full explanation on every page.** Rejected: copies drift.
3. **A product warning on unpinned hosts.** Deferred: it is a behaviour
   change, and could be a follow-up issue.

## Left to the builder

- The callout's exact words, and its admonition kind (`{important}` is
  suggested).
- Where in each track page the step fits best.
- The panel line's exact form, for example
  `otto --lab example_lab host example-device probe`, using the scaffold's
  real host id.
