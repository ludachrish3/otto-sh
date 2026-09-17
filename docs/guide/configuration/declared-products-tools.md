# Declared products and dev tools

`[[products]]` and `[[dev_tools]]` entries in `.otto/settings.toml` attach
products and dev tools to hosts without writing a provider. The two arrays
share one schema and one behavior; only the seam differs. Code providers
({func}`~otto.host.product.register_product_provider`,
{func}`~otto.host.dev_tool.register_dev_tool_provider`) remain the fallback:
declared entries apply first at lab ingest, and a provider instance whose
name a declared entry already claimed stands down.

```toml
[[products]]
name = "firmware"
kind = "file"
artifact = "build/fw-rev2.bin"
match = { "metadata.hw_version" = "rev2" }

[[products]]                 # fallback: declared last, wins only when rev2 didn't
name = "firmware"
kind = "file"
artifact = "build/fw.bin"

[[dev_tools]]
name = "trace-probe"
kind = "file"
artifact = "tools/probe.sh"
match = { id = "bb.*", os_version = ">=3.7" }
```

Reserved keys: `name` (the product/tool identity), `kind` (which registered
kind builds it), `match` (which hosts get it). Every other key is a
parameter of the kind, and a built-in kind **refuses** a key it does not
know — the entry fails to load, naming the unknown key and listing the valid
ones, rather than ignoring a typo that would have changed what runs on the
host.

## Matching

`match` is a table ANDed across keys; a key IS the path on the host. Name
`id`, `element.name`, `element.id`, `os_type`, `os_name`, `os_version`, `ip`,
`source_lab` — or a dotted `metadata.<key>` / `element.metadata.<key>` path.
Anything else is a settings error at bootstrap; a retired spelling
(`element`, `element_id`, `element_metadata.<key>`) is refused with the
key that replaced it. Values are typed:

| Value | Meaning |
|---|---|
| `"bb.*_qemu"` | regex, full match |
| `">=3.7"` (any `>= <= == ~= != > <` prefix) | version comparison |
| `true`, `3` | equality |
| `["rev2", "rev3"]` | any-of |

A version comparison needs a **quoted** TOML string — `os_version = ">=3.7"`,
not `os_version = 3.7`. The bare number is a TOML float, which `match`
compares by equality against the host's `os_version`, never as a version; it
will not do what a specifier-prefixed string does.

An entry with no `match` at all — and an entry whose `match` is an empty
table — applies to every host: there is no clause to fail, so the entry is the
repo's blanket declaration, bounded only by its `[project]` targeting below.

