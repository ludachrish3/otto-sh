# Project activation

**The labs you load decide which projects a command deals with, and the
switches override that decision in either direction.** A project that is not
active for an invocation is not walked, cannot have its instructions
dispatched, and — this is the part that changes daily life — cannot fail your
run by being broken.

That last clause is why the feature exists. `OTTO_SUT_DIRS` names every repo
in the workspace, and before activation every one of them was equally present
in every invocation: a colleague's half-finished repo with an unimportable
`init` module took down `otto host dut1 run uptime` for everybody, and the only
cure was to edit the environment variable. Activation makes "which projects is
this run about?" a question otto answers per invocation, from the labs you
already had to name.

## The model

Resolution order, stated once and consulted by every enforcement point:

**explicit switch > lab inference > default-on**

- An explicit `--exclude-projects` wins over everything; then
  `--include-projects`; a name in both is a usage error.
- Otherwise, if the repo declared a `[project]` table, its `lab_patterns` and
  `host_patterns` are matched against the loaded labs. A repo no loaded lab
  applies to — or one whose `host_patterns` match no host in the labs that do —
  is inactive. See {ref}`project-scope` in {doc}`../configuration/lab-config`
  for that schema.
- Otherwise the repo is active. **A repo that declared no `[project]` table is
  always active**, which is what keeps a single-repo workspace exactly as it
  was, and what makes activation opt-in for everyone else.

With no lab loaded there is no inference to make and every repo is active.

## The switches

| Switch | Meaning |
| ------ | ------- |
| `--include-projects`, `-I` | Force these projects **active**, overriding lab inference |
| `--exclude-projects`, `-E` | Switch these projects **off**, overriding lab inference |

Both are root options, so they go **before** the subcommand. Both repeat, and
each occurrence also splits on commas: `-E a,b` and `-E a -E b` are the same
selection. **Only** the comma separates — unlike `OTTO_SUT_DIRS`, which also
splits on the OS path separator, because these are names rather than paths.
Segments are stripped, and an empty one is dropped rather than refused, so a
trailing comma or a shell-built `-E "$NAMES"` is harmless.

Names are matched against the discovered repos' `name` fields the way project
dependencies are: PEP 503-normalized, so case and `_`/`-`/`.` punctuation do
not matter. `-I radio_FW` finds `radio-fw`.

A name that matches no discovered repo is a usage error rather than a silent
no-op, because the failure mode it prevents is a run you believed was narrowed
and was not:

```console
$ otto --lab radio -E radio-fww run flash-radio
no project 'radio-fww' — did you mean 'radio-fw'?
```

So is a line that says both things about one project:

```console
$ otto --lab radio -I radio-fw -E radio-fw run flash-radio
Usage: otto [OPTIONS] COMMAND [ARGS]...
Try 'otto -h' for help.
╭─ Error ──────────────────────────────────────────────────────────────────────╮
│ Invalid value for --include-projects / --exclude-projects: project(s)        │
│ radio-fw appear in both --include-projects and --exclude-projects — pick one │
╰──────────────────────────────────────────────────────────────────────────────╯
```

Both exit **2**: they are things you typed, not things the lab decided.

Tab completion offers the discovered repo names. It is fed by discovery alone —
never by a full bootstrap — so `otto -I <TAB>` cannot be slowed or hung by
another repo's `init` module.

## What enforcement looks like

Activation shows up at exactly three surfaces, and each says which axis
decided.

### Fleet walks skip the repo, out loud

The project verbs (`install`, `uninstall`, `status`, and the rest) walk the
active repos and log a line for each one they left out. Switched off:

```console
$ otto --lab radio -E radio-fw run status
WARN     repo 'radio-fw' switched off for this run (--exclude-projects radio-fw)
         — skipping it for status
```

Left out by the labs, where the answer *is* the display, so `status` prints a
row instead of a warning:

```console
$ otto --lab bench run status
radio-fw  not applicable (labs: bench)
lab is uninstalled
```

The asymmetry is deliberate. Every reason a lab verdict can leave a repo out is
already printed beside it, but nothing in that report records that you typed
`-E` at all — so the switch gets a line and the ordinary "my lab does not
include that project" case does not.

### Instruction dispatch refuses, and tells you how to undo it

An instruction belongs to the repo that registered it. Ask for one whose owner
is inactive and otto refuses **before the body runs**, naming the axis and
handing you a runnable fix:

```console
$ otto --lab bench run flash-radio
'flash-radio' belongs to repo 'radio-fw', which is inactive for the loaded lab(s) [bench] (lab_patterns: radio)
  activate it: -l radio    or: -I radio-fw
```

