# Guards for three bug classes behind the BusyBox transfer failures: #1295, #1296, #1297

**Status:** design for approval, 2026-11-23. Checked three times by OpenAI gpt-6-astra and Claude Fable 5.2; every finding is applied. Added after the third check, and checked by both again: the field failure reproduced (a kernel without IPv6), and rulings 12 and 13 (the probe measures the netcat rungs).

## Why

The maintainer used the released 0.20.0 against BusyBox hosts and met three failures the test bed had never shown:

- **#1295:** a BusyBox host always transfers with `shell`. The order of the `valid_transfers` list in the lab file is ignored, and a list without `shell` fails at load.
- **#1296:** a netcat transfer fails with a non-integer value from its port scan.
- **#1297:** a netcat transfer waits five seconds and then reports that its listener was never ready.

Each is one instance of a class. This design adds one guard per class, written to fail on today's code first. The fix for each issue is the change that makes its guard pass.

## Rulings (Chris, 2026-11-23)

1. **A list the user writes is honoured as written.** Its first entry is the default and every entry may be tried, whether or not otto has proven it on its bed. An entry may fail on the user's host.
2. **A built-in default applies only when the user gave no list.** No code forces a protocol into or onto a provided list.
3. **No validation against bed proof.** otto does not trim or check a user's list against what its bed covers.
4. **Catch the pattern, not the bug.** Guards first, then a short audit for what a guard cannot express.
5. #1297 is scoped to netcat. #1295, #1296 and #1297 are high priority, after PR #1298.
6. **A default pin always yields to a user's list.** "The reason that the list is provided is to provide a default override." Profiles may keep setting a pin; it applies only when the user wrote no list. No profile is refused.
7. **A BusyBox lab is not required to have `shell` as its default transfer.** `shell` is only what a BusyBox host gets when it writes no list.
8. **The rule covers direct construction in Python** (`UnixHost(valid_transfers=[...])`), in the same pull request.
9. **#1295 is a bug fix, not a breaking change.** No `!`.
10. **The bed cost of forcing every netcat rung is accepted** (put to him as about 64 to 80 cells; at one route per host it is about 40 to 48), with a committed table of each bed host's tools, as its own lane if the cap is at risk.
11. **One bed guest selects the built-in `busybox` profile** (bb1350). `[host_preferences]` keeps winning over a lab entry, as documented.
12. **`otto host <id> probe` measures the netcat rungs** and prints the `nc_options` lines to paste. `auto` stays the default: the run-time choice costs one command per host and is not removed.
13. **The probe's listener check opens no port for now.** A check against a listener the probe opens itself is a low-priority backlog issue.
    Superseded by ruling 4 of `2026-11-24-netcat-conformance-design.md`: the probe now opens, counts and closes one listener of its own.
14. **A custom host class changes its default by redeclaring the list only** (2026-11-24). Its first entry is the default. No public name is added for marking a default.
15. **bb1350's entry in the loading golden is edited by hand** (`tests/_fixtures/unchanged_loading.json`), never regenerated; every other entry stays byte for byte.

## What is established

Measured by hand on the bed's five BusyBox guests (1.16.1, 1.21.1, 1.28.1, 1.31.0, 1.35.0) on 2026-11-23, with the scripts from `src/otto/host/transfer/nc.py` at 29b7bac87. Raw output is on #1296.

