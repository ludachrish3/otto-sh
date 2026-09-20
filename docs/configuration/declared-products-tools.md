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
kind = "shell"
artifact = "build/fw-rev2.bin"
match = { "metadata.hw_version" = "rev2" }

[[products]]                 # fallback: declared last, wins only when rev2 didn't
name = "firmware"
kind = "shell"
artifact = "build/fw.bin"

[[dev_tools]]
name = "trace-probe"
kind = "shell"
artifact = "tools/probe.sh"
match = { id = "bb.*", os_version = ">=3.7" }
```

Reserved keys: `name` (the product/tool identity), `kind` (which registered
kind builds it), `match` (which hosts get it). Every other key is a parameter
of the kind, and a built-in kind **refuses** a key it does not know — the entry
fails to load, naming the unknown key and listing the valid ones.

## Kinds

A kind supplies **what drives its verbs** — install, check, uninstall, and how
its coverage counters are collected. Use one of otto's built-ins below, or a
repo-registered kind ([Custom kinds](#custom-kinds)), when something brings
runtime knowledge to those verbs; anything else is a `shell` entry, whose verbs
are the shell commands you write and whose artifact can be anything a shell
command can install.

| Kind | What drives its verbs | Coverage |
|---|---|---|
| `shell` | the `install` / `check` / `uninstall` commands you write | `.gcda` under `cov_dir`, fetched with `find` |
| `llext` | the host's binary loader (a Zephyr LLEXT extension) | dumped over the console by the embedded collector |
| `kmod` | the kernel's module loader (`insmod`/`rmmod`) | `none`, or `module` (the `otto_kgcov` runtime dumps to `cov_dir`), or `kernel` (`CONFIG_GCOV_KERNEL`'s debugfs tree copied to `cov_dir`) |
| `docker_image` | a docker daemon (`docker load`/`pull`, `run -d`, `rm -f`) | `.gcda` under `cov_dir`, bind-mounted into the container |

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

## The `shell` kind

Built in, and often all a simple case needs. `artifact` is the only required
param; everything else is optional:

| Param | Meaning |
|---|---|
| `artifact` | **Required.** Local file, forward slashes, anchored to the repo root |
| `stage_dir` | Staging directory on the *host*, accepted by **every kind that places a file**. Must be an **absolute** path — a relative one (or a `~`, which no transfer backend expands) would mean a different directory to the transfer that puts the artifact and to the command that names it afterwards, so it is refused at lab load. Leave it empty and otto resolves it: the host's `default_dest_dir` when the host record declares one, otherwise the login user's home, read from the host once per host object. The artifact lands at `<stage_dir>/<artifact basename>`; a `shell` product's STAYS there (it is the product), while the transient kinds (`kmod`, `docker_image`, the kernel-module dev tools) delete their copy once it is consumed. Two entries on ONE host — products and dev tools together — that would stage the same basename into the same directory are refused at lab load, naming both. An `llext` entry has no `stage_dir`: the load is the transfer, so there is no directory to name |
| `install` / `uninstall` / `check` | optional command strings run on the host |
| `cov_dir` | host directory the product writes its coverage counters under (its `GCOV_PREFIX`); default `/tmp/<name>`, and the empty string is refused |
| `debug_log_globs` | host paths or globs of the product's own debug logs, hauled into `logs/<host_id>/<product>/debug/` ({ref}`the run tree <run-tree>`) |
| `instrumented` | `true`/`false`, overriding the artifact scan — the answer for an archive (`.tar`, `.tar.gz`, `.zip`, …), which always scans `unknown` because the scan cannot see inside it |

The last three are **products only**: a `[[dev_tools]]` entry naming any of
them is refused by name, because a dev tool is never under test and so has no
coverage. What otto does with them is {doc}`../cli/cov/index`'s subject, and
{doc}`../getting-started/coverage` is the walkthrough.

Without `install`/`uninstall` those steps are no-op successes (staging
placed the artifact). Without `check`, `is_installed` answers False — otto
assumes not installed and re-stages, which is safe for what this kind
serves.

`install`, `uninstall` and `check` run under the host's default command timeout
(30 seconds) — the `shell` kind passes no `timeout` to `host.run`. An install
that needs longer belongs to a repo-registered kind instead.

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
kind = "shell"
artifact = "build/app"
stage_dir = "/opt/app"               # where staging puts the artifact
install = "GCOV_PREFIX={cov_dir} GCOV_PREFIX_STRIP=3 /opt/app/{name} &"
```

`GCOV_PREFIX_STRIP` is the compiler's knob, not otto's: it drops that many
leading components from the path baked into the build, so the tree under
`cov_dir` stays shallow. Count the leading components of the build directory's
absolute path on the machine that compiled the artifact and pass that; otto
never reads the value.

The command runs through `host.run` with **no working directory of its own**,
so a relative `./app` would resolve against the login shell's directory, not
against `stage_dir`. Spell the path out, as above.

