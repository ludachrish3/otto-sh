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
    from otto.utils import Status

    class MyApp(FileProduct):
        async def install(self, host):
            return (await host.run(f"tar xzf {self.artifact.name}", )).status, ""
        async def uninstall(self, host):
            return (await host.run("rm -rf /opt/myapp")).status, ""
        async def is_installed(self, host):
            return (await host.run("test -d /opt/myapp")).status.is_ok

### Injecting products

    host = UnixHost(ip="10.0.0.1", element="box", creds={"u": "p"},
                    products=[MyApp(artifact=Path("dist/myapp.tgz"), dest_dir=Path("/opt"))])

### Lifecycle verbs

| Method | Behavior |
|--------|----------|
| `await host.stage()` | Stage every product (no install). |
| `await host.install(stage_only=False)` | Stage, then install (unless `stage_only`). |
| `await host.uninstall()` | Uninstall every product (best-effort). |
| `await host.is_installed()` | True iff ≥1 product and all installed. |
| `await host.is_uninstalled()` | Inverse of `is_installed()`. |

With no products, `stage`/`install`/`uninstall` are successful no-ops and
`is_installed()` is `False`.

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
| `await host.read_file(path)` | Return text contents (raises `FileNotFoundError`). |
| `await host.write_file(path, data, append=False)` | Write text (base64 on the wire, injection-safe). |

`write_file` and `read_file` transfer text; for
exact-byte/binary fidelity use
{meth}`~otto.host.unix_host.UnixHost.put` and
{meth}`~otto.host.unix_host.UnixHost.get`.

### Embedded hosts

`EmbeddedHost` supports the subset its filesystem provides — `exists`, `ls`,
`rm` (via the device `fs` commands). `mkdir`/`cp`/`mv`/`read_file`/`write_file`
raise `NotImplementedError`; use `get`/`put` for device reads/writes.

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
from otto.utils import cli_exposed
from otto.host import UnixHost

class MyHost(UnixHost):
    @cli_exposed(help_="Flash firmware to the board")
    async def flash_firmware(self, image: Path) -> tuple[Status, str]:
        ...
```

```text
otto host <my-host-id> flash-firmware ./build/app.bin
```

A verb returning `(Status, str)` exits non-zero when the status is not OK.

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
state: Annotated[str | None, Arg()] = None     # otto host <id> power on
path:  Annotated[str | Path, Arg()] = "."       # otto host <id> ls /var/log
```

**`Opt(...)`** — force a parameter to an `--option` regardless of whether it
has a default.  Used by `run`'s `timeout`:

```python
timeout: Annotated[float | None, Opt(help='Timeout in seconds.')] = None
```

**`Exclude`** — drop a parameter from the CLI entirely; the method receives its
default value.  Use this for SDK-only parameters that make no sense as CLI
flags — `run`'s `expects` and `log` are the canonical examples:

```python
expects: Annotated[Expect | None, Exclude] = None
log:     Annotated[bool, Exclude] = True
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