- **The netstat port scan needs `grep -o`.** BusyBox 1.16.1 has no `-o`: the script returns grep's usage text and then the port, and `int()` raises `ValueError`. The scan also treated every port as free, because its list of used ports was empty.
- **The `auto` ladder hides that.** `_find_free_port_auto` catches the `ValueError` and tries the next rung, which on 1.16.1 is `/proc`, so the bed's netcat cells pass.
- **The `/proc` rungs read IPv4 only.** BusyBox `nc -l` binds dual-stack (`:::9001`). The `/proc` listener check counts 0 for it; the netstat check counts 1.
- **A kernel without IPv6 breaks both netstat rungs, on every BusyBox version.** BusyBox `netstat` then prints `netstat: /proc/net/tcp6: No such file or directory` on stderr, exits 0 and still lists the IPv4 sockets. stderr is merged into the value. The port scan returns that line and then the port, `int()` raises, and the ladder moves on. The listener check returns that line and then `1`; the poll reads a value that is not a number as "not ready yet" and spends its five seconds. Reproduced on 2026-11-23 with the five pinned binaries in a root whose `/proc/net` has `tcp` and no `tcp6`. This is the maintainer's failure in the field: his log shows the same line in the port scan's error, then `Remote nc listener on port N not ready within 5.0s`. Every bed guest has IPv6, so the bed never saw it.
- **Netcat over ssh to BusyBox works on the bed.** `bed-busybox[bb1350:ssh:nc]` is a conformance cell and passed on 2026-11-23.
- **No bed host selects the built-in `busybox` profile.** The five guests are `os_type: 'unix'` with `command_frame: 'ash'`. This is the main reason the bed never saw #1295.

Read from the code, not run:

- With the tool missing, the ss and netstat port scans print the base port and exit 0, and the listener checks print `0` (they end in `grep -c`). Today only the tool's complaint, merged into the output, stops that from being read as an answer.
- stderr reaches the value on every transport, the stateless ssh exec channel included (`stderr=asyncssh.STDOUT`, `src/otto/host/session.py`).

Not established:

- Whether the `/proc` listener blind spot has bitten anyone. It bites only a host with neither `ss` nor `netstat`, a host pinned to `proc`, or one whose probe failed.
- An integer error from the `/proc` port scan itself was not reproduced. The error the maintainer remembered as `/proc`'s is the netstat scan's, whose text names `/proc/net/tcp6`.

## The three classes

Three read-only surveys of `src/otto` sized each class (appendix).

| Class | The instance | Extent found |
| --- | --- | --- |
| **A. A default overrides explicit input** | The `busybox` profile sets `transfer: "shell"` beside `valid_transfers`; the selection reads a set `transfer` as a pin. | 1 live profile instance; the same shape in direct construction of a host class; any profile layer may do it. |
| **B. A fallback ladder hides a broken rung** | The netcat port ladder swallows a rung's failure; a rung runs only where it happens to be first. | 21 run-time ladders of three different shapes; 5 fall through silently. |
| **C. Command output is parsed on trust** | `int(result.value.strip())` on a script's output. | 68 parse sites: 34 robust, 22 partial, 12 on trust. |

## Guard A: one selection rule at every entry point

**The rule.** For a menu capability (`term`, `transfer`, `impairer`):

- *The list* is the user's if they wrote one, else the default list of the profile or host class.
- *The selection* is, in order: the first of the user's preferences that is in the list; the user's own pin (`transfer: "scp"` written in the lab entry or passed to the constructor); the first entry of the list.
- *A default pin* (one set by a profile or a host class, not by the user) applies only when the user wrote no list (ruling 6).

A pin the user writes is still checked against the list, as today. On a console session `nc` is left out of the selection (one command channel), a pin of `nc` is refused and a list of only `nc` is refused, as today, on both entry points; the host's stored list is never changed.

**One declared table.** One table, in the leaf module that already holds the resolvers (`src/otto/host/capability.py`; `specs.py` imports the host classes, so the table cannot live in either), gives for each host family each capability's pin field, list field, default list and default pin. Today the pin-to-list mapping is written by hand four times in `src/otto/host/specs.py` (`_pins_in_menus` twice, `_selection_menus`, `_resolve_selections`) and the default lists twice (`specs.py` and `src/otto/host/remote_host.py`). Those functions, the host classes' defaults, the merge and the guard all read the table. The selection itself stays in the one resolver (`resolve_active`, `src/otto/host/capability.py`); the constructor calls it instead of validating on its own.

**Product change, lab path.**

