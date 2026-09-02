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
parameter of the kind.

## Matching

`match` is a table ANDed across keys; a host must satisfy every clause.
Keys name a host attribute — `id`, `element`, `element_id`, `os_type`,
`os_name`, `os_version`, `ip`, `source_lab` — or a dotted
`metadata.<key>` / `element_metadata.<key>` path. Anything else is a
settings error at bootstrap. Values are typed:

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

Built in, and often all a simple case needs:

| Param | Meaning |
|---|---|
| `artifact` | local file, forward slashes, anchored to the repo root |
| `dest_dir` | destination on the *host* (the host's own path rules) |
| `install` / `uninstall` / `check` | optional command strings run on the host |

Without `install`/`uninstall` those steps are no-op successes (staging
placed the artifact). Without `check`, `is_installed` answers False — otto
assumes not installed and re-stages, which is safe for what this kind
serves.

`install`, `uninstall` and `check` run under the host's default command
timeout (30 seconds) — the `file` kind passes no `timeout` to `host.run`.
An install that needs longer belongs to a repo-registered kind instead; a
`timeout` param on the `file` kind is a possible later extension, not
something to add yourself.

Needs beyond that are a custom kind:

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
