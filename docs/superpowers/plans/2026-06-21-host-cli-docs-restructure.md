# Host CLI Documentation Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the flat, sprawling `otto host` user-guide pages into one nested "Hosts" group, splitting `host.md`'s 462-line braid along its three concerns and consolidating the five small capability guides into one page.

**Architecture:** Pure documentation move under `docs/guide/`. Create a `docs/guide/host/` subdirectory with six MyST pages (`index`, `commands/index`, `commands/netcat`, `capabilities`, `connections`, `configuration`), migrate content from the seven existing pages, rewire the toctrees, and delete the old files. No source code or autodoc changes.

**Tech Stack:** Sphinx + MyST-parser (Markdown), `sphinx-build -W` (warnings are errors), `doc8` RST lint, Sphinx doctest. Build via `make docs`.

**Spec:** `docs/superpowers/specs/2026-06-21-host-cli-docs-restructure-design.md`.

## Global Constraints

- **The build is `-W` (warnings are errors).** Every commit MUST produce a clean `make docs`: no orphan pages (every doc in exactly one toctree), no toctree entry pointing at a missing doc, and **no duplicate `(label)=` definitions** across docs. This is why the two anchors (`connection-options`, `per-host-toolchain`) move only in the final task — they may never exist in both the old `host.md` and the new `configuration.md` at the same time.
- **Per-task gate is `make docs`** (runs `doc8` + `sphinx-build -W -b html` + doctest). Faster inner-loop check: `make docs-html`. There are zero source-code changes, so `make coverage`/`nox`/`ty` cannot regress and are not part of any task.
- **Content is moved, not rewritten.** Migrate prose, JSON, and code blocks verbatim from the cited source sections. Only the explicitly shown new prose (verb-model framing, the capability table, pointer sentences) is authored fresh. Preserve the doctest block byte-for-byte.
- **Commits: do not self-commit.** The repo's `prepare-commit-msg` hook needs a real TTY for AI-attribution; an agent commit mis-tags it. Each task's final step **stages** the files and presents the ready commit message for Chris to run `git commit` himself.
- **The doctest namespace is global** (`docs/conf.py` `doctest_global_setup` provides `asyncio`, `Status`, `CommandStatus`, `split_on_commas`, `LocalHost`, `human_readable`, and a `run(coro)` helper). The migrated doctest works unchanged on its new page.
- **Source-of-truth for content boundaries:** spec §5 (old→new content map). Section line numbers in this plan are *current-state hints*; they shift as `host.md` is gutted, so locate sections by their `##`/`###` heading text, not by line number.
- **Forward-reference rule (`-W` ordering):** the new pages reference each other mutually, so no creation order avoids forward links. Any `{doc}` link a task's page makes to a page created in a *later* task MUST be written as plain **bold text** (no `{doc}` role) to keep that commit's `-W` build green — exactly as Task 1 does for its verb-model links. Links to already-existing pages (created in an earlier task, or pre-existing like `../embedded`, the API tree, the cookbook, the coverage guide) use real `{doc}` roles. **Task 6** converts every deferred bold placeholder into its real `{doc}` link once all pages exist, and runs the final clean build. The known forward placeholders are: index→{commands/index, capabilities, commands/netcat, connections, configuration}; commands/index→{../capabilities, ../connections}; commands/netcat→{../connections, ../configuration}; connections→{../configuration}.

---

### Task 1: Overview page + wire the Hosts group

Create the landing page `host/index.md`, wire it into the User Guide toctree, and move the four "framing/meta" sections out of `host.md` (including the doctest). After this task `host.md` still exists (holding §Running…§Overriding protocol) and still builds.

**Files:**
- Create: `docs/guide/host/index.md`
- Modify: `docs/guide/index.rst` (toctree)
- Modify: `docs/guide/host.md` (remove migrated sections)

**Interfaces:**
- Produces: the doc target `host/index` (the Hosts landing page) and an (initially childless) nested toctree inside it that later tasks append to. Produces the new label-free Overview content.
- Consumes: nothing.

- [ ] **Step 1: Create `docs/guide/host/index.md`**

Migrate verbatim from `host.md`: §Syntax (the `otto host <host_id> <command>…` intro + fenced syntax block), §Listing hosts (`--list-hosts`), §Dry run (`--dry-run`/`-n`), and §Programmatic equivalents (the `{doctest}` block + the `put`/`get` Python example + the trailing `{note}`). Then prepend the new framing prose. Final file:

````markdown
# Working with hosts

`otto host` provides direct access to host operations from the command line --
running commands, transferring files, opening an interactive shell, and invoking
host capabilities -- without writing a test suite or instruction.

## Syntax

The host ID comes before the subcommand, so all host-level options apply to every
action:

```text
otto host <host_id> <command> [ARGS...] [OPTIONS]
```

## The host verb model

Every `otto host` action is a **verb** on the host. The four core verbs --
`run`, `put`, `get`, and `login` -- are built in (see {doc}`commands/index`).
Every other verb is a **capability verb**: a host method marked `@cli_exposed`
that otto turns into a subcommand automatically, scoped to that host's class.
`otto host <host_id> --help` lists exactly the verbs the chosen host supports.
See {doc}`capabilities` for the capability verbs and {doc}`commands/netcat`,
{doc}`connections`, and {doc}`configuration` for transport and tuning.

## Listing hosts

Use `--list-hosts` to see which host IDs are available in the loaded lab:

```bash
otto --lab my_lab host --list-hosts
```

This is the same `--list-hosts` option available on the top-level `otto` command.

## Dry run

Like all otto commands, `--dry-run` (or `-n`) previews what would happen without
executing commands or transferring files:

```bash
otto --lab my_lab --dry-run host router1 run "make install"
```

## From Python

The `otto host` subcommands map directly to methods on the
{class}`~otto.host.host.BaseHost` class. Everything `otto host` does from the CLI
can also be done inside instructions and test suites:

```{doctest}
>>> host = LocalHost()
>>> result = run(host.run(["echo hello", "echo world"]))
>>> result.status
<Status.Success: 0>
>>> [cs.output.strip() for cs in result.statuses]
['hello', 'world']
```

File transfers work the same way -- `put` and `get` map to
{meth}`~otto.host.unix_host.UnixHost.put` and
{meth}`~otto.host.unix_host.UnixHost.get`:

```python
from pathlib import Path

# Upload
status, msg = await host.put(
    src_files=[Path("firmware.bin")],
    dest_dir=Path("/tmp"),
)

# Download
status, msg = await host.get(
    src_files=[Path("/var/log/syslog")],
    dest_dir=Path("./logs"),
)
```

```{note}
File transfer methods are only available on
{class}`~otto.host.unix_host.UnixHost` instances, not
{class}`~otto.host.local_host.LocalHost`.  The doctest above uses
`run` which is available on all host types.
`EmbeddedHost` provides its own console/tftp transfer; see {doc}`../embedded`.
```

```{toctree}
:hidden:
```
````

Note: the `{doc}` targets in the verb-model paragraph (`commands/index`,
`capabilities`, `commands/netcat`, `connections`, `configuration`) point at pages
created in later tasks. **They will produce `-W` build errors until those pages
exist.** To keep this task's build green, replace each not-yet-existing `{doc}`
link with its plain bold label for now (e.g. **Core commands** instead of
`{doc}`commands/index``), and Task 5's final step re-links them once every page
exists. The `{toctree}` is intentionally empty here (an empty toctree is valid);
later tasks append child entries to it. The `{doc}`../embedded`` link is valid
now (that page already exists). Adjust the relative `embedded` path: from
`host/index.md` it is `../embedded`.

- [ ] **Step 2: Wire `host/index` into the User Guide toctree**

In `docs/guide/index.rst`, add `host/index` to the `.. toctree::` block. Place it
immediately before the existing `host` line (both coexist this task). Resulting
relevant region:

```rst
   run
   test
   monitor
   host/index
   host
   host-dynamic-cli
   host-privilege
   host-products
   host-power
   host-file-ops
   embedded
```

- [ ] **Step 3: Remove the migrated sections from `host.md`**

Delete from `docs/guide/host.md`: §Syntax (heading + fenced block, current ~lines 7-14), §Listing hosts (~401-410), §Dry run (~412-419), and §Programmatic equivalents (~421-462, the whole tail including the doctest and the note). Leave the lead paragraph and everything from §Running commands through §Overriding protocol intact. After this edit `host.md` starts with its `# otto host` title + intro paragraph, then jumps to `## Running commands`.

- [ ] **Step 4: Build and verify**

