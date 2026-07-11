# Extending otto with custom connection and transfer backends

otto reaches a host over a **connection** (the interactive shell transport) and
moves files with a **transfer** backend (the file-copy protocol). Both are
selected by a registered string in lab data, and both are open the same way
command frames and filesystems are (see {doc}`extending-embedded`): your project
registers a class from an `init` module listed in `.otto/settings.toml`, so the
registration runs before any lab data loads. The two seams are:

- {func}`~otto.host.connections.register_term_backend` — the **connection** for
  a host (`term` in lab data). The built-ins are `ssh` and `telnet`, both
  {class}`~otto.host.connections.ConnectionManager`. Each term declares the host
  families it serves (see below): `ssh` serves `{'unix'}`, `telnet` serves
  `{'unix', 'embedded'}` (embedded hosts reach their console over telnet).
- {func}`~otto.host.transfer.register_transfer_backend` — the **file transfer**
  backend (`transfer` in lab data), which spans both host families. The built-ins are
  the unix protocols `scp` / `sftp` / `ftp` / `nc` and the embedded `console`
  (plus the reserved `tftp`).

For the lab-data fields that select them (`term`, `transfer`), see
{doc}`lab-config`; for the host classes that carry them, see {doc}`os-profiles`.

## Registration errors and replacing a built-in

Both seams (and every other extension point in otto — host classes, lab
repositories, CLI commands) share one {class}`~otto.registry.Registry` engine.
An unknown selector doesn't just fail — the error lists every registered name
for that seam and, when your typo is close to a real one, suggests it:

```text
ValueError: Unknown transfer backend 'sftpp'. Did you mean 'sftp'? Registered: console, ftp, nc, scp, sftp, tftp. Custom entries can be added via otto.host.transfer.register_transfer_backend().
```

Registering the *same* name twice is a hard failure by default, naming both
modules — this is what protects you from two `init` modules silently racing
to define `ssh`. If you deliberately want to replace a built-in (for example,
swapping in your own `json` lab-repository backend, or overriding otto's
stock `ssh` term with a hardened variant), pass `overwrite=True` to the
`register_*` function:

```python
from otto.host import register_transfer_backend

register_transfer_backend("scp", MyHardenedScp, overwrite=True)
```

Without `overwrite=True` this raises
`ValueError: transfer backend 'scp' is already registered by 'otto.host.transfer.unix'; ...`.
This asymmetry is intentional and does **not** apply everywhere: CLI top-level
commands are the one seam with no `overwrite` escape hatch at all — see
{doc}`extending-cli`'s [Collisions](extending-cli.md#collisions) section for
why a duplicate `otto <name>` registration always fails loud instead.

## `host_families` applicability

A backend declares which host families it serves. For a **transfer** backend
this is the class attribute {class}`~otto.host.transfer.BaseFileTransfer`'s
`host_families` — a `frozenset[str]` subset of `{'unix', 'embedded'}`. For a
**term** backend, where ssh/telnet share a single `ConnectionManager` class, the
families are recorded per registered name (the `host_families` keyword on
{func}`~otto.host.connections.register_term_backend`): `ssh` serves `{'unix'}`,
`telnet` serves `{'unix', 'embedded'}`.

This is not advisory. The host spec's `field_validator` checks two things for
every entry in `valid_terms` / `valid_transfers` before the host is constructed:
that the selector is **registered**, and that the host's family is **in** the
backend's families. A misapplied selector — `console` on a unix host, or `ssh`
on an embedded host — fails with a clear error naming the families the backend
actually serves, rather than blowing up at connect time. A backend registered
with **empty** families can never validate on any host, so both
`register_transfer_backend` and `register_term_backend` reject it at
registration time rather than letting it sit in the registry as a latent dead
end.

## The `create(ctx)` construction contract

Both seams construct through a uniform classmethod. The host assembles a frozen
DTO — a {class}`~otto.host.transfer.TransferContext` (or a
{class}`~otto.host.connections.TermContext`) — carrying everything any backend in
that family needs at its call site, then calls `cls.create(ctx)`. A custom
backend overrides `create` and reads only the fields it needs:

- a **unix** transfer backend reads `connections`, `exec_cmd`, `nc_options`,
  `scp_options`, and `get_local_ip`;
- an **embedded** transfer backend reads `exec_cmd` and `filesystem`.

Selector validation runs before construction, so a backend never sees a ctx
missing the fields its family supplies. The config-facing path is **always the
registered string** — that is the only way lab data names a backend. Bare
callables such as `UnixHost._connection_factory` are a code-only convenience for
injecting a test double or a programmatic backend; they are never a config
selector and never appear in `lab.json`.

## `tftp` is reserved

`tftp` is registered and applicable to embedded hosts (a future cross-family
implementation would extend its `host_families` to `unix` as well), so it
validates cleanly in lab data today. Its transfer body, however, raises
`NotImplementedError` until the protocol is implemented — it is a placeholder
that reserves the name and exercises the applicability path, not a working
backend.

## A worked example

A custom transfer backend subclasses
{class}`~otto.host.transfer.BaseFileTransfer`, declares its `host_families`,
overrides `create`, and implements the two abstract halves `_run_put` /
`_run_get` (each must call `progress_factory()` once per source file so the
transfer reports progress).

`_run_put`/`_run_get` return `dict[Path, Result]` — one entry per source
file, keyed exactly as passed (no resolution). The public `put`/`get`
methods fold that mapping into an aggregate `Result` via
{func}`~otto.host.transfer.aggregate_transfer`: `value=dest_path` on a
per-file success, a per-file `msg` on failure, and
`Status.Skipped` (`"not attempted (earlier failure)"`) for a file a
sequential backend never reached:

```python
# .otto/init.py — registered via [init] in .otto/settings.toml
from pathlib import Path

from otto.host import register_transfer_backend
from otto.host.transfer import BaseFileTransfer, TransferContext
from otto.result import Result
from otto.utils import Status


class XmodemTransfer(BaseFileTransfer):
    host_families = frozenset({"unix", "embedded"})

    @classmethod
    def create(cls, ctx: TransferContext) -> "XmodemTransfer":
        return cls(name=ctx.host_name, max_filename_len=ctx.max_filename_len)

    async def _run_put(self, src_files, dest_dir, progress_factory):
        per_file: dict[Path, Result] = {}
        for src in src_files:
            progress = progress_factory() if progress_factory is not None else None
            try:
                ...  # send src over XMODEM; drive `progress` as bytes move
                per_file[src] = Result(Status.Success, value=dest_dir / src.name)
            except OSError as exc:
                per_file[src] = Result(Status.Error, msg=f"{src}: {exc}")
        return per_file

    async def _run_get(self, src_files, dest_dir, progress_factory):
        ...  # same shape as _run_put, reading from the device instead


register_transfer_backend("xmodem", XmodemTransfer)
```

Callers see the aggregate, not the per-backend hooks — `put`/`get` still
return a single `Result` whose `value` is the per-file mapping:

```python
result = await host.put(src_files=[Path("a.bin"), Path("b.bin")], dest_dir=Path("/tmp"))
assert result.is_ok
assert result.value[Path("a.bin")].value == Path("/tmp/a.bin")
```

A host then selects it with `"transfer": "xmodem"` in `lab.json`:

```json
{ "element": "mote", "os_type": "embedded", "transfer": "xmodem" }
```

Because the selector validator and `otto schema export` both read the **live
registry** after init modules load, the new `xmodem` name is accepted in lab
data, appears in shell completion, and is included in the generated JSON Schema
automatically — there is nothing else to wire up. (For the schema export, see
{doc}`editor-schemas`.)

A custom `term` backend follows the same shape against
{class}`~otto.host.connections.ConnectionManager` and
{class}`~otto.host.connections.TermContext`, registered via
{func}`~otto.host.connections.register_term_backend` and selected with
`"term": "..."`. Because ssh/telnet share one `ConnectionManager` class, a
term's applicable families are passed as a **required** keyword argument to
`register_term_backend` rather than declared as a class attribute the way a
transfer backend does:

```python
register_term_backend("my_term", MyTerm, host_families=frozenset({"unix"}))
# host_families is required (no default) — omitting it raises TypeError
```

