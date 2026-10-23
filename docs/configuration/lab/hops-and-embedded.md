# Hops and embedded hosts

## Hops

A host's `hop` field (see {doc}`host-fields`) names the host otto connects
through to reach it: an intermediate SSH jump host. otto tunnels through the
hop and routes every connection to the host that way. Hops chain: a hop can
have its own `hop`. A hop is a *management* path, and it needs no entry in
`links`: otto derives an implicit management link from each host to its hop
(or to the built-in `local` host when it has none). A
{doc}`declared link <links>` is different: it is a data-plane route you write
in `links`.
In the {doc}`whole-lab example <../lab-config>`, the Zephyr host's `hop`
names `test4`: the management path to the device, not a declared link.

## Embedded-only fields

These fields apply only to hosts with an embedded base type (e.g.
`os_type: "zephyr"` or `os_type: "embedded"`).  See {doc}`../../cli/host/embedded` for full
details.

| Field | Type | Description |
|-------|------|-------------|
| `filesystem` | string | On-device filesystem variant (`"none"`, `"fat-ram"`, `"littlefs"`). |
| `loader` | string | Binary-load strategy for this target's runtime, by registry name (e.g. `"llext-hex"`); an unregistered name is refused when the lab file is validated.  Optional — omit it on a target that never loads binaries, and `host.load()`/`unload()` then fail loud.  Projects register their own via `register_binary_loader`. |

File transfer for embedded hosts uses `"console"` or `"tftp"` — see
{doc}`../../cli/host/embedded`.