Run: `make docs`
Expected: PASS — clean `-W` HTML build (no orphan/missing-doc/duplicate-label warnings) and doctest passes. The doctest now executes from `host/index.md`.

Confirm the doctest moved and nothing references the removed sections:

Run: `rg -n "doctest" docs/guide/host.md`
Expected: no matches (doctest now lives only in `host/index.md`).

- [ ] **Step 5: Stage and request commit**

```bash
git add docs/guide/host/index.md docs/guide/index.rst docs/guide/host.md
```

Then present this message for Chris to commit (do not run `git commit` yourself):

```
docs(host): add Hosts overview landing page

Create docs/guide/host/index.md (syntax, host verb model, --list-hosts,
--dry-run, From-Python doctest) and wire it into the user guide. Move those
framing sections out of host.md. First step of the host docs restructure;
host.md still holds the command/connection/config sections.
```

---

### Task 2: Core commands page + Netcat transfers subpage

Create `host/commands/index.md` (run/put/get/login) and its `netcat.md` subpage, append `commands/index` to the Overview toctree, and gut the corresponding sections from `host.md`.

**Files:**
- Create: `docs/guide/host/commands/index.md`
- Create: `docs/guide/host/commands/netcat.md`
- Modify: `docs/guide/host/index.md` (toctree)
- Modify: `docs/guide/host.md` (remove migrated sections; reduce netcat hop bullet to a pointer)

**Interfaces:**
- Consumes: `host/index` and its empty toctree from Task 1.
- Produces: doc targets `commands/index` (the Core commands page) and `commands/netcat` (the Netcat transfers subpage). `commands/netcat` becomes the canonical home for all netcat detail; other pages point to it.

- [ ] **Step 1: Create `docs/guide/host/commands/index.md`**

Migrate verbatim from `host.md`: §Running commands, §Uploading files, §Downloading files, §Interactive login (the full section incl. the "Ending the session", "Terminal resize", and "Hops" subsections). Prepend a short intro and append the netcat pointer + child toctree. File:

````markdown
# Core commands

The four built-in `otto host` verbs: run shell commands, move files in and out,
and open an interactive shell. (For capability verbs like `power` or `ls`, see
{doc}`../capabilities`.)

## Running commands

Execute one or more commands on a remote host with `run`:

```bash
otto --lab my_lab host router1 run "uname -a"
```

Multiple commands run in order.  If any command fails, `otto host run` exits with
a non-zero status:

```bash
otto --lab my_lab host router1 run "cd /tmp" "ls -la"
```

The host's built-in logging displays each command and its output as it runs --
the same output you see inside instructions and test suites.

## Uploading files

Transfer local files to a remote host with `put`:

```bash
otto --lab my_lab host router1 put firmware.bin /tmp/
```

Multiple source files are supported:

```bash
otto --lab my_lab host router1 put config.yaml license.key /opt/app/
```

File transfers default to SCP. To use a different backend (SFTP, FTP, or the
custom netcat backend), see {doc}`../connections` for the per-invocation
`--transfer` override and {doc}`netcat` for the netcat backend.

## Downloading files

Retrieve files from a remote host with `get`:

```bash
otto --lab my_lab host router1 get /var/log/syslog ./logs/
```

Multiple remote paths are supported:

```bash
otto --lab my_lab host router1 get /var/log/syslog /var/log/auth.log ./logs/
```

## Interactive login

Open a fully interactive shell on a remote host with `login`:

```bash
otto --lab my_lab host router1 login
```

Stdin and stdout are bridged to the remote terminal in raw mode, so full-screen
TUIs (`vi`, `top`, `less`) work the same as under a native `ssh` or `telnet`
client.  While the session runs, every remote byte is also appended to the
invocation's `otto.log` so the transcript is preserved alongside the normal
`otto host run` output.

**Ending the session.**  Exit the remote shell normally (`exit`, `logout`, or
`Ctrl+D`) or press `Ctrl+]` — the classic `telnet(1)` escape byte — to disconnect
locally without waiting on the remote.  The escape hatch exists because `Ctrl+C`
is forwarded to the remote so remote commands can be interrupted the usual way.

**Terminal resize.**  Local `SIGWINCH` is forwarded to the remote PTY on both SSH
(via `window-change` channel request) and telnet (via NAWS subnegotiation), so
remote TUIs reflow on resize.  For telnet, NAWS is enabled automatically for the
`login` command only — non-interactive `run`/`put`/`get` calls keep the historical
fixed column width.

