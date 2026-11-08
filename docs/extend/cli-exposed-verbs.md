# CLI-exposed verbs
Any method a host class exposes with {func}`~otto.utils.cli_exposed`
becomes an `otto host <id> <verb>` subcommand, with its signature inferred
into CLI arguments and options. This page is the authoring contract: how a
method is exposed, how its parameters are inferred, and how a project registers
the product and dev-tool providers whose verbs appear the same way.
For what the resulting verbs *do*, see
{doc}`../cli/host/capabilities/index`.
## Methods as CLI verbs

Any host coroutine method decorated with `@cli_exposed` is automatically an
`otto host` subcommand, scoped to the host's class.  This includes all four
core commands — `exec`, `put`, `get`, and `login` — as well as every capability
verb listed above.  They all share the same signature-driven synthesizer with
no special casing.

Example invocations:

```text
otto host <id> exec "systemctl restart x && journalctl -n5"
otto host <id> exec "apt-get update" --sudo --timeout 30
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
from pathlib import Path

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

`help_` sets the verb's `--help` text and its line in the `otto host` menu. An
override that gives none inherits the nearest `help_` of the verb it overrides;
with none anywhere, the first line of the method's docstring is used. Write it as plain text for the
person typing the command: single backticks around a literal command or flag are
fine, but no reStructuredText (double backticks, roles such as `:meth:`,
`*emphasis*`). The method's docstring stays free to be written for API readers.

A verb returning a `Result` exits non-zero when its status is not OK. A verb
returning a `bool` prints it and exits 0 for `True`, 1 for `False`; any other
plain value exits 0 ({ref}`cli-return-values`). See
[Exit codes](../cli/host/index.md#exit-codes).

(cli-follows-signature)=
### The signature decides arguments and options

The synthesizer reads the method's signature, and the parameter's **kind**
decides its CLI form: what is positional in Python is positional on the command
line, and what is keyword-only is an option. The same decision shapes both
surfaces, so `host.ls("/var/log")` and `otto host <id> ls /var/log` cannot
disagree.

| Python parameter | CLI form |
| --- | --- |
| positional (before `*`), required or defaulted | positional argument, in signature order |
| positional `list[T]` (or a union containing `list[T]`) | the verb's one space-separated variadic argument, wherever it sits |
| keyword-only (after `*`), no default | required `--option` (a `bool` is a required `--flag/--no-flag`) |
| keyword-only, with a default | `--option` with that default (a `bool` is a `--flag/--no-flag` pair) |
| keyword-only `list[T]` / `dict[str, T]` | `--opt a,b,c` / `--opt K=V,K2=V2` |
| `Exclude`-marked | not on the CLI; the method gets its default |
| `**kwargs` | not on the CLI |

Synthesis refuses a verb with a positional-only parameter (before `/`: the
command passes every value by keyword, which cannot reach it), a `*args`
(spell a variadic as a positional list parameter), or more than one positional
list parameter.

`bool` flag strings — the strings `1`, `true`, `yes`, `on` (case-insensitive)
map to `True`; everything else maps to `False`.  `Path`/`int`/`float` are
coerced from strings automatically.

### Presentation markers: `Arg`, `Opt`, `Exclude`

Import `Arg`, `Opt`, and `Exclude` from `otto.utils`:

```python
from typing import Annotated
from otto.utils import Arg, Opt, Exclude, cli_exposed
```

`Arg` and `Opt` describe how a parameter is presented — metavar, help text,
flag spelling, completion, the CLI type — and never move it between argument
and option. `Arg` belongs on a positional parameter and `Opt` on a keyword-only
one; the other way round contradicts the signature and synthesis refuses it.

**`Arg(name=..., help=..., elem_type=T)`** — a positional argument's metavar
and help. `elem_type` sets the CLI type of a scalar union, or the element type
each word of the variadic is coerced to (the method gets a `list[T]`). `put`'s
sources, the variadic:

```python
src_files: Annotated[list[Path] | Path, Arg(elem_type=Path, help="Local file(s) to upload.")]
```

A defaulted positional stays positional, as `power`'s `state` and `ls`'s `path` do:

```python
state: Annotated[str | None, Arg()] = None  # otto host <id> power on
path: Annotated[str | Path, Arg()] = "."  # otto host <id> ls /var/log
```

**`Opt(name=..., short=..., help=..., min=...)`** — a keyword-only option's
flag spelling, short alias, help, and inclusive numeric lower bound. `exec`'s
`timeout`:

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
src_files: Annotated[list[Path] | Path, Arg(elem_type=Path, remote_path="any")]
dest_dir: Annotated[Path, Arg(remote_path="dir")]
```