```console
$ otto --lab radio -E radio-fw run flash-radio
'flash-radio' belongs to repo 'radio-fw', which was switched off for this run (--exclude-projects radio-fw)
  activate it: remove --exclude-projects radio-fw    or: -I radio-fw
```

Both exit **1**. The hint is printed unwrapped whatever your terminal width,
because its whole job is to be pasted.

A third shape appears when the labs match but the hosts do not — the repo's
`host_patterns` select nothing in the loaded labs — and it names the patterns
rather than the labs, because there the labs are the part that is already
right.

First-party instructions (`status`, `install`, and the rest of the built-ins)
have no owning repo and are never refused.

### A broken repo you are not using is a warning, not a failure

This is the change you will notice most. A repo whose `init` module fails to
import used to end every invocation. Now, if that repo is inactive, the failure
demotes to one line and the run continues:

```console
$ otto --lab bench --show-lab
warning: repo /work/radio-fw: failed to load radio_fw: ModuleNotFoundError("No module named 'does_not_exist_anywhere'")
warning: repo 'radio-fw' failed to load, but is inactive for this run (not applicable to lab(s) [bench]) — continuing without it
Lab(
    name='bench',
    ...
```

Load a lab it *is* part of and the same breakage is fatal again:

```console
$ otto --lab radio --show-lab
warning: repo /work/radio-fw: failed to load radio_fw: ModuleNotFoundError("No module named 'does_not_exist_anywhere'")
Cannot run commands while a repo fails to load (see warnings above).
```

**A repo you `-I` stays fatal when it is broken.** You said it was part of this
run; a run cannot be partly about a repo that did not load.

```{note}
This gate gets to demote on the **lab axis only** — the explicit switches plus
`lab_patterns` — because it runs before the lab is built, and protecting lab
construction from a half-registered world is its job. A repo that would have
been inactive only because its `host_patterns` match no host in the lab is not
yet known to be inactive here, so its import error is still fatal.
```

## Dependencies of a project you switched off

A repo can declare `[dependencies]` on other repos (see
{doc}`../configuration/settings`). Dropping a provider that something else
requires is a real decision, and otto splits it by *which axis* dropped it.

**The labs dropped it** — the dependent says the provider must be handled, the
labs say it cannot be here. That is a contradictory configuration, so a walk
that would build on top of it refuses before contacting any device, naming both
fixes:

```console
$ otto --lab bench run install
error: repo 'radio-app' requires 'radio-fw', but radio-fw is not applicable to
the loaded lab(s) [bench] (lab_patterns: radio). Load a lab radio-fw applies to,
or pass --exclude-projects=radio-fw to declare it handled externally.
```

**You dropped it** with `-E` — that is you signing off on exactly this case
("I installed that one by hand"), so it warns and proceeds:

```console
$ otto --lab radio -E radio-fw run install
WARN     repo 'radio-fw' switched off for this run (--exclude-projects radio-fw)
         — skipping it for install
WARN     repo 'radio-app' requires 'radio-fw', which was switched off
         (--exclude-projects radio-fw) — proceeding as though radio-fw is
         handled externally
```

Only **build-up** walks (`install`, `install_tools`) refuse. A teardown or a
read-only verb warns and carries on: a cleanup that dies rather than removing
what it can leaves the lab dirtier than it found it, and a report that dies
whole is worse than a report with a row in it.

An **optional** dependency never refuses on either axis. It logs that it went
unsatisfied and names the axis that dropped it.

## Edge notes

**Excluding the repo you are standing in is allowed.** `-E` of the driving repo
is not special-cased: the lab still loads from it, and the walks simply skip it.

```console
$ otto --lab bench -E fleet-tools run status
WARN     repo 'fleet-tools' switched off for this run (--exclude-projects
         fleet-tools) — skipping it for status
```

**`-I` does not invent a fleet.** Forcing a repo active whose `host_patterns`
match no host in the loaded labs makes its own fleet walk fail loudly rather
than quietly doing nothing — which is the point of asking for it by name:

```console
$ otto --lab radio -I radio-fw run install
error: repo 'radio-fw' applies to loaded lab(s) radio, but its [project]
host_patterns match no host there, so every fleet walk it drives would be
empty.
    host_patterns: nosuchhost
Widen host_patterns in /work/radio-fw/.otto/settings.toml (['.*'] is every
host in those labs), or load a lab that holds the hosts this repo targets.
```

**Help and completion always list every instruction.** Activation is decided
per invocation, and `otto run -h` is not an invocation of any instruction — so
`flash-radio` is listed under `--lab bench` even though running it there is
refused. Discovery and dispatch answer different questions on purpose: a list
that changed shape with the lab would leave you unable to find out that the
instruction exists at all.