## Login proxies

Some accounts can't be reached by direct authentication — the classic case is
a service user with no login shell. A `creds` entry for one of these names a
**login proxy**: {func}`~otto.host.login_proxy.register_login_proxy` registers
a small async callable, from an `init` module, that drives whatever steps
*become* that user (`su`, `sudo -u`, entering a container, ...) after otto has
already authenticated as a different, directly-loginable account. It is the
same named-registry seam as the term/transfer backends above — registration
runs before any lab data loads, duplicate names fail loud unless
`overwrite=True`, and an unknown name at lookup time lists what's registered —
just keyed by proxy name instead of `term`/`transfer`, and selected from a
cred entry instead of a host entry.

### The callable contract

```python
async def proxy(io: ProxyIO, ctx: ProxyContext) -> None: ...
```

{class}`~otto.host.login_proxy.ProxyIO` is the minimal handle a proxy drives —
`send`/`expect` — satisfied alike by the raw session used at session
establishment, the `login --as-user` bridge, and the `switch_user`/`as_user`
elevation path, so one proxy function runs unmodified over all three.
{class}`~otto.host.login_proxy.ProxyContext` carries `target` (the
{class}`~otto.host.login_proxy.Cred` being become — its `login`/`password`/`params`), `via`
(the cred currently in control), and `host_id` (for error messages) —
deliberately **not** the host object itself: calling `run()` mid-proxy on the
very session being established would deadlock. Host-specific data rides in
`target.params`.

The built-in `"su"` proxy — the default for any cred with no `proxy` field —
is a proxy like any other, registered the same way:

```python
async def _su_proxy(io: ProxyIO, ctx: ProxyContext) -> None:
    login = ctx.target.login
    cmd = "su" if not login else f"su {shlex.quote(login)}"
    await io.send(cmd + "\n")
    if ctx.target.password is not None:
        await io.expect(r"[Pp]assword:")
        await io.send(ctx.target.password + "\n", log=LogMode.NEVER)
```

Note the `log=LogMode.NEVER` on the password send — otto's password-hygiene
convention for logged output applies inside a proxy step exactly as it does
everywhere else a credential is sent.

Register your own the same way, with an optional `undo=` that reverses the
steps for `as_user` restore. The default reversal (used by `"su"`) sends a
bare `exit`, correct for any su/sudo-style nested shell; only an exotic proxy
needs to override it:

```python
# .otto/init.py — registered via [init] in .otto/settings.toml
from otto.host.login_proxy import ProxyContext, ProxyIO, register_login_proxy


async def enter_container(io: ProxyIO, ctx: ProxyContext) -> None:
    container = ctx.target.params["container"]
    await io.send(f"docker exec -it {container} sh\n")


register_login_proxy("docker-shell", enter_container)
```

A runnable version of this pattern — construction, registration, and
{func}`~otto.host.login_proxy.resolve_chain` resolving the directly-loginable
cred a proxied one becomes — lives in `otto.examples.login_proxy`
(`src/otto/examples/login_proxy.py`).

### Selecting a proxy from `creds`

A cred entry's `proxy` field names the registered proxy; `via` names another
entry in the same host's `creds` list to authenticate as first (omit it to
default to the first directly-loginable entry):

```json
"creds": [
    {"login": "admin", "password": "hunter2"},
    {"login": "appuser", "proxy": "docker-shell", "via": "admin",
     "params": {"container": "app1"}}
]
```

`proxy` names are validated against the registry at lab-load time, in the same
place and the same way as `term`/`transfer` selectors — a typo'd proxy name
fails loud at load, listing the registered proxies, rather than failing later
mid-connection. See {doc}`host-database` for the full `creds` field reference,
including the ownership consequences of proxying (which transfer paths land
files owned by the via-user vs. the proxied target user).

## See also

- {doc}`extending-embedded` — custom command frames and embedded filesystems
- {doc}`os-profiles` — registering a custom host class that bundles these
- {doc}`lab-config` — the `term` / `transfer` lab-data fields
- {doc}`host-database` — the `creds` field reference and login-proxy ownership
  consequences
- {doc}`editor-schemas` — `otto schema export` for editor autocompletion
