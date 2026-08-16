# CLI dispatch metadata costs three declarations per field

Found while threading `dry_run_preview` through the CLI for the dry-run
contract workstream. Nothing is broken; this is a change-cost problem that
will only get worse, and it is the one place the registration surface can
realistically drift.

## What's actually there (surveyed, not assumed)

The registration surface is healthier than "how many mechanisms are there?"
suggests. There is **one authority** — `CommandSpec` in the `CLI_COMMANDS`
registry — reached two ways, and the decorator is not a parallel
implementation: `@cli_command` calls `register_cli_command` itself at
`src/otto/cli/registry.py:176`. So the two top-level entry points cannot
drift from each other by construction, which is what makes the module
docstring's promise true ("first-party subcommands and third-party plugins
register through the same `register_cli_command`").

`@cli_exposed` is a different axis rather than a duplicate: it marks a
coroutine method on a host class, and `HostGroup` synthesizes one
`otto host <id> <verb>` per mark, filtered to the *resolved* host's class
(`src/otto/cli/expose.py`). Different lifetime, different cardinality, shared
downstream dispatch.

The bare `typer .command()` calls across `link.py`, `tunnel.py`, `cov.py`,
`reservation.py`, `run.py`, `schema.py` are not a third mechanism — they are
subcommands inside a group whose group is registered normally.

## The cost

Adding one piece of dispatch metadata means editing **three signatures** —
`register_cli_command`, `cli_command`, `cli_exposed` — and it is read in
**two** places: the `CommandSpec` for groups, and a `__cli_*` attribute stamp
for leaves. As of this writing there are 6 `__cli_*` stamps and 5 `__otto_*`
stamps, read across 8 modules.

`dry_run_preview` paid that cost in full. So did `output_dir` before it. The
next field will too.

## Shape of the fix

Collapse the metadata into one shared dataclass that all three seams accept
and one reader resolves, so a new field is one edit rather than three. The
`__cli_*` stamp mechanism itself is fine and should stay — see the warning
below.

## Two things NOT to "simplify" while doing this

**The two-level system (spec-level + leaf-level stamp) is load-bearing, not
redundant.** `otto test`'s `dry_run_preview` opt-in deliberately lives on the
LEAF (`src/otto/suite/register.py`), not on the `test` `CommandSpec`, because
a spec-level flag would also have opted in the suite-less `otto test --tests
foo` selection path (`src/otto/cli/test.py:625-636`), which must keep the safe
default. Collapsing to one level silently runs real pytest bodies under
`--dry-run`. There is now a guard for this; check it goes red before you
believe any simplification here.

**`register_cli_command` and `cli_command` are not two implementations.** If
the refactor makes them look like siblings that each build a `CommandSpec`,
it has introduced the drift it was meant to prevent. Keep the delegation.

## Do these three together

**These are one problem in three places: registration truth lives in more than
one location, so otto's own path and the path everyone else uses can drift.**
Each reads as a small local nit in isolation, which is exactly why they keep
being deferred one at a time. Whoever picks up any one of them should read all
three first and decide the shape once.

1. **This file** — dispatch metadata costs three declarations
   (`register_cli_command`, `cli_command`, `cli_exposed`) plus two readers.
   *Symptom: adding a field means editing three signatures.*
2. **[test-harness-declares-registration-2026-08-16.md](test-harness-declares-registration-2026-08-16.md)**
   — `DispatchRunner` DECLARES registration metadata instead of READING the
   shipped registration, so tests certify a command that does not exist.
   *Symptom: three separate green-on-drift sightings, one of which hid a
   crash on the flagship surface.*
3. **[registry_builtin_registration_symmetry.md](registry_builtin_registration_symmetry.md)**
   — several string registries load built-ins by constructing the backing
   dict at import while third-party entries go through `register_*()`, so
   otto's own built-ins never exercise the entry point users rely on.
   *Symptom: the built-in path can work while the public path is broken.*

The common fix direction is the same in all three: **one authority, read by
everyone, including otto itself.** Item 3 already resolved three of its four
registries this way (empty seed dict plus a `_register_builtin_*()` bootstrap
through the public function) — that is the pattern to copy, and it is why
item 3 is the best one to read first.

## Nearest symptom, for context

`tests/_fixtures/dispatch.py:869` — `DispatchRunner` builds its OWN
`CommandSpec` rather than reading the shipped registration, so tests driven
through it certify a harness-declared command. This is a real mirror and it
already bit: the link/tunnel CLI dry-run tests would have stayed green with
`dry_run_preview` missing from `builtin_commands.py` while production
`otto link impair -n` silently seam-stopped. A registration-shape pin was
added during the dry-run workstream; the underlying mirror remains, and it
is item 2 above.
