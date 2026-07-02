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
selector and never appear in `hosts.json`.

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

A host then selects it with `"transfer": "xmodem"` in `hosts.json`:

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

## See also

- {doc}`extending-embedded` — custom command frames and embedded filesystems
- {doc}`os-profiles` — registering a custom host class that bundles these
- {doc}`lab-config` — the `term` / `transfer` lab-data fields
- {doc}`editor-schemas` — `otto schema export` for editor autocompletion