**Hops.**  `login` honors `--hop` and the `hop` field in `hosts.json`, so an
interactive session can tunnel through jump hosts just like the other
subcommands (see {doc}`../connections`):

```bash
otto --lab my_lab host --hop jumpbox router1 login
```

```{toctree}
:hidden:

netcat
```
````

- [ ] **Step 2: Create `docs/guide/host/commands/netcat.md`**

Consolidate all netcat detail here. Migrate verbatim from `host.md` §Netcat port
and listener strategies (the two strategy tables + surrounding prose + the
`nc_options` JSON example), plus the netcat bullet from §File transfer protocols
through hops (the reversed-listener / port-forward paragraph). Add an intro and a
short `nc_options` field list. File:

````markdown
# Netcat transfers

Netcat (`nc`) is otto's most customizable file-transfer backend. Unlike SCP/SFTP/
FTP — standard tools otto drives directly — the netcat backend has to *find a free
port* on the remote and *verify the listener is ready*, both with configurable,
auto-detecting strategies. This page collects everything netcat-specific.

Select it per invocation with `--transfer nc` (see {doc}`../connections`) or
persist it with `"transfer": "nc"` in `hosts.json`.

## Through hops

Netcat (PUT and GET) works through SSH hops using SSH port forwarding. PUT
connects otto to a remote ``nc -l`` listener that receives data. GET uses a
reversed-listener approach: the remote runs ``nc -l <port> < <file>`` and otto
connects through the port forward to read the data. (The other transfer protocols
through hops are covered in {doc}`../connections`.)

## Port and listener strategies

Netcat transfers need two things on the remote host: a **free port** to listen on,
and a way to **verify the listener is ready** before sending data.  Both use a
configurable strategy that defaults to ``auto``.

**Port-finding strategies** (``nc_port_strategy``, default ``auto``):

| Strategy     | How it works                                                          |
|--------------|-----------------------------------------------------------------------|
| ``auto``     | Try each built-in strategy in order and cache the first success.      |
| ``ss``       | Parse ``ss -tln`` output to find unused ports.                        |
| ``netstat``  | Parse ``netstat -tln`` output (fallback for hosts without ss).        |
| ``python``   | Bind a socket to port 0 via a ``python``/``python3`` one-liner.       |
| ``proc``     | Read ``/proc/net/tcp`` directly (Linux-only, always available).       |
| ``custom``   | Run the command in ``nc_port_cmd``; must print a free port to stdout. |

The auto cascade order is: ss → netstat → python → proc.

**Listener-check strategies** (``nc_listener_check``, default ``auto``):

| Strategy     | How it works                                                                                  |
|--------------|-----------------------------------------------------------------------------------------------|
| ``auto``     | Probe for ss, then netstat, falling back to proc. Cache the result.                           |
| ``ss``       | Check for LISTEN via ``ss -tln sport = :<port>``.                                             |
| ``netstat``  | Grep ``netstat -tln`` for the port.                                                           |
| ``proc``     | Scan ``/proc/net/tcp`` for LISTEN state (Linux-only, always available).                       |
| ``custom``   | Run the command in ``nc_listener_cmd`` with ``{port}`` placeholder. Must exit 0 if listening. |

Override the strategy under ``nc_options`` in ``hosts.json`` when auto-detection
isn't appropriate for a particular host:

```json
{
    "ip": "10.10.200.12",
    "element": "target",
    "board": "seed",
    "transfer": "nc",
    "nc_options": {
        "port_strategy": "proc",
        "listener_check": "proc"
    }
}
```

## `nc_options` reference

The `nc_options` object also accepts `exec_name` (the remote netcat binary, e.g.
`ncat`) and `port` (a fixed port instead of auto-discovery), alongside the
`port_strategy` / `listener_check` / `port_cmd` / `listener_cmd` fields above.
`nc_options` participates in the same layered merge as the other transport option
objects — see {doc}`../configuration`.

```json
{
    "nc_options": { "exec_name": "ncat", "port": 9500 }
}
```
````

- [ ] **Step 3: Append `commands/index` to the Overview toctree**

In `docs/guide/host/index.md`, change the empty toctree to include the commands
page:

```{toctree}
:hidden:

commands/index
```

- [ ] **Step 4: Gut the migrated sections from `host.md`**