The marker only affects tab completion — see
[Remote path completion](../cli/index.md#remote-path-completion) for what
completion offers and when it stays quiet.  It is rejected on a comma-list or
`key=value` option (which render as a single string the completer can't split);
make a completable path list a positional list parameter with
`Arg(remote_path=...)` instead.

**`Exclude`** — drop a parameter from the CLI entirely; the method receives its
default value.  Use this for SDK-only parameters that make no sense as CLI
flags — `exec`'s `expects` and `log` are the canonical examples:

```python
expects: Annotated[Expect | None, Exclude] = None
log: Annotated[bool, Exclude] = True
```

### Per-verb summary

| Verb | Positional args | Notable options | Notes |
| --- | --- | --- | --- |
| `exec` | `COMMAND` | `--sudo`, `--timeout SECS`, `--user NAME` | `expects`/`log` excluded from CLI |
| `put` | `SRC... DEST` (variadic src + positional dest) | — | `show_progress` excluded |
| `get` | `SRC... DEST` (variadic src + positional dest) | — | `show_progress` excluded |
| `login` | — | — | Opens interactive shell |
| `reboot` | — | `--hard / --no-hard`, `--wait / --no-wait`, `--timeout SECS` | |
| `install` | — | `--stage-only / --no-stage-only` | |
| `power` | `STATE` (optional positional) | — | `on`/`off`/omit to toggle |
| `ls` | `PATH` (optional positional, default `.`) | `--all / --no-all` | |
| `write-file` | `PATH` | `--data TEXT` (required), `--append / --no-append` | |
| `load` | `FILE` | `--name NAME` (required on an embedded host; defaults to the file stem on a Unix host), `--params` | |
## Registering products from a product project

Products are **behavior**, so they're customized in code — never declared in lab
data. A product project registers its products from a `.otto` init module, and
otto applies them to each host as it is ingested from lab data:

    from pathlib import Path
    from otto.host import register_product_provider

    def _provide(host):
        if host.os_type == "unix":
            return [MyApp(artifact=Path("dist/myapp.tgz"), stage_dir=Path("/opt"))]
        return None

    register_product_provider(_provide)

The provider runs once per lab-ingested host. Key on product-agnostic host
attributes — `element.name`, `element.id`, `os_type`, `id`, `ip`, `source_lab`
(the component lab the host came from), the two opaque tables `metadata` (the
host entry's) and `element.metadata` (its element's), and the two reservation
sets that ride beside them, `resources` (the host entry's) and
`element.resources` (its element's) — both `frozenset[str]` — to decide which hosts
get which products; source any per-host parameters (versions, artifact paths)
from your own product-project config. Providers aggregate in registration order
and dedupe by `Product.name`. The instances a provider returns must accept
otto's attribute stamps ({ref}`the stamping rule <provider-instances-are-stamped>`).

```{warning}
**`host.lab_info` is not readable at provider time.** The factory stamps
`source_lab` and the host's `element` immediately before it
runs the providers, precisely so a provider can gate on them. `lab_info` is different: it is
stamped by an attribution sweep *after* the whole lab has loaded, because it
carries the lab's declared `resources` and `metadata`, which are only known
once every source has contributed. Inside a provider it still holds the
default `LabInfo()` — so `host.lab_info.metadata.get("site")` returns `None`
for every host, silently and forever. Gate on `source_lab` instead; read
`lab_info` later, from a test or instruction, where it is populated.
```

Code-constructed hosts (`UnixHost(..., products=[...])`) keep their explicit
list; providers apply only to hosts built from lab data.

(provider-scope-gate)=

### A provider only runs inside its project's universe

Registering a provider makes the `[project]` declaration in your
`.otto/settings.toml` **required**: bootstrap refuses a project that registers
products or dev tools without saying which labs it applies to (see
{ref}`project-scope`).

Once declared, that reach is enforced at ingest. A provider is **not called** —
not called and then filtered, but never called — for a host its project's
declaration does not target:

```toml
[project]
lab_patterns  = ["bench.*"]
host_patterns = ["sensor-.*"]
```

With that in place, `_provide` above never sees a host of lab `floor`, and
never sees `gw-1` in `bench1` either.

Two cases are admitted — the provider runs for them unscoped:

- **A host with no lab attribution** — one built outside the lab loader by a
  direct `create_host_from_dict` call, and the built-in `local` host.
- **A registering project otto cannot resolve** — a provider carrying a project name
  this process has no settings for.

Skips are logged at DEBUG, naming the project, the host and the host's lab.
## Registering dev tools from a project

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
project as its owner **unless it already names one** — a tool that arrives with
`owner` already set keeps it, so one project can hand a tool to another's
ownership deliberately.

That stamp is what the **owner-scoped** walk reads: `otto run install-tools`
runs per project and passes `owner=` down to `install_dev_tools`, so one project's
tooling installs while another's is left alone. The host verb above is the
host-wide one — `otto host <id> install-tools` (and `host.install_tools()`)
takes no `owner=` and acts on every dev tool the host carries, whichever project
registered it.

Dev-tool providers are gated by the registering project's `[project]` declaration
under exactly the rule and the carve-outs products use
({ref}`provider-scope-gate`), applied separately, and registering either kind
makes `lab_patterns` required.
