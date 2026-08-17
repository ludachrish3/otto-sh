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
| Products & lifecycle | `stage`, `install`, `uninstall`, `cleanup`, `is-installed`, `is-uninstalled`, `is-clean` | — |
| Log retrieval | `get-logs`, `get-product-logs`, `get-debug-logs` | `log_dest` |
| Dev tools & toolchain tools | `install-tools`, `install-dev-tools`, `install-toolchain-tools`, `remove-toolchain-tools` | `toolchain_tools_absent` |
| Remote file operations | `exists`, `ls`, `glob`, `mkdir`, `rm`, `cp`, `mv`, `read-file`, `write-file` | — |
| Kernel modules | `lsmod`, `load`, `unload` | — |
| Userland capabilities | `probe` | — |
| Privilege elevation | — | `run(sudo=True)`, `as_user`, `switch_user`, `current_user` |

## Power, reboot & reachability

Full signatures: {class}`~otto.host.host.BaseHost`.

### Power control

Power can't run on an off host, so otto models the actor as a pluggable
`PowerController`. The built-in `command` controller runs commands on a
*controller* host:

```json
{
    "power_control": {
        "type": "command",
        "controller": "hypervisor1",
        "on_cmd": "virsh start {name}",
        "off_cmd": "virsh destroy {name}",
        "status_cmd": "virsh domstate {name}",
        "status_on": "running"
    }
}
```

Then:

    await host.power("on")     # or "off"
    await host.power()         # toggle (needs status_cmd)

Projects register richer controllers (IPMI/redfish/libvirt/PDU) via
`register_power_controller(type_name, cls)` — pass the type-name string and the
`PowerController` subclass:

    from otto.host.power import register_power_controller, PowerController

    class MyIpmiController(PowerController):
        type_name = "ipmi"
        ...

    register_power_controller("ipmi", MyIpmiController)

With no controller configured,
{meth}`~otto.host.host.BaseHost.power` and `reboot(hard=True)` raise.

### Reboot & shutdown

    await host.reboot()                       # soft: in-shell reboot (UnixHost: sudo reboot)
    await host.reboot(wait=True)              # soft reboot, then block until back up (10-min default)
    await host.reboot(hard=True)              # power-cycle via the controller
    await host.reboot(hard=True, wait=True)               # hard reboot, block until back up (10-min default; returns Failed on timeout)
    await host.reboot(hard=True, wait=True, timeout=300)  # ...or override the wait timeout (seconds)
    await host.shutdown()                     # in-shell power-off

{class}`~otto.host.local_host.LocalHost` {meth}`~otto.host.host.BaseHost.reboot`
and {meth}`~otto.host.host.BaseHost.shutdown` raise (never reboot the test runner).
`DockerContainerHost` also inherits the base raising `reboot` (soft path) and
`shutdown` with no override — both raise `NotImplementedError` at runtime.
`EmbeddedHost` overrides the soft-reboot path (`kernel reboot cold`) but inherits
the base `shutdown`, so `shutdown` raises on embedded hosts too.

### Reachability

    if await host.is_reachable(): ...
    await host.wait_until_up(120)     # after a reboot/power-on  (timeout is required)
    await host.wait_until_down(60)    # after a shutdown          (timeout is required)

## Products & lifecycle

Full signatures: {class}`~otto.host.host.BaseHost` and the `Product` classes.

Every host carries a list of **products** — units of software-under-test it
deploys. A product is a small injected strategy object; the host orchestrates.

### Defining a product

Subclass `Product` (or `FileProduct` for the single-artifact case) and implement
the project-specific halves:

    from pathlib import Path
    from otto.host import FileProduct

    class MyApp(FileProduct):
        async def install(self, host):
            return await host.run(f"tar xzf {self.artifact.name}")
        async def uninstall(self, host):
            return await host.run("rm -rf /opt/myapp")
        async def is_installed(self, host):
            return (await host.run("test -d /opt/myapp")).status.is_ok

`stage`, `install`, and `uninstall` return a `Result` — usually just whatever
`host.run` / `host.put` handed back. The *failing* product's result is
returned whole, so the command's own retcode and output reach the process exit
code; a run where every product succeeds collapses to a bare success, since
with several products there is no single result to hand back.
`is_installed` is a plain predicate.

### Injecting products

    host = UnixHost(ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")],
                    products=[MyApp(artifact=Path("dist/myapp.tgz"), dest_dir=Path("/opt"))])

### Lifecycle verbs