In `docs/guide/host.md`: delete §Running commands, §Uploading files,
§Downloading files, §Interactive login, and §Netcat port and listener strategies.
In §File transfer protocols through hops, **replace the Netcat bullet** (the
``- **Netcat** (PUT and GET) …`` paragraph) with a one-line pointer:

```markdown
- **Netcat** (PUT and GET) — see {doc}`host/commands/netcat`.
```

Leave §Reaching hosts through hops, the rest of §File transfer protocols through
hops, §Connection options (+ both anchors), the protocol option subsections,
§Per-host toolchain, and §Overriding protocol in place.

- [ ] **Step 5: Build and verify**

Run: `make docs`
Expected: PASS — clean `-W` build, doctest passes.

Run: `rg -n "Netcat port and listener|nc_port_strategy" docs/guide/host.md`
Expected: no matches (netcat strategy content now lives only in `commands/netcat.md`).

- [ ] **Step 6: Stage and request commit**

```bash
git add docs/guide/host/commands/index.md docs/guide/host/commands/netcat.md \
        docs/guide/host/index.md docs/guide/host.md
```

Commit message for Chris:

```
docs(host): split core commands + netcat into the Hosts group

Add docs/guide/host/commands/index.md (run/put/get/login) and
commands/netcat.md (the custom netcat backend: port/listener strategies,
through-hops, nc_options). Remove those sections from host.md and reduce its
netcat hop bullet to a pointer.
```

---

### Task 3: Host capabilities page

Merge the five small capability guides into one `host/capabilities.md`, apply the guide/API boundary cleanup, append it to the Overview toctree, and delete the five old files.

**Files:**
- Create: `docs/guide/host/capabilities.md`
- Modify: `docs/guide/host/index.md` (toctree)
- Modify: `docs/guide/index.rst` (drop five old entries)
- Delete: `docs/guide/host-power.md`, `docs/guide/host-products.md`, `docs/guide/host-file-ops.md`, `docs/guide/host-privilege.md`, `docs/guide/host-dynamic-cli.md`

**Interfaces:**
- Consumes: `host/index`'s toctree.
- Produces: doc target `capabilities`.

- [ ] **Step 1: Create `docs/guide/host/capabilities.md`**

Assemble one page with: a lead-in + the capability→CLI table, then five `##`
sections sourced verbatim (apart from heading level and the boundary-cleanup
lead lines) from the five small guides. Source map:

- §Power, reboot & reachability ← `host-power.md` (its `## Power control`,
  `## Reboot & shutdown`, `## Reachability` become `###` under this section).
- §Products & lifecycle ← `host-products.md` (entire body).
- §Remote file operations ← `host-file-ops.md` (entire body).
- §Privilege elevation ← `host-privilege.md` (entire body).
- §Methods as CLI verbs ← `host-dynamic-cli.md` (entire body).

Boundary cleanup (spec §6): at the top of each section that documents methods,
add one lead sentence pointing to the API class for full signatures, and convert
bare method names in prose to `{meth}`/`{class}` cross-reference roles. Keep the
existing behavior tables (they summarize behavior, they are not signature dumps).
Do not edit any file under `docs/api/`.

File skeleton (fill each section from the source files named above):

````markdown
# Host capabilities

Beyond the four core commands, hosts expose **capabilities** — richer behaviors
like power control, product lifecycle, privilege elevation, and on-host file
operations. Many are also `otto host` verbs (auto-exposed from `@cli_exposed`
methods); some are Python-only. Full method signatures live in the
{doc}`API reference <../../api/host/index>`; this page covers what each capability
is for and how to use it.

| Capability | CLI verbs | Python-only |
|------------|-----------|-------------|
| Power, reboot & reachability | `power`, `reboot`, `shutdown` | `is_reachable`, `wait_until_up`, `wait_until_down` |
| Products & lifecycle | `stage`, `install`, `uninstall`, `is-installed`, `is-uninstalled` | — |
| Remote file operations | `exists`, `ls`, `mkdir`, `rm`, `cp`, `mv`, `read-file`, `write-file` | — |
| Privilege elevation | — | `run(sudo=True)`, `as_user`, `switch_user` |

## Power, reboot & reachability

<!-- from host-power.md: Power control / Reboot & shutdown / Reachability as ### -->
<!-- lead line: full signatures on {class}`~otto.host.host.BaseHost`. -->