Expansion is **strict**: any other field name, and any conversion
(`{cov_dir!r}`), format spec (`{cov_dir:>12}`), attribute or index access, or
positional field, is refused when the lab loads, naming the entry and the two
valid placeholders — never quietly rewritten into the command that runs on the
host. A literal brace is doubled, `{{` and `}}`, which is what an `awk
'{{print $1}}'` program or a shell `${{VAR}}` needs.

## The `llext` kind

Built in, products only: a Zephyr LLEXT extension as a product. An extension
has no filesystem home — the load *is* the transfer — so `stage` is a no-op,
`install` loads the object, and `uninstall` unloads it. The entry's `name` is
the `<product>` segment of {ref}`the run tree <run-tree>`, and the
instrumentation scan reads the extension's own `.gcda` strings. There is no
`stage_dir` either: nothing is placed on a filesystem to name a directory for.

| Param | Meaning |
|---|---|
| `artifact` | the local `.llext` object; required |
| `call_after_load` | exported functions called, in order, right after a successful load (`["cov_init"]` runs the embedded-gcov constructor) |
| `dump_fn` | the exported function the embedded coverage collector calls to dump counters; default `cov_dump` |
| `instrumented` | as the `shell` kind |
| `debug_log_globs` | as the `shell` kind |

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

## The `kmod` kind

Built in, in both seams: a Linux kernel module, loaded with `insmod` and
unloaded with `rmmod`. A product's `kmod` and a dev tool's `kmod` are two
separate factories, each registered in its own registry — the product form
carries coverage; the dev-tool form is a plain module with none.

| Param | Meaning |
|---|---|
| `artifact` | **Required.** The local `.ko`; `load` transfers it, `insmod`s it, and removes it — no staged copy is left on the host |
| `stage_dir` | Where that transfer lands before the `insmod`, as the `shell` kind |
| `module_name` | Defaults to the artifact stem with `-` → `_` (what `/proc/modules` shows) |
| `params` | Appended to `insmod` **unquoted**, apart from the `gcov_dir=` token otto itself adds for `coverage = "module"` — a value with whitespace is the user's to quote. `{cov_dir}`/`{name}` placeholders, as the `shell` kind |
| `coverage` | `"none"` (default), `"module"`, or `"kernel"` |
| `gcov_path` | Required with `coverage = "kernel"`: the module's own subtree under `/sys/kernel/debug/gcov/`, as a full path; refused outside it |
| `cov_dir`, `instrumented`, `debug_log_globs` | As the `shell` kind, and likewise products only |

Refused at lab load on any host missing `load`/`unload`/`lsmod` (today only a
`UnixHost`), naming the host. With `coverage = "module"` otto appends
`gcov_dir=<cov_dir>` to `params` itself so the `otto_kgcov` runtime
({doc}`../cli/cov/instrumenting/kernel-modules`) knows where to write; a
`params` that also sets `gcov_dir=` is a validation error. gcov counts arcs as
they run, so a dump taken inside a module's exit routine already holds
everything that routine executed before the dump call — `coverage = "module"`
dumps there, and `coverage = "kernel"` keeps a module's counters after unload
(`gcov_persist=1`, the kernel default) — so either way that coverage reaches a
run's report only when the module is unloaded before the post-run fetch, which
is what a suite's teardown does. The kernel wrote the counter files as root, so
every delete a `kmod` product's coverage hooks issue runs under sudo. See
{doc}`../cli/cov/instrumenting/kernel-modules` for the runtime, the worked
example, and how a report reads back what it captured.

### As a dev tool

A `[[dev_tools]]` entry of `kind = "kmod"` is a kernel module a repo places on
a host as tooling — a tracer, a test driver — rather than as software under
test: `install-tools` loads it with `insmod` before any product, `cleanup`
unloads it with `rmmod` after every product, and it is never part of a
product's `is_installed` answer.

| Param | Meaning |
|---|---|
| `artifact` | **Required.** The local `.ko`, as the product form |
| `stage_dir` | Where the `.ko` is staged before the `insmod`, as the product form |
| `module_name` | Defaults to the artifact stem with `-` → `_`, as the product form |
| `params` | Appended to `insmod` verbatim — **no placeholders**: a dev tool has no `cov_dir` to substitute |

Refused at lab load on any host missing `load`/`unload`/`lsmod`, naming the
host — the same check as the product form. There is no `coverage`,
`gcov_path`, `cov_dir`, `instrumented` or `debug_log_globs` param here: a dev
tool has no coverage of its own.

The `kgcov` subtype — otto's own coverage library declared as a `kmod` dev
tool — is documented next.

### The kgcov subtype

`kind = "kgcov"` is the `kmod` dev tool with `module_name` fixed to
`otto_kgcov`: one entry per kernel, matched by `match` exactly as any other
dev tool.