| Method | `owner=` | Behavior |
|--------|----------|----------|
| `await host.stage()` | yes | Stage every product (no install). |
| `await host.install(stage_only=False)` | yes | Stage, then install (unless `stage_only`). |
| `await host.uninstall()` | yes | Product logs, uninstall every product, debug logs (best-effort). |
| `await host.is_installed()` | yes | True iff ≥1 product and all installed. |
| `await host.is_uninstalled()` | yes | Inverse of `is_installed()`. |
| `await host.cleanup()` | no | `uninstall()`, then remove dev tools and toolchain tools. |
| `await host.is_clean()` | no | True iff no product, dev tool, or toolchain tool is present. |

With no products, `stage`/`install`/`uninstall` are successful no-ops and
`is_installed()` is `False`.

The five product verbs take `owner=` — the name of the repo whose products to
act on (`None`, the default, means all of them). That is what keeps one repo's
`install` off another repo's products on a shared host; the empty-list rule
above applies to the *filtered* list, so a repo with nothing here is not
reported installed by someone else's products. `cleanup()` and `is_clean()`
have no `owner=`: both reach past products into the host's dev tools and
toolchain tools, which are host-wide, so there is no per-repo answer to give.

### Log retrieval

`uninstall()` gathers product logs **before** tearing anything down and debug
logs **after** — teardown activity is usually exactly what debug logs exist to
capture. Both halves are also callable directly:

| Method | Behavior |
|--------|----------|
| `await host.get_logs(product=True, debug=True)` | Both halves; `require_product_logs=True` makes an empty product haul a failure. |
| `await host.get_product_logs()` | Each product's `get_logs(host, dest)` hook (best-effort). |
| `await host.get_debug_logs()` | Fetch the host's `debug_log_globs`. |

Retrieving zero logs is success. Files land in a documented tree, keyed by host
id the way the coverage pipeline keys its own output:

```text
<output-dir>/logs/<host-id>/product/…
<output-dir>/logs/<host-id>/debug/…
```

`<output-dir>` is the active command's output directory unless a caller passes
`dest=`. `debug_log_globs` is a host field (settable per host class, per OS
profile, and per host in `lab.json`) holding remote paths — a pattern entry is
expanded on the device by `glob`, so a host family without that capability
(embedded, today) declares concrete paths or overrides `get_debug_logs`; a
pattern there fails loudly rather than silently retrieving nothing.

### Registering products from a product repo

Products are **behavior**, so they're customized in code — never declared in lab
data. Lab data stays product-agnostic so it can evolve independently of product
code: reverting a product's behavior must never force a lab change. A product
repo registers its products from a `.otto` init module, and otto applies them to
each host as it is ingested from lab data:

    from pathlib import Path
    from otto.host import register_product_provider

    def _provide(host):
        if host.os_type == "unix":
            return [MyApp(artifact=Path("dist/myapp.tgz"), dest_dir=Path("/opt"))]
        return None

    register_product_provider(_provide)

The provider runs once per lab-ingested host. Key on product-agnostic host
attributes (`element`, `element_id`, `os_type`, `id`, `ip`, `resources`) to
decide which hosts get which products; source any per-host parameters (versions,
artifact paths) from your own product-repo config. Providers aggregate in
registration order and dedupe by `Product.name`.

Code-constructed hosts (`UnixHost(..., products=[...])`) keep their explicit
list; providers apply only to hosts built from lab data.

## Dev tools & toolchain tools

Full signatures: {class}`~otto.host.dev_tool.DevTool` and
{class}`~otto.host.toolchain.ToolchainTool`.

Tooling is not product: a debug probe on a board is not software under test, so
it lives in its own list and is never part of `is_installed()`'s answer. Two
seams, because the two kinds of tooling are owned differently:

| Kind | Declared | Owned by | Installed by |
|------|----------|----------|--------------|
| **Dev tool** | in code, via a provider | the repo that registered it | `install-tools` (on by default) |
| **Toolchain tool** | in lab data, per host | the host — shared by every repo | `install-tools --toolchain` |

| Method | Behavior |
|--------|----------|
| `await host.install_tools(dev=True, toolchain=False)` | Dispatcher over the two below. |
| `await host.install_dev_tools()` | Stage then install each dev tool, in declaration order (first failure wins). |
| `await host.install_toolchain_tools()` | Put each declared tool, rename it to its declared `name`, `chown` it to its declared `user`. |
| `await host.remove_toolchain_tools()` | Remove each declared tool (best-effort). |
| `await host.toolchain_tools_absent()` | True iff none of them is present — the host-wide half of `is_clean()`. |

The asymmetric defaults are deliberate: dev tools are small and wanted on
nearly every run, while toolchain artifacts are large and rarely needed, so
asking for them is a decision.

### Registering dev tools from a repo