## Products & lifecycle

<!-- from host-products.md verbatim -->
<!-- lead line: see {class}`~otto.host.host.BaseHost` and the Product classes. -->

## Remote file operations

<!-- from host-file-ops.md verbatim -->
<!-- lead line: full signatures on {class}`~otto.host.unix_host.UnixHost`. -->

## Privilege elevation

<!-- from host-privilege.md verbatim; note: Python-only, no CLI verbs -->

## Methods as CLI verbs

<!-- from host-dynamic-cli.md verbatim: the @cli_exposed bridge + project-defined verbs -->
````

Verify the `../../api/host/index` relative path resolves from
`docs/guide/host/capabilities.md` (guide/host → ../../api/host). If a `{doc}` to
the API tree warns, fall back to plain text "the API reference" — the API pages
are autodoc and not the focus here.

- [ ] **Step 2: Append `capabilities` to the Overview toctree**

In `docs/guide/host/index.md`:

```{toctree}
:hidden:

commands/index
capabilities
```

- [ ] **Step 3: Drop the five old entries from `docs/guide/index.rst`**

Remove these lines from the `.. toctree::` block: `host-dynamic-cli`,
`host-privilege`, `host-products`, `host-power`, `host-file-ops`. (Leave `host`
and `host/index`.)

- [ ] **Step 4: Delete the five old files**

```bash
git rm docs/guide/host-power.md docs/guide/host-products.md \
       docs/guide/host-file-ops.md docs/guide/host-privilege.md \
       docs/guide/host-dynamic-cli.md
```

- [ ] **Step 5: Build and verify**

Run: `make docs`
Expected: PASS — clean `-W` build (no orphans from the deleted files, no broken
toctree entries), doctest passes.

Run: `rg -n "host-power|host-products|host-file-ops|host-privilege|host-dynamic-cli" docs/guide --glob '!docs/guide/host/**'`
Expected: no matches in live guide files (only `docs/superpowers/**`, which is
excluded from the build, may still reference them).

- [ ] **Step 6: Stage and request commit**

```bash
git add docs/guide/host/capabilities.md docs/guide/host/index.md docs/guide/index.rst
# (deletions already staged by git rm in Step 4)
```

Commit message for Chris:

```
docs(host): consolidate capability guides into one page

Merge host-power/products/file-ops/privilege/dynamic-cli into
docs/guide/host/capabilities.md with a capability->CLI table and a lead
pointing exhaustive signatures to the API autodoc. Delete the five flat files.
```

---

### Task 4: Connection control page

Create `host/connections.md` from `host.md`'s hop and protocol-override sections, append it to the Overview toctree, and remove those sections from `host.md`.

**Files:**
- Create: `docs/guide/host/connections.md`
- Modify: `docs/guide/host/index.md` (toctree)
- Modify: `docs/guide/host.md` (remove migrated sections)

**Interfaces:**
- Consumes: `commands/netcat` (Task 2) for the netcat pointer; `host/index`'s toctree.
- Produces: doc target `connections`.

- [ ] **Step 1: Create `docs/guide/host/connections.md`**

Migrate verbatim from `host.md`: §Reaching hosts through hops (incl. the
`hosts.json` `hop` JSON example), §File transfer protocols through hops (now with
the netcat bullet already reduced to a pointer in Task 2 — keep that pointer,
retargeting it to the sibling path `commands/netcat`), and §Overriding protocol
for a single session. Prepend a short intro. File:

````markdown
# Connection control

How otto reaches a host for a single invocation: route through SSH jump hosts with
`--hop`, and override the terminal or file-transfer protocol with `--term` /
`--transfer`. (For *persistent* connection tuning in `hosts.json`, see
{doc}`configuration`.)

## Reaching hosts through hops

<!-- from host.md "Reaching hosts through hops" verbatim, including the
     ### File transfer protocols through hops subsection. Retarget the Netcat
     bullet pointer to {doc}`commands/netcat`. -->

## Overriding protocol for a single session

<!-- from host.md "Overriding protocol for a single session" verbatim, including
     the --term/--transfer examples, valid-values list, and the embedded note
     (retarget {doc}`embedded` -> {doc}`../embedded`). -->
````

Adjust relative `{doc}` paths for the new location (`guide/host/`): `embedded` →
`../embedded`; netcat → `commands/netcat`; configuration → `configuration`.

