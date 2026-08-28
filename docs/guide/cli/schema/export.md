# otto schema export

```text
otto schema export [--out DIR] [--builtins-only]
```

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--out, -o DIR` | `.otto/schemas` | Directory to write `*.schema.json` into |
| `--builtins-only` | off | Emit only the built-in host types (`unix`, `embedded`, `zephyr`), excluding custom types registered by `init` modules |

```bash
otto schema export
```

This defaults to `.otto/schemas/` (pass `--out` to write elsewhere) and writes:

| File | Describes |
| --- | --- |
| `lab.schema.json` | the whole `lab.json` object — its `labs` table, its `elements` array (each element's `hosts` accepting any registered `os_type`) and its `links` array |
| `link.schema.json` | a single entry in the `links` array |
| `unix-host.schema.json`, `embedded-host.schema.json` | a single host of one type |
| `settings.schema.json` | `settings.toml` |
| `reservations.schema.json` | the reservations JSON file |
| `monitor-meta.schema.json` | the monitor dashboard's internal chart/tab-layout model — not a file you edit, and not served at any endpoint; it drives the generated TypeScript types the web dashboard builds against (`scripts/gen_web_types.sh`) |

Every document carries an `x-otto-version` stamp naming the otto that
generated it, which is how the `otto init` doctor tells an upgrade apart from
a local edit — see {doc}`editors`.

Run it again after upgrading otto to pick up new fields. Custom host classes
registered via an init module in `.otto/settings.toml` are included
automatically — each gets its own `<type>-host.schema.json` and an entry in
`lab.schema.json`. Pass `--builtins-only` to emit just the built-in types
(`unix`, `embedded`, `zephyr`), excluding any custom ones.

Point your editor at what this writes — see {doc}`editors`.