An attribute that is unset (or a metadata key a lab doesn't carry) never
matches — declare a fallback entry last if one should apply. When several
entries share a `name`, the first matching one wins, in declaration order.
Across repos this means DISCOVERY order decides which entry is "first": when
two repos each declare the same name, the one from the repo earlier in
`sut_dirs` wins — not init-module registration order, which the dependency
pass can reorder topologically. Entries are also bounded by the repo's
`[project]` targeting, exactly like providers. A repo skipped by the
dependency pass (a required dependency missing) contributes **no** entries:
its init modules never ran, so neither half of it — declared or code — is
present.

## The `file` kind

Built in, and often all a simple case needs. `artifact` is the only required
param; everything else is optional:

| Param | Meaning |
|---|---|
| `artifact` | **Required.** Local file, forward slashes, anchored to the repo root |
| `dest_dir` | Destination directory on the *host* (the host's own path rules). The artifact lands at `<dest_dir>/<artifact basename>`. Defaults to empty, which `put` resolves against the host's own `default_dest_dir` — itself empty on a Unix host, where an empty destination is the SSH user's home, and the mount point on an embedded target |
| `install` / `uninstall` / `check` | optional command strings run on the host |
| `cov_dir` | host directory the product writes its coverage counters under (its `GCOV_PREFIX`); default `/tmp/<name>`, and the empty string is refused |
| `debug_log_globs` | host paths or globs of the product's own debug logs, hauled into `logs/<host_id>/<product>/debug/` ({ref}`the run tree <run-tree>`) |
| `instrumented` | `true`/`false`, overriding the artifact scan — the answer for an archive the scan cannot see inside |

The last three are **products only**: a `[[dev_tools]]` entry naming any of
them is refused by name, because a dev tool is never under test and so has no
coverage. What otto does with them is {doc}`../cli/cov/index`'s subject, and
{doc}`../../getting-started/coverage` is the walkthrough.

Without `install`/`uninstall` those steps are no-op successes (staging
placed the artifact). Without `check`, `is_installed` answers False — otto
assumes not installed and re-stages, which is safe for what this kind
serves.

`install`, `uninstall` and `check` run under the host's default command
timeout (30 seconds) — the `file` kind passes no `timeout` to `host.run`.
An install that needs longer belongs to a repo-registered kind instead; a
`timeout` param on the `file` kind is a possible later extension, not
something to add yourself.

### Placeholders

`install`, `uninstall` and `check` are expanded before they run, for
**products only** — a dev tool's command strings are passed through
untouched:

| Placeholder | Expands to |
|---|---|
| `{cov_dir}` | the entry's `cov_dir`, or the `/tmp/<name>` default |
| `{name}` | the entry's `name` |

```toml
[[products]]
name = "app"
kind = "file"
artifact = "build/app"
dest_dir = "/opt/app"                # where staging puts the artifact
install = "GCOV_PREFIX={cov_dir} GCOV_PREFIX_STRIP=3 /opt/app/{name} &"
```

`GCOV_PREFIX_STRIP` is the compiler's knob, not otto's: it drops that many
leading components from the path baked into the build, so the tree under
`cov_dir` stays shallow. Count the leading components of the build directory's
absolute path on the machine that compiled the artifact and pass that; otto
reads the value nowhere, because the fetch flattens each product's `.gcda`
into one directory and the merge pairs them with the local `.gcno` by file
name.

The command runs through `host.run` with **no working directory of its own**,
so a relative `./app` would resolve against the login shell's directory, not
against `dest_dir`. Spell the path out, as above.

Expansion is **strict**: any other field name, and any conversion
(`{cov_dir!r}`), format spec (`{cov_dir:>12}`), attribute or index access, or
positional field, is refused when the lab loads, naming the entry and the two
valid placeholders — never quietly rewritten into the command that runs on the
host. A literal brace is doubled, `{{` and `}}`, which is what an `awk
'{{print $1}}'` program or a shell `${{VAR}}` needs.

## The `llext` kind

Built in, products only: a Zephyr LLEXT extension as a product. An extension
has no filesystem home — the load *is* the transfer — so `stage` is a no-op,
`install` loads the object, and `uninstall` unloads it. Modelling it this way
is what lets a board carry the same product machinery as any other host: the
name is the `<product>` segment of {ref}`the run tree <run-tree>`, and the
instrumentation scan reads the extension's own `.gcda` strings.

| Param | Meaning |
|---|---|
| `artifact` | the local `.llext` object; required |
| `call_after_load` | exported functions called, in order, right after a successful load (`["cov_init"]` runs the embedded-gcov constructor) |
| `dump_fn` | the exported function the embedded coverage collector calls to dump counters; default `cov_dump` |
| `instrumented` | as the `file` kind |
| `debug_log_globs` | as the `file` kind |

There is no `{cov_dir}`/`{name}` substitution here: an `llext` entry declares
no command strings to substitute into.

The matched host must carry a **binary loader** — the object is pushed through
`host.load`, and `is_installed` asks the loader's own list command what is
resident. An `llext` entry matched to a host without one is refused at lab
load, naming the host. See {doc}`../cli/cov/instrumenting/embedded`.

```toml
[[products]]
name = "cov_ext"
kind = "llext"
artifact = "build/v3_7/cov_ext.stripped.llext"
call_after_load = ["cov_init"]
match = { id = "zephyr37-llext" }

[[products]]                 # same NAME, a different board's build
name = "cov_ext"
kind = "llext"
artifact = "build/v4_4/cov_ext.stripped.llext"
call_after_load = ["cov_init"]
match = { id = "zephyr44-llext" }
```

Two entries sharing one `name` is how per-version artifacts are declared: the
first entry per name whose `match` admits the host wins, so each board gets
its own object while everything downstream — the tree's
`cov/<host_id>/cov_ext/` segment, the capture's product, the dump command —
sees a single product. Exactly one entry attaches per host, so each
version-specific entry repeats **every** param it needs: nothing is inherited
from the entry above it.

## Custom kinds

Needs beyond the built-ins are a custom kind:

```python
# register_dev_tool_kind is the twin, in otto.host.dev_tool
from otto.host.product import register_product_kind


def make_ipk(entry, host):
    return IpkProduct(name=entry.name, **entry.params)


register_product_kind("ipk", make_ipk)
```

Register from an init module listed in `settings.toml`, like every other
extension hook. The factory gets the parsed entry (`entry.params` carries
the non-reserved keys; `entry.base_dir` anchors local paths) and the
matched host, and returns the instance to attach.