The dev-tool twin of the product provider above, and the same authoring model —
a `DevTool` implements `stage`, `install`, `uninstall` and `is_installed`:

    from otto.host import register_dev_tool_provider

    def _provide(host):
        if host.os_type == "unix":
            return [TraceHelper()]
        return None

    register_dev_tool_provider(_provide)

Providers run once per lab-ingested host, aggregate in registration order, and
dedupe by `DevTool.name`. Each attached tool is stamped with the registering
repo as its owner, which is what lets one repo's `install-tools` leave another
repo's tooling alone.

### Declaring toolchain tools in lab data

Toolchain tools are host-wide artifacts (a cross-built `gdbserver`, a runtime
`.so`), so they *are* lab data — declared alongside the coverage toolchain in
the host's `lab.json` entry:

```json
"toolchain": {
    "sysroot": "/opt/arm-toolchain",
    "tools": [
        {
            "name": "gdb",
            "source": "build/arm-linux-gnueabihf-gdb",
            "dest": "/usr/local/bin",
            "user": "root",
            "mode": "755"
        }
    ]
}
```

`name` is a **rename target**, not a label: `put` lands every file under its
source basename, so a tool whose `name` differs is `mv`'d there before it is
chowned — which is how `arm-linux-gnueabihf-gdb` installs as plain `gdb`. Those
destinations are usually root-owned, hence the per-tool `user` and `mode`.

One toolchain serves every owner on a host, so placing and removing it is a
host-wide step: no repo's actions touch it (see
{doc}`../run/defaults`), and the default `install_toolchain_tools` is the method
most likely to need project surgery — override it on the host class.

## Remote file operations

Full signatures: {class}`~otto.host.unix_host.UnixHost`.

Posix-shell hosts ({class}`~otto.host.unix_host.UnixHost`,
{class}`~otto.host.local_host.LocalHost`, `DockerContainerHost`) expose
unix-CLI-style helpers for managing files **already on** the host — complementary
to {meth}`~otto.host.unix_host.UnixHost.put` and
{meth}`~otto.host.unix_host.UnixHost.get`, which move files between local and remote.

| Method | Behavior |
|--------|----------|
| `await host.exists(path)` | `True` if `path` exists. |
| `await host.ls(path=".", all=False)` | List entry names (`all` includes dotfiles). |
| `await host.mkdir(path, parents=True)` | Create a directory. |
| `await host.rm(path, recursive=False, force=False)` | Remove a path. |
| `await host.cp(src, dst, recursive=False)` | Copy on the host. |
| `await host.mv(src, dst)` | Move/rename on the host. |
| `await host.read_file(path)` | Return text contents (raises `FileNotFoundError` if the path is missing, `ValueError` if the device output is not valid base64). |
| `await host.write_file(path, data, append=False)` | Write text (base64 on the wire, injection-safe). |

`write_file` and `read_file` transfer text; for
exact-byte/binary fidelity use
{meth}`~otto.host.unix_host.UnixHost.put` and
{meth}`~otto.host.unix_host.UnixHost.get`.

### Embedded hosts

`EmbeddedHost` supports the subset its filesystem provides — `exists`, `ls`,
`rm` (via the device `fs` commands). `mkdir`/`cp`/`mv`/`read_file`/`write_file`
raise `NotImplementedError`; use `get`/`put` for device reads/writes.

## Kernel modules

Full signatures: {class}`~otto.host.unix_host.UnixHost`.

Unix hosts manage kernel modules with three verbs:

| Method | Behavior |
|--------|----------|
| `await host.lsmod()` | List loaded module names (`Result` whose `value` is `list[str]`). |
| `await host.load(file, name=None)` | Stage the `.ko` on the host, `insmod` it, then remove the staged file. `name` defaults to the file stem. |
| `await host.unload(name)` | `rmmod` the module. Idempotent: unloading a module that is not resident succeeds. |

`load` and `unload` elevate automatically — the `insmod`/`rmmod` runs under
`sudo` unless the session is already root (see `current_user` below).
As `otto host` verbs:

```text
otto host <id> lsmod
otto host <id> load ./build/my_driver.ko
otto host <id> unload my_driver
```

(`EmbeddedHost` has its own `load`/`unload` pair for loading binaries into the
device runtime via the host's binary loader — same verb names, different
signatures; see {doc}`embedded`.)

(userland-capabilities)=

## Userland capabilities

otto adapts to the device's userland rather than assuming a GNU one: which
elevation mechanism exists, which `timeout` calling convention the applet
speaks, whether `base64` is there at all. Those answers are settled by a probe
round the first time a fresh host object needs one — cheap on a server, slow on
a BusyBox device — and they can be *pinned* in that host's `userland_options`
so the round never happens again.

