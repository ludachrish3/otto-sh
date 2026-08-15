# Two dry-run follow-ups the contract work left open

Both were found while making `host`, `link` and `tunnel` honest under
`--dry-run` (that work is on main). Neither was folded in: the first is a
different subsystem with a real safety question, the second is presentation.
This file exists because that commit message called them "queued" and, until
now, nothing was.

## 1. `BaseHost.reboot` has NO dry-run guard, and `hard=True` really power-cycles

`src/otto/host/host.py:1120` — the body contains zero `is_dry_run()` checks
(verified by grep, not by reading around it). Three separate consequences, in
descending order of how much they should worry you:

**`hard=True` power-cycles a real machine.** `:1144-1145` goes straight to
`self._require_power_control().cycle(...)`. `PowerController` drives the actual
PDU/hypervisor, not a host command, so nothing in the dry-run plumbing is even
in the path. `otto -n ... reboot --hard` cycles the box. This is the one that
matters: `--dry-run` is what someone types when they are NOT sure, and the
project rule is that otto never powers a real VM without asking.

**A soft reboot drops live transports.** `:1147` `_soft_reboot()` goes through
the command path, so under a dry run it gets the synthetic
`Status.Skipped, retcode=0` — and `Skipped.is_ok` is **True**. So `:1161-1162`
fires `rebuild_connections()` on a reboot that never happened, tearing down
every cached transport for a host that is still up. A dry run with a real side
effect.

**The wait phase really probes.** `:1163+` — `wait_until_down` /
`wait_until_up` reach `is_reachable` → `verify_connection`, which logs
`[DRY RUN] Connection verified` and then genuinely dials
(`remote_host.py:265-273`). So a dry run also burns the full down/up timeout
against a live host.

Shape: the same rule the rest of the contract now follows — announce what would
happen (which host, soft vs hard, which power controller, the wait bounds), do
nothing, and return a report that is not `is_ok` in a way that makes
`rebuild_connections()` fire. Note the `is_ok` interaction specifically: fixing
only the top of the function and leaving `Skipped.is_ok` True downstream is how
the transport teardown survives a partial fix.

Guard it with an injected hostile condition, not an inherited one: a host whose
`PowerController` is a spy that records calls, asserting the spy was NOT called
under `--dry-run` and IS called without it. The positive half is what stops the
test passing against a `reboot` that does nothing at all.

## 2. `otto link repair --all -n` repeats its caveats once per link

The dry-run sweep prints the same "not checked:" paragraphs verbatim for every
link — roughly 15+ terminal lines each at 80 columns, so a lab with a handful of
links buries the per-link plan under identical prose. A single impair or repair
is fine and should not change; this is specific to the sweep.

Shape: hoist the shared caveats to one block for the whole sweep, keep the
per-link `would:` lines. Worth doing when someone is next in
`src/otto/cli/link.py`; not worth a dedicated change.