- `_merge_host_dict` (`src/otto/host/factory.py`) holds the profile's defaults and the entry as two dictionaries. When the entry writes a list and not the pin, the profile's pin is not carried, and it is left out of the recorded sources, so `otto lab explain` credits the selection to the entry's list and not to the profile. This covers built-in, registered, project and host-class profiles, and `Ref` specs, because all of them reach this one merge. `[host_preferences]` is applied later and is unaffected.
- The `busybox` profile drops `"transfer": "shell"`. Its default list starts with `shell`, so a BusyBox host that writes no list still gets `shell`.

**Product change, direct construction.** `UnixHost(ip=..., valid_transfers=["nc", "sftp"])` must select `nc`. Today it raises "transfer 'scp' is not in this host's transfer menu", because the class default `transfer = "scp"` cannot be told from a pin (`remote_host.py`, `unix_host.py`, `embedded_host.py`).

- *How "not given" is expressed.* The declared default keeps its value (`"scp"`) and becomes a private string subclass that marks it as a default. It is a module-level class that overrides nothing about comparison, so it compares, hashes, pickles and prints as the plain string. A value the caller passes is a plain string and is a pin. Changing the default to `None` is not used: the API check reports a changed default as breaking (`scripts/api_compat.py`), which ruling 9 excludes. **First step of PR 1:** confirm with `make check-api-snapshot` and `make check-breaking` that the recorded public API is unchanged by the marked default; if it is not, stop and report, do not add `!`.
- *What a marked value means.* "Nobody chose this": the constructor selects the head of the list, the caller's list if one was passed and the class's default list otherwise. A marked class default must therefore be the head of its own default list, and a test asserts that for every host class otto ships. That is why no mark is needed on the list: with no list passed, the head of the default list is the default itself, and with the same list passed explicitly the answer is the same.
- *What a host carries.* When the selection came from a default (the class's or the head of the list), the attribute stays marked. The resolver returns a marked value for a head-of-list selection, so a host built from a lab entry carries the mark too, and does not look pinned because the lab path handed its constructor a string. The mark is attached by the resolver, which runs after the lab entry's validation, so validation cannot strip it; nothing serialises a live host. Only a head-of-list selection is marked. A selection that came from the user's preferences is their choice: it stays plain and a copy treats it as it does today. So a copy made with `dataclasses.replace` or `copy_host` (`src/otto/host/os_profile.py`) and a new list reselects from the new list, and a copy that changes something else keeps its selection. When the caller pinned it, the attribute is plain and a copy keeps the pin, failing as today if a new list lacks it. A copy that wants to drop a pin passes the new selection beside the new list, as today; no new operation is added for it.
- *The console rule on a copy.* A term override at run time that lands on `console` already reselects a transfer from the list without `nc` (`src/otto/run_context.py`). It uses the same resolver, so its selection is marked, and the constructor's reselection of a marked value applies the same console rule through the same function (`console_transfer_menu`). A test builds a host whose list starts with `nc`, overrides it to `console`, and checks that it selects the first other entry and that a second copy keeps it.
- *A subclass's class-level default.* A subclass that redeclares `transfer = "ftp"` as a plain string has written a pin, and it is treated as one: it cannot be told from a value the caller passed. A subclass that wants a different default that still yields to a list redeclares the list with that default first and leaves the pin field alone (ruling 14): the inherited default is marked, so the head of the subclass's list is selected. `docs/extend/custom-host-classes.md` says so. The mark stays private. This limit is declared, not engineered around.

**The guard.**

1. *Behaviour, both entry points, one test:* for every built-in, registered and bed-project profile, and for every host class directly, each capability is given a list with empty preferences and no pin, through the lab path (`_merge_host_dict` then the spec build) and through the constructor. Both must select the list's first entry and store the list unchanged. The lists are drawn from the capability's whole vocabulary for that host class, not only the profile's default: every single entry, every ordered pair, and the full default list forwards and reversed. The selection depends only on the first entry and on membership, so pairs cover it; that is about 40 lists per capability and profile, some hundreds of host builds in all.
2. *Stated cases, both entry points:* a pin with no list; a pin outside the list; a preference that beats a pin; an empty list; the three console refusals; an explicit pin equal to the class default (kept, and refused when the list lacks it); a copy with a new list after a default selection (reselects) and after an explicit pin (keeps it).
3. *Survey round trip:* the `valid_transfers` line the survey suggests (`menu_pin`, `src/otto/host/survey/report.py`) is fed back through a `busybox`-profile host and builds, including a suggestion without `shell`.

**Behaviour change, in the release notes.** A lab entry on the `busybox` profile whose list does not start with `shell` selects `shell` today and will select its first entry after the fix. That is the bug being fixed (ruling 9: no `!`). The release-notes page lists a commit without `!` by its subject alone, so the subject states the change; the commit body and the profiles page give the one-line way to keep `shell` (put it first, or write `transfer`).

**Why `shell` stays the profile's own default.** A stock BusyBox with dropbear has no `scp` applet and no sftp server, so `shell` is the one transfer that works on every such host. It is the first entry of the profile's default list and nothing more.

**The bed gets a host on the profile (ruling 11).** bb1350's lab entry selects `os_type: "busybox"`.

- It keeps its own `valid_terms` and `valid_transfers: ['shell', 'nc']` and its userland pins, so its five conformance cells and the recorded cell order do not change.
- The profile also sets `has_bash: False`, which the entry does not set today (so it is true, on a guest with no bash). That becomes false, which is correct, and it changes behaviour on this guest: tunnel discovery skips it, it is refused as a tunnel path member, and a link `--expire` on it is refused. bb1350 is the chaos lane's BusyBox anchor, so the chaos and tunnel lanes that touch it are run before the pull request, and `tests/e2e/chaos/test_connection_drop.py`'s asserted refusal is checked.
- Tests and text that name its current spelling are updated: `tests/unit/test_profiles.py` (its `os_type`), `tests/unit/test_lab_data_hops.py` (which reads any non-`unix` selector as embedded), the notes in `tests/conformance/_bed.py`, `_vocabulary.py` and `tests/_fixtures/profiles.py`, the bed's lab golden (`tests/_fixtures/goldens/tech1_lab.json`, compared by `tests/unit/labs/test_testbed_templates.py`), bb1350's entry in the loading golden (ruling 15), and the capture `docs/examples/captures/probe-bb1350.txt`, regenerated on the bed.
- The getting-started example's BusyBox host (`docs/examples/getting-started/lab_data/lab.jsonnet`) moves to the profile too, with its test's expectations.

**Discoverability of the profile.** Today the `busybox` profile is named in one row of one table (`docs/configuration/os-profiles.md`). PR 1 changes four places:

- The `os_type` row of the host-fields page lists the built-in values.
- The bed's BusyBox guide (`docs/developing/bed-case-study/defining-hosts/busybox.md`) leads with the profile; the long spelling becomes the alternative.
- The `otto init` template's comment names it.
- `otto host <id> probe` prints a hint, as prose outside the block of lines to paste, when two things hold: the host's `os_type` is the default `unix`, and the shell the probe measured for the session is not bash (that is what the probe measures; it does not measure whether bash is installed). The hint says that the built-in `busybox` profile exists for a BusyBox system and links the profiles page. It does not claim the host is BusyBox, because nothing measures that, and it is never a pasteable line, because the probe's rule is that a guess stays out of the pin. The shell must have been measured by this probe, not declared in the entry. A host on any other profile, the user's own included, gets no hint, and neither does any host in a project where the name `unix` or `busybox` resolves to something other than the built-in profile.

**Known limits of PR 1 (tracked on #1300).**

- A pin an os profile sets is stored as a plain value. A later copy of the host that is given a new list treats that pin as the user's. Telling them apart needs a record of who set each value; no copy in `src/` does this today.
- A unix host built directly with `os_type="busybox"` does not get the profile's defaults; only a lab entry does. This predates PR 1.
- A registered host class that redeclares a default list gets that list on the lab path through a fill in the factory, beside the constructor's own default. A custom spec and its class that redeclare different lists still disagree between the two entry points. Moving the fill into contextual validation removes the second site.

**Docs (PR 1).** `docs/configuration/os-profiles.md`, `docs/configuration/lab/host-fields.md`, `docs/configuration/lab/host-preferences.md` (already the one place the precedence order is stated), `docs/extend/custom-host-classes.md`, the `busybox` profile docstring, the generated BusyBox gap page line that says to pin `transfer`, and the four discoverability changes above.

## Guard B: the netcat ladders are declared, each rung is run, and failure is loud

Scope: the two netcat ladders (port finder, listener check). The surveys found three ladder shapes; one mechanism does not fit all of them. The userland options (elevation, checksum and the rest) probe once and settle a value where "absent" is an answer. In-shell ladders with no option to pin a rung stay audit items.

**One declaration.** A table names each netcat ladder's rungs in order, which rungs `auto` may choose, the control values that are not rungs (`auto`, `custom`), and the tool each rung needs. Today the order is spelt in seven places: two `*_ORDER` constants, the shell probe, two tuples in `prepare`, the two dispatchers, the listener check's own fallback resolver (`_resolve_listener_strategy`), the option docstrings and `docs/cli/host/netcat.md`. The constants, the probe, the tuples, the dispatch and the fallback resolver are derived from the table; the docs are checked against it. The table also carries each rung's script builder as a pure function, so a rung can be run without a transfer backend. The builders of the `/proc` rungs take the directory they read (`/proc/net` by default).

**Scripts that say whether their tool worked.** Every built-in port scan and listener check captures its tool's exit status and prints a sentinel when the tool is missing or failed. An empty listing counts as "nothing in use" only when the tool succeeded. A listener count of 0 from a tool that succeeded is still "not ready yet". A kernel without `/proc/net/tcp6` is not a failure of the `/proc` rungs. Every script sends its whole stderr away, the shell's own complaints included, so a tool that complains and still answers gives a clean answer. Today a missing tool is noticed only because its complaint spoils the value, so the tool's status is checked before its output is filtered, and `grep -c` exiting 1 on a count of 0 is not a failure.

**Loud failure.**

- *Port finder, `auto`:* a rung that fails is logged at warning once per transfer backend (the record sits beside the cached strategy on the transfer's own state, so a copied or rebuilt host may warn once more), naming the rung and the option that pins another; the next rung is tried. The cached rung and the cascade are one path, so a cached rung that fails is reported once and not retried by the cascade. `HostUnreachableError`, cancellation and `NcPortSharedError` still stop the ladder at once. If every rung fails, the one existing error names the host, each rung's reason and the pin option.
- *Port finder, pinned:* a named error that names the ladder, the rung, the host and what came back. A pinned rung already fails today; this changes a bare `ValueError` into that error. It derives from `HostCommandError`, which is what the port finder raises today, and from `ValueError`, so a caller that catches the bare error still catches it.
- *Listener check:* it stays chosen by tool presence and gains no fall-through. A built-in rung whose tool failed raises at once. So does a value that is text and not a count: it is never read as "not ready yet", and the error quotes what came back. A check that timed out or returned nothing is not an answer either way and is polled again, as today; if the deadline passes that way the timeout says the check never answered. A check whose command never ran (a dead session, no exit status) is likewise no answer. A timeout names the stage (remote readiness, or the connect that follows it: connect on a direct route, forwarded connect through a tunnel), the host, the rung that was polled, the option that pins another, and whether the listener process had already exited; the put and get paths pass the listener's handle to the wait for that. Both failures derive from `ConnectionError`, as `NcPortSharedError` does, so a transfer reports them as a failed result exactly as it reports a listener timeout today, and the handlers that build that result carry the error's text through unchanged.
- *The existing retry.* A failed netcat attempt is rerun once on a fresh port today (`nc.py`, put and get). A failure that cannot change on a second try, a tool that failed or a pinned rung that failed, is not retried. A value that is not a count has no known cause, so it keeps the one retry. A readiness timeout keeps its one retry, and the error then says that two attempts were made.
- *`custom`* keeps its contract: exit 0 is ready, anything else is not ready yet and is polled. No sentinel or count is asked of a user's command.

**Fixes this forces (#1296, #1297).** Every built-in script drops its stderr. The netstat scan stops using `grep -o`. The `/proc` rungs read `/proc/net/tcp6` as well. Both five-second waits name their stage.

**The guard.**

1. *Per-version scripts, no bed (red first):* `tests/busybox` already runs otto's command spellings against the five pinned BusyBox binaries. Each rung's script gets a row there. The netstat row fails today on 1.16.1.
2. *No IPv6, no bed (red first):* the tier is hermetic and its chroot harness was removed on purpose, so this needs no root. For the netstat rungs, a stand-in `netstat` in the applet directory prints the complaint the real binaries were measured to print, then runs the real applet; both netstat rows fail today on all five versions. For the `/proc` rungs, the builder is pointed at a directory with `tcp` and no `tcp6`.
3. *Dual-stack listener, no bed (red first):* a local dual-stack listener, counted by each listener rung the machine has. The `/proc` rung fails today.
4. *Missing tool, no bed:* each script run with its tool absent gives the sentinel, never a port or a count.
5. *Every path, no bed:* a failing rung reached through the probe's choice, through the cache and through the cascade gives the same report, and the three errors that must stop the ladder do.
6. *Completeness:* every `Literal`-typed field of the netcat and userland option specs is in exactly one of three named sets: a declared ladder's pin, a settled userland value (the probe-once options), or not a ladder. For a ladder's pin, the `Literal`'s alternatives must equal the table's rungs plus its control values. A new option, or a new alternative on an existing option, that skips the table fails.
7. *Every rung runs on the bed (ruling 10):* one small contract per (host, ladder, rung), forced through the pin option: one put, which runs both ladders, and one get where the host's route is tunnelled. A get on a direct route binds a local listener and runs neither ladder, so it has no cell. Each host runs it over one route, its first declared term that is not `console`; the rung scripts run on the transfer's control session whatever the term, and the existing transfer contract already crosses terms. The bed host builder (`build_bed_host`, `tests/conformance/_bed.py`) carries the pinned option into the host before its transfer backend is built, and the contract asserts the rung that actually ran. The domain is a committed table of which rungs each bed host can run (`ss`, `netstat`, `python`, a readable `/proc/net/tcp`); the contract asserts the table in both directions, so a stale table fails instead of dropping a test. `custom` has no cell: it is a user's command and is covered by unit tests. About 40 to 48 new cells at one route per host. The axis is not folded into the support matrix. Its run time is measured before it joins `conformance-bed`; if the lane's 1500 s cap is at risk it runs as a lane of its own, named by `gate-scope` for the same diffs.

**The probe measures the rungs (rulings 12 and 13).** This is one more measurement of the survey that `otto host <id> probe` already runs, not a second mechanism.

- *When.* The rungs run when the survey found `nc` supported on the host and the session is not a console (netcat cannot be selected on a console). Otherwise one sentence says why they were not measured. A ladder the entry already pins, `custom` included, is reported as declared and is not run; a pin is recognised before any command runs, so a failed measurement never hides it. The measurement is the survey's last, so a host where it hangs cannot take the shared time budget from the older rows, and an error of any kind from its commands is reported as not measured or as that rung failing, never raised out of the survey.
- *How.* One function runs a rung's script, from the table's builder, over a runner and reads the answer with Guard C's reader. The transfer and the probe both call it. The probe passes the runner the transfer uses (`host.exec`), because the survey's own (`host.run`) is a different shell and `PATH` over ssh. Each ladder is walked in its order and stops at the first rung that works; a rung met on the way whose tool is present and whose script failed is named.
- *The listener rungs.* Each is asked about a TCP port from the survey's own inventory of the host's listeners, and must count at least one. The port comes from the inventory, never from the entry: behind a port forward the declared port is the forwarder's, while a port the inventory lists is on the host. A listener other than the session's own port is preferred, and the session's port is used when the inventory lists no other, so a host with only its sshd is still measured. With no inventory, or no TCP listener in it, the listener ladder is unmeasured. This check cannot see a rung that misses a listener bound the way `nc -l` binds; guard test 3 covers that case, and #1299 is the backlog issue for a check against a listener the probe opens.
- *What it changes on the host.* Nothing is left behind and no port is opened for listening. The `python` port rung binds and closes a socket, as it does in a transfer; it is reached only when `ss` and `netstat` are both absent or broken.
- *What it prints.* The result rides the survey, and `menu_pin` (`src/otto/host/survey/report.py`), which already prints one `<proto>_options` fragment per protocol, prints the one `nc_options` fragment. A measured rung is pasteable; an unmeasured ladder prints nothing. Under `--user` the rungs are measured as that user and listed, and none goes in the fragment: a pin in the entry applies to every login and a pinned rung has no fall-through, while another login may not have the same tools on its `PATH`.

**Docs (PR 2 and 3).** `docs/cli/host/netcat.md`, the strategy option docstrings (`src/otto/host/transfer/base.py`), `docs/configuration/host-options.md`, and in PR 3 the probe's page and its captures (`docs/examples/captures/probe-bb1350.txt`, `probe-test1.txt`).

## Guard C: one reader for a value a command returned

**One pure reader.** A function over the returned text, not a runner: the sites run through different runners (a transfer's control session under a lock, a docker parent, a probe that must not raise) and keep them. The reader takes the text and a kind and returns the value or raises one named error quoting what came back. Kinds: integer with bounds, word from a set and absolute path (the kinds PR 2's callers use); hex id, IP address and a "lines of a kind" form for a listing are added with the audit sites that need them, not before. It has a non-raising form for probes that fold failure into "unknown".

**Framing, stated honestly.** A script that frames its answer with the existing marker (`marker_cmd`, `after_marker`, `src/otto/host/product.py`) drops what was printed before the script started (banners, sudo). It does not drop what the script's own tools print. A safe script does both: sends its tools' stderr away and reports tool failure by sentinel (Guard B), and the reader rejects anything that is not exactly the stated kind. The marker helpers stay in `product.py`. A module of their own was tried and measured: every run that loads `product` paid four more file operations for it (the import budget's bootstrapped-run surface), and it bought nothing, because every caller of the helpers already imports `product`.

**Existing readers folded in.** `login_home` (one absolute path, `src/otto/host/unix_host.py`) and `parent_clock` (one integer, `src/otto/host/docker_cli.py`) use the reader, so it is not a second copy.

**The guard: a lint rule.** An ast-grep rule in `.ast-grep/rules`, run by `make lint-arch`, with rule tests, bans `int(...)` and `float(...)` applied directly to a result's stripped value in `src/otto`. Seven hits today: six in `nc.py`, one in `transfer/shell.py`. Nothing wider: `.value` is not specific to command results, and the other shapes the surveys found are not syntactically distinguishable from intended code.

**Not in the rule, on the audit list:** the 12 on-trust and 22 partial sites that a syntactic rule cannot see, led by the three "first line of `docker ps -q` is the container id" sites.

## The short audit

One tracking issue with a checklist, filed when this design is approved. Verified by reading unless marked.

- Four of the five BusyBox guests still do not use the built-in `busybox` profile (bb1350 moves in PR 1).
- The five BusyBox guests pin every userland option in the bed lab file, so a normal bed run never probes them.
- netcat's size checks hard-code `stat -c` and ignore the `stat_size` option (`nc.py`).
- A file operation hard-codes `base64 -d` and ignores the decode-flag option (`file_ops.py`).
- Daemon launch drops from `systemd-run` to `setsid` in the shell with no record of which ran (`daemon.py`).
- `docker ps -q` first line as a container id; "any output" as "the stack is up" (`docker_host.py`, `compose.py`).
- Errors that name a key the user never wrote, because a profile set it (`specs.py`, three sites).
- `--term console` re-resolves the transfer and loses the entry's pin (`src/otto/run_context.py`).
- Four of the five BusyBox guests declare no `has_bash` and so read as having bash, which they do not; tunnel and link code branch on it.
- The survey's listener inventory is a third `ss`, `netstat`, `/proc` ladder (`src/otto/host/survey/inventory.py`); it merges stderr on purpose and parses lines, so it is not one of the two netcat ladders, but it is the same choice written again.
- No bed guest runs a kernel without IPv6; the per-version tier covers it without a bed.
- *Hypothesis:* a survey probe uses a `timeout` argument style some BusyBox builds reject (`survey/dial.py`).

## Approaches considered

1. **Guards first, then a short audit (chosen by the maintainer).**
2. **Audit first.** Finds what a guard cannot express, but leaves nothing behind unless the guards are written anyway.
3. **Fix the three issues only.** The next instance of each class arrives the same way.

For Guard A, two ways to honour ruling 2:

- **Drop a default pin under a user's list (chosen, ruling 6).** One rule in the merge and in construction. No existing profile breaks; a profile keeps two ways to say "default".
- **Refuse a pin in any profile (rejected).** One way to say "default", and a break: the `busybox` profile, five `[os_profiles]` tables in the bed's own projects (`tests/project1`, `tests/project3`: `transfer = "console"`) and any user's table would have to change. It also would not cover direct construction.

## Order of work

1. **PR 1: Guard A and #1295.** First the API check on the marked default. Then the table, the merge rule, direct construction, the three tests, the probe's hint, docs. One bed lab change: bb1350 selects the `busybox` profile, with the chaos and tunnel lanes that touch it run before the pull request.
2. **PR 2: Guard C and the netcat fixes (#1296, #1297).** The reader and the lint rule; the scripts' stderr drop, tool-status sentinel, `grep -o` and tcp6 fixes; loud failure for the port finder and the listener check. Guard B's tests 1 to 5 land here and fail first.
3. **PR 3: the rest of Guard B.** The ladder table and its derived sites, the completeness test, the bed axis and its tool table, and the probe's measurement of the rungs.

## Not in scope

- Validating a user's list against the bed (ruling 3). scp and sftp against BusyBox (ruling 5).
- A mechanism for every ladder in otto. The userland options and the un-pinnable in-shell ladders stay as they are, with their findings on the audit list.
- Recorded-output fixtures. The per-version BusyBox tier runs the real binaries and does not go stale when a script changes.
- The local and compile-time ladders (gcov selection by stamp, the kmodcov kernel ladder).

## Appendix: survey findings

**Class A (defaults).** 14 layering mechanisms read. Live: the `busybox` profile pin (`os_profile.py`). Same shape: host class defaults `term`, `transfer`, `impairer` beside their lists. Open door: any profile layer may set those keys (`profile_inputs`, `src/otto/host/construction.py`); host-class profiles and built-ins are validated by different functions. Latent: `model_fields_set` checks that treat a profile-set key as user-written. Follow the rule already: profile selection, `merge_host_preferences`, `merge_creds`, the inventory join, `userland_options`, ssh algorithm lists, `resolve_init_timeout`.

**Class B (ladders).** 21 run-time ladders. Silent fall-through: the netcat port finder (two `except (RuntimeError, ValueError)` sites), the netcat listener check (polls to timeout), daemon launch, the netcat timeout prefix, the sudo form probe. Never forced on a real host: netcat port `python` and `custom`, listener check `proc`, `stat_size=wc`, `base64 --decode`, `checksum` absent, `timeout_style` absent, `elevation` none, the `setsid` fallback, the `socat` link-probe backend.

**Class C (parsing).** On trust, by impact: the five netcat port finders; `docker ps -q` first line as a container id (three sites); `stack_already_up`; `image_id_of`; `_container_ip`; the coverage temp directory. Robust models: `marker_cmd` and `after_marker`, `classify_gcda_listing`, the survey's `OTTO_TOOL=` line, `_parse_applet_answers`, `_read_ownership`.