| Param | Meaning |
|---|---|
| `artifact` | **Required.** The built `.ko`, as the `kmod` form |
| `stage_dir` | Where the `.ko` is staged before the `insmod`, as the `kmod` form |
| `params` | Appended to `insmod` verbatim, as the `kmod` form — must not set `gcov_dir=`, which is the consumer's own parameter (see below) |
| `source` | Optional: the vendored directory (`otto init --kgcov` / `otto cov kgcov export`) this build came from, repo-anchored — named in the interface-mismatch remedy below |

`module_name` is not a kgcov key: it is fixed to `otto_kgcov`, and declaring
it is an unknown-param error.

```toml
[[dev_tools]]
name = "kgcov-6.8"
kind = "kgcov"
artifact = "build/lib/otto_kgcov.ko"
source = "third_party/otto_kgcov"
match = { id = "test[12]" }
```

Two refusals, on top of the `kmod` kind's own:

- **Wrong interface.** A built `.ko`'s `MODULE_VERSION` reports
  `<version>+kgcov<n>`; when `n` does not match this otto's own interface
  number, the entry is refused at lab load if the file already exists, and
  the same check always runs again at `install` — so a `.ko` built later,
  after lab load found no file to check, is still refused before the
  library is ever loaded — naming the tool, the artifact, both interface
  numbers, and the re-export remedy: `otto cov kgcov export <source>` when
  the entry declares `source`, and otherwise an instruction to re-export the
  vendored library and declare `source` so it can be named. The artifact's
  own directory is never offered: that is the build directory, and exporting
  into it would put sources where the `.ko` lands. A `.ko` with no
  `MODULE_VERSION` at all is refused the same way, naming it as not built
  from exported sources.
- **Two kgcov entries on one host.** A host runs one kernel and holds one
  otto_kgcov; a host whose `match` tables select two `kgcov` entries is
  refused at lab load, naming the host and both entries.

Which product needs the library, and how otto loads it on demand before that
product's own module, is the consumer's side — see
{doc}`../cli/cov/instrumenting/kernel-modules`.

## The `docker_image` kind

Built in, products only: a docker image, installed with `docker run -d` and
removed with `docker rm -f`. Registered for every host — nothing is checked
at declaration; `install` itself probes `docker` on the host's `PATH` and
fails loud naming the host when it is not.

| Param | Meaning |
|---|---|
| `image` | **Required.** A `registry/name:tag` reference, or the path of a `docker save` tarball (`.tar`, `.tar.gz`, `.tgz`) |
| `stage_dir` | Tarball form only: where the tarball is put for `docker load`, as the `shell` kind |
| `pull` | `false` (default): a reference must already be present (`docker image inspect`, fail loud naming it); `true`: `docker pull` first. Reference form only — declaring `pull = true` on a tarball entry is refused when the product is built |
| `run_args` | Extra `docker run` arguments; `{cov_dir}`/`{name}` placeholders substituted as the `shell` kind does, but inserted unquoted — quote any value of your own that contains whitespace |
| `container_name` | `--name`; defaults to the product name |
| `cov_dir`, `instrumented`, `debug_log_globs` | As the `shell` kind, and likewise products only |

`install` runs `docker run -d --name <container_name> -v
<cov_dir>:<cov_dir> <run_args> <image>` — the bind mount is the whole
coverage story: an instrumented binary inside the container writing under
`GCOV_PREFIX=<cov_dir>` writes onto the daemon host, where the ordinary
fetcher and the default hooks already work, no collector and no host class
needed. A tarball entry's staged copy under its `stage_dir` is removed right
after a successful `docker load` — it is an intermediate the load has already
consumed, not the product itself. `is_installed` asks the daemon whether
the container is *running* — one that exists but has exited answers not
installed, so a re-install then collides with docker's own "name already
in use" unless the old container is removed first.

`uninstall` always runs `docker rm -f <container_name>` (idempotent — a
missing container is not a failure), and follows with `docker rmi` only
for an image otto itself loaded from a tarball; a pulled or
already-present reference stays in the daemon's cache. That image is
resolved from the container itself (`docker container inspect -f
'{{.Config.Image}}' <container_name>`), never from in-process state, so
it is found even when `uninstall` runs in a separate process from the
`install` that loaded it — the normal shape of `otto install` followed
later by `otto uninstall`. A container that is already gone has nothing
to resolve an image from, so nothing beyond the (already-absent)
container is removed.

An `image` path ending in one of the tarball suffixes above is always
`unknown` under the instrumentation scan — those suffixes are a subset of
the archive suffixes the `shell` kind's own `instrumented` row above
names, which the scan never opens — so an instrumented tarball entry
declares `instrumented = true` itself, the same as any other archive. A
reference has no local artifact for the scan to look at either,
so it too reads `unknown` unless `instrumented` says otherwise. See
{doc}`../cli/cov/instrumenting/containers` for the bind mount, the
tarball/reference split, and how a docker-compose service under test
(`[docker.use_cases]`) differs from this kind.

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