`probe` is how you get the pin:

```text
otto host <id> probe
```

It resolves the capabilities and prints two things. First a reading of every
capability with its value **and its source**:

```text
capability       value      source
applet_base64    present    probed
applet_nc        present    probed
applet_scp       absent     probed
base64_flag      -d         probed
checksum         md5sum     probed
elevation        sudo       probed
shell_dialect    bash       probed
stat_size        stat       probed
timeout_style    coreutils  probed
```

The source is the actionable column. `declared` means the value is already
pinned in this host's `userland_options` and is never re-probed; `probed` means
the device answered, so it is worth pinning; `assumed` means otto could not ask
and the value is only what otto did before it asked anything.

Then the pasteable payload — the settled answers, under the key a `lab.json`
host entry carries:

```json
"userland_options": {
  "elevation": "sudo",
  "timeout_style": "coreutils",
  "applet_base64": "present"
}
```

Paste that into the host's entry and the next connection issues no probe at
all. See {doc}`configuration` for where the table lives and how it layers.

**Assumed values are deliberately absent from the payload.** Inside a JSON
object a guess is indistinguishable from a measurement, and a pinned value is
never re-probed — so pinning one would make a momentary blip permanent. The
reading above the payload is where those values are visible, labelled for what
they are. A host that could answer nothing therefore prints an empty pin and
says why, rather than offering thirteen guesses.

`LocalHost` and `DockerContainerHost` build no capability resolver at all, so
`probe` on those reports that hole plainly instead of printing a pin. That is
recorded rather than accidental — see
{class}`~otto.host.userland.UserlandHost` for what giving them one would cost.

## Privilege elevation

Privilege elevation is Python-only — there are no CLI verbs for `as_user` or
`switch_user`. Full signatures: {class}`~otto.host.host.BaseHost`.

### One-off: `run(sudo=True)`

    await host.run("apt-get update", sudo=True)

The command is wrapped as `sudo -S -p 'otto-sudo:' <cmd>`. On a
{class}`~otto.host.unix_host.UnixHost` the login user's password (from `creds`)
is auto-answered through the expect channel; `LocalHost`/Docker assume
passwordless sudo by default. Caller-supplied `expects` are preserved (the
password expect is tried first). Embedded/RTOS hosts raise `NotImplementedError`.

### Scoped: `async with host.as_user(...)`

    async with host.as_user("root"):
        await host.run("systemctl restart foo")   # runs as root
    # session returns to the original user here

{meth}`~otto.host.host.BaseHost.as_user` `su`'s the **persistent session**
to the target user on entry and sends `exit` on the way out. The imperative form
is {meth}`~otto.host.host.BaseHost.switch_user`. Target-user passwords come
from `creds` when present, or pass `password=` explicitly. Embedded hosts raise
`NotImplementedError`.

### Inspecting the effective user: `current_user`

Each shell session tracks the OS user it is currently running as. The
read-only {attr}`~otto.host.host.BaseHost.current_user` property reports it
for the host's default session — seeded from the login user and changed only
by `switch_user` / `as_user`:

    async with host.as_user("root"):
        assert host.current_user == "root"
    assert host.current_user != "root"   # back to the login user

Named sessions elevate independently, so each carries its own
`current_user` (see `HostSession.current_user`).

## Methods as CLI verbs

Any host coroutine method decorated with `@cli_exposed` is automatically an
`otto host` subcommand, scoped to the host's class.  This includes all four
core commands — `run`, `put`, `get`, and `login` — as well as every capability
verb listed above.  They all share the same signature-driven synthesizer with
no special casing.

Example invocations:

```text
otto host <id> run "systemctl restart x" "journalctl -n5"
otto host <id> run --sudo --timeout 30 "apt-get update"
otto host <id> put a.txt b.txt /tmp/
otto host <id> get /var/log/syslog /tmp/
otto host <id> login
otto host <id> reboot --hard --wait
otto host <id> install --stage-only
otto host <id> ls /var/log --all
otto host <id> power on
```

The menu is **class-scoped**: `otto host <id> --help` lists only the verbs defined on
that host's class. A unix host shows the file-ops verbs (`mkdir`, `cp`, `read-file`, …);
an embedded host shows `exists`/`ls`/`rm` but not the file-ops it doesn't implement.

### Authoring CLI-exposed methods

`@cli_exposed` is importable from `otto.utils`. Add it to any `async def` method on a
host subclass and it appears in the `otto host` menu for that class's hosts with no
extra wiring:

```python
from otto.result import Result
from otto.utils import cli_exposed
from otto.host import UnixHost


class MyHost(UnixHost):
    @cli_exposed(help_="Flash firmware to the board")
    async def flash_firmware(self, image: Path) -> Result: ...
```

```text
otto host <my-host-id> flash-firmware ./build/app.bin
```

A verb returning a `Result` exits non-zero when its status is not OK (see
{doc}`Exit codes <index>`). Custom verbs on third-party host classes may
return plain values instead of a `Result`; the CLI prints them as-is and
exits 0.

### Parameter inference rules

The synthesizer reads the method's type annotations and builds the Typer
command automatically:

| Parameter shape | CLI form |
| --- | --- |
| No default value | positional argument |
| Has a default value | `--option` |
| `bool` (with default) | `--flag / --no-flag` pair |
| `list[T]` with no default | space-separated positional variadic |
| `list[T]` option | `--opt a,b,c` (comma-separated) |
| `dict[str, T]` option | `--opt K=V,K2=V2` (comma-separated key=value) |

At most **one** parameter per verb may be a positional variadic.

`bool` flag strings — the strings `1`, `true`, `yes`, `on` (case-insensitive)
map to `True`; everything else maps to `False`.  `Path`/`int`/`float` are
coerced from strings automatically.

### Overriding inference with `Annotated[...]` markers

Import `Arg`, `Opt`, and `Exclude` from `otto.utils` to override the defaults:

```python
from typing import Annotated
from otto.utils import Arg, Opt, Exclude, cli_exposed
```

**`Arg(variadic=True, type=T)`** — make a union-typed (or otherwise
Typer-incompatible) list a space-separated positional variadic.  `type`
specifies the element type the CLI receives; the method gets a `list[T]`.
Used by `run` (`cmds`), `put` (`src_files`), and `get` (`src_files`):

```python
cmds: Annotated[str | Sequence[str], Arg(variadic=True, type=str)]
```

**`Arg()`** — keep a *defaulted* scalar positional (prevents it from becoming
an `--option`).  Used by `power` (`state`) and `ls` (`path`), where passing
the value positionally is natural:

```python
state: Annotated[str | None, Arg()] = None  # otto host <id> power on
path: Annotated[str | Path, Arg()] = "."  # otto host <id> ls /var/log
```

**`Opt(...)`** — force a parameter to an `--option` regardless of whether it
has a default.  Used by `run`'s `timeout`:

```python
timeout: Annotated[
    float,
    Opt(help="Per-command/cumulative timeout (seconds); use inf for unbounded.", min=0.0),
] = DEFAULT_COMMAND_TIMEOUT
```

**`Arg(remote_path=...)` / `Opt(remote_path=...)`** — complete this parameter
against the *remote* host's filesystem instead of the local one.  `"any"`
offers files and directories, `"dir"` offers directories only.  Used by `get`
(`src_files`, `"any"`) and `put` (`dest_dir`, `"dir"`); your own verbs can take
a remote path the same way:

```python
src_files: Annotated[
    list[Path] | Path,
    Arg(variadic=True, elem_type=Path, remote_path="any"),
]
dest_dir: Annotated[Path, Arg(remote_path="dir")]
```

The marker only affects tab completion — see
[Remote path completion](../cli-reference.md#remote-path-completion) for what
completion offers and when it stays quiet.  It is rejected on a comma-list or
`key=value` option (which render as a single string the completer can't split);
mark a completable path list as `Arg(variadic=True, remote_path=...)` instead.

**`Exclude`** — drop a parameter from the CLI entirely; the method receives its
default value.  Use this for SDK-only parameters that make no sense as CLI
flags — `run`'s `expects` and `log` are the canonical examples:

```python
expects: Annotated[Expect | None, Exclude] = None
log: Annotated[bool, Exclude] = True
```

### Per-verb summary

| Verb | Positional args | Notable options | Notes |
| --- | --- | --- | --- |
| `run` | `COMMANDS...` (variadic) | `--sudo`, `--timeout SECS` | `expects`/`log` excluded from CLI |
| `put` | `SRC... DEST` (variadic src + positional dest) | — | `show_progress` excluded |
| `get` | `SRC... DEST` (variadic src + positional dest) | — | `show_progress` excluded |
| `login` | — | — | Opens interactive shell |
| `reboot` | — | `--hard / --no-hard`, `--wait / --no-wait`, `--timeout SECS` | |
| `install` | — | `--stage-only / --no-stage-only` | |
| `power` | `STATE` (optional positional) | — | `on`/`off`/omit to toggle |
| `ls` | `PATH` (optional positional, default `.`) | `--all / --no-all` | |