- [ ] **Step 2: Append `connections` to the Overview toctree**

In `docs/guide/host/index.md`:

```{toctree}
:hidden:

commands/index
capabilities
connections
```

- [ ] **Step 3: Remove the migrated sections from `host.md`**

Delete §Reaching hosts through hops (incl. its `### File transfer protocols
through hops` subsection) and §Overriding protocol for a single session. After
this edit, `host.md` contains only its title/intro, §Connection options (+ both
anchors), the protocol option subsections, and §Per-host toolchain.

- [ ] **Step 4: Build and verify**

Run: `make docs`
Expected: PASS — clean `-W` build, doctest passes.

Run: `rg -n "Reaching hosts through hops|Overriding protocol" docs/guide/host.md`
Expected: no matches.

- [ ] **Step 5: Stage and request commit**

```bash
git add docs/guide/host/connections.md docs/guide/host/index.md docs/guide/host.md
```

Commit message for Chris:

```
docs(host): extract connection control page

Move hops (--hop, chaining, transfer-through-hops) and the --term/--transfer
override sections from host.md into docs/guide/host/connections.md.
```

---

### Task 5: Host configuration page + retire host.md

Create `host/configuration.md` from `host.md`'s remaining configuration sections, **moving the two cross-referenced anchors here in the same commit that removes them from `host.md`**, then delete the now-empty `host.md`, append `configuration` to the toctree, and re-link the Overview's `{doc}` references.

**Files:**
- Create: `docs/guide/host/configuration.md`
- Modify: `docs/guide/host/index.md` (toctree + re-link verb-model `{doc}`s)
- Modify: `docs/guide/index.rst` (drop the `host` entry)
- Delete: `docs/guide/host.md`

**Interfaces:**
- Consumes: `commands/netcat` (for the `nc_options` pointer); `host/index`'s toctree.
- Produces: doc target `configuration`; the global labels `connection-options` and `per-host-toolchain` (relocated here — they must not also exist in `host.md` after this commit).

- [ ] **Step 1: Create `docs/guide/host/configuration.md`**

Migrate verbatim from `host.md`: the `(connection-options)=` label + §Connection
options (the six-object table, the four-layer precedence list, the per-key merge
prose, the `get_host()`/`all_hosts()` note), the `### SSH` subsection (incl.
`#### Port forwarding`), `### Telnet`, `### SFTP, SCP, FTP, Netcat`, and the
`(per-host-toolchain)=` label + §Per-host toolchain. **Carry both `(label)=`
definitions across exactly as written.** In the six-object table, change the
`nc_options` row's protocol cell to point at the netcat page; and in the
`### SFTP, SCP, FTP, Netcat` block, replace the netcat-specific JSON line with a
pointer. File outline:

````markdown
# Host configuration (hosts.json)

Persistent per-host connection tuning, declared in `hosts.json`. (For
per-invocation overrides, see {doc}`connections`; for the custom netcat backend,
see {doc}`commands/netcat`.)

(connection-options)=

## Connection options

<!-- from host.md "Connection options" verbatim: the six-object table (change the
     nc_options row to: see {doc}`commands/netcat`), the 4-layer precedence list,
     per-key merge prose, and the per-call note. Retarget {ref}`host-preferences`
     and {ref}`per-call-overrides` as-is (outbound, unchanged). -->

### SSH

<!-- from host.md "### SSH" verbatim, including "#### Port forwarding". Retarget
     the cookbook link ../cookbook/connection-options.md -> ../../cookbook/connection-options.md -->

### Telnet

<!-- from host.md "### Telnet" verbatim -->

### SFTP, SCP, FTP, Netcat

<!-- from host.md "### SFTP, SCP, FTP, Netcat" verbatim, but drop the nc_options
     entry from the JSON example and add: "Netcat has additional options and
     auto-detection strategies — see {doc}`commands/netcat`." -->

(per-host-toolchain)=

## Per-host toolchain

<!-- from host.md "Per-host toolchain" verbatim. Retarget the coverage link
     coverage.md -> ../coverage.md -->
````

Fix relative paths for `guide/host/`: `../cookbook/...` → `../../cookbook/...`;
`coverage.md` → `../coverage.md`.

- [ ] **Step 2: Delete the now-empty `host.md` and drop its toctree entry**

