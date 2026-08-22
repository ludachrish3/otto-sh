# CLI-exposed verbs
Any method a host class exposes with {func}`~otto.utils.cli_exposed`
becomes an `otto host <id> <verb>` subcommand, with its signature inferred
into CLI arguments and options. This page is the authoring contract: how a
method is exposed, how its parameters are inferred, and how a repo registers
the product and dev-tool providers whose verbs appear the same way.
For what the resulting verbs *do*, see
{doc}`../guide/cli/host/capabilities/index`.
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

A verb returning a `Result` exits non-zero when its status is not OK, and a
verb returning a plain value exits 0 with the value printed as-is — see
[Exit codes](../guide/cli/host/index.md#exit-codes).

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
[Remote path completion](../guide/cli/index.md#remote-path-completion) for what
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
## Registering products from a product repo

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

(provider-scope-gate)=

### A provider only runs inside its repo's universe

Registering a provider makes the `[project]` declaration in your
`.otto/settings.toml` **required**: bootstrap refuses a repo that registers
products or dev tools without saying which labs it applies to (see
{ref}`project-scope`).

Once declared, that reach is enforced at ingest. A provider is **not called** —
not called and then filtered, but never called — for a host its repo's
declaration does not target:

```toml
[project]
lab_patterns  = ["bench.*"]
host_patterns = ["sensor-.*"]
```

With that in place, `_provide` above never sees a host of lab `floor`, and
never sees `gw-1` in `bench1` either. Skipping *before* the call is the point:
a provider that ran has already been handed a machine its repo never declared,
and providers inspect hosts and keep their own state.

Two cases are admitted rather than judged, because a gate that cannot compute a
narrowing must narrow nothing:

- **A host with no lab attribution** — one built outside the lab loader by a
  direct `create_host_from_dict` call, and the built-in `local` host. These
  predate scoping and behave exactly as before.
- **A registering repo otto cannot resolve** — a provider carrying a repo name
  this process has no settings for. Refusing there would turn "otto could not
  find its config" into "your host has no products", which is the same
  silent-wrong-answer the scoping exists to prevent, pointed the other way.

Skips are logged at DEBUG, naming the repo, the host and the host's lab.
## Registering dev tools from a repo

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
repo as its owner **unless it already names one** — a tool that arrives with
`owner` already set keeps it, so one repo can hand a tool to another's
ownership deliberately.

That stamp is what the **owner-scoped** walk reads: `otto run install-tools`
runs per repo and passes `owner=` down to `install_dev_tools`, so one repo's
tooling installs while another's is left alone. The host verb above is the
host-wide one — `otto host <id> install-tools` (and `host.install_tools()`)
takes no `owner=` and acts on every dev tool the host carries, whichever repo
registered it.

Dev-tool providers are gated by the registering repo's `[project]` declaration
under exactly the rule and the carve-outs products use
({ref}`provider-scope-gate`) — the two
registries are separate, so the gate is applied separately, and registering
either kind makes `lab_patterns` required.