`host.md` should now contain only its `# otto host` title and intro paragraph
(all sections migrated). Remove it and its label definitions in one go:

```bash
git rm docs/guide/host.md
```

In `docs/guide/index.rst`, remove the `host` line from the `.. toctree::` block
(leave `host/index`).

- [ ] **Step 3: Append `configuration` to the Overview toctree and re-link**

In `docs/guide/host/index.md`, finalize the toctree:

```{toctree}
:hidden:

commands/index
capabilities
connections
configuration
```

Then re-link the verb-model paragraph: replace the plain bold placeholders from
Task 1 with the real cross-references now that every page exists —
`{doc}`commands/index``, `{doc}`capabilities``, `{doc}`commands/netcat``,
`{doc}`connections``, `{doc}`configuration``.

- [ ] **Step 4: Build and verify (the critical anchor-relocation check)**

Run: `make docs`
Expected: PASS — clean `-W` build with **no duplicate-label warning** for
`connection-options` or `per-host-toolchain` (they now exist only in
`configuration.md`), no orphans, doctest passes.

Confirm the two external references still resolve to the relocated labels:

Run: `rg -n "connection-options|per-host-toolchain" docs/cookbook/connection-options.md docs/guide/coverage.md docs/guide/host/configuration.md`
Expected: the cookbook and coverage references remain, and the only `(label)=`
*definitions* are in `configuration.md`. The clean `-W` build above is the
authoritative proof they resolve.

Run: `rg -n "guide/host\b|^\s*host$" docs/guide/index.rst; ls docs/guide/host.md 2>&1`
Expected: `index.rst` lists `host/index` (not bare `host`); `host.md` no longer exists.

- [ ] **Step 5: Stage and request commit**

```bash
git add docs/guide/host/configuration.md docs/guide/host/index.md docs/guide/index.rst
# (host.md deletion already staged by git rm)
```

Commit message for Chris:

```
docs(host): extract hosts.json configuration page; retire host.md

Move connection options (*_options framework, ssh/telnet/sftp/scp/ftp) and the
per-host toolchain into docs/guide/host/configuration.md, relocating the
connection-options and per-host-toolchain anchors. Delete the now-empty host.md
and finalize the Hosts toctree and overview cross-links. Completes the
restructure.
```

---

## Self-Review

**Spec coverage** (spec §4/§5 page set → task):
- `host/index.md` (Overview, verb model, From-Python doctest) → Task 1 ✓
- `host/commands/index.md` (run/put/get/login) → Task 2 ✓
- `host/commands/netcat.md` (netcat backend, strategies, through-hops, nc_options) → Task 2 ✓
- `host/capabilities.md` (5 sections + capability→CLI table + boundary cleanup, spec §6) → Task 3 ✓
- `host/connections.md` (hops, transfer-through-hops, --term/--transfer) → Task 4 ✓
- `host/configuration.md` (*_options framework, ssh/telnet/sftp/scp/ftp, toolchain, both anchors) → Task 5 ✓
- toctree rewire (`index.rst` six entries → one `host/index`; nested child toctree) → Tasks 1–5 ✓
- Anchor preservation (`connection-options`, `per-host-toolchain`) with no duplicate-label window → Task 5, Step 4 ✓
- Out-of-scope respected: no `src/`, no `docs/api/`, no `cli-reference.md`, no non-host guide changes ✓

**Placeholder scan:** The `<!-- ... -->` markers in Tasks 3–5 are deliberate *source-mapping instructions* (which existing file/section to migrate verbatim), not unfilled placeholders — every one names an exact source section and the transformation to apply. All authored-fresh prose (verb model, capability table, intros, pointer sentences) is shown in full. No "TBD"/"handle edge cases"/"similar to Task N".

**Type/name consistency:** Doc targets are referenced consistently — `host/index`, `commands/index`, `commands/netcat`, `capabilities`, `connections`, `configuration`. Relative-path adjustments for the `guide/host/` (and `guide/host/commands/`) depth are called out per link. The two global labels are spelled `connection-options` and `per-host-toolchain` throughout. Toctree grows monotonically (Task 1 empty → +commands/index → +capabilities → +connections → +configuration).

**Ordering safety:** Each task's build is `-W`-green because new pages are created and wired in the same commit, deleted files are de-listed in the same commit, and the duplicate-prone anchors move only in the final commit that also removes their old home.
